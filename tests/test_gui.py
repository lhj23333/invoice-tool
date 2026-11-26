import json
from email.message import EmailMessage
from pathlib import Path
import tempfile
from threading import Event
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from invoice_workbench.gui import GENERAL_SETTINGS, MAIL_SETTINGS, Workbench
from invoice_workbench.import_dialogs import MAIL_MODE
from invoice_workbench.selection import SelectionDialog
from .helpers import BUYER, TAX, write_invoice, xml_invoice


class GuiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.app = Workbench(self.root / "data", self.root / "settings.json")
        self.app.withdraw()
        self.app.update()

    def tearDown(self):
        self.app.dirty = False
        self.app.close_app()
        self.temp.cleanup()

    def test_empty_window_builds_and_closes(self):
        self.app.deiconify()
        self.app.update()
        self.assertEqual(len(self.app.tree.get_children()), 0)
        self.assertEqual(self.app.stats[0].get(), "0")
        self.assertTrue(self.app.tree.winfo_viewable())
        self.assertTrue(self.app.intake_button.winfo_viewable())
        self.assertFalse(self.app.empty_card.winfo_viewable())

        self.app.search_entry.insert(0, "发票")
        self.app.update_idletasks()
        self.assertFalse(self.app.empty_card.winfo_viewable())

    def test_xml_analysis_record_displays_without_a_stored_original(self):
        source = self.root / "source"
        source.mkdir()
        message = EmailMessage()
        message.set_content("电子发票")
        message.add_attachment(xml_invoice(), maintype="application", subtype="xml", filename="invoice.xml")
        (source / "incoming.eml").write_bytes(message.as_bytes())
        self.app.store.import_folder(self.app.snapshot["revision"], str(source))
        self.app.reload()
        item, = self.app.snapshot["invoices"]
        self.app.tree.selection_set(item["id"])
        self.app.select_invoice()
        self.assertEqual(self.app.tree.item(item["id"], "values")[0], "invoice.xml")
        self.assertIsNone(self.app.selected_file())
        self.assertEqual(str(self.app.recognize_button.cget("state")), "disabled")
        self.assertIn("请补充可核验的发票原件", self.app.raw.get("1.0", "end"))

    def test_outlook_client_id_is_saved_restored_and_cleared(self):
        client_id = str(uuid4())
        settings_file = self.app.settings_file
        workspace = self.app.store.root
        self.app.deiconify()
        self.app.open_settings(MAIL_SETTINGS)
        self.app.mail_address.set("name@outlook.com")
        self.app.update()
        save_button = next(button for button in self.app.actions if button.cget("text") == "保存收票设置")
        entry = self.app.mail_client_entry
        self.assertIs(entry.master.master, self.app.mail_advanced.content)
        self.assertFalse(self.app.mail_advanced.expanded)
        self.assertFalse(entry.winfo_viewable())
        self.app.mail_advanced.header.invoke()
        self.app.update_idletasks()
        self.assertTrue(self.app.mail_advanced.expanded)
        self.assertTrue(entry.winfo_viewable())
        entry.insert(0, f" {client_id.upper()} ")
        self.app.mail_advanced.header.invoke()
        self.app.update_idletasks()
        self.assertFalse(entry.winfo_viewable())
        save_button.invoke()
        self.assertEqual(json.loads(settings_file.read_text(encoding="utf-8"))["mail_client_id"], client_id)
        self.assertNotIn(client_id, (workspace / "invoices.json").read_text(encoding="utf-8"))
        self.assertEqual(self.app.mail_feedback.cget("style"), "success.SettingsFeedback.TLabel")

        self.app.close_app()
        self.app = Workbench(workspace, settings_file)
        self.app.withdraw()
        self.app.open_settings(MAIL_SETTINGS)
        self.assertEqual(self.app.mail_client_id.get(), client_id)
        self.app.mail_client_id.set("")
        save_button = next(button for button in self.app.actions if button.cget("text") == "保存收票设置")
        save_button.invoke()
        self.assertEqual(json.loads(settings_file.read_text(encoding="utf-8"))["mail_client_id"], "")

    def test_mail_settings_route_and_keep_invalid_input_until_corrected(self):
        directory = self.root / "library"
        directory.mkdir()
        self.app.store.set_library(self.app.snapshot["revision"], directory)
        self.app.reload()
        self.app.deiconify()
        self.app.open_import(MAIL_MODE)
        dialog = self.app.import_dialog
        self.assertTrue(dialog.confirm.instate(["disabled"]))
        dialog.change_settings.invoke()
        tabs = self.app.settings_pages
        self.assertEqual(self.app.pages.current_index, 2)
        self.assertEqual(tabs.variable.get(), MAIL_SETTINGS)
        self.assertIsNone(self.app.import_dialog)
        self.assertTrue(self.app.mail_clear_button.instate(["disabled"]))

        self.app.mail_address_entry.insert(0, "name@outlook.com")
        self.assertIn("Outlook · OAuth2 登录", self.app.mail_provider_pill.cget("text"))
        self.assertIn("未保存", self.app.mail_feedback.cget("text"))
        self.app.mail_advanced.header.invoke()
        self.app.mail_port.set("0")
        settings_before = self.app.settings_file.read_bytes()
        save_button = next(button for button in self.app.actions if button.cget("text") == "保存收票设置")
        save_button.invoke()
        self.assertEqual(self.app.mail_feedback.cget("style"), "error.SettingsFeedback.TLabel")
        self.assertEqual(self.app.settings_file.read_bytes(), settings_before)

        tabs.control.buttons[0].invoke()
        self.assertEqual(tabs.variable.get(), GENERAL_SETTINGS)
        self.assertFalse(tabs.frames[1].grid_info())
        tabs.control.buttons[1].invoke()
        self.assertTrue(tabs.frames[1].grid_info())
        self.assertTrue(self.app.mail_advanced.expanded)
        self.assertEqual(self.app.mail_port.get(), "0")
        self.app.mail_port.set("993")
        save_button.invoke()
        self.assertEqual(self.app.mail_feedback.cget("style"), "success.SettingsFeedback.TLabel")
        self.assertEqual(json.loads(self.app.settings_file.read_text(encoding="utf-8"))["mail_port"], 993)

    def test_provider_detection_opens_advanced_for_custom_server_and_preserves_draft(self):
        app = self.app
        app.deiconify()
        app.open_settings(MAIL_SETTINGS)
        app.mail_address_entry.insert(0, "name@QQ.COM")
        app.update()
        self.assertIn("QQ 邮箱 · 默认 imap.qq.com:993", app.mail_provider_pill.cget("text"))
        self.assertFalse(app.mail_advanced.expanded)
        self.assertFalse(app.mail_client_entry.winfo_viewable())

        app.mail_address.set("name@example.com")
        app.update_idletasks()
        self.assertIn("其他邮箱", app.mail_provider_pill.cget("text"))
        self.assertTrue(app.mail_advanced.expanded)
        self.assertTrue(app.mail_client_entry.winfo_viewable())
        app.mail_host.set("imap.example.com")
        app.mail_port.set("1993")
        self.assertIn("imap.example.com:1993", app.mail_provider_pill.cget("text"))
        app.mail_advanced.header.invoke()
        app.mail_address_entry.event_generate("<FocusOut>")
        self.assertFalse(app.mail_advanced.expanded)

        app.mail_address.set("name@163.com")
        app.update_idletasks()
        self.assertIn("网易 163 邮箱", app.mail_provider_pill.cget("text"))
        self.assertFalse(app.mail_advanced.expanded)
        self.assertFalse(app.mail_client_entry.winfo_viewable())
        self.assertEqual((app.mail_host.get(), app.mail_port.get()), ("imap.example.com", "1993"))

    def test_cancel_mail_connection_releases_window_before_worker_returns(self):
        cancelled, release, completed = Event(), Event(), Event()

        def connect():
            release.wait(5)
            completed.set()

        self.app.task("正在连接 Outlook IMAP", connect, cancelled=cancelled, connecting_mail=True)
        try:
            self.assertIn("正在连接", self.app.mail_session_pill.cget("text"))
            self.app.cancel_task_button.invoke()
            self.assertTrue(cancelled.is_set())
            self.assertFalse(self.app.busy)
            self.assertFalse(completed.is_set())
            self.assertEqual(self.app.cancel_task_button.winfo_manager(), "")
            self.assertEqual(self.app.mail_session_pill.cget("text"), "未连接")
        finally:
            release.set()
            self.assertTrue(completed.wait(2))

    def test_stale_task_progress_from_previous_task_is_ignored(self):
        stale_id, stale_emit = self.app._task_channel()
        release = Event()
        self.app.task("新任务", release.wait)
        try:
            self.app.update()
            stale_emit("progress", "过期进度")
            time.sleep(0.15)
            self.app.update()
            self.assertEqual(self.app.status_text.get(), "新任务")
        finally:
            release.set()
            deadline = time.monotonic() + 5
            while self.app.busy and time.monotonic() < deadline:
                self.app.update()
                time.sleep(0.01)
            self.assertFalse(self.app.busy)

    def test_cancel_discards_late_events_from_the_cancelled_task(self):
        cancelled = Event()
        self.app.task("正在收票", cancelled.wait, cancelled=cancelled)
        stale_id = self.app.active_task_id
        self.app.update()
        self.app.cancel_task_button.invoke()
        self.assertEqual(self.app.status_text.get(), "已取消收票任务。")
        self.app.events.put((stale_id, "done", ("结果", None)))
        self.app.events.put((stale_id, "progress", "过期进度"))
        cancelled.set()
        time.sleep(0.15)
        self.app.update()
        self.assertFalse(self.app.busy)
        self.assertEqual(self.app.status_text.get(), "已取消收票任务。")

    def test_native_close_releases_workspace_while_connecting_mail(self):
        cancelled = Event()
        workspace = self.app.store
        settings_file = self.app.settings_file
        self.app.task("正在连接 Outlook IMAP", lambda: cancelled.wait(5), cancelled=cancelled)
        try:
            self.app.tk.call(self.app.protocol("WM_DELETE_WINDOW"))
            self.assertTrue(cancelled.is_set())
            self.assertTrue(workspace.closed)
        finally:
            if workspace.closed:
                self.app = Workbench(workspace.root, settings_file)

    def test_fields_selection_and_preview_xml(self):
        source = self.root / "source"
        write_invoice(source)
        self.app.store.import_folder(self.app.store.data["revision"], str(source))
        self.app.reload()
        item_id = self.app.tree.get_children()[0]
        self.app.tree.selection_set(item_id)
        self.app.select_invoice()
        self.assertEqual(self.app.fields["buyer_name"].get(), BUYER)
        self.app.details.select(1)
        self.app.update()
        self.app.render_preview()
        self.assertTrue(self.app.preview_canvas.find_all())

    def test_selection_dialog_and_recommendation(self):
        source = self.root / "source"
        write_invoice(source, amount="1002.50")
        store = self.app.store
        store.import_folder(store.data["revision"], str(source))
        store.set_company(store.data["revision"], BUYER, TAX)
        self.app.reload()
        dialog = SelectionDialog(self.app)
        self.app.update()
        result = store.recommend("1000.00")
        dialog.calculated(result)
        self.assertIn("2.50", dialog.summary.get())
        self.assertEqual(str(dialog.confirm.cget("state")), "normal")
        dialog.target.set("1001.00")
        self.assertEqual(str(dialog.confirm.cget("state")), "disabled")
        dialog.destroy()

    def test_pdf_and_image_preview(self):
        samples = Path(__file__).resolve().parent.parent / "samples"
        self.app.store.import_folder(self.app.store.data["revision"], str(samples), recursive=True)
        self.app.reload()
        pdf_item = next(i for i in self.app.snapshot["invoices"] if any(f["name"].endswith(".pdf") for f in i["files"]))
        self.app.load_invoice(pdf_item)
        self.app.details.select(1)
        self.app.update()
        self.app.file_choice.current(next(n for n, f in enumerate(pdf_item["files"]) if f["name"].endswith(".pdf")))
        self.app.render_preview()
        self.assertIsNotNone(self.app.image_reference)
        image_item = next(i for i in self.app.snapshot["invoices"] if any(f["name"].endswith(".png") for f in i["files"]))
        self.app.load_invoice(image_item)
        self.app.render_preview()
        self.assertIsNotNone(self.app.image_reference)


if __name__ == "__main__":
    unittest.main()
