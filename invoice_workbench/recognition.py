from functools import lru_cache
from io import BytesIO
from pathlib import Path
import re
import subprocess
from tempfile import TemporaryDirectory
import unicodedata
import xml.etree.ElementTree as ET
from zipfile import BadZipFile, ZipFile

from PIL import Image, ImageOps, UnidentifiedImageError

from .domain import UserError


ROOT = Path(__file__).resolve().parent.parent
EVIDENCE_VERSION = 3
MIN_SCORE = 0.98
# 金额行的行级置信度可以略低于其它关键字段，但必须与同页中文大写金额精确一致（到分）才能采用。
AMOUNT_SCORE_FLOOR = 0.88
MAX_OFD_XML_BYTES = 32 * 1024 * 1024
MAX_OFD_ATTACHMENT_BYTES = 4 * 1024 * 1024
MAX_OFD_ATTACHMENTS = 3
MODEL_FILES = {"Det": "PP-OCRv6_det_small.onnx", "Cls": "ch_ppocr_mobile_v2.0_cls_mobile.onnx",
               "Rec": "PP-OCRv6_rec_small.onnx"}
CN_DIGITS = {"零": 0, "〇": 0, "壹": 1, "贰": 2, "叁": 3, "肆": 4, "伍": 5, "陆": 6, "柒": 7, "捌": 8, "玖": 9}
CN_UNITS = {"拾": 10, "佰": 100, "仟": 1000}
CN_NUMERALS = "零〇壹贰叁肆伍陆柒捌玖拾佰仟万亿"
CN_FRACTION = "零〇壹贰叁肆伍陆柒捌玖拾角分整正"
CN_AMOUNT = re.compile(f"[{CN_NUMERALS}]+[圆元][{CN_FRACTION}]{{0,8}}")


@lru_cache(maxsize=1)
def _engine():
    import rapidocr

    directory = Path(rapidocr.__file__).parent / "models"
    model_paths = {f"{kind}.model_path": str(directory / name) for kind, name in MODEL_FILES.items()}
    if any(not Path(path).is_file() for path in model_paths.values()):
        raise UserError("本地 OCR 模型不完整，请重新安装项目依赖。")
    return rapidocr.RapidOCR(params={**model_paths, "Global.text_score": 0.5,
                                    "EngineConfig.onnxruntime.intra_op_num_threads": 2})


def _display_width(text):
    """Visual columns of a text run: full/half-width characters count as 2, others as 1."""
    return sum(2 if unicodedata.east_asian_width(char) in "WF" else 1 for char in text)


def _text_rows(cells):
    """Group recognised cells into visual rows: [(height, [(left, score, text), ...]), ...]."""
    rows = []
    for top, left, height, score, text in sorted(cells):
        if rows and abs(top - rows[-1][0]) < min(height, rows[-1][1]) * 0.52:
            rows[-1][2].append((left, score, text))
        else:
            rows.append((top, height, [(left, score, text)]))
    return rows


def _render_row(height, row, minimum):
    line = ""
    for left, score, text in sorted(row):
        if score < minimum:
            text = "□"
        # 一列 = 半个字宽 ≈ 0.5 倍行高；系数偏大会把右侧栏位压进左栏间隙。
        before = round(left / max(5, height * 0.50))
        line += " " * max(1 if line else 0, before - _display_width(line)) + text
    return line


def _text_lines(cells, minimum):
    return "\n".join(_render_row(height, row, minimum) for _, height, row in _text_rows(cells))


def chinese_amount(text):
    """严格解析中文财务大写金额，返回整数分；写法不完整或不合法时返回 None。"""
    text = re.sub(r"\s+", "", str(text))
    if not CN_AMOUNT.fullmatch(text):
        return None
    integer_text, fraction_text = re.split(r"[圆元]", text, maxsplit=1)
    total = section = number = 0
    for char in integer_text:
        if char in CN_DIGITS:
            number = CN_DIGITS[char]
        elif char in CN_UNITS:
            section += (number or 1) * CN_UNITS[char]
            number = 0
        elif char == "万":
            total += (section + number) * 10000
            section = number = 0
        elif char == "亿":
            total = (total + section + number) * 100000000
            section = number = 0
        else:
            return None
    total += section + number
    number = 0
    jiao = fen = 0
    dangling = False
    for char in fraction_text:
        if char in CN_DIGITS:
            number = CN_DIGITS[char]
            dangling = True
        elif char == "角":
            jiao, number, dangling = number, 0, False
        elif char == "分":
            fen, number, dangling = number, 0, False
        elif char not in "整正":
            return None
    if dangling:
        return None
    return total * 100 + jiao * 10 + fen


def uppercase_amounts(text):
    """同页出现的全部中文大写金额（分）。"""
    compact = re.sub(r"\s+", "", text)
    values = (chinese_amount(match.group(0)) for match in CN_AMOUNT.finditer(compact))
    return [value for value in values if value is not None]


def _line_amount_cents(line):
    """低分行中的金额候选（整数、一至两位小数、千分位），统一换算为分。"""
    for token in re.findall(r"(?<![\d.,])\d[\d,]*(?:\.\d{1,2})?(?![\d.])", line):
        whole, _, fraction = token.replace(",", "").partition(".")
        yield int(whole or 0) * 100 + int((fraction + "00")[:2])


def amount_cross_checked(text, cents, low_lines=()):
    """小写金额与同页大写金额精确一致、且行级置信度达标时才确认金额字段。

    ``low_lines`` 为低于 MIN_SCORE 的行：(行得分, 原始行文本)。
    """
    if cents <= 0:
        return False
    compact = re.sub(r"\s+", "", text)
    matches = list(CN_AMOUNT.finditer(compact))
    if not matches or any(chinese_amount(match.group(0)) != cents for match in matches):
        return False
    for score, line in low_lines:
        compact_line = re.sub(r"\s+", "", line)
        # 不能只匹配固定的两位小数写法，否则 ¥555 或 ¥555.0 会绕过行级置信度下限。
        if cents in _line_amount_cents(compact_line) or any(match.group(0) in compact_line for match in matches):
            if score < AMOUNT_SCORE_FLOOR:
                return False
    return True


def recognize_image(image):
    import numpy as np

    image = ImageOps.exif_transpose(image).convert("RGB")
    image.thumbnail((3000, 3000))
    output = _engine()(np.ascontiguousarray(np.asarray(image)[:, :, ::-1]))
    cells = []
    if output.boxes is not None and output.txts:
        for vertices, text, score in zip(output.boxes, output.txts, output.scores):
            left, top = vertices.min(axis=0)
            height = max(1.0, vertices[:, 1].max() - top)
            cells.append((float(top + height / 2), float(left), float(height), float(score), text))
    raw_lines, reliable_lines, low_lines = [], [], []
    for _, height, row in _text_rows(cells):
        raw_line = _render_row(height, row, 0)
        raw_lines.append(raw_line)
        # 低分行保留原始文本和得分，供字段级复核（金额大小写交叉校验）使用。
        if (lowest := min(score for _, score, _ in row)) < MIN_SCORE:
            reliable_lines.append(_render_row(height, row, MIN_SCORE))
            low_lines.append((lowest, raw_line))
        else:
            reliable_lines.append(raw_line)
    return "\n".join(raw_lines), "\n".join(reliable_lines), low_lines


def recognize_raster(data):
    try:
        with Image.open(BytesIO(data)) as image:
            if image.width * image.height > 32_000_000:
                raise UserError("图片像素过多，请缩小后导入。")
            return recognize_image(image)
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError) as exc:
        raise UserError("图片无法读取，请检查原件。") from exc


def mail_qr(data):
    import cv2
    import numpy as np

    try:
        with Image.open(BytesIO(data)) as image:
            if image.width * image.height > 32_000_000:
                raise UserError("图片像素过多，请缩小后导入。")
            image = ImageOps.exif_transpose(image).convert("L")
            image.thumbnail((1024, 1024))
            found, corners = cv2.QRCodeDetector().detect(np.asarray(image))
            if not found:
                return False, False
            area = cv2.contourArea(corners.reshape(4, 2))
            return True, area >= 0.35 * image.width * image.height
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError) as exc:
        raise UserError("图片无法读取，请检查原件。") from exc


def recognize_pdf_pages(data, indices):
    import pypdfium2 as pdfium

    try:
        with pdfium.PdfDocument(data) as document:
            for index in indices:
                page = document[index]
                try:
                    scale = min(4.0, 2900 / max(page.get_size()))
                    bitmap = page.render(scale=scale)
                    try:
                        yield recognize_image(bitmap.to_pil())
                    finally:
                        bitmap.close()
                finally:
                    page.close()
    except pdfium.PdfiumError as exc:
        raise UserError("PDF 页面无法转换为图像。") from exc


def _validate_ofd(data):
    try:
        with ZipFile(BytesIO(data)) as archive:
            entries = archive.infolist()
            if len(entries) > 3000 or sum(entry.file_size for entry in entries) > 120 * 1024 * 1024:
                raise UserError("OFD 内容超过处理限制。")
            if "OFD.xml" not in archive.namelist():
                raise UserError("OFD 缺少文档入口。")
            for entry in entries:
                name = entry.filename.replace("\\", "/")
                if name.startswith("/") or ":" in name or ".." in name.split("/") or entry.flag_bits & 1:
                    raise UserError("OFD 包含非法路径或加密内容。")
    except BadZipFile as exc:
        raise UserError("无法读取 OFD 原件。") from exc


def _ofd_local(tag):
    return tag.rsplit("}", 1)[-1].lower()


def _ofd_root(archive, name):
    """读取并解析 OFD 包内 XML；拒绝 DTD/实体声明、空字节与超大部件。"""
    try:
        payload = archive.read(name)
    except (KeyError, OSError, RuntimeError, BadZipFile):
        return None
    if len(payload) > MAX_OFD_XML_BYTES:
        return None
    if re.search(br"<!\s*(?:DOCTYPE|ENTITY)\b", payload, re.I) or b"\x00" in payload:
        return None
    try:
        return ET.fromstring(payload)
    except ET.ParseError:
        return None


def _ofd_content(archive):
    """页面/模板 Content.xml 的文本对象：ObjectID 到文本的映射与版面单元格。"""
    texts, cells, seen = {}, [], set()
    for name in archive.namelist():
        lowered = name.lower()
        if not lowered.endswith("content.xml") or ("/pages/" not in lowered and "/tpls/" not in lowered):
            continue
        root = _ofd_root(archive, name)
        if root is None:
            continue
        for node in root.iter():
            if _ofd_local(node.tag) != "textobject":
                continue
            text = "".join(child.text or "" for child in node.iter() if _ofd_local(child.tag) == "textcode")
            text = re.sub(r"\s+", "", text)
            if not text:
                continue
            if identity := node.get("ID"):
                texts.setdefault(identity, text)
            boundary = (node.get("Boundary") or "").split()
            if len(boundary) < 4:
                continue
            try:
                left, top, _, height = (float(value) for value in boundary[:4])
            except ValueError:
                continue
            key = (round(left, 2), round(top, 2), text)
            if key in seen:
                continue
            seen.add(key)
            cells.append((top + height / 2, left, max(height, 1.0), 1.0, text))
    return texts, cells


def _ofd_custom_data(archive):
    """OFD.xml 的 DocInfo/CustomDatas：键名去除空白后映射为值。"""
    values = {}
    root = _ofd_root(archive, "OFD.xml")
    if root is None:
        return values
    for node in root.iter():
        if _ofd_local(node.tag) != "customdata":
            continue
        name = re.sub(r"\s+", "", node.get("Name") or "")
        value = (node.text or "").strip()
        if name and value:
            values.setdefault(name, value)
    return values


def _ofd_tag_files(archive):
    names = []
    for name in archive.namelist():
        if not name.lower().endswith("tags/customtags.xml"):
            continue
        root = _ofd_root(archive, name)
        if root is None:
            continue
        base = name.rsplit("/", 1)[0]
        for node in root.iter():
            if _ofd_local(node.tag) == "fileloc" and (node.text or "").strip():
                names.append(f"{base}/{node.text.strip().lstrip('/')}")
    return names


def _ofd_tag_values(archive, texts):
    """CustomTag.xml 的语义标签（BuyerName/TaxInclusiveTotalAmount 等）到文本值。"""
    values = {}
    for name in _ofd_tag_files(archive):
        root = _ofd_root(archive, name)
        if root is None:
            continue
        for node in root.iter():
            key = _ofd_local(node.tag)
            if not key or key == "objectref":
                continue
            value = "".join(texts.get((ref.text or "").strip(), "") for ref in node.iter()
                            if _ofd_local(ref.tag) == "objectref")
            if value:
                values.setdefault(key, value)
    return values


def _ofd_attachments(archive):
    """OFD 包内内置结构化发票 XML（Attachs/Attachments.xml 指向的原件）。"""
    payloads = []
    for name in sorted(archive.namelist()):
        lowered = name.lower()
        if "/attachs/" not in lowered and "/attachments/" not in lowered:
            continue
        if not lowered.endswith(".xml") or name.rsplit("/", 1)[-1].lower() == "attachments.xml":
            continue
        try:
            if archive.getinfo(name).file_size > MAX_OFD_ATTACHMENT_BYTES:
                continue
            payloads.append(archive.read(name))
        except (KeyError, OSError, RuntimeError, BadZipFile):
            continue
        if len(payloads) >= MAX_OFD_ATTACHMENTS:
            break
    return payloads


def ofd_document(data):
    """毫秒级解析 OFD 包：结构化字段、文字层与版面文本，不启动 Java 渲染。"""
    _validate_ofd(data)
    with ZipFile(BytesIO(data)) as archive:
        texts, cells = _ofd_content(archive)
        return {"custom": _ofd_custom_data(archive), "tags": _ofd_tag_values(archive, texts),
                "attachments": _ofd_attachments(archive), "text": _text_lines(cells, 0)}


def _process_ofd(data):
    _validate_ofd(data)
    renderer = ROOT / "runtime-ofd"
    java = renderer / "java" / "bin" / "java.exe"
    if not java.is_file() or not (renderer / "invoice-ofd.jar").is_file():
        raise UserError("未找到本地 OFD 转换组件，请使用完整便携包或完成项目安装。")
    with TemporaryDirectory(prefix="invoice-ofd-") as directory:
        directory = Path(directory)
        source = directory / "invoice.ofd"
        source.write_bytes(data)
        output = directory / "pages"
        args = [str(java), "-Xmx384m", "-Djava.awt.headless=true", f"-Djava.io.tmpdir={directory}",
                "-Djavax.xml.accessExternalDTD=", "-Djavax.xml.accessExternalSchema=", "-Djdk.xml.dtd.support=deny",
                "-cp", str(renderer / "invoice-ofd.jar") + ";" + str(renderer / "lib" / "*"),
                "InvoiceOFD", str(source), str(output)]
        try:
            subprocess.run(args, capture_output=True, check=True, timeout=80,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except subprocess.TimeoutExpired as exc:
            raise UserError("OFD 转换超时，请检查原件。") from exc
        except subprocess.CalledProcessError as exc:
            if exc.returncode == 2:
                raise UserError("OFD 最多支持 20 页。") from exc
            raise UserError("OFD 转换失败，请检查原件。") from exc
        files = sorted(output.glob("*.png"))
        if not files:
            raise UserError("OFD 未生成可识别页面。")
        return [recognize_raster(path.read_bytes()) for path in files]


def recognize_ofd(data):
    return _process_ofd(data)
