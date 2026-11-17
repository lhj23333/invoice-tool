"""Behavior regressions from the initial code review."""
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from invoice_workbench.domain import UserError
from invoice_workbench.gui import Workbench
from invoice_workbench.selection import SelectionDialog
from .helpers import BUYER, TAX, StoreFixture, review_all, write_invoice


class OriginalRecoveryTests(StoreFixture, unittest.TestCase):
    def test_missing_original_is_restored_for_an_existing_batch(self):
        batch_id = self.make_batch()
        item = self.store.data["invoices"][0]
        file = item["files"][0]
        path = self.store.original(file)
        original = path.read_bytes()
        path.unlink()
        report = self.store.import_folder(self.rev(), str(self.input))
        self.assertEqual(report["duplicates"], 1)
        self.assertEqual(report["errors"], [])
        self.assertEqual(self.store.original(file).read_bytes(), original)
        self.assertEqual(len(self.store.data["invoices"]), 1)
        self.assertTrue(self.store.invoice(item["id"])["reviewed"])
        self.assertTrue(self.store.export(batch_id).is_file())

    def test_corrupt_original_is_reported_without_overwriting(self):
        item = self.import_one()
        path = self.store.original(item["files"][0])
        source = (self.input / "invoice.xml").read_bytes()
        path.write_bytes(b"externally modified")
        report = self.store.import_folder(self.rev(), str(self.input))
        self.assertEqual(report["duplicates"], 0)
        self.assertEqual(len(report["errors"]), 1)
        self.assertEqual(path.read_bytes(), b"externally modified")
        self.assertEqual((self.input / "invoice.xml").read_bytes(), source)

    def test_external_index_change_stops_missing_original_recovery(self):
        item = self.import_one()
        path = self.store.original(item["files"][0])
        path.unlink()
        changed = self.store.state_path.read_bytes() + b"\n"
        self.store.state_path.write_bytes(changed)
        with self.assertRaises(UserError):
            self.store.import_folder(self.rev(), str(self.input))
        self.assertFalse(path.exists())
        self.assertEqual(self.store.state_path.read_bytes(), changed)


class DraftAndSelectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.app = Workbench(self.root / "data", self.root / "settings.json")
        self.app.withdraw()
        source = self.root / "source"
        write_invoice(source, "a.xml", number="26000000000000000001", amount="600.00")
        write_invoice(source, "b.xml", number="26000000000000000002", amount="1000.00")
        store = self.app.store
        store.import_folder(store.data["revision"], str(source))
        store.set_company(store.data["revision"], BUYER, TAX)
        review_all(store)
        self.app.reload()
        self.app.load_invoice(self.app.snapshot["invoices"][0])

    def tearDown(self):
        self.app.dirty = False
        self.app.close_app()
        self.temp.cleanup()

    def wait_task(self):
        deadline = time.monotonic() + 5
        while self.app.busy and time.monotonic() < deadline:
            self.app.update()
            time.sleep(0.01)
        self.assertFalse(self.app.busy, "background task did not complete")

    def test_saving_company_preserves_draft_until_invoice_is_saved(self):
        self.app.fields["note"].set("unsaved note")
        invoice_id = self.app.current_id
        self.app.pages.select(2)
        with patch("invoice_workbench.gui.messagebox.askyesno") as prompt:
            self.app.save_company()
            self.wait_task()
            prompt.assert_not_called()
        self.assertEqual(self.app.fields["note"].get(), "unsaved note")
        self.assertTrue(self.app.dirty)
        self.assertEqual(self.app.store.invoice(invoice_id)["note"], "")
        self.app.save_review()
        self.wait_task()
        self.assertEqual(self.app.store.invoice(invoice_id)["note"], "unsaved note")
        self.assertFalse(self.app.dirty)

    def test_failed_invoice_save_preserves_draft(self):
        self.app.fields["amount"].set("invalid")
        self.app.fields["note"].set("keep this draft")
        with patch("invoice_workbench.gui.messagebox.showerror") as error, self.assertLogs(level="ERROR"):
            self.app.save_review()
            self.wait_task()
            error.assert_called_once()
        self.assertEqual(self.app.fields["amount"].get(), "invalid")
        self.assertEqual(self.app.fields["note"].get(), "keep this draft")
        self.assertTrue(self.app.dirty)

    def test_archive_respects_declined_draft_discard(self):
        self.app.fields["note"].set("keep this draft")
        with patch("invoice_workbench.gui.messagebox.askyesno", return_value=False):
            self.app.archive_invoice()
        self.assertFalse(self.app.busy)
        self.assertFalse(self.app.selected_item()["archived"])
        self.assertTrue(self.app.dirty)

    def test_old_recommendation_is_discarded_after_target_changes(self):
        dialog = SelectionDialog(self.app)
        dialog.withdraw()
        entered, release = threading.Event(), threading.Event()
        recommend = self.app.store.recommend

        def delayed(*args):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("recommendation test timed out")
            return recommend(*args)

        try:
            with patch.object(self.app.store, "recommend", side_effect=delayed):
                dialog.calculate()
                self.assertTrue(entered.wait(2))
                dialog.target.set("600.00")
                release.set()
                self.wait_task()
            self.assertIsNone(dialog.result)
            self.assertEqual(dialog.selected_ids, [])
            self.assertEqual(str(dialog.confirm.cget("state")), "disabled")
            dialog.calculate()
            self.wait_task()
            self.assertEqual(dialog.result["total_cents"], 60000)
            self.assertEqual(dialog.declared.get(), "600.00")
            self.assertEqual(str(dialog.confirm.cget("state")), "normal")
        finally:
            release.set()
            if self.app.busy:
                self.wait_task()
            dialog.destroy()
