from pathlib import Path
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from invoice_workbench.domain import UserError
from invoice_workbench.gui import Workbench
from invoice_workbench.session import process_running
from invoice_workbench.store import Store
from invoice_workbench.workflow import import_directory
from .helpers import write_invoice

TEST_ROOT = Path(__file__).resolve().parent.parent / ".test-tmp"
TEST_ROOT.mkdir(exist_ok=True)


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=TEST_ROOT)
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "data")

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_missing_marker_restores_without_losing_index(self):
        before = self.store.state_path.read_bytes()
        self.store.lock_path.unlink()
        self.assertEqual(self.store.view()["invoices"], [])
        self.assertEqual(json.loads(self.store.lock_path.read_text())["session"], self.store.lock_id)
        self.assertEqual(self.store.state_path.read_bytes(), before)

    def test_missing_marker_cannot_open_second_writer_or_force_recover(self):
        self.store.lock_path.unlink()
        for recover in (False, True):
            with self.assertRaises(UserError):
                Store(self.store.root, recover_lock=recover)
        self.store.view()

    def test_missing_marker_with_changed_index_stays_blocked(self):
        self.store.lock_path.unlink()
        modified = self.store.state_path.read_bytes() + b"\n"
        self.store.state_path.write_bytes(modified)
        with self.assertRaises(UserError):
            self.store.view()
        self.assertFalse(self.store.lock_path.exists())
        self.assertEqual(self.store.state_path.read_bytes(), modified)

    def test_replaced_marker_is_never_overwritten(self):
        marker = {"session": "another-computer", "host": "other", "pid": 1}
        self.store.lock_path.write_text(json.dumps(marker))
        with self.assertRaises(UserError):
            self.store.view()
        self.store.close()
        self.assertEqual(json.loads(self.store.lock_path.read_text()), marker)

    def test_corrupt_marker_requires_explicit_recovery(self):
        self.store.lock_path.write_text("not-json")
        with self.assertRaises(UserError):
            self.store.view()
        self.store.close()
        with self.assertRaises(UserError):
            Store(self.store.root)
        self.store = Store(self.store.root, recover_lock=True)
        self.assertTrue(list(self.store.root.glob("recovered-session-*.json")))

    def test_only_definitely_dead_local_process_auto_recovers(self):
        directory = self.store.root
        self.store.close()
        with subprocess.Popen([sys.executable, "-B", "-c", "pass"]) as process:
            self.assertEqual(process.wait(timeout=10), 0)
        marker = {"session": "old", "host": socket.gethostname(), "pid": process.pid}
        (directory / ".invoice-session.json").write_text(json.dumps(marker))
        self.store = Store(directory)
        self.assertNotEqual(self.store.lock_id, "old")
        self.assertTrue(list(directory.glob("recovered-session-*.json")))

    def test_foreign_session_never_auto_recovers(self):
        directory = self.store.root
        self.store.close()
        marker = {"session": "remote", "host": "another-host", "pid": 999999}
        (directory / ".invoice-session.json").write_text(json.dumps(marker))
        with self.assertRaises(UserError):
            Store(directory)
        self.assertEqual(json.loads((directory / ".invoice-session.json").read_text()), marker)

    def test_current_process_is_alive(self):
        self.assertTrue(process_running(os.getpid()))
        self.assertIsNone(process_running(None))


class ImportWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=TEST_ROOT)
        self.root = Path(self.temp.name)
        self.app = Workbench(self.root / "data", self.root / "settings.json")
        self.app.withdraw()
        self.app.update()

    def tearDown(self):
        self.app.dirty = False
        self.app.close_app()
        self.temp.cleanup()

    def wait_import(self, app=None):
        app = app or self.app
        deadline = time.monotonic() + 10
        while app.busy and time.monotonic() < deadline:
            app.update()
            time.sleep(0.01)
        self.assertFalse(app.busy, "background import did not complete")
        app.update()

    def test_gui_import_resets_filters_and_preserves_source_files(self):
        source = self.root / "source"
        invoice = write_invoice(source / "nested")
        original = invoice.read_bytes()
        initial = {p.relative_to(source) for p in source.rglob("*")}
        self.app.search.set("no match")
        self.app.filter.set("可用")
        self.app.start_import(source)
        self.wait_import()
        self.assertEqual(len(self.app.tree.get_children()), 1)
        self.assertEqual(self.app.stats[0].get(), "1")
        self.assertIn("新增 1", self.app.status_text.get())
        self.assertEqual(self.app.filter.get(), "全部")
        self.assertEqual(self.app.search.get(), "")
        self.assertEqual(initial, {p.relative_to(source) for p in source.rglob("*")})
        self.assertEqual(invoice.read_bytes(), original)
        self.assertEqual(self.app.selected_item()["sources"], [str(invoice)])
        self.app.start_import(source)
        self.wait_import()
        self.assertEqual(len(self.app.tree.get_children()), 1)
        self.assertIn("已有 1", self.app.status_text.get())

    def test_change_workspace_copies_legacy_index_and_archived_original(self):
        source = self.root / "source"
        original = write_invoice(source)
        payload = original.read_bytes()
        previous = self.app.store
        previous.set_library(self.app.snapshot["revision"], source)
        previous.import_folder(previous.data["revision"], source)
        index_before = previous.state_path.read_bytes()
        self.app.reload()
        destination = self.root / "archive"
        destination.mkdir()
        self.app.change_workspace(destination)
        self.wait_import()
        self.assertEqual(self.app.store.workspace, destination)
        self.assertEqual(self.app.directory_text.get(), str(destination))
        self.assertEqual(len(self.app.tree.get_children()), 1)
        item, = self.app.snapshot["invoices"]
        file, = item["files"]
        archived = self.app.store.original(file)
        self.assertTrue(archived.is_relative_to(destination))
        self.assertTrue(self.app.store.state_path.is_relative_to(destination))
        self.assertEqual(archived.read_bytes(), payload)
        self.assertEqual(original.read_bytes(), payload)
        self.assertEqual(previous.state_path.read_bytes(), index_before)
        self.assertFalse((source / "invoices.json").exists())
        config = self.app.settings_file
        self.app.close_app()
        self.app = Workbench(settings_file=config)
        self.app.withdraw()
        self.assertEqual(self.app.store.workspace, destination)
        self.assertEqual(self.app.store.original(file), archived)

    def test_import_dialog_separates_external_folder_and_file_sources_from_archive(self):
        source = self.root / "external"
        first = write_invoice(source, "first.xml")
        second = write_invoice(source, "second.xml", number="26000000000000000002")
        third = write_invoice(source, "third.xml", number="26000000000000000003")
        originals = {p: p.read_bytes() for p in (first, second, third)}
        archive = self.app.store.workspace
        self.app.deiconify()
        self.app.intake_button.invoke()
        dialog = self.app.import_dialog
        self.assertTrue(dialog.confirm.instate(["disabled"]))
        self.assertIn(str(archive), dialog.archive_target.cget("text"))
        dialog._set_files((str(first), str(third)))
        dialog.confirm.invoke()
        self.wait_import()
        self.assertEqual(len(self.app.snapshot["invoices"]), 2)
        self.assertEqual(self.app.last_import_report["kind"], "files")
        self.assertEqual(self.app.settings["last_import_directory"], str(source))
        self.app.intake_button.invoke()
        dialog = self.app.import_dialog
        dialog.recent_directory.invoke()
        self.assertEqual(dialog.directory.get(), str(source))
        dialog.confirm.invoke()
        self.wait_import()
        self.assertEqual(len(self.app.snapshot["invoices"]), 3)
        self.assertEqual(self.app.last_import_report["imported"], 1)
        for item in self.app.snapshot["invoices"]:
            file, = item["files"]
            self.assertTrue(self.app.store.original(file).is_relative_to(archive))
        self.assertEqual({p: p.read_bytes() for p in originals}, originals)
        self.assertEqual(self.app.store.workspace, archive)
        self.app.intake_button.invoke()
        dialog = self.app.import_dialog
        dialog.change_target.invoke()
        self.assertIsNone(self.app.import_dialog)
        self.assertEqual(self.app.settings_pages.current_index, 0)

    def test_scan_workspace_shortcut_omits_archived_objects(self):
        workspace = self.app.store.workspace
        invoice = write_invoice(workspace)
        self.app.deiconify()
        self.app.intake_button.invoke()
        dialog = self.app.import_dialog
        dialog.scan_button.invoke()
        dialog.confirm.invoke()
        self.wait_import()
        self.assertEqual(self.app.last_import_report["scanned"], 1)
        self.assertEqual(self.app.last_import_report["imported"], 1)
        self.app.intake_button.invoke()
        dialog = self.app.import_dialog
        dialog.scan_button.invoke()
        dialog.confirm.invoke()
        self.wait_import()
        self.assertEqual(self.app.last_import_report["scanned"], 1)
        self.assertEqual(self.app.last_import_report["imported"], 0)
        self.assertTrue(invoice.is_file())

    def test_legacy_source_setting_migrates_without_writing_to_source(self):
        source = self.root / "legacy-source"
        write_invoice(source)
        settings = self.root / "legacy-settings.json"
        settings.write_text(json.dumps({"data_directory": str(source)}))
        with patch("invoice_workbench.gui.APP_ROOT", self.root / "new-app"):
            app = Workbench(settings_file=settings)
        try:
            app.withdraw()
            app.update()
            self.wait_import(app)
            self.assertEqual(len(app.tree.get_children()), 1)
            self.assertNotEqual(app.store.root, source)
            self.assertFalse((source / "invoices.json").exists())
        finally:
            app.close_app()

    def test_empty_folder_has_clear_result_without_index_write(self):
        source = self.root / "empty"
        source.mkdir()
        self.app.store.set_library(self.app.snapshot["revision"], source)
        self.app.reload()
        before = self.app.store.state_path.read_bytes()
        self.app.start_import(source)
        self.wait_import()
        self.assertIn("没有找到支持的文件", self.app.status_text.get())
        self.assertEqual(before, self.app.store.state_path.read_bytes())

    def test_source_cannot_be_the_workspace(self):
        with self.assertRaises(UserError):
            import_directory(self.app.store, self.app.snapshot["revision"], self.app.store.root)

    def test_partial_failure_still_shows_successful_invoice(self):
        source = self.root / "source"
        write_invoice(source)
        (source / "bad.pdf").write_bytes(b"not a PDF")
        self.app.start_import(source)
        self.wait_import()
        self.assertTrue(self.app.import_banner.grid_info())
        self.assertIn("本次新增 1 张", self.app.import_result.cget("text"))
        self.assertIn("1 条异常", self.app.import_result.cget("text"))
        self.assertEqual(len(self.app.tree.get_children()), 1)
        self.assertIn("失败 1", self.app.status_text.get())

    def test_reconnect_reloads_changed_index_without_stale_widgets(self):
        state = json.loads(self.app.store.state_path.read_text())
        state["revision"] += 1
        self.app.store.state_path.write_text(json.dumps(state))
        self.assertFalse(self.app.reload())
        self.assertFalse(self.app.session_ok)
        self.app.reconnect_library()
        self.assertTrue(self.app.session_ok)
        self.assertEqual(self.app.snapshot["revision"], state["revision"])

    def test_only_one_navigation_and_no_extra_exit_button(self):
        self.assertEqual([button.cget("text") for button in self.app.nav_buttons], ["发票资料", "报销批次", "设置"])
        self.assertNotEqual(self.app.pages.winfo_class(), "TNotebook")
        self.assertEqual(self.app.stats_frame.master, self.app.pages.frames[0])
        self.app.pages.select(1)
        self.assertEqual(self.app.page_title.get(), "报销批次")
        self.assertFalse(self.app.pages.frames[0].grid_info())
        self.assertTrue(self.app.pages.frames[1].grid_info())
        self.app.pages.select(2)
        self.assertEqual(self.app.page_title.get(), "设置")
        self.assertFalse(self.app.pages.frames[1].grid_info())
        texts = []
        def visit(widget):
            if widget.winfo_class() == "TButton":
                texts.append(widget.cget("text"))
            for child in widget.winfo_children():
                visit(child)
        visit(self.app)
        self.assertNotIn("退出工具", texts)
        self.assertEqual(texts.count("发票资料"), 1)
        self.assertEqual(texts.count("报销批次"), 1)
        self.assertEqual(texts.count("设置"), 1)

    def test_details_can_collapse_for_full_width_invoice_list(self):
        self.assertFalse(self.app.detail_visible)
        source = self.root / "source"
        write_invoice(source)
        self.app.start_import(source)
        self.wait_import()
        self.assertTrue(self.app.detail_visible)
        self.app.hide_detail()
        self.assertFalse(self.app.detail_visible)
        self.assertEqual(len(self.app.splitter.panes()), 1)
        self.app.show_detail()
        self.assertEqual(len(self.app.splitter.panes()), 2)

    def test_mapped_selection_allocates_visible_detail_pane(self):
        source = self.root / "source"
        write_invoice(source)
        self.app.store.import_folder(self.app.snapshot["revision"], str(source))
        self.app.reload()
        self.app.deiconify()
        self.app.update()
        self.app.tree.selection_set(self.app.tree.get_children()[0])
        self.app.update()
        self.assertTrue(self.app.detail_visible)
        self.assertGreater(self.app.detail_pane.winfo_width(), 250)
        self.assertLess(self.app.splitter.sashpos(0), self.app.splitter.winfo_width() - 250)


if __name__ == "__main__":
    unittest.main()
