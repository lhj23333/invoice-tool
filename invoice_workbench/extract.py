from __future__ import annotations

from email import policy
from email.parser import BytesParser
from io import BytesIO
from pathlib import Path
import re
import xml.etree.ElementTree as ET

from pypdf import PdfReader
from pypdf.errors import PdfReadError

from .domain import UserError, comparable, money
from .recognition import (EVIDENCE_VERSION, MIN_SCORE, amount_cross_checked, ofd_document, recognize_ofd,
                          recognize_pdf_pages, recognize_raster)

SUPPORTED = {".pdf", ".xml", ".png", ".jpg", ".jpeg", ".ofd"}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg"}
MAX_BYTES = 20 * 1024 * 1024
# .eml 只是容器，可能夹带无关大附件；限制容器大小，附件逐个按 MAX_BYTES 校验。
MAX_MESSAGE_BYTES = 80 * 1024 * 1024
DECORATIVE_IMAGE = re.compile(r"^(?:logo|icon|banner|signature|spacer|pixel|tracking)[\d_. -]*$", re.I)
INVOICE_NAME_HINT = re.compile(r"[发發]\s*票|\binvoices?\b", re.I)
MULTIPLE_INVOICE_IDENTIFIERS_WARNING = "原件包含多个不同发票号码或代码，无法自动核验；请将每张发票分别保存后重新导入。"
REQUIRED_FIELDS = ("number", "date", "amount_cents", "buyer_name")
OCR_FIELDS = (*REQUIRED_FIELDS, "code", "buyer_tax_id", "seller_name")
XML_FIELDS = {
    "number": ("invoicenumber", "invoiceno", "digitalinvoiceno", "fphm", "发票号码"),
    "code": ("invoicecode", "fpdm", "发票代码"),
    "date": ("issuedate", "invoicedate", "kprq", "开票日期"),
    "buyer_name": ("buyername", "purchasername", "gfmc", "购买方名称"),
    "buyer_tax_id": ("buyertaxid", "buyertaxno", "purchasertaxid", "gfsh", "购买方纳税人识别号"),
    "seller_name": ("sellername", "xfmc", "销售方名称"),
    "amount": ("totalwithtax", "totaltaxincludedamount", "totaltax-includedamount", "taxinclusivetotalamount",
               "amountwithtax", "jshj", "价税合计"),
}
XML_TAG_FIELDS = {tag: field for field, aliases in XML_FIELDS.items() for tag in aliases}
# 商品明细容器里的 AmountWithTax 只是单行金额，不能当作价税合计。
# 覆盖数电票（IssuItemInformation / FPMX / MX）与常见商品明细节点，避免真实 XML 漏判明细层。
XML_DETAIL_TAGS = {"item", "items", "invoiceitem", "invoiceitems", "issuitem", "issuiteminformation",
                   "iteminfo", "iteminformation", "itemdetail", "itemlist",
                   "goods", "goodslist", "goodsdetail", "goodsinfo",
                   "detail", "details", "detaillist", "line", "lines", "linelist",
                   "product", "products", "productlist", "commodity", "commodities",
                   "traderitem", "traderitems", "fpmx", "mx", "spmx", "hwmx"}
RAW_TEXT_LIMIT = 30000
AMOUNT_UNCONFIRMED_WARNING = "价税合计（小写）识别可信度不足，且未能与同页大写金额核对一致，请核对原件。"
OFD_LAYER_CONFLICT_WARNING = "OFD 结构字段与文字层不一致，请核对原件。"
SUSPECT_DUPLICATE_WARNING = "疑似与已有同票号记录重复，请核对原件后处理。"
OFD_CUSTOM_FIELDS = {"发票号码": "number", "发票代码": "code", "开票日期": "date", "价税合计": "amount",
                     "购买方名称": "buyer_name", "购买方纳税人识别号": "buyer_tax_id", "销售方名称": "seller_name"}
OFD_TAG_FIELDS = {"invoiceno": "number", "invoicecode": "code", "issuedate": "date", "buyername": "buyer_name",
                  "buyertaxid": "buyer_tax_id", "sellername": "seller_name",
                  "taxinclusivetotalamount": "amount"}
# OFD 结构层只使用票面上的价税合计；合计金额/合计税额是未税金额，不能直接当作价税合计。
PARTY_STOP = re.compile(r"项目名称|货物或应税|规格型号|价\s*税\s*合\s*计|合\s*计|备\s*注|收款人|开票人")
PARTY_LABEL = re.compile(r"名\s*称|税\s*号|纳税人识别号|统一社会信用代码|^销")
INVOICE_TITLE = re.compile(
    r"电\s*子(?:\s*\([^)]*\))?\s*发\s*票|数\s*电\s*发\s*票|增\s*值\s*税.{0,16}发\s*票|"
    r"^\s*(?:tax\s+|electronic\s+)?invoice\s*$", re.I | re.M | re.S
)


def file_format(path):
    suffix = Path(path).suffix.lower()
    return ".jpg" if suffix == ".jpeg" else suffix


def has_required_fields(result):
    return (all(result.get(field) for field in REQUIRED_FIELDS)
            and (len(result["number"]) != 8 or bool(result.get("code"))))


def check_invoice_identifiers(result, numbers, codes):
    if len(set(numbers)) > 1 or (any(len(number) == 8 for number in numbers) and len(set(codes)) > 1):
        result["multiple_invoice_identifiers"] = True
        result["warnings"].append(MULTIPLE_INVOICE_IDENTIFIERS_WARNING)


def has_invoice_title(result):
    return bool(INVOICE_TITLE.search(result.get("raw_text", "")))


def has_invoice_content(result):
    if result.get("ocr_unconfirmed") and result.get("raw_text"):
        if has_invoice_content(from_text(result["raw_text"])):
            return True
    return (bool(re.fullmatch(r"\d{8}|\d{20}", result.get("number", "")))
            or bool(result.get("multiple_invoice_identifiers"))
            or (has_invoice_title(result)
                and any(result.get(field) for field in ("date", "amount_cents", "buyer_tax_id", "code"))))


def _column(line: str, boundary: int) -> str:
    """按列边界截取左栏；边界落在文字中间时扩展到该栏自己的分隔间隙。"""
    if boundary >= len(line):
        return line
    if line[boundary - 1:boundary].strip() and line[boundary:boundary + 1].strip():
        gap = re.search(r"[ \t]{3,}", line[boundary:])
        if gap:
            return line[:boundary + gap.start()]
    return line[:boundary]


def _party_values(block: str) -> tuple[str, str]:
    """从单栏区块中取出抬头与税号（税号可能排在其标签上方）。"""
    name = re.search(r"名\s*称\s*:\s*([^\n]+)", block)
    tax = re.search(r"(?:纳税人识别号|统一社会信用代码)\s*:?\s*([A-Z0-9]{15,20})(?![A-Z0-9])", block, re.I)
    tax_id = tax.group(1).upper() if tax else ""
    if not tax_id and re.search(r"纳税人识别号|统一社会信用代码", block):
        # 税号排在标签上方时按区块内唯一的税号候选取值，避免抓到账号等长数字串。
        tokens = {token.upper() for token in
                  re.findall(r"(?<![A-Z0-9])[A-Z0-9]{15,20}(?![A-Z0-9])", block)}
        tax_id = tokens.pop() if len(tokens) == 1 else ""
    return (name.group(1).strip() if name else ""), tax_id


def _apply_parties(result: dict, left: str, right: str) -> bool:
    """用左右两栏的购买方/销售方信息补全字段，不因空值清除已提取的有效值。"""
    name, tax = _party_values(left)
    if name:
        result["buyer_name"] = name
    if tax:
        result["buyer_tax_id"] = tax
    elif result.get("buyer_tax_id") and result["buyer_tax_id"] in right.upper():
        # 前序分支把右栏（销售方）税号当成了购买方税号，清空而不是保留错误值。
        result["buyer_tax_id"] = ""
    seller, _ = _party_values(right)
    if seller:
        result["seller_name"] = seller
    return bool(name or tax)


def from_text(text: str) -> dict:
    # 提取结果由自动核验流程与存储字段逐项比较。
    text = text.replace("\u3000", " ").replace("：", ":")
    result = {"raw_text": text[:RAW_TEXT_LIMIT], "method": "PDF 文字提取", "warnings": []}

    def first(pattern: str, source: str = text) -> str:
        match = re.search(pattern, source, re.I)
        return match.group(1).strip() if match else ""

    number_pattern = r"(?:发\s*票\s*号\s*码|Invoice\s*(?:No\.?|Number))\s*:?\s*(\d{20}|\d{8})(?!\d)"
    code_pattern = r"(?:发\s*票\s*代\s*码|Invoice\s*Code)\s*:?\s*(\d{10,12})(?!\d)"
    numbers = re.findall(number_pattern, text, re.I)
    codes = re.findall(code_pattern, text, re.I)
    result["number"] = numbers[0] if numbers else ""
    result["code"] = codes[0] if codes else ""
    check_invoice_identifiers(result, numbers, codes)
    dt = first(r"(?:开\s*票\s*日\s*期|Invoice\s*Date)\s*:?\s*(\d{4}\s*[年/.-]\s*\d{1,2}\s*[月/.-]\s*\d{1,2})")
    if dt:
        parts = re.findall(r"\d+", dt)
        result["date"] = f"{int(parts[0]):04}-{int(parts[1]):02}-{int(parts[2]):02}"
    amount_text = text.replace("−", "-").replace("－", "-")
    # 将负号和货币符号一起保留，前面的排版文字不能吞掉负号。
    amount_pattern = r"(-?\s*[¥￥]?\s*-?\s*[\d,]+\.\d{2})(?!\d)"
    # 两位小数未命中时，只接受带货币符号的整数或一位小数，与 money() 支持范围一致。
    loose_pattern = r"(-?\s*[¥￥]\s*-?\s*[\d,]+(?:\.\d)?)(?![\d.])"
    total_prefix = r"(?:价\s*税\s*合\s*计\s*(?:\([^)]*\))?|Total\s*(?:with\s*Tax|Amount))[^\d\n¥￥-]{0,25}"
    lower_prefix = r"小\s*写\s*[)）]?\s*:?\s*"
    # 部分 PDF 将大写金额排在“小写”和货币符号之间。
    distant_prefix = r"小\s*写[^¥￥\n-]{0,160}"
    currency_amount_pattern = r"(-?\s*[¥￥]\s*-?\s*[\d,]+\.\d{2})(?!\d)"
    amount = ""
    for pattern in (total_prefix + amount_pattern, lower_prefix + amount_pattern,
                    distant_prefix + currency_amount_pattern,
                    total_prefix + loose_pattern, lower_prefix + loose_pattern,
                    distant_prefix + loose_pattern):
        amount = first(pattern, amount_text)
        if amount:
            break
    if amount:
        try:
            result["amount_cents"] = money(re.sub(r"\s+", "", amount))
        except UserError as exc:
            result["warnings"].append(str(exc))
    # 限定购买方区块，避免把销售方税号当作购买方税号。
    buyer = first(r"(?:购\s*买\s*方(?:\s*信\s*息)?|Buyer)([\s\S]*?)(?:销\s*售\s*方|Seller|货物或应税|项目名称|$)")
    if buyer:
        name = re.search(r"(?:名\s*称|Name)\s*:\s*([^\n]+)", buyer, re.I)
        tax = re.search(r"(?:纳税人识别号|统一社会信用代码|Tax\s*ID)\s*:?\s*([A-Z0-9]{15,20})(?![A-Z0-9])", buyer, re.I)
        if name and name.group(1).strip():
            result["buyer_name"] = name.group(1).strip()
        if tax:
            result["buyer_tax_id"] = tax.group(1).upper()
    legacy_buyer = re.search(r"^[ \t]*购[ \t]+(?:名\s*)?称\s*:\s*(\S[^\n]*?)(?=\s{3,}|$)([\s\S]*?)(?=^[ \t]*(?:销[ \t]+|项目名称|货物或应税)|\Z)",
                             text, re.M)
    if legacy_buyer and not result.get("buyer_name"):
        name = legacy_buyer[1].strip()
        # 并排版式里紧跟在“购 名称:”后面的可能是右侧“销 名称:”，不能当作购方抬头。
        if name and not PARTY_LABEL.search(name):
            result["buyer_name"] = name
            tax = re.search(r"(?:纳税人识别号|统一社会信用代码)\s*:?\s*([A-Z0-9]{15,20})(?![A-Z0-9])",
                            legacy_buyer[2], re.I)
            if tax:
                result["buyer_tax_id"] = tax.group(1).upper()
    lines = text.splitlines()
    # 版式一：同行出现“购买方信息 … 销售方信息”，按列边界切分左侧购买方区块。
    for n, line in enumerate(lines):
        header = re.search(r"购\s*买\s*方(?:\s*信\s*息)?", line)
        if not header:
            continue
        seller_header = re.search(r"销\s*售\s*方(?:\s*信\s*息)?", line[header.end():])
        if not seller_header:
            continue
        boundary = header.end() + seller_header.start()
        left_lines, right_lines = [], []
        for party_line in lines[n + 1:n + 14]:
            if PARTY_STOP.search(party_line):
                break
            left_lines.append(_column(party_line, boundary))
            right_lines.append(party_line[boundary:])
        if _apply_parties(result, "\n".join(left_lines), "\n".join(right_lines)):
            break
    # 版式二：竖排“购 名称 / 买 / 方”标签，且同行左侧确有抬头时才按列切分。
    for n, line in enumerate(lines):
        label = re.match(r"[ \t]*购[ \t]+(?:名\s*)?称[ \t]*:", line)
        if not label:
            continue
        value = line[label.end():].lstrip(" \t")
        gap = re.search(r"[ \t]{3,}", value)
        if not gap or not re.match(r"[^ \t]", value):
            continue
        boundary = len(line) - len(value) + gap.end()
        if not re.search(r"名\s*称\s*:\s*\S", line[:boundary]):
            continue
        party_lines = []
        for party_line in lines[n:n + 12]:
            if PARTY_STOP.search(party_line):
                break
            party_lines.append(party_line)
        left_text = "\n".join(_column(party_line, boundary) for party_line in party_lines)
        right_text = "\n".join(party_line[boundary:] for party_line in party_lines)
        # 右侧必须确实存在另一列“名称”，否则不是并排版式（例如右侧是密码区）。
        if not re.search(r"名\s*称\s*:", right_text):
            continue
        _apply_parties(result, left_text, right_text)
        break
    return result


def _amount_key(value: str):
    try:
        return money(value)
    except UserError:
        return " ".join(value.split())


def from_xml(data: bytes) -> dict:
    if re.search(br"<!\s*(?:DOCTYPE|ENTITY)\b", data, re.I) or b"\x00" in data:
        raise UserError("不支持含 DTD、实体定义或非 UTF-8/GB 编码的 XML。")
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise UserError("XML 格式无效。") from exc
    except (LookupError, ValueError) as exc:
        raise UserError(f"XML 编码无法解析：{exc}") from exc

    def tag(node):
        return node.tag.rsplit("}", 1)[-1].lower()

    def find(node, aliases):
        return next((e.text.strip() for e in node.iter() if tag(e) in aliases and e.text and e.text.strip()), "")

    parents = {child: parent for parent in root.iter() for child in parent}

    def inside_detail(node):
        parent = parents.get(node)
        while parent is not None:
            if tag(parent) in XML_DETAIL_TAGS:
                return True
            parent = parents.get(parent)
        return False

    values = {}
    identifiers = {"number": [], "code": []}
    buyer_groups = []
    summary_amounts, detail_amounts = [], []
    preview = []
    remaining = RAW_TEXT_LIMIT
    for node in root.iter():
        name = tag(node)
        if name in {"buyer", "buyerinformation", "purchaser", "purchaserinformation"}:
            buyer_groups.append(node)
        value = (node.text or "").strip()
        if not value:
            continue
        if remaining > 0:
            separator = "\n" if preview else ""
            line = f"{separator}{name}: {value[:remaining]}"[:remaining]
            preview.append(line)
            remaining -= len(line)
        field = XML_TAG_FIELDS.get(name)
        if field:
            if field == "amount":
                (detail_amounts if inside_detail(node) else summary_amounts).append(value)
            else:
                values.setdefault(field, value)
            if field in identifiers:
                identifiers[field].append(value)
    result = {field: values.get(field, "") for field in XML_FIELDS if field != "amount"}
    result.update(method="XML 字段提取", warnings=[], raw_text="".join(preview))
    check_invoice_identifiers(result, identifiers["number"], identifiers["code"])
    for node in buyer_groups:
        if result["buyer_name"] and result["buyer_tax_id"]:
            break
        result["buyer_name"] = result["buyer_name"] or find(node, {"name"})
        result["buyer_tax_id"] = result["buyer_tax_id"] or find(node, {"taxid", "taxno", "taxpayerid"})
    dt = result["date"]
    digits = re.findall(r"\d+", dt)
    if len(digits) >= 3:
        result["date"] = f"{int(digits[0]):04}-{int(digits[1]):02}-{int(digits[2]):02}"
    elif re.fullmatch(r"\d{8}", dt):
        result["date"] = f"{dt[:4]}-{dt[4:6]}-{dt[6:]}"
    # 优先取汇总区块的价税合计，避免明细行同名标签先出现时抓错单行金额。
    amount = summary_amounts[0] if summary_amounts else detail_amounts[0] if detail_amounts else ""
    if len({_amount_key(value) for value in summary_amounts}) > 1:
        result["warnings"].append("XML 汇总层出现多个不一致的价税合计，请核对原件。")
    if amount:
        try:
            result["amount_cents"] = money(amount)
        except UserError as exc:
            result["warnings"].append(str(exc))
    return result


def from_recognition(pages, method, warnings=()):
    raw = "\n".join(page[0] for page in pages)
    reliable = "\n".join(page[1] for page in pages)
    # 低于 MIN_SCORE 的行保留原始文本与得分，供金额大小写交叉校验使用。
    low_lines = [entry for page in pages for entry in (page[2] if len(page) > 2 else ())]
    result = from_text(reliable)
    observed = result.copy() if raw == reliable else from_text(raw)
    result.update(raw_text=raw[:RAW_TEXT_LIMIT], method=method, recognition_version=EVIDENCE_VERSION)
    result["warnings"].extend(warnings)
    amount_confirmed = False
    if observed.get("amount_cents") and observed["amount_cents"] != result.get("amount_cents"):
        if amount_cross_checked(raw, observed["amount_cents"], low_lines):
            # 小写金额与大写金额精确一致：该金额字段视为已确认，不再因行级得分 0.93-0.97 阻断。
            result["amount_cents"] = observed["amount_cents"]
            amount_confirmed = True
        elif not result.get("amount_cents"):
            # 保留候选金额供人工核对，但保持 ocr_unconfirmed 阻断自动选票。
            result["amount_cents"] = observed["amount_cents"]
            result["warnings"].append(AMOUNT_UNCONFIRMED_WARNING)
    for field in OCR_FIELDS:
        value = result.get(field)
        if isinstance(value, str) and "□" in value:
            result[field] = ""
    if observed.get("multiple_invoice_identifiers") and not result.get("multiple_invoice_identifiers"):
        result["multiple_invoice_identifiers"] = True
        result["warnings"].append(MULTIPLE_INVOICE_IDENTIFIERS_WARNING)
    if any(observed.get(field) and observed[field] != result.get(field)
           for field in OCR_FIELDS if not (field == "amount_cents" and amount_confirmed)):
        result["warnings"].append(f"原件有字段未达到本地识别可信度要求（{MIN_SCORE:.0%}）。")
    if not has_required_fields(result):
        result["warnings"].append("本地识别未能确认全部必需字段，请核对原件或补充可识别的同票文件。")
    if result["warnings"]:
        result["ocr_unconfirmed"] = True
    return result


def _check_size(data):
    if len(data) > MAX_BYTES:
        raise UserError("单个文件不能超过 20 MB。")
    if not data:
        raise UserError("文件为空。")


def _read_pdf(data, *, detect_scans=False):
    if not data.startswith(b"%PDF-"):
        raise UserError("文件内容不是有效的 PDF。")
    try:
        reader = PdfReader(BytesIO(data))
        if reader.is_encrypted and not reader.decrypt(""):
            raise UserError("PDF 已加密，请解密后导入。")
        if len(reader.pages) > 20:
            raise UserError("每个 PDF 最多处理 20 页，请分别保存后导入。")
        parts = []
        scanned = False
        for page in reader.pages:
            content = page.get_contents()
            if content and len(content.get_data()) > 10 * 1024 * 1024:
                raise UserError("PDF 页面内容过大，请另存为简化的 PDF 后导入。")
            text = page.extract_text(extraction_mode="layout") or ""
            parts.append(text)
            if detect_scans and not scanned:
                characters = sum(char.isalnum() for char in text)
                scanned = characters == 0 or (characters < 48 and bool(len(page.images)))
        return parts, scanned
    except NotImplementedError as exc:
        raise UserError(f"PDF 使用了暂不支持的加密或内容处理方式，请解密或重新另存后导入：{exc}") from exc
    except PdfReadError as exc:
        raise UserError(f"PDF 无法读取：{exc}") from exc


def _sparse_text(text):
    """True when a page has (almost) no text layer and must be recognized from its image."""
    return sum(char.isalnum() for char in text) < 48


def _complete_pdf(data, parts, result):
    if result.get("multiple_invoice_identifiers"):
        return result, False
    layers = [result] if len(parts) == 1 else [from_text(text) for text in parts]
    complete = has_required_fields(result) and not result["warnings"]
    indices = []
    for index, (text, layer) in enumerate(zip(parts, layers)):
        if _sparse_text(text):
            # 扫描页没有可读文字层，只能靠渲染识别补全字段。
            indices.append(index)
        elif not complete:
            # 整份文字层缺少必需字段时才逐页补做 OCR。
            if layer["warnings"] or not has_required_fields(layer):
                indices.append(index)
        elif layer["warnings"]:
            # 文字层本身异常（如金额无法解析），仍需图像交叉核验。
            indices.append(index)
    if not indices:
        return result, False
    pages = [(text, text) for text in parts]
    warnings = []
    try:
        for index, page in zip(indices, recognize_pdf_pages(data, indices)):
            raw, reliable = page[0], page[1]
            low_lines = page[2] if len(page) > 2 else ()
            layer = layers[index]
            image = from_text(reliable)
            if any(layer.get(field) and image.get(field)
                   and comparable(field, layer[field]) != comparable(field, image[field]) for field in OCR_FIELDS):
                warnings.append(f"第 {index + 1} 页文字层与图像识别结果不一致。")
            pages[index] = (parts[index] + "\n" + raw, parts[index] + "\n" + reliable, low_lines)
    except UserError as exc:
        warnings.append(str(exc))
    return from_recognition(pages, "PDF 本地识别", warnings), True


def _extract_image(data, suffix):
    signatures = {".png": b"\x89PNG\r\n\x1a\n", ".jpg": b"\xff\xd8\xff", ".jpeg": b"\xff\xd8\xff"}
    if suffix not in signatures or not data.startswith(signatures[suffix]):
        raise UserError("文件格式与扩展名不匹配，或格式暂不支持。")
    try:
        pages = [recognize_raster(data)]
    except UserError as exc:
        return from_recognition([], "图片本地识别", [str(exc)])
    return from_recognition(pages, "图片本地识别")


def _ofd_structured_values(document):
    """把 OFD 结构层（内置发票 XML / CustomDatas / CustomTag）归一化为字段值。"""
    raw = {}
    for name, value in document["custom"].items():
        if field := OFD_CUSTOM_FIELDS.get(name):
            raw.setdefault(field, value)
    for name, value in document["tags"].items():
        if field := OFD_TAG_FIELDS.get(name):
            raw.setdefault(field, value)
    values = {}
    number = re.sub(r"\s+", "", raw.get("number", ""))
    if re.fullmatch(r"\d{8}|\d{20}", number):
        values["number"] = number
    code = re.sub(r"\s+", "", raw.get("code", ""))
    if re.fullmatch(r"\d{10}|\d{12}", code):
        values["code"] = code
    digits = re.findall(r"\d+", raw.get("date", ""))
    if len(digits) >= 3:
        values["date"] = f"{int(digits[0]):04}-{int(digits[1]):02}-{int(digits[2]):02}"
    for field in ("buyer_name", "seller_name"):
        if value := re.sub(r"\s+", "", raw.get(field, "")):
            values[field] = value
    tax_id = re.sub(r"\s+", "", raw.get("buyer_tax_id", "")).upper()
    if re.fullmatch(r"[A-Z0-9]{15,20}", tax_id):
        values["buyer_tax_id"] = tax_id
    if raw.get("amount"):
        try:
            values["amount_cents"] = money(raw["amount"])
        except UserError:
            pass
    return values


def _ofd_result(document):
    """优先复用 OFD 包内的结构化发票 XML，再用 CustomDatas/CustomTag 补齐缺失字段。"""
    result = None
    for payload in document["attachments"]:
        try:
            parsed = from_xml(payload)
        except UserError:
            continue
        if parsed.get("number"):
            result = parsed
            break
    if result is None:
        result = {"raw_text": "", "method": "OFD 结构提取", "warnings": []}
    for field, value in _ofd_structured_values(document).items():
        if not result.get(field):
            result[field] = value
    return result


def _ofd_evidence(data, *, render):
    document = ofd_document(data)
    structured = _ofd_result(document)
    text = document["text"]
    layer_warnings = list(structured["warnings"])
    if has_required_fields(structured) and not layer_warnings:
        # 文字层保留发票标题等上下文，供邮件筛选与详情展示；字段仍以结构层为准。
        structured.update(raw_text=(text or structured.get("raw_text") or "")[:RAW_TEXT_LIMIT],
                          method="OFD 结构提取", recognition_version=EVIDENCE_VERSION)
        return structured
    parsed = from_text(text)
    conflicts = [field for field in OCR_FIELDS
                 if structured.get(field) and parsed.get(field)
                 and comparable(field, structured[field]) != comparable(field, parsed[field])]
    if not conflicts:
        for field in OCR_FIELDS:
            if structured.get(field) and not parsed.get(field):
                parsed[field] = structured[field]
    # 结构层自身的异常（如汇总金额互相矛盾）不能因为改走文字层而丢失。
    parsed["warnings"].extend(layer_warnings)
    if not conflicts and parsed.get("number") and has_required_fields(parsed) and not parsed["warnings"]:
        parsed.update(raw_text=text[:RAW_TEXT_LIMIT], method="OFD 文字提取", recognition_version=EVIDENCE_VERSION)
        return parsed
    if not render:
        return parsed
    # 结构层与文字层都无法确认全部字段时才渲染页面做本地识别。
    warnings = [OFD_LAYER_CONFLICT_WARNING] if conflicts else []
    return from_recognition(recognize_ofd(data), "OFD 本地识别", warnings)


def _extract_ofd(data):
    try:
        return _ofd_evidence(data, render=True)
    except UserError as exc:
        return from_recognition([], "OFD 本地识别", [str(exc)])


def extract(data: bytes, suffix: str) -> dict:
    _check_size(data)
    if suffix == ".xml":
        return from_xml(data)
    if suffix == ".pdf":
        parts, _ = _read_pdf(data)
        return _complete_pdf(data, parts, from_text("\n".join(parts)))[0]
    if suffix == ".ofd":
        return _extract_ofd(data)
    return _extract_image(data, suffix)


def inspect_mail_document(data, suffix, *, allow_ocr):
    _check_size(data)
    if suffix == ".xml":
        return from_xml(data), False, False
    if suffix == ".pdf":
        parts, scanned = _read_pdf(data, detect_scans=True)
        result = from_text("\n".join(parts))
        if (allow_ocr and scanned) or has_invoice_title(result):
            result, ocr = _complete_pdf(data, parts, result)
            return result, False, ocr
        return result, scanned, False
    if suffix == ".ofd":
        result = _ofd_evidence(data, render=False)
        deferred = not allow_ocr and not (has_invoice_content(result) or has_invoice_title(result))
        return result, deferred, False
    return _extract_image(data, suffix), False, True
def _eml_text(part) -> str:
    try:
        text = part.get_content()
    except (KeyError, LookupError, UnicodeError):
        return ""
    return text if isinstance(text, str) else ""


def eml_attachments(data: bytes) -> list[dict]:
    """读取本地 .eml 的受支持附件，并复用邮箱路径的内联装饰图过滤规则。

    返回条目为 {"name", "payload", "oversized"}；oversized 表示单个附件超过
    MAX_BYTES，由调用方按附件记录错误，不影响同封邮件的其他附件。message/rfc822
    嵌套转发邮件的附件由 walk() 一并展开，主题与正文用于判断发票上下文。
    """
    message = BytesParser(policy=policy.default).parsebytes(data)
    subjects = [str(message.get("Subject") or "")]
    texts, parts = [], []
    for part in message.walk():
        if part.get_content_type() == "message/rfc822":
            if title := part.get("Subject"):
                subjects.append(str(title))
            continue
        if part.is_multipart():
            continue
        name = part.get_filename()
        if not name:
            if part.get_content_type() in {"text/plain", "text/html"}:
                texts.append(_eml_text(part))
            continue
        name = Path(name.replace("\\", "/")).name
        suffix = Path(name).suffix.lower()
        if suffix not in SUPPORTED:
            continue
        parts.append((name, suffix, part.get_content_disposition() or "", part.get_payload(decode=True) or b""))
    context = bool(INVOICE_NAME_HINT.search("\n".join([*subjects, *texts])))
    has_document = any(suffix in SUPPORTED - IMAGE_SUFFIXES for _, suffix, _, _ in parts)
    attachments = []
    for name, suffix, disposition, payload in parts:
        if suffix in IMAGE_SUFFIXES:
            hinted = bool(INVOICE_NAME_HINT.search(name))
            # logo/banner/signature/pixel 等装饰图，以及同封邮件已有 PDF/XML/OFD
            # 主文档时的普通内联签名图，都不是发票原件，直接跳过。
            if DECORATIVE_IMAGE.fullmatch(Path(name).stem) and not hinted:
                continue
            if disposition == "inline" and not hinted and (has_document or not context):
                continue
        attachments.append({"name": name, "payload": payload, "oversized": len(payload) > MAX_BYTES})
    return attachments
