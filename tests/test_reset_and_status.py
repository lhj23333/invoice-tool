import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
import uuid

from invoice_workbench.domain import UserError
from invoice_workbench.gui import Workbench
from invoice_workbench.store import Store
from .helpers import BUYER, TAX, StoreFixture, write_invoice
from . import test_review_fixes


class ResetStoreTests(StoreFixture, unittest.TestCase):
    def test_reset_clears_managed_data_and_keeps_sources_and_session(self):
        batch_id = self.make_batch()
        self.store.export(batch_id)
        original = (self.input / "invoice.xml").read_bytes()
        unrelated = self.store.root / "notes" / "keep.txt"
        unrelated.parent.mkdir()
        unrelated.write_text("用户保留的资料", encoding="utf-8")
        session = self.store.lock_path.read_bytes()
        revision = self.rev()
        self.store.reset(revision)
        snapshot = self.store.view()
        self.assertEqual(snapshot["invoices"], [])
        self.assertEqual(snapshot["batches"], [])
        self.assertEqual(snapshot["company"], {"name": "", "tax_id": ""})
        self.assertGreater(snapshot["revision"], revision)
        for name in ("objects", "history", "exports"):
            self.assertEqual(list((self.store.root / name).iterdir()), [])
        self.assertEqual(self.store.lock_path.read_bytes(), session)
        self.assertEqual(unrelated.read_text(encoding="utf-8"), "用户保留的资料")
        self.assertEqual((self.input / "invoice.xml").read_bytes(), original)
        with self.assertRaises(UserError):
            Store(self.store.root)
        root = self.store.root
        self.store.close()
        self.store = Store(root)
        self.assertEqual(self.store.view()["invoices"], [])
        self.store.reset(self.rev())
        self.assertEqual(list((root / "history").iterdir()), [])

    def test_reset_accepts_new_imports_and_company_settings(self):
        self.import_one()
        self.store.reset(self.rev())
        self.store.set_company(self.rev(), BUYER, TAX)
        result = self.store.import_folder(self.rev(), str(self.input))
        self.assertEqual(result["imported"], 1)
        self.assertEqual(self.store.recommend("500")["total_cents"], 60000)

    def test_stale_revision_and_external_change_stop_reset(self):
        self.import_one()
        before = self.store.state_path.read_bytes()
        objects = list((self.store.root / "objects").iterdir())
        with self.assertRaises(UserError):
            self.store.reset(self.rev() - 1)
        self.assertEqual(self.store.state_path.read_bytes(), before)
        changed = before + b"\n"
        self.store.state_path.write_bytes(changed)
        with self.assertRaises(UserError):
            self.store.reset(self.rev())
        self.assertEqual(self.store.state_path.read_bytes(), changed)
        self.assertTrue(all(p.exists() for p in objects))

    @unittest.skipUnless(os.name == "nt", "需要 Windows 文件共享权限")
    def test_locked_original_stops_reset_before_index_changes(self):
        item = self.import_one()
        original = self.store.original(item["files"][0])
        before = self.store.state_path.read_bytes()
        with original.open("rb") as held:
            with self.assertRaisesRegex(UserError, "文件被占用"):
                self.store.reset(self.rev())
            self.assertEqual(self.store.state_path.read_bytes(), before)
            self.assertEqual(held.read(), original.read_bytes())
        self.store.reset(self.rev())
        self.assertEqual(self.store.view()["invoices"], [])

    @unittest.skipUnless(os.name == "nt", "需要 Windows junction")
    def test_linked_managed_directory_stops_reset_without_deleting_target(self):
        self.import_one()
        before = self.store.state_path.read_bytes()
        original = self.store.root / "objects"
        moved = self.root / "saved-objects"
        original.rename(moved)
        subprocess.run(["cmd", "/c", "mklink", "/J", str(original), str(moved)], check=True, capture_output=True)
        files = {p.name: p.read_bytes() for p in moved.iterdir()}
        with self.assertRaises(UserError):
            self.store.reset(self.rev())
        self.assertEqual(self.store.state_path.read_bytes(), before)
        self.assertEqual({p.name: p.read_bytes() for p in moved.iterdir()}, files)

    @unittest.skipUnless(os.name == "nt", "需要 Windows junction")
    def test_nested_junction_stops_reset_before_scanning_target(self):
        self.import_one()
        before = self.store.state_path.read_bytes()
        junction = self.store.root / "objects" / "cycle"
        subprocess.run(["cmd", "/c", "mklink", "/J", str(junction), str(self.store.root / "objects")], check=True, capture_output=True)
        with self.assertRaisesRegex(UserError, "内部存在链接"):
            self.store.reset(self.rev())
        self.assertEqual(self.store.state_path.read_bytes(), before)


@unittest.skipUnless(os.name == "nt", "需要 Windows 桌面和原生消息框")
class ResetAndStatusGuiTests(unittest.TestCase):
    answer_dialog = test_review_fixes.WorkspaceReconnectTests.answer_dialog

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.settings_file = self.root / "settings.json"
        self.app = Workbench(self.root / "workspace", self.settings_file)
        self.app.withdraw()
        source = self.root / "source"
        write_invoice(source, "a.xml")
        write_invoice(source, "b.xml", number="26000000000000000002")
        store = self.app.store
        store.set_company(store.data["revision"], BUYER, TAX)
        store.import_folder(store.data["revision"], str(source))
        self.app.reload()
        self.app.load_invoice(self.app.snapshot["invoices"][0])
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
        self.app.update()

    def test_detail_opening_and_unchanged_selection_do_not_write_status(self):
        before = self.app.store.state_path.read_bytes()
        for item in self.app.snapshot["invoices"] * 2:
            self.app.tree.selection_set(item["id"])
            self.app.select_invoice()
            self.app.status_choice.event_generate("<<ComboboxSelected>>")
            self.app.update()
            self.assertFalse(self.app.busy)
        self.assertEqual(self.app.store.state_path.read_bytes(), before)

    def test_mouse_wheel_scrolls_detail_without_changing_status(self):
        before = self.app.store.state_path.read_bytes()
        status = self.app.invoice_status.get()
        for delta in (-120, 120, -360):
            self.app.status_choice.event_generate("<MouseWheel>", delta=delta)
            self.app.update()
            self.assertEqual(self.app.invoice_status.get(), status)
            self.assertFalse(self.app.busy)
        self.assertEqual(self.app.store.state_path.read_bytes(), before)
        self.assertNotIn("已选入批次", self.app.status_choice["values"])

    def test_deliberate_dropdown_change_updates_status(self):
        invoice_id = self.app.current_id
        self.app.invoice_status.set("已提交")
        self.app.status_choice.event_generate("<<ComboboxSelected>>")
        self.wait_task()
        self.assertEqual(self.app.store.invoice(invoice_id)["reimbursement_status"], "submitted")

    def test_used_status_without_batch_disables_archive_and_field_editing(self):
        for label in ("已提交", "已报销"):
            self.app.invoice_status.set(label)
            self.app.status_choice.event_generate("<<ComboboxSelected>>")
            self.wait_task()
            self.assertEqual(str(self.app.archive_button.cget("state")), "disabled")
            self.assertEqual(str(self.app.review_button.cget("state")), "disabled")
            self.assertTrue(all(str(entry.cget("state")) == "readonly" for entry in self.app.entries))
            self.assertEqual(str(self.app.status_choice.cget("state")), "readonly")
        self.app.invoice_status.set("已退回")
        self.app.status_choice.event_generate("<<ComboboxSelected>>")
        self.wait_task()
        self.assertEqual(str(self.app.archive_button.cget("state")), "normal")
        self.assertEqual(str(self.app.review_button.cget("state")), "normal")

    def test_declining_reset_preserves_data_settings_and_unsaved_edits(self):
        state = self.app.store.state_path.read_bytes()
        settings = self.settings_file.read_bytes()
        self.app.fields["note"].set("保留尚未保存的备注")
        self.answer_dialog(self.app.reset_button.invoke, "恢复初始化", 7)
        self.assertFalse(self.app.busy)
        self.assertEqual(self.app.store.state_path.read_bytes(), state)
        self.assertEqual(self.settings_file.read_bytes(), settings)
        self.assertTrue(self.app.dirty)

    def test_confirmed_reset_clears_database_settings_and_visible_state(self):
        store = self.app.store
        invoice_id = store.data["invoices"][0]["id"]
        batch = store.create_batch(store.data["revision"], [invoice_id], "500", "500", uuid.uuid4().hex)
        store.export(batch)
        self.app.settings["last_import_directory"] = str(self.root / "source")
        self.app.settings["extra_setting"] = "待清除"
        self.app._save_settings()
        self.app.reload()
        self.app.batch_tree.selection_set(batch)
        self.app.show_batch()
        self.app.search.set("原有搜索")
        source = {p.name: p.read_bytes() for p in (self.root / "source").iterdir()}
        workspace = store.root
        self.answer_dialog(self.app.reset_button.invoke, "恢复初始化", 6)
        self.wait_task()
        self.assertEqual(store.root, workspace)
        self.assertEqual(store.view()["invoices"], [])
        self.assertEqual(store.view()["batches"], [])
        self.assertEqual(self.app.tree.get_children(), ())
        self.assertEqual(self.app.batch_tree.get_children(), ())
        self.assertEqual(self.app.batch_info.get("1.0", "end").strip(), "")
        self.assertEqual(self.app.company_name.get(), "")
        self.assertEqual(self.app.company_tax.get(), "")
        self.assertEqual(self.app.search.get(), "")
        self.assertEqual(self.app.filter.get(), "全部")
        self.assertEqual(self.app.stats[0].get(), "0")
        self.assertIsNone(self.app.current_id)
        self.assertFalse(self.app.dirty)
        expected = {"data_directory": str(workspace)}
        self.assertEqual(self.app.settings, expected)
        self.assertEqual(json.loads(self.settings_file.read_text(encoding="utf-8")), expected)
        self.assertEqual({p.name: p.read_bytes() for p in (self.root / "source").iterdir()}, source)
        self.app.close_app()
        self.app = Workbench(settings_file=self.settings_file)
        self.app.withdraw()
        self.assertEqual(self.app.store.root, workspace)
        self.assertEqual(self.app.snapshot["invoices"], [])
