from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from invoice_workbench.gui import Workbench
from invoice_workbench.selection import SelectionDialog
from .helpers import BUYER, TAX, review_all, write_invoice


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
        review_all(store)
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
