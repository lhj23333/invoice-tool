from datetime import datetime
from email import policy
from email.message import EmailMessage
from io import BytesIO
import os
from pathlib import Path
import shutil
import unittest
from uuid import uuid4
from zipfile import ZipFile

from imapclient.response_parser import parse_fetch_response, parse_response

from invoice_workbench.domain import UserError
from invoice_workbench.extract import MAX_BYTES
from invoice_workbench.mail import (
    IMAPMailbox, MAX_MESSAGE_BYTES, _collect_message, _folder_options, _message_body, _redirect_url,
    date_bounds, domain_list, download_invoice, mailbox_profile,
)
from invoice_workbench.outlook_auth import CLIENT_ID_ENV, CLIENT_ID_FILE, OutlookOAuth, configured_client_id
from invoice_workbench.store import Store
from invoice_workbench.workflow import remove_duplicate_files
from .helpers import StoreFixture, write_invoice, xml_invoice


SAMPLES = Path(__file__).resolve().parents[1] / "samples"


def incomplete_ofd():
    with BytesIO() as package:
        with ZipFile(package, "w") as archive:
            archive.writestr("说明.txt", "缺少 OFD.xml 的测试附件")
        return package.getvalue()


class CollectionTests(StoreFixture, unittest.TestCase):
    def test_complete_directory_report_reopens_with_every_file_outcome(self):
        sample = Path(__file__).resolve().parents[1] / "samples" / "sample-01.xml"
        for index in range(10):
            shutil.copy2(sample, self.input / f"invoice-{index}.xml")
        result = self.store.import_folder(self.rev(), str(self.input))
        self.assertEqual((result["imported"], result["duplicates"]), (1, 9))
        self.assertEqual(len(result["items"]), 10)
        self.assertEqual(len({row["invoice_id"] for row in result["items"]}), 1)
        workspace = self.store.root
        self.store.close()
        self.store = Store(workspace)
        self.assertEqual(self.store.view()["import_runs"], [result])
        self.assertNotIn("import_runs", self.store.view(include_import_runs=False))
        self.assertEqual(self.store.import_history(), [result])

    def test_download_blocklist_covers_subdomains_before_any_connection(self):
        blocked = domain_list(" GITHUBUSERCONTENT.com.，githubusercontent.com ")
        self.assertEqual(blocked, ["githubusercontent.com"])
        with self.assertRaisesRegex(UserError, "黑名单"):
            download_invoice("https://raw.githubusercontent.com/ofdrw/ofdrw/master/README.md", blocked)
        with self.assertRaisesRegex(UserError, "HTTPS"):
            download_invoice("http://raw.githubusercontent.com/ofdrw/ofdrw/master/README.md", [])

    def test_malformed_download_target_keeps_other_attachments(self):
        bad_url = "https://[invalid"
        with self.assertRaisesRegex(UserError, "下载跳转地址无效"):
            _redirect_url("https://invoice.example.com/start", bad_url)
        message = EmailMessage()
        message.set_content(f'<a href="{bad_url}">下载发票</a>', subtype="html")
        message.add_attachment((SAMPLES / "sample-01.pdf").read_bytes(), maintype="application", subtype="pdf",
                               filename="invoice.pdf")
        rows = _collect_message(message.as_bytes(), {"subject": "电子发票", "message_id": "sample"}, (), self.input, 1)
        report = self.store.import_mail(self.rev(), "收件箱", 1, rows)
        self.assertEqual(report["imported"], 1)
        error, = report["errors"]
        self.assertEqual(error["file"], "下载发票")

    def test_imap_folders_ignore_noselect_flags_from_server_response(self):
        data = [br'(\NoSelect \HasChildren) "/" "Folders"', br'(\HasNoChildren) "/" "Folders/Invoices"',
                br'(\HasNoChildren) "/" "INBOX"']
        flags_and_names = parse_response(data)
        listing = [(flags, delim, name.decode("ascii"))
                   for flags, delim, name in zip(flags_and_names[::3], flags_and_names[1::3], flags_and_names[2::3])]
        self.assertEqual(_folder_options(listing), [("INBOX", "收件箱"), ("Folders/Invoices", "Folders/Invoices")])

    def test_mail_scope_converts_inclusive_local_dates_to_utc_boundaries(self):
        start, end = map(datetime.fromisoformat, date_bounds("2026-11-16", "2026-11-18"))
        self.assertEqual(start.astimezone().strftime("%Y-%m-%d %H:%M"), "2026-11-16 00:00")
        self.assertEqual(end.astimezone().strftime("%Y-%m-%d %H:%M"), "2026-11-19 00:00")
        with self.assertRaises(UserError):
            date_bounds("2026-11-18", "2026-11-16")

    def test_imap_body_uses_received_bytes_when_reported_size_differs(self):
        sample = Path(__file__).resolve().parents[1] / "samples" / "sample-01.xml"
        message = EmailMessage(policy=policy.SMTP)
        message["Subject"] = "电子发票"
        message.set_content("发票见附件。")
        message.add_attachment(sample.read_bytes(), maintype="application", subtype="xml", filename=sample.name)
        raw = message.as_bytes()
        for reported_size in (len(raw) - 1, len(raw) + 1):
            with self.subTest(reported_size=reported_size):
                prefix = f"1 (UID 7 RFC822.SIZE {reported_size} BODY[] {{{len(raw)}}}".encode("ascii")
                response = parse_fetch_response([(prefix, raw), b")"])[7]
                self.assertEqual(_message_body(response), (raw, ""))

    def test_imap_body_rejects_missing_and_over_limit_payloads(self):
        for response in ({}, {b"BODY[]": None}, {b"BODY[]": b""}):
            with self.subTest(response=response):
                raw, error = _message_body(response)
                self.assertIsNone(raw)
                self.assertIn("未返回邮件正文", error)
        raw, error = _message_body({b"BODY[]": b" " * (MAX_MESSAGE_BYTES + 1)})
        self.assertIsNone(raw)
        self.assertIn("超过 80 MB", error)

    def collect_sample_mail(self, sequence, attachments, body="发票见附件。", blocked=()):
        message = EmailMessage(policy=policy.SMTP)
        message["Subject"] = "电子发票"
        message.set_content(body)
        for name, data in attachments:
            message.add_attachment(data, maintype="application", subtype=Path(name).suffix[1:].lower(), filename=name)
        return _collect_message(message.as_bytes(), {"subject": str(message["Subject"]), "message_id": str(sequence)},
                                blocked, self.input, sequence)

    def test_mail_prefers_pdf_to_ofd_and_still_merges_xml(self):
        rows = self.collect_sample_mail(1, [("sample-01.OFD", incomplete_ofd()),
                                           ("sample-01.PDF", (SAMPLES / "sample-01.pdf").read_bytes()),
                                           ("sample-01.xml", (SAMPLES / "sample-01.xml").read_bytes())])
        report = self.store.import_mail(self.rev(), "收件箱", 1, rows)
        self.assertEqual((report["imported"], report["merged"], report["duplicates"], report["errors"]), (1, 1, 0, []))
        self.assertEqual([row["result"] for row in report["items"]], ["note", "imported", "merged"])
        self.assertIn("已跳过 OFD", report["items"][0]["detail"])
        item, = self.store.view()["invoices"]
        self.assertTrue(item["verification"]["passed"])
        self.assertEqual([file["name"] for file in item["files"]], ["sample-01.PDF"])
        self.assertEqual(item["seller_name"], "合成样本商户")
        self.assertTrue(report["items"][2]["xml_analysis"])
        self.assertFalse(list((self.store.root / "objects").glob("*.xml")))
        self.assertFalse(list((self.store.root / "objects").glob("*.ofd")))
        repeat = self.store.import_mail(self.rev(), "收件箱", 1, rows)
        self.assertEqual((repeat["imported"], repeat["duplicates"]), (0, 2))
        self.assertEqual(self.store.view()["import_runs"][-1], repeat)

    def test_xml_analysis_waits_for_original_and_merges_after_reopen(self):
        rows = self.collect_sample_mail(1, [("sample-01.xml", (SAMPLES / "sample-01.xml").read_bytes())])
        report = self.store.import_mail(self.rev(), "收件箱", 1, rows)
        invoice_id = report["items"][0]["invoice_id"]
        item = self.store.invoice(invoice_id)
        self.assertEqual(item["files"], [])
        self.assertFalse(item["verification"]["passed"])
        self.assertNotIn("raw_text", item)
        self.assertFalse(list((self.store.root / "objects").iterdir()))
        root = self.store.root
        self.store.close()
        self.store = Store(root)
        pdf = self.collect_sample_mail(2, [("sample-01.pdf", (SAMPLES / "sample-01.pdf").read_bytes())])
        result = self.store.import_mail(self.rev(), "收件箱", 1, pdf)
        self.assertEqual((result["imported"], result["merged"]), (0, 1))
        self.assertTrue(self.store.invoice(invoice_id)["verification"]["passed"])
        batch = self.store.create_batch(self.rev(), [invoice_id], "500", "500", uuid4().hex)
        with ZipFile(self.store.export(batch)) as archive:
            self.assertEqual(set(archive.namelist()), {"明细.csv", "sample-01.pdf"})

    def test_xml_amount_conflict_still_blocks_selection(self):
        rows = self.collect_sample_mail(1, [("sample-01.pdf", (SAMPLES / "sample-01.pdf").read_bytes()),
                                           ("conflicting.xml", xml_invoice(amount="650.00"))])
        report = self.store.import_mail(self.rev(), "收件箱", 1, rows)
        ids = {row["invoice_id"] for row in report["items"]}
        self.assertEqual(self.store.duplicate_ids(), ids)
        self.assertTrue(all("同票字段冲突" in row["detail"] for row in report["items"]))
        self.assertFalse(list((self.store.root / "objects").glob("*.xml")))

    def test_failed_mail_with_xml_does_not_save_an_eml_copy(self):
        library = self.root / "library originals"
        library.mkdir()
        self.store.set_library(self.rev(), library)
        rows = self.collect_sample_mail(1, [("unsupported.pdf", b"not a PDF"), ("invoice.xml", xml_invoice())])
        report = self.store.import_mail(self.rev(), "收件箱", 1, rows)
        self.assertEqual(len(report["errors"]), 1)
        self.assertNotIn("mail_object", report["items"][0])
        self.assertIn("按主题", report["items"][0]["detail"])
        self.assertFalse(list(library.iterdir()))
        self.assertFalse(list((self.store.root / "objects").iterdir()))

    def test_ofd_only_mail_is_processed_despite_pdf_in_another_mail(self):
        rows = self.collect_sample_mail(1, [("sample-01.pdf", (SAMPLES / "sample-01.pdf").read_bytes())])
        rows.extend(self.collect_sample_mail(2, [("sample-02.ofd", incomplete_ofd())],
                                             "发票.pdf https://invoice.example.com/sample-02.pdf",
                                             blocked=["invoice.example.com"]))
        report = self.store.import_mail(self.rev(), "收件箱", 2, rows)
        self.assertEqual((report["imported"], report["merged"], len(report["errors"])), (2, 0, 1))
        self.assertFalse(report["notes"])
        self.assertIn("黑名单", report["errors"][0]["error"])
        self.assertIn("OFD 缺少文档入口", report["items"][1]["detail"])

    def test_mail_store_prefers_pdf_for_same_message_across_staged_files(self):
        ofd, = self.collect_sample_mail(1, [("sample-01.ofd", incomplete_ofd())])
        pdf, = self.collect_sample_mail(2, [("sample-01.pdf", (SAMPLES / "sample-01.pdf").read_bytes())])
        ofd["message_id"] = pdf["message_id"]
        self.assertNotEqual(ofd["original_message"], pdf["original_message"])
        report = self.store.import_mail(self.rev(), "收件箱", 2, [ofd, pdf])
        self.assertEqual([row["result"] for row in report["items"]], ["note", "imported"])
        self.assertEqual(len(self.store.view()["invoices"]), 1)
        self.assertFalse(list((self.store.root / "objects").glob("*.ofd")))

    def test_mail_store_keeps_previously_verified_pdf_for_the_same_message(self):
        pdf = self.collect_sample_mail(1, [("sample-01.pdf", (SAMPLES / "sample-01.pdf").read_bytes())])
        first = self.store.import_mail(self.rev(), "收件箱", 1, pdf)
        self.assertEqual(first["imported"], 1)
        ofd = self.collect_sample_mail(1, [("sample-01.ofd", incomplete_ofd())])
        report = self.store.import_mail(self.rev(), "收件箱", 1, ofd)
        self.assertEqual((report["imported"], report["errors"]), (0, []))
        self.assertEqual([row["result"] for row in report["items"]], ["note"])
        self.assertEqual(len(self.store.view()["invoices"]), 1)
        self.assertFalse(list((self.store.root / "objects").glob("*.ofd")))

    def test_mail_processes_ofd_when_present_pdf_is_damaged_or_over_limit(self):
        ofd = incomplete_ofd()
        for sequence, pdf in enumerate((b"%PDF-truncated", b"%PDF-" + b" " * MAX_BYTES), 1):
            with self.subTest(sequence=sequence):
                rows = self.collect_sample_mail(sequence, [("invoice.pdf", pdf), ("invoice.ofd", ofd)])
                report = self.store.import_mail(self.rev(), "收件箱", 1, rows)
                self.assertEqual([row["result"] for row in report["items"]], ["error", "imported" if sequence == 1 else "duplicates"])
                self.assertIn("OFD 缺少文档入口", report["items"][1]["detail"])

    def test_mail_does_not_skip_ofd_when_the_historical_pdf_is_missing(self):
        content = (SAMPLES / "sample-01.pdf").read_bytes()
        pdf = self.collect_sample_mail(1, [("invoice.pdf", content)])
        report = self.store.import_mail(self.rev(), "收件箱", 1, pdf)
        item = self.store.invoice(report["items"][0]["invoice_id"])
        original = self.store.original(item["files"][0])
        variant = self.collect_sample_mail(2, [("another.pdf", content + b"\n")])
        self.store.import_mail(self.rev(), "收件箱", 1, variant)
        original.unlink()
        rows = self.collect_sample_mail(1, [("invoice.ofd", incomplete_ofd())])
        report = self.store.import_mail(self.rev(), "收件箱", 1, rows)
        self.assertEqual(report["items"][0]["result"], "imported")
        self.assertIn("OFD 缺少文档入口", report["items"][0]["detail"])

    def test_local_eml_prefers_pdf_before_ofd_recognition(self):
        self.collect_sample_mail(1, [("sample-01.ofd", incomplete_ofd()),
                                     ("sample-01.pdf", (SAMPLES / "sample-01.pdf").read_bytes())])
        report = self.store.import_folder(self.rev(), str(self.input))
        self.assertEqual((report["scanned"], report["imported"], report["errors"]), (1, 1, []))
        self.assertEqual([row["result"] for row in report["items"]], ["note", "imported"])
        self.assertIn("已跳过 OFD", report["items"][0]["detail"])
        item, = self.store.view()["invoices"]
        self.assertTrue(item["verification"]["passed"])
        self.assertFalse(list((self.store.root / "objects").glob("*.ofd")))

    def test_local_eml_processes_ofd_when_pdf_import_fails(self):
        self.collect_sample_mail(1, [("invoice.ofd", incomplete_ofd()), ("invoice.pdf", b"not a PDF")])
        report = self.store.import_folder(self.rev(), str(self.input))
        self.assertEqual([row["result"] for row in report["items"]], ["imported", "error"])
        self.assertIn("OFD 缺少文档入口", report["items"][0]["detail"])

    def test_configured_library_saves_mail_with_readable_names_and_survives_reset(self):
        library = self.root / "invoice library"
        library.mkdir()
        existing = write_invoice(library, "发票.xml", number="26000000000000000002").read_bytes()
        content = (SAMPLES / "sample-01.pdf").read_bytes()
        occupied_pdf = content + b"\r\n"
        self.store.set_library(self.rev(), library)
        self.store.import_folder(self.rev(), str(library), recursive=True)
        (library / "发票.pdf").write_bytes(occupied_pdf)
        rows = self.collect_sample_mail(1, [("发票.pdf", content)])
        report = self.store.import_mail(self.rev(), "收件箱", 1, rows)
        self.assertEqual((report["imported"], report["errors"]), (1, []))
        self.assertEqual((library / "发票.xml").read_bytes(), existing)
        self.assertEqual((library / "发票_2.pdf").read_bytes(), content)
        self.assertFalse(list((self.store.root / "objects").iterdir()))
        batch = self.store.create_batch(self.rev(), [self.store.view()["invoices"][0]["id"]], "500", "500", uuid4().hex)
        export = self.store.export(batch)
        self.store.reset(self.rev())
        self.assertEqual(self.store.library, library)
        self.assertEqual(self.store.view()["invoices"], [])
        self.assertEqual({p.name: p.read_bytes() for p in library.iterdir()},
                         {"发票.xml": existing, "发票.pdf": occupied_pdf, "发票_2.pdf": content})
        self.assertTrue(export.is_file())
        root = self.store.root
        self.store.close()
        self.store = Store(root)
        self.assertEqual(self.store.library, library)

    def test_configuring_library_reuses_legacy_local_path_and_migrates_mail_and_batch(self):
        library = self.root / "my originals"
        library.mkdir()
        original = library / "nested" / "sample-01.xml"
        original.parent.mkdir()
        shutil.copyfile(SAMPLES / "sample-01.xml", original)
        self.store.import_folder(self.rev(), str(library), recursive=True)
        rows = self.collect_sample_mail(1, [("sample-01.pdf", (SAMPLES / "sample-01.pdf").read_bytes())])
        self.store.import_mail(self.rev(), "收件箱", 1, rows)
        item, = self.store.view()["invoices"]
        batch = self.store.create_batch(self.rev(), [item["id"]], "500", "500", uuid4().hex)
        self.store.set_library(self.rev(), library)
        item, = self.store.view()["invoices"]
        originals = {self.store.original(file) for file in item["files"]}
        self.assertEqual(originals, {original, library / "sample-01.pdf"})
        self.assertEqual({self.store.original(file) for file in self.store.batch(batch)["items"][0]["files"]}, originals)
        self.assertTrue(self.store.export(batch).is_file())

    def test_cleanup_retargets_index_and_batch_before_removing_local_duplicate(self):
        self.store.set_library(self.rev(), self.input)
        old = write_invoice(self.input, "z.xml")
        self.store.import_folder(self.rev(), str(self.input), recursive=True)
        invoice, = self.store.view()["invoices"]
        batch = self.store.create_batch(self.rev(), [invoice["id"]], "500", "500", uuid4().hex)
        keep = self.input / "a.xml"
        shutil.copyfile(old, keep)
        report = self.store.import_folder(self.rev(), str(self.input), recursive=True)
        self.assertEqual(remove_duplicate_files(self.store, self.rev(), report), 1)
        self.assertFalse(old.exists())
        self.assertEqual(self.store.original(self.store.invoice(invoice["id"])["files"][0]), keep)
        self.assertEqual(self.store.original(self.store.batch(batch)["items"][0]["files"][0]), keep)

    def test_cleanup_keeps_usable_pdf_extension_and_preserves_batch(self):
        self.store.set_library(self.rev(), self.input)
        original = self.input / "z.pdf"
        content = (SAMPLES / "sample-01.pdf").read_bytes()
        original.write_bytes(content)
        self.store.import_folder(self.rev(), str(self.input))
        invoice, = self.store.view()["invoices"]
        batch = self.store.create_batch(self.rev(), [invoice["id"]], "500", "500", uuid4().hex)
        alias = self.input / "a.xml"
        alias.write_bytes(content)
        report = self.store.import_folder(self.rev(), str(self.input))
        self.assertEqual(remove_duplicate_files(self.store, self.rev(), report), 1)
        self.assertFalse(alias.exists())
        self.assertEqual(original.read_bytes(), content)
        self.assertEqual(self.store.original(self.store.batch(batch)["items"][0]["files"][0]), original)
        self.assertTrue(self.store.export(batch).is_file())
        self.store.set_invoice_status(self.rev(), invoice["id"], "returned")
        self.assertTrue(self.store.create_batch(self.rev(), [invoice["id"]], "500", "500", uuid4().hex))

    def test_moved_library_reuses_nested_invoice_and_history_paths(self):
        self.store.set_library(self.rev(), self.input)
        original = write_invoice(self.input / "nested")
        content = original.read_bytes()
        self.store.import_folder(self.rev(), str(self.input), recursive=True)
        invoice, = self.store.view()["invoices"]
        batch = self.store.create_batch(self.rev(), [invoice["id"]], "500", "500", uuid4().hex)
        moved = self.root / "moved"
        self.input.rename(moved)
        self.store.set_library(self.rev(), moved)
        expected = moved / "nested" / original.name
        self.assertEqual(self.store.original(self.store.invoice(invoice["id"])["files"][0]), expected)
        self.assertEqual(self.store.original(self.store.batch(batch)["items"][0]["files"][0]), expected)
        self.assertEqual(expected.read_bytes(), content)
        self.assertEqual(list(moved.rglob("*.xml")), [expected])
        self.assertFalse(self.input.exists())

    def test_rescan_reattaches_renamed_original_to_invoice_and_batch(self):
        self.store.set_library(self.rev(), self.input)
        original = write_invoice(self.input)
        report = self.store.import_folder(self.rev(), str(self.input))
        invoice_id = report["items"][0]["invoice_id"]
        batch = self.store.create_batch(self.rev(), [invoice_id], "500", "500", uuid4().hex)
        renamed = original.rename(self.input / "renamed.xml")

        result = self.store.import_folder(self.rev(), str(self.input))
        self.assertEqual((result["duplicates"], result["errors"]), (1, []))
        record = self.store.invoice(invoice_id)
        self.assertTrue(record["verification"]["passed"])
        self.assertEqual(self.store.original(record["files"][0]), renamed)
        self.assertEqual(self.store.original(self.store.batch(batch)["items"][0]["files"][0]), renamed)

    def test_imap_profile_selects_known_service_or_explicit_tls_endpoint(self):
        self.assertEqual(mailbox_profile("name@QQ.COM"), ("name@qq.com", "imap.qq.com", 993))
        self.assertEqual(mailbox_profile("name@163.com"), ("name@163.com", "imap.163.com", 993))
        self.assertEqual(mailbox_profile("name@example.com", "mail.example.com", "1993"),
                         ("name@example.com", "mail.example.com", 1993))
        with self.assertRaises(UserError):
            mailbox_profile("name@example.com")
        self.assertEqual(mailbox_profile("name@outlook.com"), ("name@outlook.com", "outlook.office365.com", 993))
        with self.assertRaisesRegex(UserError, "outlook.office365.com"):
            mailbox_profile("name@outlook.com", "mail.example.com")

    def test_outlook_profile_requires_registered_browser_authorization(self):
        profile = mailbox_profile("name@outlook.com")
        with self.assertRaisesRegex(UserError, "Microsoft 浏览器授权"):
            IMAPMailbox(profile).folders()
        with self.assertRaisesRegex(UserError, "应用注册"):
            OutlookOAuth(profile[0], "")

    def test_outlook_client_id_priority_and_validation(self):
        local_id, environment_id, packaged_id = (str(uuid4()) for _ in range(3))
        (self.root / CLIENT_ID_FILE).write_text(packaged_id + "\n", encoding="utf-8")
        previous = os.environ.pop(CLIENT_ID_ENV, None)
        try:
            self.assertEqual(configured_client_id(self.root), packaged_id)
            os.environ[CLIENT_ID_ENV] = environment_id
            self.assertEqual(configured_client_id(self.root, local_client_id=" "), environment_id)
            self.assertEqual(configured_client_id(self.root, local_client_id=f" {local_id.upper()} "), local_id)
            with self.assertRaisesRegex(UserError, "客户端 ID 格式无效"):
                configured_client_id(self.root, local_client_id="invalid")
        finally:
            if previous is None:
                os.environ.pop(CLIENT_ID_ENV, None)
            else:
                os.environ[CLIENT_ID_ENV] = previous
