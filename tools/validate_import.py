"""Read-only source checks plus a real desktop import into an explicitly named workspace."""
from pathlib import Path
import argparse
import hashlib
import json
import sys
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from invoice_workbench.gui import Workbench


def hashes(source):
    return {p.relative_to(source).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in source.rglob("*") if p.is_file()}


def validate(source, workspace, settings_file, output):
    before = hashes(source)
    app = Workbench(workspace, settings_file)
    app.withdraw()
    errors = []
    def wait():
        end = time.monotonic() + 60
        while app.busy and time.monotonic() < end:
            app.update()
            time.sleep(0.01)
        if app.busy:
            raise RuntimeError("Import exceeded the verification deadline")
        app.update()
    try:
        with patch("invoice_workbench.gui.messagebox.showerror", side_effect=lambda *a, **k: errors.append(str(a))), \
                patch("invoice_workbench.gui.messagebox.showwarning", side_effect=lambda *a, **k: errors.append(str(a))):
            app.start_import(source)
            wait()
            first_status = app.status_text.get()
            first_count = len(app.tree.get_children())
            records = list(app.snapshot["invoices"])
            previews = 0
            for item in records:
                app.load_invoice(item)
                app.details.select(1)
                app.update()
                app.render_preview()
                previews += app.image_reference is not None
                for file in item["files"]:
                    if hashlib.sha256(app.store.original(file).read_bytes()).hexdigest() != file["hash"]:
                        raise AssertionError("Stored original changed")
            app.start_import(source)
            wait()
            repeat_count = len(app.tree.get_children())
            repeat_status = app.status_text.get()
            # The same missing-marker condition as the reported dialog.
            app.store.lock_path.unlink()
            if not app.reload():
                raise AssertionError("Missing-marker recovery failed")
    finally:
        app.dirty = False
        app.close_app()
    reopened = Workbench(workspace, settings_file)
    reopened.withdraw()
    try:
        reopened.update()
        persisted = len(reopened.tree.get_children())
    finally:
        reopened.close_app()
    after = hashes(source)
    report = {"source_files": len(before), "invoice_records": first_count,
              "repeat_records": repeat_count, "reopened_records": persisted,
              "pdf_previews": previews, "source_bytes_and_paths_unchanged": before == after,
              "automatic_backup_created": (workspace / "backups").exists(),
              "extracted_numbers": sum(bool(i.get("number")) for i in records),
              "extracted_dates": sum(bool(i.get("date")) for i in records),
              "extracted_amounts": sum(bool(i.get("amount_cents")) for i in records),
              "extracted_buyers": sum(bool(i.get("buyer_name")) for i in records),
              "extracted_tax_ids": sum(bool(i.get("buyer_tax_id")) for i in records),
              "errors": errors, "first_import_status": first_status, "repeat_import_status": repeat_status}
    assert not errors, errors
    assert before == after
    assert first_count == repeat_count == persisted and first_count > 0
    assert not report["automatic_backup_created"]
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--settings-file", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    validate(args.source.resolve(), args.workspace.resolve(), args.settings_file.resolve(), args.output.resolve())
