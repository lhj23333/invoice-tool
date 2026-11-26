from datetime import datetime, timezone
from email import policy
from email.message import EmailMessage
from io import BytesIO
import os
from pathlib import Path
import shutil
import socket
import unittest
from threading import Event
from unittest.mock import patch
from uuid import uuid4
from zipfile import ZipFile

import cv2
from imapclient.response_parser import parse_fetch_response, parse_response
from imapclient.response_types import BodyData
from pypdf import PdfReader, PdfWriter

from invoice_workbench.domain import UserError, identity
from invoice_workbench.extract import MAX_BYTES, has_invoice_content, inspect_mail_document
from invoice_workbench.mail import (
    CONNECT_BUDGET, CONNECT_TIMEOUT, CollectionCancelled, IMAPMailbox, MailMessage, _connect_checked,
    _decode_part, _delivery_hint, _folder_options, _mail_links, _message_batches, _part_requests, _parts,
    _redirect_url, _screen_file, collect, date_bounds, domain_list, download_invoice, mailbox_profile,
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

    def test_download_falls_back_to_next_resolved_address(self):
        payload = b"%PDF-1.4 fallback"
        attempts = []

        class FakeSocket:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def close(self):
                pass

        class FakeResponse:
            status = 200

            def getheader(self, name, default=""):
                return {"Content-Type": "application/pdf",
                        "Content-Disposition": 'attachment; filename="invoice.pdf"',
                        "Content-Length": str(len(payload))}.get(name, default)

            def read(self, size=-1):
                return payload

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        class FakeConnection:
            def __init__(self, host, timeout=None, context=None):
                self.sock = None

            def request(self, method, path):
                self.path = path

            def getresponse(self):
                return FakeResponse()

            def close(self):
                pass

        class FakeContext:
            def wrap_socket(self, sock, server_hostname=None):
                return sock

        addresses = [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2001:4860:4860::8888", 443, 0, 0)),
                     (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))]

        def connect(address, timeout):
            attempts.append(address)
            if len(attempts) == 1:
                raise OSError("first address unreachable")
            return FakeSocket()

        with patch("invoice_workbench.mail.getaddrinfo", return_value=addresses), \
                patch("invoice_workbench.mail.create_connection", side_effect=connect), \
                patch("invoice_workbench.mail.create_default_context", return_value=FakeContext()), \
                patch("invoice_workbench.mail.HTTPSConnection", FakeConnection):
            name, data = download_invoice("https://invoice.example.com/file", [])

        self.assertEqual(attempts, [("2001:4860:4860::8888", 443), ("8.8.8.8", 443)])
        self.assertEqual((name, data), ("invoice.pdf", payload))

    def test_connect_fallback_uses_short_per_address_timeout(self):
        clock = [0.0]
        attempts = []

        class Context:
            def wrap_socket(self, sock, server_hostname=None):
                return sock

        class Socket:
            def close(self):
                pass

        def connect(address, timeout):
            attempts.append((address[0], timeout))
            if len(attempts) < 3:
                clock[0] += timeout  # 黑洞地址等满超时才失败
                raise OSError("black hole address")
            return Socket()

        addresses = [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2001:4860:4860::8888", 443, 0, 0)),
                     (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2001:4860:4860::8844", 443, 0, 0)),
                     (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))]
        with patch("invoice_workbench.mail.create_connection", side_effect=connect), \
                patch("invoice_workbench.mail.monotonic", side_effect=lambda: clock[0]):
            sock = _connect_checked("invoice.example.com", addresses, Context())
        self.assertIsInstance(sock, Socket)
        # 前两个黑洞地址各只等收紧后的 6s，可用地址只花剩余预算，累计不超过 15s。
        self.assertEqual(attempts, [("2001:4860:4860::8888", CONNECT_TIMEOUT),
                                    ("2001:4860:4860::8844", CONNECT_TIMEOUT),
                                    ("8.8.8.8", CONNECT_BUDGET - 2 * CONNECT_TIMEOUT)])
        self.assertEqual(sum(timeout for _, timeout in attempts), CONNECT_BUDGET)

    def test_connect_fallback_stops_at_total_budget(self):
        clock = [0.0]
        attempts = []

        class Context:
            def wrap_socket(self, sock, server_hostname=None):
                return sock

        def connect(address, timeout):
            attempts.append(timeout)
            clock[0] += timeout  # 每个地址都等满超时，模拟无响应的黑洞节点
            raise OSError("black hole address")

        addresses = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (f"8.8.8.{index}", 443)) for index in range(1, 5)]
        with patch("invoice_workbench.mail.create_connection", side_effect=connect), \
                patch("invoice_workbench.mail.monotonic", side_effect=lambda: clock[0]):
            with self.assertRaises(OSError):
                _connect_checked("invoice.example.com", addresses, Context())
        self.assertEqual(attempts, [CONNECT_TIMEOUT, CONNECT_TIMEOUT, CONNECT_BUDGET - 2 * CONNECT_TIMEOUT])
        self.assertLessEqual(sum(attempts), CONNECT_BUDGET)

    def test_naive_internal_date_is_read_as_utc_without_aborting_scan(self):
        header = ("From: billing@example.com\r\nSubject: 电子发票\r\n"
                  "Message-ID: <naive@example.com>\r\n\r\n").encode("utf-8")
        structure = BodyData.create((b"TEXT", b"PLAIN", (b"CHARSET", b"UTF-8"), None, None, b"7BIT", 6, 1))

        class FakeClient:
            def select_folder(self, folder, readonly=True):
                return {b"UIDVALIDITY": 9}

            def search(self, criteria):
                return [11, 12]

            def fetch(self, uids, requests):
                return {11: {b"INTERNALDATE": datetime(2026, 11, 17, 12, 0), b"BODYSTRUCTURE": structure,
                             b"BODY[HEADER.FIELDS (FROM SUBJECT MESSAGE-ID)]": header},
                        12: {b"INTERNALDATE": None, b"BODYSTRUCTURE": structure,
                             b"BODY[HEADER.FIELDS (FROM SUBJECT MESSAGE-ID)]": header}}

        rows, messages = [], []
        stats = dict.fromkeys(("scanned", "reused", "metadata_reads", "partial_bodies", "filtered"), 0)
        for batch in _message_batches(FakeClient(), ("a@example.com", "imap.example.com", 993), "INBOX",
                                      date_bounds("2026-11-17", "2026-11-17"), 4, stats, rows, {},
                                      {"messages": {}}):
            messages.extend(message for message, _ in batch)
        self.assertEqual([message.metadata["received"] for message in messages], ["2026-11-17T12:00:00+00:00"])
        self.assertEqual(len(rows), 1)
        self.assertIn("收件时间", rows[0]["error"])

    def test_missing_internal_date_falls_back_to_date_header(self):
        def header(date_line):
            return ("From: billing@example.com\r\nSubject: 电子发票\r\n"
                    "Message-ID: <date-header@example.com>\r\n" + date_line + "\r\n").encode("utf-8")
        structure = BodyData.create((b"TEXT", b"PLAIN", (b"CHARSET", b"UTF-8"), None, None, b"7BIT", 6, 1))

        class FakeClient:
            def select_folder(self, folder, readonly=True):
                return {b"UIDVALIDITY": 9}

            def search(self, criteria):
                return [41, 42]

            def fetch(self, uids, requests):
                return {41: {b"INTERNALDATE": None, b"BODYSTRUCTURE": structure,
                             b"BODY[HEADER.FIELDS (FROM SUBJECT MESSAGE-ID DATE)]":
                                 header("Date: Tue, 17 Nov 2026 08:40:00 +0800\r\n")},
                        42: {b"INTERNALDATE": None, b"BODYSTRUCTURE": structure,
                             b"BODY[HEADER.FIELDS (FROM SUBJECT MESSAGE-ID DATE)]":
                                 header("Date: Tue, 17 Nov 2026 00:40:00\r\n")}}

        rows, messages = [], []
        stats = dict.fromkeys(("scanned", "reused", "metadata_reads", "partial_bodies", "filtered"), 0)
        for batch in _message_batches(FakeClient(), ("a@example.com", "imap.example.com", 993), "INBOX",
                                      date_bounds("2026-11-17", "2026-11-17"), 4, stats, rows, {},
                                      {"messages": {}}):
            messages.extend(message for message, _ in batch)
        # 带时区的 Date 头保持原偏移，无时区的按 UTC 处理；两者指向同一时刻。
        self.assertEqual([datetime.fromisoformat(message.metadata["received"]).astimezone(timezone.utc)
                          for message in messages],
                         [datetime(2026, 11, 17, 0, 40, tzinfo=timezone.utc)] * 2)
        self.assertEqual(rows, [])
        self.assertEqual(stats["scanned"], 2)

    def test_oversized_irrelevant_mail_skips_body_download(self):
        header = ("From: news@example.com\r\nSubject: 项目周报\r\n"
                  "Message-ID: <large@example.com>\r\n\r\n").encode("utf-8")
        structure = BodyData.create((b"APPLICATION", b"ZIP", (b"NAME", b"archive.zip"), None, None, b"BASE64",
                                     8 * 1024 * 1024, None))
        calls = []

        class FakeClient:
            def select_folder(self, folder, readonly=True):
                return {b"UIDVALIDITY": 9}

            def search(self, criteria):
                return [11]

            def fetch(self, uids, requests):
                calls.append(requests)
                return {11: {b"INTERNALDATE": datetime(2026, 11, 17, 12, 0), b"BODYSTRUCTURE": structure,
                             b"BODY[HEADER.FIELDS (FROM SUBJECT MESSAGE-ID)]": header}}

        rows, messages = [], []
        stats = dict.fromkeys(("scanned", "reused", "metadata_reads", "partial_bodies", "filtered"), 0)
        for batch in _message_batches(FakeClient(), ("a@example.com", "imap.example.com", 993), "INBOX",
                                      date_bounds("2026-11-17", "2026-11-17"), 4, stats, rows, {},
                                      {"messages": {}}):
            messages.extend(batch)
        self.assertEqual(messages, [])
        self.assertEqual(stats["filtered"], 1)
        self.assertEqual(len(calls), 1)

    def test_cancelled_collection_stops_before_connecting(self):
        class Mailbox:
            profile = ("a@example.com", "imap.example.com", 993)
            forwarded_mode = {"sections": True}

            def _connection(self, **kwargs):
                raise AssertionError("cancelled collection must not connect")

        cancelled = Event()
        cancelled.set()
        with self.assertRaises(CollectionCancelled):
            collect(Mailbox(), "INBOX", date_bounds("2026-11-17", "2026-11-17"), [], self.input, None,
                    cancelled=cancelled)

    def test_cancelled_collection_skips_preview_download(self):
        header = ("From: billing@example.com\r\nSubject: 电子发票\r\n"
                  "Message-ID: <cancel@example.com>\r\n\r\n").encode("utf-8")
        structure = BodyData.create((b"TEXT", b"PLAIN", (b"CHARSET", b"UTF-8"), None, None, b"7BIT", 6, 1))
        calls = []

        class FakeClient:
            def select_folder(self, folder, readonly=True):
                return {b"UIDVALIDITY": 9}

            def search(self, criteria):
                return [11]

            def fetch(self, uids, requests):
                calls.append(requests)
                return {11: {b"INTERNALDATE": datetime(2026, 11, 17, 12, 0), b"BODYSTRUCTURE": structure,
                             b"BODY[HEADER.FIELDS (FROM SUBJECT MESSAGE-ID)]": header}}

        class Connection:
            def __enter__(self):
                return FakeClient()

            def __exit__(self, *exc):
                return False

        class Mailbox:
            profile = ("a@example.com", "imap.example.com", 993)
            forwarded_mode = {"sections": True}

            def _connection(self, **kwargs):
                return Connection()

        cancelled = Event()

        def progress(message):
            if message.startswith("正在筛选邮件"):
                cancelled.set()

        with self.assertRaises(CollectionCancelled):
            collect(Mailbox(), "INBOX", date_bounds("2026-11-17", "2026-11-17"), [], self.input, progress,
                    cancelled=cancelled)
        self.assertEqual(len(calls), 1)

    def test_malformed_download_target_keeps_other_attachments(self):
        bad_url = "https://[invalid"
        with self.assertRaisesRegex(UserError, "下载跳转地址无效"):
            _redirect_url("https://invoice.example.com/start", bad_url)
        rows = self.collect_sample_mail(1, [("invoice.pdf", (SAMPLES / "sample-01.pdf").read_bytes())],
                                        f'<a href="{bad_url}">下载发票</a>', body_type="html")
        report = self.store.import_mail(self.rev(), "收件箱", 1, rows)
        self.assertEqual(report["imported"], 1)
        error, = report["errors"]
        self.assertEqual(error["file"], "下载发票")

    def test_invoice_guidance_does_not_trigger_a_download(self):
        subject = "供应商发票申请流程"
        form_url = "https://www.irs.gov/pub/irs-pdf/fw9.pdf"
        invoice_url = "https://invoice.example.com/download"
        guidance = f'<p>发票申请流程已更新，请查收附件。</p><p><a href="{form_url}">下载流程表格</a></p>'
        message = EmailMessage()
        message.set_content(guidance, subtype="html")
        links = {}
        body = _mail_links(message, subject, links)
        self.assertFalse(links)
        self.assertFalse(_delivery_hint(subject + "\n" + body))
        message.set_content(guidance + f'<p>电子发票已开具。</p><a href="{invoice_url}">下载发票</a>', subtype="html")
        _mail_links(message, subject, links)
        self.assertEqual(links, {invoice_url: "下载发票"})

    def test_nested_mail_preview_uses_charset_from_body_structure(self):
        message = EmailMessage()
        message.set_content("电子发票已开具。", charset="gb18030", cte="base64")
        encoded = message.get_payload().encode("ascii")
        structure = BodyData.create((b"TEXT", b"PLAIN", (b"CHARSET", b"GB18030"), None, None,
                                     b"BASE64", len(encoded), 1))
        part, = _parts(structure, section="1.1")
        self.assertEqual(_part_requests(part, 1024, include_mime=False), ("BODY.PEEK[1.1]",))
        prefix = f"1 (UID 7 BODY[1.1] {{{len(encoded)}}}".encode("ascii")
        response = parse_fetch_response([(prefix, encoded), b")"])[7]
        mail = MailMessage({"imap_uid": 7}, b"", (part,), (part,))
        preview, received = _decode_part(mail, part, response, 1024, preview=True)
        self.assertEqual(preview.get_content().strip(), "电子发票已开具。")
        self.assertEqual(received, len(encoded))

    def test_generic_notice_finds_direct_link_with_invoice_filename(self):
        invoice = "https://invoice.example.com/%E5%8F%91%E7%A5%A8.ofd?download=1"
        form = "https://www.irs.gov/pub/irs-pdf/fw9.pdf"
        message = EmailMessage()
        message.set_content(f'<a href="{invoice}">下载文件</a><a href="{form}">下载表格</a>', subtype="html")
        links = {}
        _mail_links(message, "资料通知", links)
        self.assertEqual(links, {invoice: "下载文件"})

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

    def test_unnamed_text_pdf_is_identified_without_ocr(self):
        evidence, deferred, ocr = inspect_mail_document((SAMPLES / "sample-01.pdf").read_bytes(), ".pdf", allow_ocr=False)
        self.assertTrue(has_invoice_content(evidence))
        self.assertFalse(deferred)
        self.assertFalse(ocr)

    def test_pdf_that_only_references_an_invoice_is_filtered_without_ocr(self):
        pdf = SAMPLES / "sample-01.pdf"
        writer = PdfWriter()
        page = writer.add_page(PdfReader(pdf).pages[0])
        contents = page.get_contents()
        number_text = [operands for operands, operator in contents.operations if operator == b"Tj"][1]
        contents.operations = [(operands, operator) for operands, operator in contents.operations
                               if operator != b"Tj" or operands is number_text]
        page.replace_contents(contents)
        changed = BytesIO()
        writer.write(changed)
        evidence, _, called_ocr = inspect_mail_document(changed.getvalue(), ".pdf", allow_ocr=False)
        original, _, _ = inspect_mail_document(pdf.read_bytes(), ".pdf", allow_ocr=False)
        self.assertEqual(identity(evidence), identity(original))
        self.assertFalse(called_ocr)
        row = {"file": "reference.pdf"}
        stats = {"deferred": 0, "ocr_files": 0}
        self.assertFalse(_screen_file(changed.getvalue(), row["file"], row, self.input, stats, {},
                                      allow_ocr=False, hinted=False))
        self.assertFalse(list(self.input.iterdir()))

    def test_qr_image_requires_delivery_context_and_reopens_for_review(self):
        original, _, _ = inspect_mail_document((SAMPLES / "sample-01.pdf").read_bytes(), ".pdf", allow_ocr=False)
        image = cv2.QRCodeEncoder_create().encode(identity(original))
        enlarged = cv2.resize(image, (512, 512), interpolation=cv2.INTER_NEAREST)
        encoded, output = cv2.imencode(".png", enlarged)
        self.assertTrue(encoded)
        data = output.tobytes()
        row = {"file": "scan.png"}
        stats = {"deferred": 0, "ocr_files": 0}
        cache = {}
        self.assertFalse(_screen_file(data, row["file"], row, self.input, stats, cache,
                                      allow_ocr=True, hinted=False))
        self.assertTrue(_screen_file(data, row["file"], row, self.input, stats, cache,
                                     allow_ocr=True, hinted=True, image_context=True))
        self.assertEqual(stats["ocr_files"], 0)
        self.store.import_mail(self.rev(), "收件箱", 1, [row])
        directory = self.store.root
        self.store.close()
        self.store = Store(directory)
        pending, = self.store.import_history()[0]["items"]
        self.assertEqual(pending["result"], "review")
        self.assertEqual(self.store.original(pending["review_object"]).read_bytes(), data)
        self.assertEqual(self.store.view()["invoices"], [])
        self.store.decide_mail_review(self.rev(), pending["review_id"], original=SAMPLES / "sample-01.pdf")
        self.assertEqual(self.store.import_history()[-1]["items"][0]["result"], "reviewed")
        self.assertFalse(list((directory / "objects").glob("review-*")))

    def collect_sample_mail(self, sequence, attachments, body="发票见附件。", blocked=(), *, body_type="plain"):
        rows = []
        cache = {}
        stats = {"deferred": 0, "ocr_files": 0}
        metadata = {"subject": "电子发票", "message_id": str(sequence)}
        for name, data in attachments:
            row = {"file": name, **metadata}
            try:
                if _screen_file(data, name, row, self.input, stats, cache, allow_ocr=True, hinted=True):
                    rows.append(row)
            except UserError as exc:
                rows.append({**row, "error": str(exc)})
        message = EmailMessage(policy=policy.SMTP)
        message.set_content(body, subtype=body_type)
        links = {}
        _mail_links(message, metadata["subject"], links)
        for url, label in links.items():
            with self.assertRaises(UserError) as failure:
                download_invoice(url, blocked)
            rows.append({"file": label, **metadata, "error": str(failure.exception)})
        return rows

    def write_sample_eml(self, sequence, attachments):
        message = EmailMessage(policy=policy.SMTP)
        message["Subject"] = "电子发票"
        message.set_content("发票见附件。")
        for name, data in attachments:
            message.add_attachment(data, maintype="application", subtype=Path(name).suffix[1:].lower(), filename=name)
        (self.input / f"mail-{sequence}.eml").write_bytes(message.as_bytes())

    def test_mail_keeps_pdf_and_merges_xml_when_ofd_is_invalid(self):
        rows = self.collect_sample_mail(1, [("sample-01.OFD", incomplete_ofd()),
                                           ("sample-01.PDF", (SAMPLES / "sample-01.pdf").read_bytes()),
                                           ("sample-01.xml", (SAMPLES / "sample-01.xml").read_bytes())])
        report = self.store.import_mail(self.rev(), "收件箱", 1, rows)
        self.assertEqual((report["imported"], report["merged"], report["duplicates"], len(report["errors"])), (1, 1, 0, 1))
        self.assertEqual([row["result"] for row in report["items"]], ["error", "imported", "merged"])
        self.assertIn("OFD 缺少文档入口", report["items"][0]["detail"])
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
        review_id = report["items"][0]["review_id"]
        pending = self.store.import_history()[0]["items"][0]
        self.assertEqual(pending["result"], "review")
        self.assertEqual(self.store.view()["invoices"], [])
        self.assertEqual(self.store.original(pending["review_object"]).read_bytes(), (SAMPLES / "sample-01.xml").read_bytes())
        root = self.store.root
        self.store.close()
        self.store = Store(root)
        pdf = self.collect_sample_mail(2, [("sample-01.pdf", (SAMPLES / "sample-01.pdf").read_bytes())])
        result = self.store.import_mail(self.rev(), "收件箱", 1, pdf)
        self.assertEqual((result["imported"], result["merged"]), (1, 0))
        invoice_id = result["items"][0]["invoice_id"]
        resolved = self.store.import_history()[-1]["items"][0]
        self.assertEqual((resolved["review_id"], resolved["result"], resolved["invoice_id"]),
                         (review_id, "reviewed", invoice_id))
        self.assertFalse(list((self.store.root / "objects").glob("review-*")))
        self.assertTrue(self.store.invoice(invoice_id)["verification"]["passed"])
        batch = self.store.create_batch(self.rev(), [invoice_id], "500", "500", uuid4().hex)
        with ZipFile(self.store.export(batch)) as archive:
            self.assertEqual(set(archive.namelist()), {"明细.csv", "sample-01.pdf"})

    def test_conflicting_mail_xml_waits_for_review_and_preserves_verified_original(self):
        rows = self.collect_sample_mail(1, [("sample-01.pdf", (SAMPLES / "sample-01.pdf").read_bytes()),
                                           ("conflicting.xml", xml_invoice(amount="650.00"))])
        report = self.store.import_mail(self.rev(), "收件箱", 1, rows)
        self.assertEqual([row["result"] for row in report["items"]], ["imported", "review"])
        self.assertIn("同票字段冲突", self.store.import_history()[0]["items"][1]["detail"])
        item, = self.store.view()["invoices"]
        self.assertTrue(item["verification"]["passed"])
        self.assertFalse(self.store.duplicate_ids())
        self.store.create_batch(self.rev(), [item["id"]], "500", "500", uuid4().hex)
        self.assertEqual(len(list((self.store.root / "objects").glob("review-*.xml"))), 1)

    def test_verified_mail_pdf_keeps_conflicting_xml_for_review(self):
        rows = self.collect_sample_mail(1, [("conflicting.xml", xml_invoice(amount="650.00"))])
        xml = self.store.import_mail(self.rev(), "收件箱", 1, rows)
        rows = self.collect_sample_mail(2, [("invoice.pdf", (SAMPLES / "sample-01.pdf").read_bytes())])
        report = self.store.import_mail(self.rev(), "收件箱", 1, rows)
        item, = self.store.view()["invoices"]
        self.assertEqual(item["id"], report["items"][0]["invoice_id"])
        self.assertEqual(item["amount_cents"], 60000)
        self.assertTrue(item["verification"]["passed"])
        self.assertFalse(item.get("mail_xml"))
        pending = self.store.import_history()[-1]["items"][0]
        self.assertEqual((pending["review_id"], pending["result"]), (xml["items"][0]["review_id"], "review"))
        self.store.decide_mail_review(self.rev(), pending["review_id"], original=SAMPLES / "sample-01.pdf")
        resolved = self.store.import_history()[-1]["items"][0]
        self.assertEqual((resolved["result"], resolved["invoice_id"]), ("reviewed", item["id"]))
        self.assertFalse(list((self.store.root / "objects").glob("review-*.xml")))

    def test_mail_pdf_variant_reuses_verified_local_original(self):
        original = SAMPLES / "sample-01.pdf"
        first = self.store.import_files(self.rev(), [original])
        rows = self.collect_sample_mail(2, [("second.pdf", original.read_bytes() + b"\r\n")])
        report = self.store.import_mail(self.rev(), "收件箱", 1, rows)
        item, = self.store.view()["invoices"]
        self.assertEqual(report["duplicates"], 1)
        self.assertEqual(report["items"][0]["invoice_id"], first["items"][0]["invoice_id"])
        self.assertEqual(len(item["sources"]), 2)
        self.assertEqual(len(item["files"]), 1)
        batch = self.store.create_batch(self.rev(), [item["id"]], "500", "500", uuid4().hex)
        with ZipFile(self.store.export(batch)) as archive:
            self.assertEqual(set(archive.namelist()), {"明细.csv", "sample-01.pdf"})

    def test_verified_mail_variant_restores_unusable_original(self):
        content = (SAMPLES / "sample-01.pdf").read_bytes()
        first = self.store.import_mail(self.rev(), "收件箱", 1, self.collect_sample_mail(1, [("first.pdf", content)]))
        invoice_id = first["items"][0]["invoice_id"]
        self.store.original(self.store.invoice(invoice_id)["files"][0]).unlink()
        variant = content + b"\r\n"
        rows = self.collect_sample_mail(2, [("second.pdf", variant)])
        report = self.store.import_mail(self.rev(), "收件箱", 1, rows)
        item, = self.store.view()["invoices"]
        self.assertEqual(report["merged"], 1)
        self.assertEqual(item["id"], invoice_id)
        self.assertTrue(item["verification"]["passed"])
        replacement, = item["files"]
        self.assertEqual(self.store.original(replacement).read_bytes(), variant)

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
        self.assertFalse(list((self.store.root / "objects").glob("*.eml")))
        pending = self.store.import_history()[0]["items"][1]
        self.assertEqual(pending["result"], "review")
        self.store.decide_mail_review(self.rev(), pending["review_id"], dismiss=True)
        self.assertEqual(self.store.import_history()[0]["items"][1]["result"], "dismissed")
        self.assertFalse(list((self.store.root / "objects").iterdir()))

    def test_ofd_only_mail_is_processed_despite_pdf_in_another_mail(self):
        rows = self.collect_sample_mail(1, [("sample-01.pdf", (SAMPLES / "sample-01.pdf").read_bytes())])
        rows.extend(self.collect_sample_mail(2, [("sample-02.ofd", incomplete_ofd())],
                                             "发票.pdf https://invoice.example.com/sample-02.pdf",
                                             blocked=["invoice.example.com"]))
        report = self.store.import_mail(self.rev(), "收件箱", 2, rows)
        self.assertEqual((report["imported"], report["merged"], len(report["errors"])), (1, 0, 2))
        self.assertFalse(report["notes"])
        self.assertIn("黑名单", report["errors"][1]["error"])
        self.assertIn("OFD 缺少文档入口", report["items"][1]["detail"])

    def test_mail_store_keeps_ofd_error_for_same_message_across_staged_files(self):
        ofd, = self.collect_sample_mail(1, [("sample-01.ofd", incomplete_ofd())])
        pdf, = self.collect_sample_mail(2, [("sample-01.pdf", (SAMPLES / "sample-01.pdf").read_bytes())])
        ofd["message_id"] = pdf["message_id"]
        report = self.store.import_mail(self.rev(), "收件箱", 2, [ofd, pdf])
        self.assertEqual([row["result"] for row in report["items"]], ["error", "imported"])
        self.assertEqual(len(self.store.view()["invoices"]), 1)
        self.assertFalse(list((self.store.root / "objects").glob("*.ofd")))

    def test_mail_store_keeps_ofd_error_without_same_invoice_evidence(self):
        pdf = self.collect_sample_mail(1, [("sample-01.pdf", (SAMPLES / "sample-01.pdf").read_bytes())])
        first = self.store.import_mail(self.rev(), "收件箱", 1, pdf)
        self.assertEqual(first["imported"], 1)
        ofd = self.collect_sample_mail(1, [("sample-01.ofd", incomplete_ofd())])
        report = self.store.import_mail(self.rev(), "收件箱", 1, ofd)
        self.assertEqual((report["imported"], len(report["errors"])), (0, 1))
        self.assertEqual([row["result"] for row in report["items"]], ["error"])
        self.assertEqual(len(self.store.view()["invoices"]), 1)
        self.assertFalse(list((self.store.root / "objects").glob("*.ofd")))

    def test_mail_processes_ofd_when_present_pdf_is_damaged_or_over_limit(self):
        ofd = incomplete_ofd()
        for sequence, pdf in enumerate((b"%PDF-truncated", b"%PDF-" + b" " * MAX_BYTES), 1):
            with self.subTest(sequence=sequence):
                rows = self.collect_sample_mail(sequence, [("invoice.pdf", pdf), ("invoice.ofd", ofd)])
                report = self.store.import_mail(self.rev(), "收件箱", 1, rows)
                self.assertEqual([row["result"] for row in report["items"]], ["error", "error"])
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
        self.assertEqual(report["items"][0]["result"], "error")
        self.assertIn("OFD 缺少文档入口", report["items"][0]["detail"])

    def test_local_eml_checks_each_attachment_even_with_verified_pdf(self):
        self.write_sample_eml(1, [("sample-01.ofd", incomplete_ofd()),
                                  ("sample-01.pdf", (SAMPLES / "sample-01.pdf").read_bytes())])
        report = self.store.import_folder(self.rev(), str(self.input))
        self.assertEqual((report["scanned"], report["imported"], len(report["errors"])), (1, 1, 1))
        self.assertEqual([row["result"] for row in report["items"]], ["error", "imported"])
        self.assertIn("OFD 缺少文档入口", report["items"][0]["detail"])
        item, = self.store.view()["invoices"]
        self.assertTrue(item["verification"]["passed"])
        self.assertFalse(list((self.store.root / "objects").glob("*.ofd")))

    def test_local_eml_processes_ofd_when_pdf_import_fails(self):
        self.write_sample_eml(1, [("invoice.ofd", incomplete_ofd()), ("invoice.pdf", b"not a PDF")])
        report = self.store.import_folder(self.rev(), str(self.input))
        self.assertEqual([row["result"] for row in report["items"]], ["error", "error"])
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
