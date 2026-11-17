import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from invoice_workbench.store import Store
from invoice_workbench.workflow import contains_invoices
from .helpers import write_invoice


TEST_ROOT = Path(__file__).resolve().parent.parent / ".test-tmp"


class DirectoryScanTests(unittest.TestCase):
    def setUp(self):
        TEST_ROOT.mkdir(exist_ok=True)
        temp = tempfile.TemporaryDirectory(dir=TEST_ROOT)
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.store = Store(self.root / "workspace")
        self.addCleanup(self.store.close)

    def scan(self):
        return self.store.import_folder(self.store.data["revision"], str(self.source), recursive=True)

    def test_ordinary_subdirectories_are_scanned_and_imported(self):
        nested = self.source / "nested" / "invoices"
        nested.mkdir(parents=True)
        self.assertFalse(contains_invoices(self.source))
        invoice = write_invoice(nested)
        original = invoice.read_bytes()
        self.assertTrue(contains_invoices(self.source))
        result = self.scan()
        self.assertEqual(result["scanned"], 1)
        self.assertEqual(result["imported"], 1)
        self.assertEqual(result["errors"], [])
        self.assertEqual(invoice.read_bytes(), original)
        item = self.store.view()["invoices"][0]
        self.assertEqual(item["sources"], [str(invoice)])
        self.assertEqual(self.store.original(item["files"][0]).read_bytes(), original)

    def test_managed_subdirectories_are_skipped(self):
        for name in (".git", ".venv", "node_modules", "__pycache__"):
            write_invoice(self.source / "nested" / name)
        self.assertFalse(contains_invoices(self.source))
        self.assertEqual(self.scan()["scanned"], 0)

    @unittest.skipUnless(os.name == "nt", "需要 Windows 目录联接")
    def test_junctions_outside_source_and_back_to_source_are_skipped(self):
        outside = self.root / "outside"
        invoice = write_invoice(outside)
        original = invoice.read_bytes()
        for name, target in (("external", outside), ("cycle", self.source)):
            link = self.source / name
            subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)],
                           check=True, capture_output=True, timeout=10)
            self.addCleanup(link.rmdir)
        self.assertFalse(contains_invoices(self.source))
        self.assertEqual(self.scan()["scanned"], 0)
        nested = write_invoice(self.source / "nested")
        self.assertTrue(contains_invoices(self.source))
        result = self.scan()
        self.assertEqual(result["scanned"], 1)
        self.assertEqual(result["imported"], 1)
        self.assertEqual(result["errors"], [])
        self.assertEqual(self.store.view()["invoices"][0]["sources"], [str(nested)])
        self.assertEqual(invoice.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
