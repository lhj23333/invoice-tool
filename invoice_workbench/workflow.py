from pathlib import Path
import json
import os
from tempfile import TemporaryDirectory
from time import perf_counter

from .domain import UserError
from .extract import SUPPORTED, file_format
from .filesystem import is_directory_link
from .mail import CollectionCancelled, MAIL_SCREENING_VERSION, collect
from .store import ARCHIVE_DIRECTORIES, INTAKE_EXCLUDED_DIRECTORIES, fingerprint

MANAGED = ARCHIVE_DIRECTORIES | INTAKE_EXCLUDED_DIRECTORIES


def contains_invoices(directory):
    for base, dirs, names in os.walk(directory, followlinks=False):
        dirs[:] = [d for d in dirs if d not in MANAGED and not is_directory_link(Path(base) / d)]
        if any(Path(n).suffix.lower() in SUPPORTED | {".eml"} for n in names):
            return True
    return False


def import_directory(store, revision, source, progress=None, *, scan_workspace=False):
    source = Path(source).expanduser().resolve()
    if source == store.root and not (scan_workspace and source == store.workspace):
        raise UserError("请使用「扫描当前工作区新增文件」导入工作区中的发票。")
    if progress:
        progress("正在读取发票并生成记录…")
    return store.import_folder(revision, str(source), recursive=True, progress=progress)


def import_mailbox(store, revision, mailbox, folder, source, bounds, blocked, progress=None, *,
                   deep_scan=False, force_rescan=False, cancelled=None):
    started = perf_counter()
    scope = json.dumps((*mailbox.profile, folder), ensure_ascii=False)
    policy = fingerprint(json.dumps((MAIL_SCREENING_VERSION, sorted(blocked))).encode("utf-8"))
    previous = {} if force_rescan else store.mail_checkpoint(scope, policy)
    checkpoint = {"scope": scope, "policy": policy}
    with TemporaryDirectory(prefix="invoice-mail-") as directory:
        stats, rows = collect(mailbox, folder, bounds, blocked, Path(directory), progress,
                              deep_scan=deep_scan, previous=previous, checkpoint=checkpoint,
                              cancelled=cancelled)
        if cancelled is not None and cancelled.is_set():
            raise CollectionCancelled()
        stats["screen_seconds"] = round(perf_counter() - started, 2)
        return store.import_mail(revision, source, stats.pop("scanned"), rows, progress,
                                 screening=stats, checkpoint=checkpoint)


def _check_duplicate_path(store, source, path):
    if path.is_symlink() or path.resolve() != path or not path.is_relative_to(source):
        raise UserError("重复文件路径已变化，已停止删除。")
    if any(is_directory_link(parent) for parent in path.parents if parent.is_relative_to(source)):
        raise UserError("重复文件目录已变为链接，已停止删除。")
    if store._is_intake_excluded(path.parent):
        raise UserError("重复文件位于程序管理的目录，已停止删除。")


def remove_duplicate_files(store, revision, report):
    source = Path(report["source"])
    with store.mutex:
        store._check()
        if revision != store.data["revision"]:
            raise UserError("资料已更新，请重新导入后整理重复文件。")
        if source.resolve() != source or is_directory_link(source):
            raise UserError("导入目录路径已变化，已停止删除。")
        removals = {}
        for duplicate in report["duplicate_files"]:
            keep, remove = Path(duplicate["keep"]), Path(duplicate["remove"])
            for path in (keep, remove):
                _check_duplicate_path(store, source, path)
                if fingerprint(path.read_bytes()) != duplicate["hash"]:
                    raise UserError(f"文件已变化，已停止删除：{path}")
            matching = [f for item in store.data["invoices"] for f in item["files"] if f["hash"] == duplicate["hash"]]
            if not matching:
                raise UserError("工作区缺少对应原件，已停止删除。")
            for file in matching:
                if file_format(keep) != file_format(store.original(file)):
                    raise UserError("保留文件的扩展名与可核验原件不一致，已停止删除。")
            replacement = (keep, duplicate["hash"])
            if removals.setdefault(remove, replacement) != replacement:
                raise UserError("同一重复文件对应多个保留文件，已停止删除。")
        if removals.keys() & {keep for keep, _ in removals.values()}:
            raise UserError("保留文件被列入删除范围，已停止删除。")
        if not removals:
            return 0
        references = []
        for file, _ in store._file_references():
            remove = store._original_path(file)
            if remove not in removals:
                continue
            keep, digest = removals[remove]
            if (file.get("storage") != "library" or not keep.is_relative_to(store.library)
                    or digest != file["hash"] or file_format(keep) != file_format(file["path"])):
                raise UserError(f"文件仍被工作区引用，无法更新原件路径，已停止删除：{remove}")
            references.append((file, keep.relative_to(store.library).as_posix()))
        if references:
            # 先保存全部原件引用，再删除副本。
            with store.change(revision):
                for file, relative in references:
                    file["path"] = relative
        for remove, (keep, digest) in removals.items():
            store._check()
            _check_duplicate_path(store, source, keep)
            _check_duplicate_path(store, source, remove)
            if fingerprint(keep.read_bytes()) != digest or fingerprint(remove.read_bytes()) != digest:
                raise UserError("重复文件在整理期间发生变化，已停止删除。")
            remove.unlink()
        return len(removals)
