from contextlib import closing
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from uuid import uuid4

from invoice_workbench.domain import UserError
from invoice_workbench.store import Store
from invoice_workbench.workflow import contains_invoices, import_directory, remove_duplicate_files
from .helpers import BUYER, TAX, write_invoice


TEST_ROOT = Path(__file__).resolve().parent.parent / ".test-tmp"
SAMPLE = TEST_ROOT.parent / "samples" / "sample-01.xml"


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

    def test_scan_of_another_legacy_workspace_skips_its_managed_directories(self):
        with closing(Store(self.source)) as other:
            original = self.root / SAMPLE.name
            shutil.copyfile(SAMPLE, original)
            other.import_files(other.view()["revision"], [original])
            file, = other.view()["invoices"][0]["files"]
            archived = other.original(file)
            shutil.copyfile(SAMPLE, self.source / "000-copy.xml")
            shutil.copyfile(SAMPLE, self.source / "exports" / SAMPLE.name)

            report = self.scan()
            self.assertEqual((report["scanned"], report["imported"], report["duplicate_files"]), (1, 1, []))
            self.assertEqual(other.original(file), archived)
            self.assertEqual(archived.read_bytes(), SAMPLE.read_bytes())

    def test_default_workspace_scan_allows_cleanup_of_source_duplicates(self):
        workspace = self.store.workspace
        original, copy = workspace / "a.xml", workspace / "b.xml"
        for path in (original, copy):
            shutil.copyfile(SAMPLE, path)
        report = import_directory(self.store, self.store.view()["revision"], workspace, scan_workspace=True)
        self.assertEqual(remove_duplicate_files(self.store, self.store.view()["revision"], report), 1)
        self.assertFalse(copy.exists())
        self.assertEqual(original.read_bytes(), SAMPLE.read_bytes())
        file, = self.store.view()["invoices"][0]["files"]
        self.assertEqual(self.store.original(file).read_bytes(), SAMPLE.read_bytes())

    def test_cleanup_keeps_original_referenced_inside_legacy_library(self):
        library = self.root / "z-library"
        library.mkdir()
        outside, inside = self.source / SAMPLE.name, library / SAMPLE.name
        for path in (outside, inside):
            shutil.copyfile(SAMPLE, path)
        self.store.set_company(self.store.view()["revision"], BUYER, TAX)
        report = import_directory(self.store, self.store.view()["revision"], self.root)
        self.assertEqual(report["duplicate_files"][0]["remove"], str(inside))
        item, = self.store.view()["invoices"]
        batch = self.store.create_batch(self.store.view()["revision"], [item["id"]], "500", "500", uuid4().hex)
        self.store.set_library(self.store.view()["revision"], library)
        index = self.store.state_path.read_bytes()

        with self.assertRaisesRegex(UserError, "仍被工作区引用"):
            remove_duplicate_files(self.store, self.store.view()["revision"], report)
        self.assertEqual(self.store.state_path.read_bytes(), index)
        self.assertEqual(inside.read_bytes(), outside.read_bytes())

        report = import_directory(self.store, self.store.view()["revision"], self.root)
        self.assertEqual(remove_duplicate_files(self.store, self.store.view()["revision"], report), 1)
        self.assertFalse(outside.exists())
        self.assertEqual(self.store.original(self.store.invoice(item["id"])["files"][0]), inside)
        self.assertEqual(self.store.original(self.store.batch(batch)["items"][0]["files"][0]), inside)
        self.assertTrue(self.store.export(batch).is_file())

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
