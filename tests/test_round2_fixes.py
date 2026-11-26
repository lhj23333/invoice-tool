from email import policy
from email.message import EmailMessage
from io import BytesIO, StringIO
from pathlib import Path
import csv
import unittest
import uuid
from zipfile import ZipFile

from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from invoice_workbench.extract import MAX_BYTES, extract
from invoice_workbench.mail import _mail_links
from invoice_workbench.store import fingerprint
from .helpers import StoreFixture, write_invoice


SAMPLES = Path(__file__).resolve().parents[1] / "samples"


def text_pdf(text):
    writer = PdfWriter()
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"),
                             NameObject("/Subtype"): NameObject("/Type1"),
                             NameObject("/BaseFont"): NameObject("/Helvetica")})
    page = writer.add_blank_page(width=612, height=792)
    page[NameObject("/Resources")] = DictionaryObject({
        NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})})
    content = DecodedStreamObject()
    content.set_data(("BT /F1 12 Tf 16 TL 40 760 Td\n" +
                      "\n".join(f"({line}) Tj T*" for line in text.splitlines()) + "\nET").encode("ascii"))
    page[NameObject("/Contents")] = writer._add_object(content)
    stream = BytesIO()
    writer.write(stream)
    return stream.getvalue()


def sample_eml(subject, attachments, body="材料见附件。"):
    message = EmailMessage(policy=policy.SMTP)
    message["Subject"] = subject
    message.set_content(body)
    for name, data, disposition in attachments:
        suffix = Path(name).suffix.lstrip(".").lower()
        maintype = "image" if suffix in {"png", "jpg", "jpeg"} else "application"
        message.add_attachment(data, maintype=maintype, subtype=suffix, filename=name, disposition=disposition)
    return message.as_bytes()


def image_bytes():
    return b"\x89PNG\r\n\x1a\n" + b"0" * 64


def invoice_pdf(title):
    """与 sample-01.pdf 同票字段的合成 PDF；行程单版本去掉发票标题行。"""
    return text_pdf("\n".join([title, "Invoice Number: 26000000000000000001", "Invoice Date: 2026-10-01",
                               "Buyer", "Name: Example Company", "Tax ID: 91440300MA0000000X",
                               "Total Amount: 600.00"]))


class Round2FixTests(StoreFixture, unittest.TestCase):
    def eml(self, name, data):
        path = self.input / name
        path.write_bytes(data)
        return path

    def test_eml_container_over_20mb_still_imports_small_invoice(self):
        data = sample_eml("项目资料归档", [
            ("项目资料归档.zip", b"\0" * (21 * 1024 * 1024), "attachment"),
            ("invoice.pdf", (SAMPLES / "sample-01.pdf").read_bytes(), "attachment"),
        ])
        path = self.eml("archive.eml", data)
        self.assertGreater(path.stat().st_size, MAX_BYTES)
        report = self.store.import_folder(self.rev(), str(self.input))
        self.assertEqual((report["scanned"], report["imported"], len(report["errors"])), (1, 1, 0))
        self.assertEqual([row["result"] for row in report["items"]], ["imported"])
        item, = self.store.view()["invoices"]
        self.assertEqual([file["name"] for file in item["files"]], ["invoice.pdf"])

    def test_local_eml_skips_marketing_inline_images(self):
        data = sample_eml("双十一云服务优惠，可开具发票", [
            ("logo.png", image_bytes(), "inline"),
            ("banner.png", image_bytes(), "inline"),
            ("pixel.png", image_bytes(), "inline"),
        ])
        self.eml("marketing.eml", data)
        report = self.store.import_folder(self.rev(), str(self.input))
        self.assertEqual((report["imported"], report["duplicates"], len(report["errors"])), (0, 0, 0))
        self.assertEqual(len(report["notes"]), 1)
        self.assertIn("未发现支持的发票附件", report["notes"][0])
        self.assertEqual(self.store.view()["invoices"], [])

    def test_local_eml_ignores_signature_image_next_to_pdf(self):
        data = sample_eml("报销发票（附电子签名）", [
            ("signature.png", image_bytes(), "inline"),
            ("invoice-700.pdf", (SAMPLES / "sample-01.pdf").read_bytes(), "attachment"),
        ])
        self.eml("invoice.eml", data)
        report = self.store.import_folder(self.rev(), str(self.input))
        self.assertEqual((report["imported"], len(report["errors"]), report["notes"]), (1, 0, []))
        item, = self.store.view()["invoices"]
        self.assertEqual([file["name"] for file in item["files"]], ["invoice-700.pdf"])

    def test_shipping_notice_order_link_is_not_downloaded(self):
        subject = "您的订单已发货，电子发票将在签收后开具"
        message = EmailMessage()
        message["Subject"] = subject
        message.set_content("<p>您的订单已出库。</p>"
                            "<p>电子发票将在签收后开具，可在订单详情页下载。</p>"
                            '<p><a href="https://example.com/orders">查看订单</a>'
                            '<a href="https://example.com/logistics">查看物流</a></p>', subtype="html")
        links = {}
        _mail_links(message, subject, links)
        self.assertEqual(links, {})
        message.set_content("<p>电子发票已开具，请下载。</p>"
                            '<p><a href="https://example.com/invoice-700.pdf">下载发票</a></p>', subtype="html")
        _mail_links(message, subject, links)
        self.assertEqual(links, {"https://example.com/invoice-700.pdf": "下载发票"})

    def staged_rows(self, attachments, subject="电子发票"):
        rows = []
        for name, data in attachments:
            path = self.input / f"staged-{fingerprint(data)[:12]}.pdf"
            path.write_bytes(data)
            rows.append({"file": name, "name": name, "subject": subject, "message_id": "message-1",
                         "payload": path, "mail_digest": fingerprint(data),
                         "mail_evidence": extract(data, ".pdf"), "mail_hint": True})
        return rows

    def test_mail_without_invoice_title_duplicates_verified_original(self):
        report = self.store.import_mail(self.rev(), "收件箱", 1, self.staged_rows([
            ("invoice.pdf", invoice_pdf("INVOICE")), ("itinerary.pdf", invoice_pdf("TRIP ITINERARY"))]))
        self.assertEqual([row["result"] for row in report["items"]], ["imported", "duplicates"])
        self.assertEqual(len(self.store.view()["invoices"]), 1)

    def test_local_eml_trip_itinerary_matches_mail_conclusion(self):
        data = sample_eml("行程报销材料", [
            ("invoice.pdf", invoice_pdf("INVOICE"), "attachment"),
            ("itinerary.pdf", invoice_pdf("TRIP ITINERARY"), "attachment"),
        ])
        self.eml("materials.eml", data)
        report = self.store.import_folder(self.rev(), str(self.input))
        self.assertEqual([row["result"] for row in report["items"]], ["imported", "duplicates"])
        self.assertFalse(report["errors"])
        self.assertEqual(len(self.store.view()["invoices"]), 1)

    def test_duplicate_evidence_with_warnings_is_not_silently_accepted(self):
        self.store.import_files(self.rev(), [SAMPLES / "sample-01.pdf"])
        variant = (SAMPLES / "sample-01.pdf").read_bytes() + b"\n"
        path = self.input / "variant.pdf"
        path.write_bytes(variant)
        evidence = {**extract(variant, ".pdf"), "ocr_unconfirmed": True,
                    "warnings": ["价税合计（小写）识别可信度不足，请核对原件。"]}
        row = {"file": "variant.pdf", "name": "variant.pdf", "subject": "电子发票", "message_id": "message-2",
               "payload": path, "mail_digest": fingerprint(variant), "mail_evidence": evidence, "mail_hint": True}
        report = self.store.import_mail(self.rev(), "收件箱", 1, [row])
        self.assertEqual([entry["result"] for entry in report["items"]], ["review"])
        self.assertEqual(len(self.store.view()["invoices"]), 1)
        review, = self.store.data["mail_reviews"].values()
        self.assertIn("同票新证据存在核验异常", review["detail"])

    def test_export_csv_is_sorted_by_date_and_number(self):
        write_invoice(self.input, "late.xml", number="26000000000000000009", date="2025-11-15")
        write_invoice(self.input, "early.xml", number="26000000000000000003", date="2025-11-03")
        self.store.import_folder(self.rev(), str(self.input))
        by_number = {item["number"]: item["id"] for item in self.store.view()["invoices"]}
        ids = [by_number["26000000000000000009"], by_number["26000000000000000003"]]
        batch = self.store.create_batch(self.rev(), ids, "600", "600", uuid.uuid4().hex)
        with ZipFile(self.store.export(batch)) as archive:
            rows = list(csv.DictReader(StringIO(archive.read("明细.csv").decode("utf-8-sig"))))
        self.assertEqual([row["开票日期"] for row in rows], ["2025-11-03", "2025-11-15"])
        self.assertEqual([row["发票号码"] for row in rows], ["26000000000000000003", "26000000000000000009"])
        self.assertEqual([row["序号"] for row in rows], ["1", "2"])


if __name__ == "__main__":
    unittest.main()
