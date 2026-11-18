from __future__ import annotations

from email import policy
from email.parser import BytesParser
from io import BytesIO
from pathlib import Path
import re
import xml.etree.ElementTree as ET

from pypdf import PdfReader
from pypdf.errors import PdfReadError

from .domain import UserError, money

SUPPORTED = {".pdf", ".xml", ".png", ".jpg", ".jpeg", ".ofd"}
MAX_BYTES = 20 * 1024 * 1024
MULTIPLE_INVOICE_IDENTIFIERS_WARNING = "原件包含多个不同发票号码或代码，无法自动核验；请将每张发票分别保存后重新导入。"


def check_invoice_identifiers(result, numbers, codes):
    if len(set(numbers)) > 1 or (any(len(number) == 8 for number in numbers) and len(set(codes)) > 1):
        result["multiple_invoice_identifiers"] = True
        result["warnings"].append(MULTIPLE_INVOICE_IDENTIFIERS_WARNING)


def from_text(text: str) -> dict:
    # 提取结果由自动核验流程与存储字段逐项比较。
    text = text.replace("\u3000", " ").replace("：", ":")
    result = {"raw_text": text[:30000], "method": "PDF 文字提取", "warnings": []}

    def first(pattern: str, source: str = text) -> str:
        match = re.search(pattern, source, re.I)
        return match.group(1).strip() if match else ""

    number_pattern = r"(?:发\s*票\s*号\s*码|Invoice\s*(?:No\.?|Number))\s*:?\s*(\d{20}|\d{8})(?!\d)"
    code_pattern = r"(?:发\s*票\s*代\s*码|Invoice\s*Code)\s*:?\s*(\d{10,12})(?!\d)"
    result["number"] = first(number_pattern)
    result["code"] = first(code_pattern)
    check_invoice_identifiers(result, re.findall(number_pattern, text, re.I), re.findall(code_pattern, text, re.I))
    dt = first(r"(?:开\s*票\s*日\s*期|Invoice\s*Date)\s*:?\s*(\d{4}\s*[年/-]\s*\d{1,2}\s*[月/-]\s*\d{1,2})")
    if dt:
        parts = re.findall(r"\d+", dt)
        result["date"] = f"{int(parts[0]):04}-{int(parts[1]):02}-{int(parts[2]):02}"
    amount_text = text.replace("−", "-").replace("－", "-")
    # 将负号和货币符号一起保留，前面的排版文字不能吞掉负号。
    amount_pattern = r"(-?\s*[¥￥]?\s*-?\s*[\d,]+\.\d{2})(?!\d)"
    amount = first(r"(?:价\s*税\s*合\s*计\s*(?:\([^)]*\))?|Total\s*(?:with\s*Tax|Amount))[^\d\n¥￥-]{0,25}" + amount_pattern, amount_text)
    if not amount:
        amount = first(r"小\s*写\s*[)）]?\s*:?\s*" + amount_pattern, amount_text)
    if not amount:
        # 部分 PDF 将大写金额排在“小写”和货币符号之间。
        currency_amount_pattern = r"(-?\s*[¥￥]\s*-?\s*[\d,]+\.\d{2})(?!\d)"
        amount = first(r"小\s*写[^¥￥\n-]{0,160}" + currency_amount_pattern, amount_text)
    if amount:
        try:
            result["amount_cents"] = money(re.sub(r"\s+", "", amount))
        except UserError as exc:
            result["warnings"].append(str(exc))
    # Avoid accidentally taking the seller's tax ID from an unscoped match.
    buyer = first(r"(?:购\s*买\s*方(?:\s*信\s*息)?|Buyer)([\s\S]*?)(?:销\s*售\s*方|Seller|货物或应税|项目名称|$)")
    if buyer:
        name = re.search(r"(?:名\s*称|Name)\s*:\s*([^\n]+)", buyer, re.I)
        tax = re.search(r"(?:纳税人识别号|统一社会信用代码|Tax\s*ID)\s*:?\s*([A-Z0-9]{15,20})", buyer, re.I)
        result["buyer_name"] = name.group(1).strip() if name else ""
        result["buyer_tax_id"] = tax.group(1).upper() if tax else ""
    # Common digital invoices put buyer/seller in two columns, with the words
    # 购买方信息 printed vertically. Scope by the columns, not a global first tax ID.
    lines = text.splitlines()
    for n, line in enumerate(lines):
        left_name = re.match(r"[ \t]*购[ \t]+名[ \t]*称[ \t]*:[ \t]*(.*)", line)
        if not left_name:
            continue
        gap = re.search(r"[ \t]{3,}", left_name.group(1))
        if not gap:
            continue
        boundary = left_name.start(1) + gap.end()
        result["buyer_name"] = left_name.group(1)[:gap.start()].strip()
        party_lines = []
        for party_line in lines[n:n + 12]:
            if re.search(r"项目名称|货物或应税|规格型号", party_line):
                break
            party_lines.append(party_line[:boundary])
        tax_ids = re.findall(r"(?<![A-Z0-9])[A-Z0-9]{15,20}(?![A-Z0-9])", "\n".join(party_lines), re.I)
        result["buyer_tax_id"] = tax_ids[0].upper() if len(tax_ids) == 1 else ""
        right_name = re.search(r"名[ \t]*称[ \t]*:[ \t]*(.+)", line[boundary:])
        if right_name:
            result["seller_name"] = right_name.group(1).strip()
        break
    return result


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
        aliases = {a.lower() for a in aliases}
        return next((e.text.strip() for e in node.iter() if tag(e) in aliases and e.text and e.text.strip()), "")

    result = {"method": "XML 字段提取", "warnings": [],
              "raw_text": "\n".join(f"{tag(e)}: {e.text.strip()}" for e in root.iter() if e.text and e.text.strip())[:30000]}
    aliases = {
        "number": ["InvoiceNumber", "InvoiceNo", "DigitalInvoiceNo", "Fphm", "发票号码"],
        "code": ["InvoiceCode", "Fpdm", "发票代码"],
        "date": ["IssueDate", "InvoiceDate", "Kprq", "开票日期"],
        "buyer_name": ["BuyerName", "PurchaserName", "Gfmc", "购买方名称"],
        "buyer_tax_id": ["BuyerTaxID", "BuyerTaxNo", "PurchaserTaxID", "Gfsh", "购买方纳税人识别号"],
        "seller_name": ["SellerName", "Xfmc", "销售方名称"],
    }
    for field, names in aliases.items():
        result[field] = find(root, names)
    identifiers = {}
    for field in ("number", "code"):
        names = {name.lower() for name in aliases[field]}
        identifiers[field] = [e.text.strip() for e in root.iter() if tag(e) in names and e.text and e.text.strip()]
    check_invoice_identifiers(result, identifiers["number"], identifiers["code"])
    for node in root.iter():
        if tag(node) in {"buyer", "buyerinformation", "purchaser", "purchaserinformation"}:
            result["buyer_name"] = result["buyer_name"] or find(node, ["Name"])
            result["buyer_tax_id"] = result["buyer_tax_id"] or find(node, ["TaxID", "TaxNo", "TaxpayerID"])
    dt = result.get("date", "")
    digits = re.findall(r"\d+", dt)
    if len(digits) >= 3:
        result["date"] = f"{int(digits[0]):04}-{int(digits[1]):02}-{int(digits[2]):02}"
    elif re.fullmatch(r"\d{8}", dt):
        result["date"] = f"{dt[:4]}-{dt[4:6]}-{dt[6:]}"
    amount = find(root, ["TotalWithTax", "TotalTaxIncludedAmount", "TotalTax-includedAmount", "AmountWithTax", "Jshj", "价税合计"])
    if amount:
        try:
            result["amount_cents"] = money(amount)
        except UserError as exc:
            result["warnings"].append(str(exc))
    return result


def extract(data: bytes, suffix: str) -> dict:
    if len(data) > MAX_BYTES:
        raise UserError("单个文件不能超过 20 MB。")
    if not data:
        raise UserError("文件为空。")
    if suffix == ".xml":
        return from_xml(data)
    if suffix == ".pdf":
        if not data.startswith(b"%PDF-"):
            raise UserError("文件内容不是有效的 PDF。")
        try:
            reader = PdfReader(BytesIO(data))
            if reader.is_encrypted and not reader.decrypt(""):
                raise UserError("PDF 已加密，请解密后导入。")
            if len(reader.pages) > 20:
                raise UserError("第一版每个文件最多处理 20 页，请拆分文件。")
            parts = []
            for page in reader.pages:
                content = page.get_contents()
                if content and len(content.get_data()) > 10 * 1024 * 1024:
                    raise UserError("PDF 页面内容过大，请另存为简化的 PDF 后导入。")
                parts.append(page.extract_text(extraction_mode="layout") or "")
            result = from_text("\n".join(parts))
            if not result["raw_text"].strip():
                result["warnings"].append("未发现 PDF 文字层；当前未接入 OCR，无法自动核验。")
            return result
        except NotImplementedError as exc:
            raise UserError(f"PDF 使用了暂不支持的加密或内容处理方式，请解密或重新另存后导入：{exc}") from exc
        except PdfReadError as exc:
            raise UserError(f"PDF 无法读取：{exc}") from exc
    signatures = {".png": b"\x89PNG\r\n\x1a\n", ".jpg": b"\xff\xd8\xff", ".jpeg": b"\xff\xd8\xff", ".ofd": b"PK"}
    if suffix not in signatures or not data.startswith(signatures[suffix]):
        raise UserError("文件格式与扩展名不匹配，或格式暂不支持。")
    return {"method": "仅保存原件", "warnings": ["图片 / OFD 当前仅保存原件，无法自动核验。"], "raw_text": ""}


def eml_attachments(data: bytes) -> tuple[list[tuple[str, bytes]], list[str]]:
    message = BytesParser(policy=policy.default).parsebytes(data)
    attachments = []
    for part in message.walk():
        name = part.get_filename()
        if name and Path(name).suffix.lower() in SUPPORTED:
            attachments.append((Path(name.replace("\\", "/")).name, part.get_payload(decode=True) or b""))
    notes = []
    if not attachments:
        notes.append("邮件未发现支持的发票附件。正文下载链接尚未接入，请手动下载后导入。")
    return attachments, notes
