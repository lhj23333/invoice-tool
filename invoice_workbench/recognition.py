from functools import lru_cache
from io import BytesIO
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from zipfile import BadZipFile, ZipFile

from PIL import Image, ImageOps, UnidentifiedImageError

from .domain import UserError


ROOT = Path(__file__).resolve().parent.parent
EVIDENCE_VERSION = 2
MIN_SCORE = 0.98
MODEL_FILES = {"Det": "PP-OCRv6_det_small.onnx", "Cls": "ch_ppocr_mobile_v2.0_cls_mobile.onnx",
               "Rec": "PP-OCRv6_rec_small.onnx"}


@lru_cache(maxsize=1)
def _engine():
    import rapidocr

    directory = Path(rapidocr.__file__).parent / "models"
    model_paths = {f"{kind}.model_path": str(directory / name) for kind, name in MODEL_FILES.items()}
    if any(not Path(path).is_file() for path in model_paths.values()):
        raise UserError("本地 OCR 模型不完整，请重新安装项目依赖。")
    return rapidocr.RapidOCR(params={**model_paths, "Global.text_score": 0.5,
                                    "EngineConfig.onnxruntime.intra_op_num_threads": 2})


def _text_lines(cells, minimum):
    rows = []
    for top, left, height, score, text in sorted(cells):
        if rows and abs(top - rows[-1][0]) < min(height, rows[-1][1]) * 0.52:
            rows[-1][2].append((left, score, text))
        else:
            rows.append((top, height, [(left, score, text)]))
    lines = []
    for _, height, row in rows:
        line = ""
        for left, score, text in sorted(row):
            if score < minimum:
                text = "□"
            before = round(left / max(5, height * 0.70))
            line += " " * max(1 if line else 0, before - len(line)) + text
        lines.append(line)
    return "\n".join(lines)


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
    return _text_lines(cells, 0), _text_lines(cells, MIN_SCORE)


def recognize_raster(data):
    try:
        with Image.open(BytesIO(data)) as image:
            if image.width * image.height > 32_000_000:
                raise UserError("图片像素过多，请缩小后导入。")
            return recognize_image(image)
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


def recognize_ofd(data):
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
    renderer = ROOT / "runtime-ofd"
    java = renderer / "java" / "bin" / "java.exe"
    if not java.is_file() or not (renderer / "invoice-ofd.jar").is_file():
        raise UserError("未找到本地 OFD 转换组件，请使用完整便携包或完成项目安装。")
    with TemporaryDirectory(prefix="invoice-ofd-") as directory:
        directory = Path(directory)
        source = directory / "invoice.ofd"
        source.write_bytes(data)
        output = directory / "pages"
        try:
            subprocess.run([str(java), "-Xmx384m", "-Djava.awt.headless=true", f"-Djava.io.tmpdir={directory}",
                                      "-Djavax.xml.accessExternalDTD=", "-Djavax.xml.accessExternalSchema=", "-Djdk.xml.dtd.support=deny",
                                      "-cp", str(renderer / "invoice-ofd.jar") + ";" + str(renderer / "lib" / "*"),
                                      "InvoiceOFD", str(source), str(output)], capture_output=True, check=True,
                                     timeout=80, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
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
