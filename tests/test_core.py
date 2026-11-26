from copy import deepcopy
from email.message import EmailMessage
from itertools import combinations
import random
from time import perf_counter
import unittest
import uuid
import zipfile

from invoice_workbench import store as store_module
from invoice_workbench.domain import UserError, choose_invoices, company_matches, identity, money, validate_invoice, yuan
from invoice_workbench.extract import extract, from_text
from invoice_workbench.store import Store, csv_cell
from .helpers import BUYER, TAX, StoreFixture, write_invoice, xml_invoice


class MoneyAndSolver(unittest.TestCase):
    def test_amounts_are_exact(self):
        self.assertEqual(money("1,002.50"), 100250)
        self.assertEqual(money("￥0.01"), 1)
        self.assertEqual(yuan(10000000001), "100000000.01")
        for invalid in ("NaN", "Infinity", "-1", "0", "1.001", "invalid"):
            with self.subTest(invalid=invalid), self.assertRaises(UserError):
                money(invalid)

    def test_global_minimum_against_bruteforce(self):
        randomizer = random.Random(20261117)
        for _ in range(100):
            amounts = [randomizer.randint(1, 500) for _ in range(randomizer.randint(1, 11))]
            target = randomizer.randint(1, sum(amounts) + 100)
            invoices = [{"id": str(n), "date": "2026-01-01", "amount_cents": a} for n, a in enumerate(amounts)]
            forced = {0} if randomizer.choice([True, False]) else set()
            excluded = {len(amounts) - 1} - forced if randomizer.choice([True, False]) else set()
            totals = []
            for size in range(len(amounts) + 1):
                for selection in combinations(range(len(amounts)), size):
                    chosen = set(selection)
                    total = sum(amounts[i] for i in chosen)
                    if forced <= chosen and not excluded & chosen and total >= target:
                        totals.append(total)
            result = choose_invoices(invoices, target, [str(i) for i in forced], [str(i) for i in excluded])
            if totals:
                self.assertTrue(result["optimal"])
                self.assertEqual(result["total_cents"], min(totals))
                self.assertTrue({str(i) for i in forced} <= set(result["ids"]))
            else:
                self.assertGreater(result["shortfall_cents"], 0)

    def test_memory_limit_does_not_claim_approximate_optimum(self):
        with self.assertRaises(UserError):
            choose_invoices([{"id": "a", "amount_cents": 30_000_001}, {"id": "b", "amount_cents": 30_000_002}], 30_000_000)

    def test_tighter_greedy_bound_keeps_global_minimum(self):
        amounts = [990, 20, 20, 700]
        invoices = [{"id": str(n), "date": "2026-01-01", "amount_cents": a} for n, a in enumerate(amounts)]
        self.assertEqual(choose_invoices(invoices, 1000)["total_cents"], 1010)

    def test_tight_bound_fits_budget_where_loose_bound_did_not(self):
        # 升序贪心要跨过一张远大于目标的大票，旧上界会被撑到 2040 万，触发内存与上限拒绝；
        # 单张即达标的发票让新上界收回到 1800 万，可以正常求得精确解。
        amounts = [100_000] * 24 + [18_000_000]
        invoices = [{"id": str(n), "date": "2026-01-01", "amount_cents": a} for n, a in enumerate(amounts)]
        started = perf_counter()
        result = choose_invoices(invoices, 4_000_000)
        self.assertLess(perf_counter() - started, 15)
        self.assertTrue(result["optimal"])
        self.assertEqual(result["total_cents"], 18_000_000)
        self.assertEqual(result["ids"], ["24"])

    def test_company_and_legacy_identity(self):
        self.assertEqual(identity({"number": "12345678", "code": "123456789012"}), "123456789012:12345678")
        self.assertIsNone(identity({"number": "12345678"}))
        self.assertTrue(company_matches({"buyer_name": BUYER, "buyer_tax_id": TAX}, {"name": BUYER, "tax_id": TAX}))
        self.assertFalse(company_matches({"buyer_name": BUYER, "buyer_tax_id": "OTHER"}, {"name": BUYER, "tax_id": TAX}))

    def test_invalid_dates_and_missing_codes(self):
        fields = {"number": "26000000000000000001", "date": "2026-02-30", "amount": "1", "buyer_name": BUYER}
        with self.assertRaises(UserError):
            validate_invoice(fields)
        fields.update(date="2026-02-01", number="12345678")
        with self.assertRaises(UserError):
            validate_invoice(fields)


class ExtractTests(unittest.TestCase):
    def test_xml_namespace_and_buyer_scope(self):
        parsed = extract(xml_invoice(), ".xml")
        self.assertEqual(parsed["buyer_name"], BUYER)
        self.assertEqual(parsed["amount_cents"], 60000)
        namespaced = b'<Invoice xmlns="urn:example"><InvoiceNo>26000000000000000001</InvoiceNo><Buyer><Name>Buyer</Name><TaxID>91440300MA0000000X</TaxID></Buyer><TotalTax-includedAmount>8.01</TotalTax-includedAmount></Invoice>'
        parsed = extract(namespaced, ".xml")
        self.assertEqual(parsed["buyer_name"], "Buyer")
        self.assertEqual(parsed["amount_cents"], 801)

    def test_dtd_and_invalid_file_rejected(self):
        for payload, suffix in ((b'<!DOCTYPE x [<!ENTITY e "bad">]><x>&e;</x>', ".xml"), (b'not a pdf', ".pdf"), (b'<html/>', ".png")):
            with self.subTest(suffix=suffix), self.assertRaises(UserError):
                extract(payload, suffix)

    def test_chinese_pdf_text_fields(self):
        parsed = from_text("发票号码：26000000000000000001\n开票日期：2026年11月17日\n购买方信息\n名称：示例科技有限公司\n纳税人识别号：91440300MA0000000X\n销售方\n名称：商户\n价税合计（小写）￥1002.50")
        self.assertEqual(parsed["number"], "26000000000000000001")
        self.assertEqual(parsed["date"], "2026-11-17")
        self.assertEqual(parsed["amount_cents"], 100250)
        self.assertEqual(parsed["buyer_tax_id"], TAX)

    def test_red_invoice_not_treated_as_positive(self):
        parsed = extract(xml_invoice(amount="-10.00"), ".xml")
        self.assertNotIn("amount_cents", parsed)
        self.assertTrue(parsed["warnings"])

    def test_vertical_buyer_and_seller_columns(self):
        text = ("购  名称:示例科技有限公司                              销  名称:销售公司\n"
                "买                                                售\n"
                "方                                                方\n"
                "信  统一社会信用代码/纳税人识别号:91440300MA0000000X  信  统一社会信用代码/纳税人识别号:91330100MA0000000Y\n"
                "息                                                息\n"
                "项目名称 规格型号\n价税合计（大写）  （小写）壹佰贰拾圆整" + " " * 60 + "¥ 120.00")
        parsed = from_text(text)
        self.assertEqual(parsed["buyer_name"], BUYER)
        self.assertEqual(parsed["buyer_tax_id"], TAX)
        self.assertEqual(parsed["amount_cents"], 12000)

    def test_personal_buyer_does_not_take_seller_tax_id(self):
        text = ("购  名称:张三                                        销  名称:销售公司\n"
                "买                                                  售\n"
                "信  统一社会信用代码/纳税人识别号:                   信  统一社会信用代码/纳税人识别号:91330100MA0000000Y\n"
                "项目名称 规格型号")
        parsed = from_text(text)
        self.assertEqual(parsed["buyer_name"], "张三")
        self.assertEqual(parsed["buyer_tax_id"], "")

    def test_buyer_tax_id_above_its_label(self):
        text = ("购  名称:示例科技有限公司                              销  名称:销售公司\n"
                "买                                                售\n"
                "信                    91440300MA0000000X              91330100MA0000000Y信\n"
                "息  统一社会信用代码/纳税人识别号:                  息  统一社会信用代码/纳税人识别号:\n"
                "项目名称 规格型号")
        parsed = from_text(text)
        self.assertEqual(parsed["buyer_tax_id"], TAX)


class StoreTests(StoreFixture, unittest.TestCase):
    def test_repeat_import_is_idempotent(self):
        item = self.import_one()
        report = self.store.import_folder(self.rev(), str(self.input))
        self.assertEqual(report["duplicates"], 1)
        self.assertEqual(len(self.store.data["invoices"]), 1)
        self.assertEqual(len(item["files"]), 1)
        self.assertTrue(self.store.data["invoices"][0]["verification"]["passed"])

    def test_same_invoice_different_files_merge(self):
        self.import_one()
        write_invoice(self.input, "variant.xml", extra="<Format>second-file</Format>")
        report = self.store.import_folder(self.rev(), str(self.input))
        self.assertEqual(report["merged"], 1)
        self.assertEqual(len(self.store.data["invoices"]), 1)
        self.assertEqual(len(self.store.data["invoices"][0]["files"]), 2)

    def test_conflicting_duplicate_blocks_selection(self):
        self.import_one()
        write_invoice(self.input, "conflict.xml", amount="601.00")
        self.store.import_folder(self.rev(), str(self.input))
        self.assertEqual(len(self.store.data["invoices"]), 2)
        self.assertTrue(all(i["duplicate"] for i in self.store.view()["invoices"]))
        self.assertGreater(self.store.recommend("1.00")["shortfall_cents"], 0)
        items = list(self.store.data["invoices"])
        self.store.archive(self.rev(), items[1]["id"], True)
        self.assertEqual(self.store.recommend("500.00")["total_cents"], 60000)

    def test_full_invoice_occupied_and_excess_not_reused(self):
        batch_id = self.make_batch(amount="1002.50", target="1000.00")
        self.assertEqual(self.store.batch(batch_id)["declared_cents"], 100000)
        self.assertEqual(self.store.batch(batch_id)["total_cents"], 100250)
        self.assertEqual(self.store.recommend("2.50")["available_cents"], 0)
        self.store.set_invoice_status(self.rev(), self.store.batch(batch_id)["items"][0]["id"], "available")
        self.assertEqual(self.store.recommend("2.50")["total_cents"], 100250)

    def test_export_preserves_status_and_file_hashes(self):
        batch_id = self.make_batch()
        revision = self.rev()
        first = self.store.export(batch_id)
        self.store.export(batch_id)
        self.assertEqual(self.rev(), revision)
        self.assertEqual(self.store.batch(batch_id)["status"], "confirmed")
        with zipfile.ZipFile(first) as z:
            self.assertIn("明细.csv", z.namelist())
            self.assertTrue(z.read("明细.csv").startswith(b"\xef\xbb\xbf"))
            self.assertTrue(all("/" not in n and "\\" not in n for n in z.namelist()))
            copies = [n for n in z.namelist() if n != "明细.csv"]
            self.assertEqual(len(copies), 1)
            self.assertEqual(z.read(copies[0]), (self.input / "invoice.xml").read_bytes())

    def test_confirm_idempotency_and_occupied_edit_block(self):
        self.import_one()
        ids = [i["id"] for i in self.store.data["invoices"]]
        request_id, old_rev = uuid.uuid4().hex, self.rev()
        batch_id = self.store.create_batch(old_rev, ids, "500", "500", request_id)
        self.assertEqual(self.store.create_batch(old_rev, ids, "500", "500", request_id), batch_id)
        with self.assertRaises(UserError):
            self.store.review(self.rev(), ids[0], {**self.store.invoice(ids[0]), "amount": "600"})
        with self.assertRaises(UserError):
            self.store.archive(self.rev(), ids[0], True)

    def test_external_change_blocks_idempotent_request_reuse(self):
        self.import_one()
        ids = [item["id"] for item in self.store.data["invoices"]]
        request_id = uuid.uuid4().hex
        self.store.create_batch(self.rev(), ids, "500", "500", request_id)
        self.store.state_path.write_text(self.store.state_path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
        with self.assertRaises(UserError):
            self.store.create_batch(self.rev(), ids, "500", "500", request_id)

    def test_closed_session_blocks_idempotent_request_reuse(self):
        self.import_one()
        ids = [item["id"] for item in self.store.data["invoices"]]
        request_id = uuid.uuid4().hex
        self.store.create_batch(self.rev(), ids, "500", "500", request_id)
        self.store.close()
        with self.assertRaises(UserError):
            self.store.create_batch(self.rev(), ids, "500", "500", request_id)

    def test_history_snapshots_are_pruned_to_recent_versions(self):
        original = store_module.MAX_HISTORY_FILES
        store_module.MAX_HISTORY_FILES = 4
        try:
            for _ in range(9):
                self.store.set_company(self.rev(), BUYER, TAX)
        finally:
            store_module.MAX_HISTORY_FILES = original
        names = sorted(path.name for path in (self.store.root / "history").glob("*.json"))
        revision = self.store.data["revision"]
        self.assertEqual([int(name.split("-")[0]) for name in names],
                         list(range(revision - 4, revision)))

    def test_export_to_explicit_destination_leaves_workspace_clean(self):
        batch_id = self.make_batch()
        destination = self.root / "报销材料.zip"
        destination.write_bytes(b"stale")
        path = self.store.export(batch_id, destination=destination)
        self.assertEqual(path, destination.resolve())
        self.assertTrue(zipfile.is_zipfile(destination))
        self.assertEqual(list((self.store.root / "exports").glob("*.zip")), [])
        self.assertFalse(list(self.root.glob(".export-*.tmp")))
        with zipfile.ZipFile(destination) as archive:
            self.assertIn("明细.csv", archive.namelist())

    def test_export_destination_requires_existing_directory(self):
        batch_id = self.make_batch()
        with self.assertRaises(UserError):
            self.store.export(batch_id, destination=self.root)
        with self.assertRaises(UserError):
            self.store.export(batch_id, destination=self.root / "missing" / "a.zip")

    def test_invoice_return_releases_for_reuse(self):
        batch_id = self.make_batch()
        invoice_id = self.store.batch(batch_id)["items"][0]["id"]
        self.store.set_invoice_status(self.rev(), invoice_id, "submitted")
        self.store.set_invoice_status(self.rev(), invoice_id, "returned")
        self.assertFalse(self.store.occupied())
        self.assertEqual(self.store.recommend("1")["total_cents"], 60000)
        self.store.set_invoice_status(self.rev(), invoice_id, "reimbursed")
        self.assertEqual(self.store.recommend("1")["available_cents"], 0)

    def test_stale_revision_is_rejected_without_partial_change(self):
        before = deepcopy(self.store.data)
        with self.assertRaises(UserError):
            self.store.set_company(0, "Other", "")
        self.assertEqual(self.store.data, before)

    def test_second_session_and_external_modification_blocked(self):
        with self.assertRaises(UserError):
            Store(self.store.root)
        self.store.state_path.write_text(self.store.state_path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
        with self.assertRaises(UserError):
            self.store.set_company(self.rev(), "Other", "")

    def test_corrupt_original_blocks_export(self):
        batch_id = self.make_batch()
        file = self.store.data["invoices"][0]["files"][0]
        (self.store.root / file["path"]).write_bytes(b"corrupted")
        with self.assertRaises(UserError):
            self.store.export(batch_id)

    def test_archive_restore_automatically_verifies(self):
        item = self.import_one()
        self.store.archive(self.rev(), item["id"], True)
        self.assertEqual(self.store.recommend("1")["available_cents"], 0)
        self.store.archive(self.rev(), item["id"], False)
        self.assertTrue(self.store.invoice(item["id"])["verification"]["passed"])
        self.assertEqual(self.store.recommend("1")["total_cents"], 60000)

    def test_eml_attachment_import(self):
        message = EmailMessage()
        message["Subject"] = "合成发票附件"
        message.set_content("Invoice attachment")
        message.add_attachment(xml_invoice(), maintype="application", subtype="xml", filename="invoice.xml")
        (self.input / "sample.eml").write_bytes(message.as_bytes())
        report = self.store.import_folder(self.rev(), str(self.input))
        self.assertEqual(report["imported"], 1)
        item, = self.store.view()["invoices"]
        self.assertEqual(item["files"], [])
        self.assertEqual(item["mail_xml"][0]["fields"]["amount_cents"], 60000)
        self.assertFalse(item["verification"]["passed"])
        self.assertFalse(list((self.store.root / "objects").iterdir()))

    def test_persistence_and_snapshot(self):
        batch_id = self.make_batch()
        root = self.store.root
        self.assertTrue(list((root / "history").glob("*.json")))
        self.store.close()
        self.store = Store(root)
        self.assertEqual(self.store.batch(batch_id)["total_cents"], 60000)
        self.assertEqual(self.store.recommend("1")["available_cents"], 0)

    def test_csv_formula_injection(self):
        for text in ("=1+1", "+1", "@SUM(1)", "  -1"):
            self.assertTrue(csv_cell(text).startswith("'"))

if __name__ == "__main__":
    unittest.main()

