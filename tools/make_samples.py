"""Generate clearly marked synthetic invoices for development only."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tests.helpers import BUYER, TAX, xml_invoice


def main():
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.pdfgen import canvas
    from PIL import Image, ImageDraw

    folder = ROOT / "samples"
    folder.mkdir(exist_ok=True)
    for n, amount in enumerate(("600.00", "402.50", "998.00", "1020.00"), 1):
        (folder / f"sample-{n:02d}.xml").write_bytes(xml_invoice(number=f"260000000000000000{n:02d}", amount=amount, date=f"2026-10-{n:02d}"))
    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    pdf = canvas.Canvas(str(folder / "sample-01.pdf"), pagesize=(680, 380))
    pdf.setFont("STSong-Light", 17)
    pdf.setFillColorRGB(.08, .42, .36)
    pdf.drawString(32, 342, "电子发票 · 合成样本，不可用于报销")
    pdf.setStrokeColorRGB(.5, .6, .6)
    pdf.line(32, 326, 648, 326)
    pdf.setFillColorRGB(.1, .13, .16)
    pdf.setFont("STSong-Light", 12)
    for y, text in zip(range(298, 100, -25), (
        "发票号码：26000000000000000001", "开票日期：2026年10月01日", "购买方信息", f"名称：{BUYER}",
        f"纳税人识别号：{TAX}", "销售方", "名称：合成样本商户", "价税合计（小写）￥600.00")):
        pdf.drawString(32, y, text)
    pdf.setFont("Helvetica", 9)
    pdf.drawString(32, 40, "SYNTHETIC SAMPLE - NOT A VALID INVOICE")
    pdf.save()
    images = folder / "image-preview"
    images.mkdir(exist_ok=True)
    image = Image.new("RGB", (960, 500), "#f4f7f5")
    draw = ImageDraw.Draw(image)
    draw.rectangle((20, 20, 940, 480), outline="#167365", width=3)
    draw.text((50, 55), "SYNTHETIC IMAGE SAMPLE - NOT A VALID INVOICE", fill="#167365")
    draw.text((50, 120), "Image import / manual review sample", fill="#202a35")
    draw.text((50, 170), "Number, amount and company must be entered after checking the original.", fill="#202a35")
    image.save(images / "manual-review.png")
    (folder / "README.txt").write_text("本目录全部为合成样本，不可用于报销。\n四张 XML 发票；sample-01.pdf 与 sample-01.xml 是同一张票，应自动归并。\nimage-preview 中的图片用于预览和人工填写测试。\n目标 1000 元时，最佳组合为 600.00 + 402.50 = 1002.50 元。\n", encoding="utf-8")
    print("Synthetic samples generated.")


if __name__ == "__main__":
    main()
