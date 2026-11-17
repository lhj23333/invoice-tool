"""Tests for the development-only verified copy helper."""
from pathlib import Path
import json
import tempfile
import unittest

from invoice_workbench.domain import UserError
from tools.backup import file_hash, verified_backup
from .helpers import write_invoice


class BackupTests(unittest.TestCase):
    def test_complete_backup_preserves_source_and_subdirectories(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source"
            p = write_invoice(source)
            (source / "nested").mkdir()
            (source / "empty-directory").mkdir()
            (source / "nested" / "other.dat").write_bytes(b"not only invoices")
            old_hash = file_hash(p)
            report = verified_backup(source, root / "backups")
            self.assertTrue(report["complete"])
            self.assertEqual(report["file_count"], 2)
            self.assertEqual(file_hash(p), old_hash)
            self.assertTrue((Path(report["files_directory"]) / "empty-directory").is_dir())
            manifest = json.loads(Path(report["manifest_path"]).read_text(encoding="utf-8"))
            self.assertTrue(manifest["complete"])
            for file in report["files"]:
                self.assertEqual(file_hash(Path(report["files_directory"]) / file["path"]), file["sha256"])

    def test_nested_backup_destination_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp)
            write_invoice(source)
            with self.assertRaises(UserError):
                verified_backup(source, source / "backups")

    def test_change_during_backup_prevents_success(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source"
            write_invoice(source)
            def mutate(_):
                (source / "new-file.dat").write_bytes(b"arrived during backup")
            with self.assertRaises(UserError):
                verified_backup(source, root / "backups", mutate)
            manifests = list((root / "backups").glob("*/backup-manifest.json"))
            self.assertFalse(json.loads(manifests[0].read_text(encoding="utf-8"))["complete"])

