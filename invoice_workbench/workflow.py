"""Shared import workflow for the desktop UI and integration checks."""
from pathlib import Path
import os

from .domain import UserError
from .extract import SUPPORTED

MANAGED = {"objects", "history", "exports", "backups", ".git", ".venv", "node_modules", "__pycache__"}


def contains_invoices(directory):
    for base, dirs, names in os.walk(directory, followlinks=False):
        dirs[:] = [d for d in dirs if d not in MANAGED and not (Path(base) / d).is_symlink()
                   and not (Path(base) / d).is_junction()]
        if any(Path(n).suffix.lower() in SUPPORTED | {".eml"} for n in names):
            return True
    return False


def import_directory(store, revision, source, progress=None):
    source = Path(source).expanduser().resolve()
    if source == store.root or source in store.root.parents or any(
            source.is_relative_to(store.root / d) for d in MANAGED):
        raise UserError("请选择存放原始发票的文件夹，不要选择资料库或其内部目录。")
    if progress:
        progress("正在读取发票并生成记录…")
    return store.import_folder(revision, str(source), recursive=True)
