"""Copy first, verify SHA-256, and only then allow imports from the backup."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import hashlib
import json
import os
import shutil
import uuid

from invoice_workbench.domain import UserError


def file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def inventory(root: Path) -> dict[str, tuple[int, int]]:
    items = {}
    for base, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            p = Path(base) / name
            if p.is_symlink() or p.is_junction():
                raise UserError("目录含符号链接或联接点，无法保证完整备份。请选择实际文件所在目录。")
        for name in files:
            p = Path(base) / name
            st = p.stat()
            items[p.relative_to(root).as_posix()] = (st.st_size, st.st_mtime_ns)
    return items


def verified_backup(source: str | Path, backup_parent: str | Path, progress=None) -> dict:
    source = Path(source).expanduser().resolve()
    parent = Path(backup_parent).expanduser().resolve()
    if not source.is_dir():
        raise UserError("待备份的发票目录不存在。")
    if parent.is_relative_to(source):
        raise UserError("备份位置不能位于原目录内部，请选择另一个目录。")
    before = inventory(source)
    if not before:
        raise UserError("目录内没有文件。")
    parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    destination = parent / f"invoice-backup-{stamp}-{uuid.uuid4().hex[:8]}"
    files_root = destination / "files"
    files_root.mkdir(parents=True)
    for base, dirs, _ in os.walk(source, followlinks=False):
        for name in dirs:
            (files_root / (Path(base) / name).relative_to(source)).mkdir(parents=True, exist_ok=True)
    manifest_path = destination / "backup-manifest.json"
    manifest = {"schema": 1, "source": str(source), "created": datetime.now(timezone.utc).isoformat(),
                "complete": False, "file_count": len(before), "total_bytes": 0, "files": []}
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    for n, relative in enumerate(sorted(before), 1):
        original, copy = source / relative, files_root / relative
        copy.parent.mkdir(parents=True, exist_ok=True)
        digest = file_hash(original)
        shutil.copy2(original, copy)
        if file_hash(copy) != digest or file_hash(original) != digest:
            raise UserError(f"备份校验未通过：{relative}。本次未开始导入，已保留不完整备份供检查。")
        size = copy.stat().st_size
        manifest["files"].append({"path": relative, "size": size, "sha256": digest})
        manifest["total_bytes"] += size
        if progress:
            progress(f"备份并校验 {n}/{len(before)}：{relative}")
    if inventory(source) != before:
        raise UserError("备份期间原目录发生变化，本次未开始导入。请等下载和网盘同步完成后重试。")
    # Check again after the copy of every file: a previously copied file may have changed.
    for entry in manifest["files"]:
        if file_hash(source / entry["path"]) != entry["sha256"]:
            raise UserError("备份期间原文件发生变化，本次未开始导入。请重试。")
    manifest["complete"] = True
    manifest["verified"] = datetime.now(timezone.utc).isoformat()
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return {**manifest, "directory": str(destination), "files_directory": str(files_root), "manifest_path": str(manifest_path)}

