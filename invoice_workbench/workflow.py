from pathlib import Path
import os

from .domain import UserError
from .extract import SUPPORTED
from .filesystem import is_directory_link
from .store import fingerprint

MANAGED = {"objects", "history", "exports", "backups", ".git", ".venv", "node_modules", "__pycache__"}


def contains_invoices(directory):
    for base, dirs, names in os.walk(directory, followlinks=False):
        dirs[:] = [d for d in dirs if d not in MANAGED and not is_directory_link(Path(base) / d)]
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


def remove_duplicate_files(store, revision, report):
    source = Path(report["source"])
    with store.mutex:
        store._check()
        if revision != store.data["revision"]:
            raise UserError("资料已更新，请重新导入后整理重复文件。")
        if source.resolve() != source or is_directory_link(source):
            raise UserError("导入目录路径已变化，已停止删除。")
        removals = []
        for duplicate in report["duplicate_files"]:
            keep, remove = Path(duplicate["keep"]), Path(duplicate["remove"])
            for path in (keep, remove):
                if path.is_symlink() or not path.resolve().is_relative_to(source) or path.resolve().is_relative_to(store.root):
                    raise UserError("重复文件路径已变化或位于工作区，已停止删除。")
                if any(is_directory_link(parent) for parent in path.parents if parent.is_relative_to(source)):
                    raise UserError("重复文件目录已变为链接，已停止删除。")
                if fingerprint(path.read_bytes()) != duplicate["hash"]:
                    raise UserError(f"文件已变化，已停止删除：{path}")
            matching = [f for item in store.data["invoices"] for f in item["files"] if f["hash"] == duplicate["hash"]]
            if not matching:
                raise UserError("重复文件尚未存入工作区，已停止删除。")
            for file in matching:
                store.original(file)
            if keep.resolve() == remove.resolve():
                raise UserError("重复文件与保留文件路径相同，已停止删除。")
            removals.append((keep, remove, duplicate["hash"]))
        if {p.resolve() for _, p, _ in removals} & {p.resolve() for p, _, _ in removals}:
            raise UserError("保留文件被列入删除范围，已停止删除。")
        for keep, remove, digest in removals:
            store._check()
            if fingerprint(keep.read_bytes()) != digest or fingerprint(remove.read_bytes()) != digest:
                raise UserError("重复文件在整理期间发生变化，已停止删除。")
            remove.unlink()
        return len(removals)
