import json
from email.message import EmailMessage
from pathlib import Path
import tempfile
from threading import Event
from tkinter import ttk
import unittest
from unittest.mock import patch
from uuid import uuid4

from invoice_workbench.gui import Workbench
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
        self.assertEqual(len(self.app.tree.get_children()), 0)
        self.assertEqual(self.app.stats[0].get(), "0")

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
        self.app.pages.select(2)
        self.app.mail_address.set("name@outlook.com")
        save_button = next(button for button in self.app.actions if button.cget("text") == "保存收票设置")
        entry = next(widget for widget in save_button.master.master.winfo_children()
                     if isinstance(widget, ttk.Entry) and widget.cget("textvariable") == str(self.app.mail_client_id))
        entry.insert(0, f" {client_id.upper()} ")
        save_button.invoke()
        self.assertEqual(json.loads(settings_file.read_text(encoding="utf-8"))["mail_client_id"], client_id)
        self.assertNotIn(client_id, (workspace / "invoices.json").read_text(encoding="utf-8"))

        self.app.close_app()
        self.app = Workbench(workspace, settings_file)
        self.app.withdraw()
        self.app.pages.select(2)
        self.assertEqual(self.app.mail_client_id.get(), client_id)
        self.app.mail_client_id.set("")
        save_button = next(button for button in self.app.actions if button.cget("text") == "保存收票设置")
        save_button.invoke()
        self.assertEqual(json.loads(settings_file.read_text(encoding="utf-8"))["mail_client_id"], "")

    def test_cancel_mail_connection_releases_window_before_worker_returns(self):
        cancelled, release, completed = Event(), Event(), Event()

        def connect():
            release.wait(5)
            completed.set()

        self.app.task("正在连接 Outlook IMAP", connect, cancelled=cancelled)
        try:
            self.app.cancel_task_button.invoke()
            self.assertTrue(cancelled.is_set())
            self.assertFalse(self.app.busy)
            self.assertFalse(completed.is_set())
            self.assertEqual(self.app.cancel_task_button.winfo_manager(), "")
        finally:
            release.set()
            self.assertTrue(completed.wait(2))

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
