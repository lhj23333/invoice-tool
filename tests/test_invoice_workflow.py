from copy import deepcopy
import csv
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
import uuid
import zipfile

from invoice_workbench.domain import UserError
from invoice_workbench.gui import DuplicateFilesDialog, Workbench
from invoice_workbench.labels import selectable, state_label
from invoice_workbench.selection import SelectionDialog
from invoice_workbench.store import Store, fingerprint
from invoice_workbench.workflow import import_directory, remove_duplicate_files
from .helpers import BUYER, TAX, StoreFixture, write_invoice
from tools.validate_import import hashes, validate


class AutomaticVerificationTests(StoreFixture, unittest.TestCase):
    def test_import_is_immediately_available_without_manual_review(self):
        item = self.import_one()
        self.assertTrue(item["verification"]["passed"])
        self.assertEqual(self.store.recommend("500")["ids"], [item["id"]])
        snapshot = self.store.view()["invoices"][0]
        self.assertTrue(selectable(snapshot))
        self.assertEqual(state_label(snapshot), "可用")

    def test_field_changes_cannot_bypass_original_verification(self):
        item = self.import_one()
        original = {**item, "amount": "600.00"}
        changes = {"number": "26000000000000000002", "date": "2026-10-02", "amount": "601.00",
                   "buyer_name": "其他公司", "buyer_tax_id": "91440300MA0000000Y",
                   "seller_name": "其他商户", "code": "123456789012"}
        for field, value in changes.items():
            with self.subTest(field=field):
                self.store.review(self.rev(), item["id"], {**original, field: value})
                saved = self.store.invoice(item["id"])
                self.assertFalse(saved["verification"]["passed"])
                self.assertTrue(saved["verification"]["issues"])
                self.assertEqual(self.store.recommend("1")["available_cents"], 0)
                self.store.review(self.rev(), item["id"], original)
                self.assertTrue(self.store.invoice(item["id"])["verification"]["passed"])

    def test_missing_fields_and_company_mismatch_remain_unavailable(self):
        self.import_one(tax="")
        self.assertEqual(self.store.recommend("1")["available_cents"], 0)
        self.store.set_company(self.rev(), "其他公司", TAX)
        self.assertEqual(self.store.recommend("1")["available_cents"], 0)

    def test_image_fields_cannot_be_manually_marked_verified(self):
        sample = Path(__file__).resolve().parent.parent / "samples" / "image-preview" / "manual-review.png"
        (self.input / "image.png").write_bytes(sample.read_bytes())
        self.store.import_folder(self.rev(), str(self.input))
        item = self.store.data["invoices"][0]
        self.assertFalse(item["verification"]["passed"])
        self.store.review(self.rev(), item["id"], {"number": "26000000000000000001", "date": "2026-10-01",
                                                "amount": "600", "buyer_name": BUYER, "buyer_tax_id": TAX})
        self.assertFalse(self.store.invoice(item["id"])["verification"]["passed"])
        self.assertEqual(self.store.recommend("1")["available_cents"], 0)

    def test_legacy_records_are_verified_and_statuses_migrated(self):
        batch_id = self.make_batch()
        state = deepcopy(self.store.data)
        state["schema"] = 1
        state["batches"][0]["status"] = "returned"
        for item in state["invoices"]:
            item.pop("verification")
            item.pop("reimbursement_status")
            item.pop("active_batch_id")
            item["reviewed"] = False
        root = self.store.root
        self.store.close()
        (root / "invoices.json").write_text(json.dumps(state), encoding="utf-8")
        self.store = Store(root)
        self.assertEqual(self.store.data["schema"], 3)
        item = self.store.view()["invoices"][0]
        self.assertTrue(item["verification"]["passed"])
        self.assertEqual(item["reimbursement_status"], "returned")
        self.assertIsNone(item["batch_id"])
        self.assertTrue(selectable(item))
        self.assertEqual(self.store.batch(batch_id)["items"], state["batches"][0]["items"])
        self.assertEqual(self.store.recommend("500")["ids"], [item["id"]])

    def test_legacy_active_batch_states_keep_whole_invoice_occupied(self):
        self.make_batch()
        original = deepcopy(self.store.data)
        root = self.store.root
        for status, expected in (("confirmed", "reserved"), ("submitted", "submitted"), ("reimbursed", "reimbursed"), ("cancelled", "available")):
            with self.subTest(status=status):
                state = deepcopy(original)
                state["schema"] = 1
                state["batches"][0]["status"] = status
                for item in state["invoices"]:
                    for field in ("verification", "reimbursement_status", "active_batch_id"):
                        item.pop(field)
                self.store.close()
                (root / "invoices.json").write_text(json.dumps(state), encoding="utf-8")
                self.store = Store(root)
                item = self.store.view()["invoices"][0]
                self.assertEqual(item["reimbursement_status"], expected)
                self.assertEqual(bool(self.store.occupied()), status != "cancelled")
                self.assertEqual(self.store.recommend("1")["available_cents"], 60000 if status == "cancelled" else 0)


class SourceCleanupTests(StoreFixture, unittest.TestCase):
    def duplicates(self):
        original = write_invoice(self.input, "a.xml")
        copy = self.input / "nested" / "copy.xml"
        copy.parent.mkdir()
        copy.write_bytes(original.read_bytes())
        third = self.input / "z.xml"
        third.write_bytes(original.read_bytes())
        report = import_directory(self.store, self.rev(), self.input)
        return original, copy, third, report

    def test_cleanup_preserves_one_source_and_index_original(self):
        original, copy, third, report = self.duplicates()
        before = original.read_bytes()
        index = self.store.state_path.read_bytes()
        self.assertEqual(len(report["duplicate_files"]), 2)
        self.assertEqual(remove_duplicate_files(self.store, self.rev(), report), 2)
        self.assertEqual(original.read_bytes(), before)
        self.assertFalse(copy.exists())
        self.assertFalse(third.exists())
        self.assertEqual(self.store.state_path.read_bytes(), index)
        self.assertEqual(self.store.original(self.store.data["invoices"][0]["files"][0]).read_bytes(), before)
        repeat = import_directory(self.store, self.rev(), self.input)
        self.assertEqual(repeat["duplicate_files"], [])
        self.assertEqual(len(self.store.data["invoices"]), 1)

    def test_changed_duplicate_stops_before_any_deletion(self):
        original, copy, third, report = self.duplicates()
        third.write_bytes(third.read_bytes() + b"\n")
        with self.assertRaises(UserError):
            remove_duplicate_files(self.store, self.rev(), report)
        self.assertTrue(all(p.exists() for p in (original, copy, third)))

    def test_missing_keeper_stops_deletion(self):
        original, copy, third, report = self.duplicates()
        original.unlink()
        with self.assertRaises(FileNotFoundError):
            remove_duplicate_files(self.store, self.rev(), report)
        self.assertTrue(copy.exists() and third.exists())

    def test_corrupt_stored_original_stops_deletion(self):
        original, copy, third, report = self.duplicates()
        self.store.original(self.store.data["invoices"][0]["files"][0]).write_bytes(b"changed")
        with self.assertRaises(UserError):
            remove_duplicate_files(self.store, self.rev(), report)
        self.assertTrue(all(p.exists() for p in (original, copy, third)))

    def test_external_index_change_stops_deletion(self):
        original, copy, third, report = self.duplicates()
        self.store.state_path.write_bytes(self.store.state_path.read_bytes() + b"\n")
        with self.assertRaises(UserError):
            remove_duplicate_files(self.store, self.rev(), report)
        self.assertTrue(all(p.exists() for p in (original, copy, third)))

    @unittest.skipUnless(os.name == "nt", "需要 Windows junction")
    def test_replaced_source_directory_stops_deletion(self):
        _, _, _, report = self.duplicates()
        displaced = self.root / "displaced-source"
        self.input.rename(displaced)
        subprocess.run(["cmd", "/c", "mklink", "/J", str(self.input), str(displaced)], check=True, capture_output=True)
        with self.assertRaises(UserError):
            remove_duplicate_files(self.store, self.rev(), report)
        self.assertEqual(len(list(displaced.rglob("*.xml"))), 3)

    def test_different_content_and_conflicts_are_preserved(self):
        write_invoice(self.input, "a.xml")
        write_invoice(self.input, "variant.xml", extra="<Format>second-file</Format>")
        first = import_directory(self.store, self.rev(), self.input)
        self.assertEqual(first["duplicate_files"], [])
        write_invoice(self.input, "conflict.xml", amount="601.00")
        write_invoice(self.input, "conflict-copy.xml", amount="601.00")
        second = import_directory(self.store, self.rev(), self.input)
        self.assertEqual(second["duplicate_files"], [])
        self.assertEqual(len(list(self.input.iterdir())), 4)


class InvoiceStatusAndExportTests(StoreFixture, unittest.TestCase):
    def test_partial_return_and_new_batch_preserve_other_invoice_and_history(self):
        write_invoice(self.input, "a.xml")
        write_invoice(self.input, "b.xml", number="26000000000000000002")
        self.store.import_folder(self.rev(), str(self.input))
        ids = [i["id"] for i in self.store.data["invoices"]]
        first = self.store.create_batch(self.rev(), ids, "1000", "1000", uuid.uuid4().hex)
        history = deepcopy(self.store.batch(first))
        self.store.set_invoice_status(self.rev(), ids[0], "submitted")
        self.store.set_invoice_status(self.rev(), ids[0], "returned")
        self.assertEqual(self.store.occupied(), {ids[1]: first})
        self.assertEqual(self.store.recommend("500")["ids"], [ids[0]])
        second = self.store.create_batch(self.rev(), [ids[0]], "500", "500", uuid.uuid4().hex)
        self.assertEqual(self.store.occupied(), {ids[0]: second, ids[1]: first})
        self.assertEqual(self.store.batch(first), history)
        root = self.store.root
        self.store.close()
        self.store = Store(root)
        self.assertEqual(self.store.occupied(), {ids[0]: second, ids[1]: first})
        self.assertEqual(self.store.recommend("1")["available_cents"], 0)

    def test_manual_reimbursed_without_batch_cannot_be_selected(self):
        item = self.import_one()
        self.store.set_invoice_status(self.rev(), item["id"], "reimbursed")
        self.assertFalse(selectable(self.store.view()["invoices"][0]))
        self.assertEqual(self.store.recommend("1")["available_cents"], 0)
        with self.assertRaises(UserError):
            self.store.create_batch(self.rev(), [item["id"]], "500", "500", uuid.uuid4().hex)
        self.store.set_invoice_status(self.rev(), item["id"], "returned")
        self.assertEqual(self.store.recommend("500")["ids"], [item["id"]])

    def test_reserved_status_requires_a_batch(self):
        item = self.import_one()
        with self.assertRaises(UserError):
            self.store.set_invoice_status(self.rev(), item["id"], "reserved")

    def test_export_is_flat_and_handles_case_insensitive_filename_collisions(self):
        a = write_invoice(self.input / "a", "invoice.xml")
        b = write_invoice(self.input / "b", "INVOICE.xml", number="26000000000000000002")
        self.store.import_folder(self.rev(), str(self.input), recursive=True)
        ids = [i["id"] for i in self.store.data["invoices"]]
        batch = self.store.create_batch(self.rev(), ids, "1000", "1000", uuid.uuid4().hex)
        before = deepcopy(self.store.data)
        with zipfile.ZipFile(self.store.export(batch)) as archive:
            names = archive.namelist()
            self.assertEqual(len(names), 3)
            self.assertTrue(all("/" not in n and "\\" not in n for n in names))
            self.assertEqual(len({n.casefold() for n in names}), 3)
            records = list(csv.reader(io.StringIO(archive.read("明细.csv").decode("utf-8-sig"))))
            self.assertEqual(len(records), 3)
            hashes = {fingerprint(archive.read(n)) for n in names if n != "明细.csv"}
            self.assertEqual(hashes, {fingerprint(a.read_bytes()), fingerprint(b.read_bytes())})
        self.assertEqual(self.store.data, before)


class BusinessValidationToolTests(unittest.TestCase):
    def test_parameterized_validation_preserves_source_and_cleans_workspaces(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            original = write_invoice(source, "a.xml")
            write_invoice(source, "b.xml", number="26000000000000000002")
            (source / "duplicate.xml").write_bytes(original.read_bytes())
            before = hashes(source)
            test_root = root.parent
            directories = set(test_root.iterdir())
            result = validate(source)
            self.assertEqual(result["invoice_records"], 2)
            self.assertEqual(result["automatically_verified"], 2)
            self.assertEqual(result["source_duplicate_candidates"], 1)
            self.assertEqual(result["repeat_import_added"], 0)
            self.assertTrue(result["selection_export_and_return_checked"])
            self.assertEqual(result["copied_duplicates_deleted"], 2)
            self.assertEqual(hashes(source), before)
            self.assertEqual(set(test_root.iterdir()), directories)


@unittest.skipUnless(os.name == "nt", "需要 Windows 原生消息框")
class DesktopWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.app = Workbench(self.root / "workspace", self.root / "settings.json")
        self.app.withdraw()
        self.app.update()

    def tearDown(self):
        self.app.dirty = False
        self.app.close_app()
        self.temp.cleanup()

    def wait_task(self):
        deadline = time.monotonic() + 10
        while self.app.busy and time.monotonic() < deadline:
            self.app.update()
            time.sleep(0.01)
        self.assertFalse(self.app.busy)

    def import_duplicates(self, response):
        source = self.root / "source"
        original = write_invoice(source, "a.xml")
        copy = source / "b.xml"
        copy.write_bytes(original.read_bytes())
        answered = []
        def respond():
            dialogs = [w for w in self.app.winfo_children() if isinstance(w, DuplicateFilesDialog)]
            if not dialogs:
                self.app.after(20, respond)
                return
            dialog = dialogs[0]
            answered.append(len(dialog.tree.get_children()))
            (dialog.yes_button if response == 6 else dialog.no_button).invoke()
        self.app.after(20, respond)
        self.app.start_import(source)
        self.wait_task()
        self.assertEqual(answered, [1])
        self.assertEqual(len(self.app.tree.get_children()), 1)
        self.assertTrue(original.exists())
        return copy

    def test_duplicate_cleanup_choices_are_visible_before_file_list(self):
        self.app.deiconify()
        self.app.update()
        source = self.root / "source"
        original = write_invoice(source, "a.xml")
        duplicate = source / "b.xml"
        duplicate.write_bytes(original.read_bytes())
        dialog = DuplicateFilesDialog(self.app, str(source), [{
            "keep": str(original), "remove": str(duplicate), "hash": fingerprint(original.read_bytes())
        }])
        try:
            dialog.update_idletasks()
            self.app.update()
            for button in (dialog.yes_button, dialog.no_button):
                self.assertTrue(button.winfo_ismapped())
                self.assertLessEqual(button.winfo_y() + button.winfo_height(), dialog.winfo_height())
            self.assertLess(dialog.yes_button.winfo_rooty(), dialog.tree.winfo_rooty())
        finally:
            dialog.destroy()

    def test_declining_cleanup_keeps_files_and_continues_import(self):
        copy = self.import_duplicates(7)
        self.assertTrue(copy.exists())
        self.assertTrue(self.app.selected_item()["verification"]["passed"])

    def test_accepting_cleanup_deletes_only_the_duplicate(self):
        copy = self.import_duplicates(6)
        self.assertFalse(copy.exists())
        self.assertIn("已删除 1", self.app.status_text.get())

    def test_status_dropdown_releases_invoice_for_selection(self):
        source = self.root / "source"
        write_invoice(source)
        store = self.app.store
        store.set_company(store.data["revision"], BUYER, TAX)
        store.import_folder(store.data["revision"], str(source))
        invoice_id = store.data["invoices"][0]["id"]
        store.create_batch(store.data["revision"], [invoice_id], "500", "500", uuid.uuid4().hex)
        self.app.reload()
        self.app.load_invoice(self.app.snapshot["invoices"][0])
        self.app.invoice_status.set("已退回")
        self.app.status_choice.event_generate("<<ComboboxSelected>>")
        self.wait_task()
        self.assertEqual(store.invoice(invoice_id)["reimbursement_status"], "returned")
        self.assertEqual(self.app.stats[2].get(), "¥ 600.00")
        dialog = SelectionDialog(self.app)
        self.assertEqual([i["id"] for i in dialog.candidates], [invoice_id])
        dialog.destroy()
