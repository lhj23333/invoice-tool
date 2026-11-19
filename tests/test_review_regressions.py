from copy import deepcopy
from email.message import EmailMessage
import io
import json
from pathlib import Path
import shutil
import unittest
import uuid
import zipfile

from pypdf import PdfReader, PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
from pypdfium2 import PdfDocument

from invoice_workbench.domain import UserError
from invoice_workbench.extract import extract, from_text
from invoice_workbench.store import Store, fingerprint
from .helpers import BUYER, TAX, StoreFixture, write_invoice, xml_invoice


def invoice_text(number, code=""):
    return (f"SYNTHETIC SAMPLE - NOT A VALID INVOICE\nInvoice Number: {number}\n"
            f"Invoice Code: {code}\nInvoice Date: 2026-10-01\nBuyer\nName: Example Company\n"
            f"Tax ID: {TAX}\nSeller\nName: Sample Shop\nTotal Amount: 600.00")


def pdf_document(pages):
    writer = PdfWriter()
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"),
                             NameObject("/Subtype"): NameObject("/Type1"),
                             NameObject("/BaseFont"): NameObject("/Helvetica")})
    for text in pages:
        page = writer.add_blank_page(width=612, height=792)
        page[NameObject("/Resources")] = DictionaryObject({
            NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})})
        content = DecodedStreamObject()
        content.set_data(("BT /F1 12 Tf 16 TL 40 760 Td\n" +
                          "\n".join(f"({line}) Tj T*" for line in text.splitlines()) + "\nET").encode("ascii"))
        page[NameObject("/Contents")] = writer._add_object(content)
    stream = io.BytesIO()
    writer.write(stream)
    return stream.getvalue()


def unsupported_encryption_pdf():
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.encrypt("")
    # 构造 pypdf 明确不支持的输入处理器，实际调用 PdfReader 触发异常。
    writer._encrypt_entry[NameObject("/Filter")] = NameObject("/Adobe.PubSec")
    stream = io.BytesIO()
    writer.write(stream)
    return stream.getvalue()


class UsedInvoiceTests(StoreFixture, unittest.TestCase):
    def test_reimbursed_unconfirmed_ocr_owns_its_manually_entered_invoice_number(self):
        sample = Path(__file__).resolve().parents[1] / "samples" / "image-preview" / "manual-review.png"
        shutil.copyfile(sample, self.input / sample.name)
        self.store.import_folder(self.rev(), str(self.input))
        item, = self.store.view()["invoices"]
        self.assertTrue(item["ocr_unconfirmed"])
        invoice_id = item["id"]
        self.store.review(self.rev(), invoice_id, {
            "number": "26000000000000000001", "date": "2026-10-01", "amount": "600.00",
            "buyer_name": BUYER, "buyer_tax_id": TAX,
        })
        self.assertFalse(self.store.invoice(invoice_id)["verification"]["passed"])
        self.store.set_invoice_status(self.rev(), invoice_id, "reimbursed")
        write_invoice(self.input)
        report = self.store.import_folder(self.rev(), str(self.input))
        self.assertEqual((report["imported"], report["merged"]), (0, 1))
        item, = self.store.view()["invoices"]
        self.assertEqual((item["id"], item["reimbursement_status"]), (invoice_id, "reimbursed"))
        self.assertEqual(self.store.recommend("500")["available_cents"], 0)
        with self.assertRaises(UserError):
            self.store.create_batch(self.rev(), [invoice_id], "500", "500", uuid.uuid4().hex)

    def test_same_invoice_format_merges_into_archived_used_record(self):
        item = self.import_one()
        self.store.set_invoice_status(self.rev(), item["id"], "reimbursed")
        with self.store.change(self.rev()):
            item["archived"] = True
        write_invoice(self.input, "copy.xml", extra="<!-- 同一发票的另一个文件 -->")
        result = self.store.import_folder(self.rev(), str(self.input))
        self.assertEqual(result["merged"], 1)
        self.assertEqual(len(self.store.data["invoices"]), 1)
        self.assertEqual(len(self.store.invoice(item["id"])["files"]), 2)
        self.assertEqual(self.store.recommend("1")["available_cents"], 0)

    def test_manual_used_status_blocks_archive_and_field_changes(self):
        item = self.import_one()
        for status in ("submitted", "reimbursed"):
            with self.subTest(status=status):
                self.store.set_invoice_status(self.rev(), item["id"], status)
                before = deepcopy(self.store.data)
                with self.assertRaises(UserError):
                    self.store.archive(self.rev(), item["id"], True)
                with self.assertRaises(UserError):
                    self.store.review(self.rev(), item["id"], {**item, "amount": "600.00", "note": "changed"})
                self.assertEqual(self.store.data, before)
        self.store.set_invoice_status(self.rev(), item["id"], "returned")
        self.assertEqual(self.store.recommend("500")["ids"], [item["id"]])

    def test_archived_used_record_still_protects_same_invoice_after_reopen(self):
        item = self.import_one()
        for status in ("submitted", "reimbursed"):
            with self.subTest(status=status):
                self.store.set_invoice_status(self.rev(), item["id"], status)
                # 复现旧版本允许保存的已使用且移出候选记录。
                with self.store.change(self.rev()):
                    self.store.invoice(item["id"])["archived"] = True
                root = self.store.root
                self.store.close()
                self.store = Store(root)
                write_invoice(self.input, "conflicting.xml", amount="601.00")
                self.store.import_folder(self.rev(), str(self.input))
                self.assertEqual(self.store.recommend("1")["available_cents"], 0)
                conflicting = next(i for i in self.store.data["invoices"] if i["amount_cents"] == 60100)
                self.assertIn(conflicting["id"], self.store.duplicate_ids())
                with self.assertRaises(UserError):
                    self.store.create_batch(self.rev(), [conflicting["id"]], "500", "500", uuid.uuid4().hex)
                self.store.archive(self.rev(), item["id"], False)


class VerificationInputTests(StoreFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.store.set_company(self.rev(), "Example Company", TAX)

    def test_combined_pdf_imported_first_does_not_block_its_first_invoice(self):
        a, b = "26000000000000000001", "26000000000000000002"
        combined = self.input / "combined.pdf"
        combined.write_bytes(pdf_document([invoice_text(a), invoice_text(b)]))
        self.store.import_folder(self.rev(), str(self.input))
        (self.input / "individual.pdf").write_bytes(pdf_document([invoice_text(a)]))
        before = {p.name: fingerprint(p.read_bytes()) for p in self.input.iterdir()}
        result = self.store.import_folder(self.rev(), str(self.input))
        self.assertEqual((result["imported"], result["merged"], result["duplicates"]), (1, 0, 1))
        item = next(i for i in self.store.data["invoices"] if i["files"][0]["name"] == "combined.pdf")
        self.assertFalse(item["verification"]["passed"])
        self.assertTrue(any("多个" in issue for issue in item["verification"]["issues"]))
        standalone = next(i for i in self.store.data["invoices"] if i["files"][0]["name"] == "individual.pdf")
        self.assertEqual(standalone["number"], a)
        self.assertTrue(standalone["verification"]["passed"])
        self.assertEqual(self.store.duplicate_ids(), set())
        self.assertEqual(self.store.recommend("500")["ids"], [standalone["id"]])
        self.store.create_batch(self.rev(), [standalone["id"]], "500", "500", uuid.uuid4().hex)
        with self.assertRaises(UserError):
            self.store.create_batch(self.rev(), [item["id"]], "500", "500", uuid.uuid4().hex)
        self.assertEqual({p.name: fingerprint(p.read_bytes()) for p in self.input.iterdir()}, before)

    def test_same_invoice_repeated_across_pdf_pages_is_valid(self):
        text = invoice_text("26000000000000000001")
        (self.input / "repeated.pdf").write_bytes(pdf_document([text, text]))
        self.store.import_folder(self.rev(), str(self.input))
        item = self.store.data["invoices"][0]
        self.assertTrue(item["verification"]["passed"], item["verification"]["issues"])
        self.assertEqual(self.store.recommend("500")["ids"], [item["id"]])

    def test_pdf_ocr_checks_scan_with_a_text_footer_even_after_a_complete_invoice_page(self):
        vector = pdf_document([invoice_text("26000000000000000002")])
        scanned = io.BytesIO()
        with PdfDocument(vector) as document:
            page = document[0]
            bitmap = page.render(scale=3)
            bitmap.to_pil().convert("RGB").save(scanned, format="PDF", resolution=216)
            bitmap.close()
            page.close()
        first = PdfReader(io.BytesIO(pdf_document([invoice_text("26000000000000000001")])))
        writer = PdfWriter()
        writer.add_page(first.pages[0])
        second = writer.add_page(PdfReader(scanned).pages[0])
        footer = PdfReader(io.BytesIO(pdf_document(["Scanned invoice footer"]))).pages[0]
        second.merge_translated_page(footer, 0, -730)
        self.assertEqual(second.extract_text().strip(), "Scanned invoice footer")
        output = io.BytesIO()
        writer.write(output)
        parsed = extract(output.getvalue(), ".pdf")
        self.assertTrue(parsed.get("multiple_invoice_identifiers"), parsed["raw_text"])

    def test_multiple_numbers_on_one_page_and_legacy_codes_are_rejected(self):
        for pages in ([invoice_text("26000000000000000001") + "\n" + invoice_text("26000000000000000002")],
                      [invoice_text("12345678", "1234567890"), invoice_text("12345678", "0987654321")]):
            with self.subTest(pages=pages):
                parsed = extract(pdf_document(pages), ".pdf")
                self.assertTrue(any("多个" in warning for warning in parsed["warnings"]))

    def test_identifiers_are_checked_beyond_display_text_limit(self):
        text = invoice_text("26000000000000000001") + "\n" + "sample\n" * 6000 + invoice_text("26000000000000000002")
        parsed = from_text(text)
        self.assertEqual(len(parsed["raw_text"]), 30000)
        self.assertTrue(any("多个" in warning for warning in parsed["warnings"]))

    def test_xml_with_multiple_invoice_numbers_cannot_be_selected(self):
        write_invoice(self.input, extra="<InvoiceNumber>26000000000000000002</InvoiceNumber>")
        self.store.import_folder(self.rev(), str(self.input))
        item = self.store.data["invoices"][0]
        self.assertFalse(item["verification"]["passed"])
        self.assertTrue(any("多个" in issue for issue in item["verification"]["issues"]))

    def test_combined_xml_imported_later_does_not_change_verified_invoice(self):
        standalone = write_invoice(self.input, buyer="Example Company")
        self.store.import_folder(self.rev(), str(self.input))
        original = deepcopy(self.store.data["invoices"][0])
        combined = write_invoice(self.input, "combined.xml", buyer="Example Company",
                                 extra="<InvoiceNumber>26000000000000000002</InvoiceNumber>")
        before = {path: fingerprint(path.read_bytes()) for path in (standalone, combined)}
        result = self.store.import_folder(self.rev(), str(self.input))
        self.assertEqual((result["imported"], result["merged"], result["duplicates"]), (1, 0, 1))
        root = self.store.root
        self.store.close()
        self.store = Store(root)
        repeat = self.store.import_folder(self.rev(), str(self.input))
        self.assertEqual((repeat["imported"], repeat["merged"], repeat["duplicates"]), (0, 0, 2))
        self.assertEqual(self.store.invoice(original["id"]), original)
        other = next(i for i in self.store.data["invoices"] if i["id"] != original["id"])
        self.assertEqual(len(other["files"]), 1)
        self.assertEqual(self.store.original(other["files"][0]).read_bytes(), combined.read_bytes())
        self.store.review(self.rev(), other["id"], {**other, "amount": "600.00", "note": "含多张发票"})
        saved = self.store.invoice(other["id"])
        self.assertEqual(saved["note"], "含多张发票")
        self.assertFalse(saved["verification"]["passed"])
        self.assertEqual(self.store.duplicate_ids(), set())
        self.assertEqual(self.store.recommend("500")["ids"], [original["id"]])
        self.assertEqual({path: fingerprint(path.read_bytes()) for path in before}, before)

    def test_legacy_single_file_record_with_multiple_invoice_codes_stays_separate(self):
        legacy_code = "<InvoiceCode>1234567890</InvoiceCode>"
        write_invoice(self.input, "combined.xml", number="12345678", buyer="Example Company",
                      extra=legacy_code + "<InvoiceCode>0987654321</InvoiceCode>")
        self.store.import_folder(self.rev(), str(self.input))
        with self.store.change(self.rev()):
            self.store.data["invoices"][0].pop("multiple_invoice_identifiers")
        root = self.store.root
        self.store.close()
        self.store = Store(root)
        write_invoice(self.input, number="12345678", buyer="Example Company", extra=legacy_code)
        result = self.store.import_folder(self.rev(), str(self.input))
        self.assertEqual((result["imported"], result["merged"], result["duplicates"]), (1, 0, 1))
        ambiguous, standalone = self.store.data["invoices"]
        self.assertFalse(ambiguous["verification"]["passed"])
        self.assertTrue(standalone["verification"]["passed"])
        self.assertEqual(self.store.duplicate_ids(), set())
        self.assertEqual(self.store.recommend("500")["ids"], [standalone["id"]])

    def test_unsupported_pdf_is_reported_without_discarding_other_imports(self):
        payload = unsupported_encryption_pdf()
        with self.assertRaises(NotImplementedError):
            PdfReader(io.BytesIO(payload))
        (self.input / "a-valid.xml").write_bytes(xml_invoice())
        (self.input / "b-unsupported.pdf").write_bytes(payload)
        (self.input / "c-valid.xml").write_bytes(xml_invoice(number="26000000000000000002"))
        before = {p.name: fingerprint(p.read_bytes()) for p in self.input.iterdir()}
        result = self.store.import_folder(self.rev(), str(self.input))
        self.assertEqual(result["imported"], 2)
        self.assertEqual(len(result["errors"]), 1)
        self.assertEqual(result["errors"][0]["file"], "b-unsupported.pdf")
        self.assertIn("PDF", result["errors"][0]["error"])
        root = self.store.root
        self.store.close()
        self.store = Store(root)
        self.assertEqual(len(self.store.data["invoices"]), 2)
        self.assertEqual({p.name: fingerprint(p.read_bytes()) for p in self.input.iterdir()}, before)

    def test_unsupported_pdf_attachment_does_not_block_valid_attachments(self):
        message = EmailMessage()
        for name, data, subtype in (("a.xml", xml_invoice(), "xml"),
                                    ("b.pdf", unsupported_encryption_pdf(), "pdf"),
                                    ("c.xml", xml_invoice(number="26000000000000000002"), "xml")):
            message.add_attachment(data, maintype="application", subtype=subtype, filename=name)
        (self.input / "invoices.eml").write_bytes(message.as_bytes())
        result = self.store.import_folder(self.rev(), str(self.input))
        self.assertEqual(result["imported"], 2)
        self.assertEqual(len(result["errors"]), 1)
        self.assertIn("b.pdf", result["errors"][0]["file"])


class NormalizedTaxTests(StoreFixture, unittest.TestCase):
    def test_verified_tax_id_is_normalized_saved_and_exported(self):
        item = self.import_one(tax=" 91440300 ma0000000x ")
        self.assertTrue(item["verification"]["passed"])
        self.assertEqual(item["buyer_tax_id"], TAX)
        self.assertTrue(self.store.view()["invoices"][0]["company_match"])
        self.assertEqual(self.store.recommend("500")["ids"], [item["id"]])
        batch = self.store.create_batch(self.rev(), [item["id"]], "500", "500", uuid.uuid4().hex)
        root = self.store.root
        self.store.close()
        self.store = Store(root)
        self.assertEqual(self.store.invoice(item["id"])["buyer_tax_id"], TAX)
        self.assertEqual(self.store.batch(batch)["items"][0]["buyer_tax_id"], TAX)
        with zipfile.ZipFile(self.store.export(batch)) as archive:
            self.assertIn(TAX, archive.read("明细.csv").decode("utf-8-sig"))


class LegacyMigrationAndExportTests(StoreFixture, unittest.TestCase):
    def test_cancelled_legacy_batch_can_be_selected_again_and_reopened(self):
        cancelled = self.make_batch()
        state = deepcopy(self.store.data)
        state["schema"] = 1
        state["batches"][0]["status"] = "cancelled"
        for item in state["invoices"]:
            for field in ("verification", "reimbursement_status", "active_batch_id"):
                item.pop(field)
        root = self.store.root
        self.store.close()
        (root / "invoices.json").write_text(json.dumps(state), encoding="utf-8")
        self.store = Store(root)
        item = self.store.data["invoices"][0]
        self.assertEqual(item["reimbursement_status"], "available")
        self.assertIsNone(item["active_batch_id"])
        ids = self.store.recommend("500")["ids"]
        self.assertEqual(ids, [item["id"]])
        active = self.store.create_batch(self.rev(), ids, "500", "500", uuid.uuid4().hex)
        self.store.close()
        self.store = Store(root)
        self.assertEqual(self.store.occupied(), {item["id"]: active})
        self.assertEqual(self.store.invoice(item["id"])["reimbursement_status"], "reserved")
        self.assertEqual(self.store.batch(cancelled)["status"], "cancelled")

    def test_reexport_while_old_zip_is_open_preserves_both_archives(self):
        batch = self.make_batch()
        first = self.store.export(batch)
        before = first.read_bytes()
        revision = self.rev()
        with first.open("rb") as opened:
            second = self.store.export(batch)
            self.assertNotEqual(first, second)
            self.assertEqual(opened.read(), before)
        self.assertEqual(first.read_bytes(), before)
        self.assertEqual(self.rev(), revision)
        with zipfile.ZipFile(second) as archive:
            self.assertIsNone(archive.testzip())
            self.assertIn("明细.csv", archive.namelist())
            self.assertEqual(archive.read("invoice.xml"), (self.input / "invoice.xml").read_bytes())
        self.assertFalse(list((self.store.root / "exports").glob("*.tmp")))
