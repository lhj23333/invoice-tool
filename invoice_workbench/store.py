from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
from itertools import count
from pathlib import Path
import csv
import hashlib
import io
import json
import os
import re
import shutil
import socket
from tempfile import TemporaryDirectory
import threading
from time import perf_counter
import uuid
import zipfile

from .domain import UserError, choose_invoices, company_matches, eligible_invoice, identity, money, validate_invoice, yuan
from .extract import (MAX_BYTES, MAX_MESSAGE_BYTES, MULTIPLE_INVOICE_IDENTIFIERS_WARNING, SUPPORTED,
                      SUSPECT_DUPLICATE_WARNING, eml_attachments, extract, file_format, has_invoice_content,
                      has_invoice_title, has_required_fields, inspect_mail_document)
from .filesystem import check_delete_access, is_directory_link
from .session import WriterGuard, process_running
from .verification import FIELDS, TITLES, comparable, original_evidence, verify_record

INVOICE_STATUSES = {"available", "reserved", "submitted", "reimbursed", "returned"}
OCCUPIED_STATUSES = {"reserved", "submitted", "reimbursed"}
MAIL_FILE_PRIORITY = {".pdf": 0, ".ofd": 1, ".png": 2, ".jpg": 2, ".jpeg": 2, ".xml": 3}
MAIL_ORIGINALS = ".invoice-mail"
WORKSPACE_DATABASE = ".invoice-workbench"
INVOICE_INDEX = "invoices.json"
ARCHIVE_DIRECTORIES = {"objects", "history", "exports", "backups"}
MAX_HISTORY_FILES = 50
MAX_RECOVERED_SESSIONS = 5
INTAKE_EXCLUDED_DIRECTORIES = {".git", ".venv", "node_modules", "__pycache__", MAIL_ORIGINALS, WORKSPACE_DATABASE}


def _is_legacy_database(directory):
    return (directory / INVOICE_INDEX).is_file() and (directory / "objects").is_dir()


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def uid() -> str:
    return uuid.uuid4().hex


def fingerprint(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def safe_name(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).rstrip(". ")[:120] or "invoice"


def csv_cell(value) -> str:
    text = str(value)
    return "'" + text if text.lstrip().startswith(("=", "+", "-", "@")) or text.startswith(("\t", "\r")) else text


def _record_identity(item: dict, *, allow_unconfirmed=False) -> str | None:
    # 旧索引中的单文件记录可能只有提取警告，仍可据此识别多票原件。
    if item.get("multiple_invoice_identifiers") or (
        len(item["files"]) == 1 and MULTIPLE_INVOICE_IDENTIFIERS_WARNING in item.get("warnings", ())
    ):
        return None
    if (not allow_unconfirmed and item.get("ocr_unconfirmed")
            and item.get("reimbursement_status") not in OCCUPIED_STATUSES):
        return None
    return identity(item)


def _revision_order(path):
    """Sort history snapshots by version prefix; newest first when reversed."""
    prefix = path.name.partition("-")[0]
    try:
        revision = int(prefix)
    except ValueError:
        revision = -1
    try:
        return revision, path.stat().st_mtime_ns
    except OSError:
        return revision, 0


def _mtime_order(path):
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return 0


def _prune_oldest(paths, keep, order):
    """Keep the newest entries; locked or already removed files are skipped."""
    if len(paths) <= keep:
        return
    for path in sorted(paths, key=order, reverse=True)[keep:]:
        try:
            path.unlink()
        except OSError:
            pass


def _protects_identity(item):
    return not item.get("archived") or item.get("reimbursement_status") in OCCUPIED_STATUSES


def _conflicting_fields(first, second):
    return [TITLES[field] for field in FIELDS
            if first.get(field) and second.get(field) and comparable(field, first[field]) != comparable(field, second[field])]


class _ImportIndex:
    def __init__(self, invoices):
        self.positions = {}
        self.identity_keys = {}
        self.identities = {}
        self.originals = {}
        self.analyses = {}
        self.evidence = {}
        self.file_reads = {}
        self.mail_targets = {}
        for item in invoices:
            self.update_identity(item)
            for file in item["files"]:
                self.add_file(item, file["hash"])
            for entry in item.get("mail_xml", ()):
                self.add_file(item, entry["hash"], analyze_only=True)

    def update_identity(self, item):
        invoice_id = item["id"]
        position = self.positions.setdefault(invoice_id, len(self.positions))
        previous = self.identity_keys.get(invoice_id)
        current = _record_identity(item, allow_unconfirmed=True)
        self.mail_targets.pop(previous, None)
        self.mail_targets.pop(current, None)
        if previous and previous != current:
            del self.identities[previous][position]
            if not self.identities[previous]:
                del self.identities[previous]
        if current:
            self.identities.setdefault(current, {})[position] = item
            self.identity_keys[invoice_id] = current
        else:
            self.identity_keys.pop(invoice_id, None)

    def match_identity(self, key):
        return next((item for item in self.matching_invoices(key)
                     if _protects_identity(item) and _record_identity(item)), None)

    def matching_invoices(self, key):
        group = self.identities.get(key, {})
        return (group[position] for position in sorted(group))

    def add_file(self, item, digest, *, analyze_only=False):
        indexes = (self.analyses,) if analyze_only else (self.originals, self.analyses)
        for index in indexes:
            previous = index.get(digest)
            if previous is None or self.positions[item["id"]] < self.positions[previous["id"]]:
                index[digest] = item

    def remove_file(self, item, digest):
        for index in (self.originals, self.analyses):
            if index.get(digest) is item:
                del index[digest]

    def cached_file(self, path, digest):
        key = path, digest
        try:
            stat = path.stat()
        except FileNotFoundError:
            stat = None
        version = (stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_ino) if stat else None
        cached = self.file_reads.get(key)
        return key, version, cached[1] if cached is not None and version == cached[0] else None

    def cache_file(self, key, version, data):
        if len(self.file_reads) >= 8:
            self.file_reads.pop(next(iter(self.file_reads)))
        self.file_reads[key] = version, data

    def read_original(self, store, file):
        path = store._original_path(file)
        key, version, data = self.cached_file(path, file["hash"])
        if data is not None:
            return path, data
        _, data = store._read_original(file)
        self.cache_file(key, version, data)
        return path, data

    def read_staged(self, path, expected):
        key, version, data = self.cached_file(path, expected)
        if data is not None and expected:
            return data, expected
        data = path.read_bytes()
        digest = fingerprint(data)
        if expected and digest != expected:
            raise UserError("收取的附件与筛选时的内容不一致，请重新收票。")
        if expected:
            self.cache_file(key, version, data)
        return data, digest


def _mail_file_suffix(row):
    # 下载失败时记录的是链接标签，只有实际取得的文件和已知附件可以参与格式筛选。
    if row.get("payload") is not None or row.get("attachment"):
        return Path(row.get("name", row["file"])).suffix.lower()
    return ""


def _mail_review_key(row, digest=""):
    origin = [row.get(key) for key in ("mailbox", "imap_host", "imap_port", "folder",
                                       "imap_uidvalidity", "imap_uid", "message_id")]
    part = row.get("mail_part") or (row["file"], digest)
    return fingerprint(json.dumps([origin, part], ensure_ascii=False).encode("utf-8"))


class Store:
    """One active local writer; cloud drive synchronization remains sequential."""

    def __init__(self, directory: str | Path, recover_lock: bool = False, *, require_existing: bool = False):
        directory = Path(directory).expanduser()
        self.root = directory.resolve()
        self.state_path = self.root / INVOICE_INDEX
        require_existing = require_existing or WORKSPACE_DATABASE in {directory.name.lower(), self.root.name.lower()}
        if require_existing and not self.state_path.is_file():
            raise UserError(f"工作区缺少发票索引 {INVOICE_INDEX}。请确认目录可用且已同步，再点击「重新连接」。")
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock_path = self.root / ".invoice-session.json"
        self.lock_id = uid()
        self.mutex = threading.RLock()
        self.closed = False
        self.guard = WriterGuard(self.root / ".invoice-writer.lock")
        try:
            if self.lock_path.exists():
                try:
                    previous = json.loads(self.lock_path.read_text(encoding="utf-8"))
                except (ValueError, OSError):
                    previous = {}
                if not isinstance(previous, dict):
                    previous = {}
                stale_local = (previous.get("host") == socket.gethostname()
                               and process_running(previous.get("pid")) is False)
                if recover_lock or stale_local:
                    self.lock_path.rename(self.root / f"recovered-session-{uid()}.json")
                    _prune_oldest([p for p in self.root.glob("recovered-session-*.json")
                                  if p.is_file() and not p.is_symlink()],
                                 MAX_RECOVERED_SESSIONS, _mtime_order)
            self._write_marker()
        except Exception as exc:
            self.guard.close()
            if isinstance(exc, FileExistsError):
                raise UserError("工作区有未结束的会话。请关闭其他设备上的工作区，等同步完成后点击「重新连接」。") from None
            raise
        try:
            for folder in ("objects", "history", "exports"):
                (self.root / folder).mkdir(exist_ok=True)
            if require_existing or self.state_path.exists():
                raw = self.state_path.read_bytes()
                self.data = json.loads(raw)
                if self.data.get("schema") not in {1, 2, 3, 4, 5, 6} or not isinstance(self.data.get("invoices"), list) or not isinstance(self.data.get("batches"), list):
                    raise UserError("资料格式不兼容，请保留原文件并检查备份。")
                self.expected = fingerprint(raw)
                if self.data["schema"] == 1:
                    self._upgrade_invoices()
                if self.data["schema"] < 3:
                    for item in self.data["invoices"]:
                        if not item.get("reviewed") and (item.get("method") == "仅保存原件" or
                                any("当前未接入 OCR" in warning for warning in item.get("warnings", []))):
                            item["ocr_unconfirmed"] = True
                    self.data["schema"] = 3
                    self.data.setdefault("import_runs", [])
                    self._save()
            else:
                self.data = {"schema": 3, "revision": 0, "company": {"name": "", "tax_id": ""},
                             "invoices": [], "batches": [], "import_runs": []}
                self.expected = None
                self._save()
        except Exception:
            self.close()
            raise

    def close(self):
        if self.closed:
            return
        try:
            lock = json.loads(self.lock_path.read_text(encoding="utf-8"))
            if lock.get("session") == self.lock_id:
                self.lock_path.unlink()
        except (OSError, ValueError):
            pass
        finally:
            self.closed = True
            self.guard.close()

    def _write_marker(self):
        # Exclusive creation never overwrites a marker from another computer.
        with self.lock_path.open("x", encoding="utf-8") as f:
            json.dump({"session": self.lock_id, "host": socket.gethostname(),
                       "pid": os.getpid(), "started": now()}, f)
            f.flush()
            os.fsync(f.fileno())

    def _check(self):
        if self.closed:
            raise UserError("资料会话已关闭，请重新启动工具。")
        raw = self.state_path.read_bytes() if self.state_path.exists() else None
        current = fingerprint(raw) if raw is not None else None
        if current != self.expected:
            raise UserError("发票索引已被外部修改，已停止写入。请核对索引后点击「重新连接」。")
        # Common sync conflict copies are not silently treated as a separate empty library.
        conflicts = [p for p in self.root.glob("invoices*.json") if p.name != INVOICE_INDEX]
        if conflicts:
            raise UserError("发现索引冲突副本。请处理冲突后点击「重新连接」。")
        try:
            lock = json.loads(self.lock_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            # The local OS guard is still held and the index has not changed.
            # Restore only an absent marker, never corrupt/replaced metadata.
            try:
                self._write_marker()
                lock = {"session": self.lock_id}
            except FileExistsError:
                raise UserError("工作区会话已变化，请点击「重新连接」。") from None
        except (OSError, ValueError):
            raise UserError("无法读取工作区会话。请等同步完成后点击「重新连接」。") from None
        if not isinstance(lock, dict) or lock.get("session") != self.lock_id:
            raise UserError("另一会话已接管资料目录，本次已停止写入。请等同步完成后重新连接。")
        return raw

    def _save(self, *, keep_history=True):
        previous = self._check()
        new_data = deepcopy(self.data)
        if "review_cleanup" in new_data:
            new_data["review_cleanup"] = {path: digest for path, digest in new_data["review_cleanup"].items()
                                          if (self.root / path).is_file()}
        new_data["revision"] += 1
        raw = json.dumps(new_data, ensure_ascii=False, indent=2).encode("utf-8")
        if keep_history and previous is not None:
            snapshot = self.root / "history" / f"{self.data['revision']:08d}-{uid()}.json"
            snapshot.write_bytes(previous)
        temp = self.root / f".invoices-{uid()}.tmp"
        try:
            with temp.open("xb") as f:
                f.write(raw)
                f.flush()
                os.fsync(f.fileno())
            self._check()
            os.replace(temp, self.state_path)
        finally:
            temp.unlink(missing_ok=True)
        self.data = new_data
        self.expected = fingerprint(raw)
        if keep_history:
            # 历史只用于回看近期版本，旧的按版本号滚动清理，避免长期占用同步目录。
            _prune_oldest(self._history_snapshots(), MAX_HISTORY_FILES, _revision_order)

    def _history_snapshots(self):
        directory = self.root / "history"
        if directory.is_symlink() or (directory.is_dir() and is_directory_link(directory)):
            return []
        return [p for p in directory.glob("*.json") if p.is_file() and not p.is_symlink()]

    @contextmanager
    def change(self, revision: int, *, keep_history=True):
        with self.mutex:
            self._check()
            if revision != self.data["revision"]:
                raise UserError("页面资料已更新，请刷新后重试。")
            before = deepcopy(self.data)
            try:
                yield
                self._save(keep_history=keep_history)
            except Exception:
                self.data = before
                raise

    def verify(self):
        """Fail fast when read-only UI work raced an external index change."""
        with self.mutex:
            self._check()

    def view(self, *, include_import_runs=True) -> dict:
        with self.mutex:
            self._check()
            data = deepcopy({key: value for key, value in self.data.items() if include_import_runs or key != "import_runs"})
            occupied = self.occupied()
            duplicates = self.duplicate_ids()
            for i in data["invoices"]:
                i["batch_id"] = occupied.get(i["id"])
                i["company_match"] = company_matches(i, data["company"])
                i["duplicate"] = i["id"] in duplicates
            data["data_directory"] = str(self.root)
            return data

    def import_history(self):
        with self.mutex:
            self._check()
            reports = deepcopy(self.data["import_runs"][::-1])
            reviews = self.data.get("mail_reviews", {})
            for report in reports:
                for row in report["items"]:
                    if review := reviews.get(row.get("review_id")):
                        row.update(deepcopy(review))
            return reports

    def mail_checkpoint(self, scope, policy):
        with self.mutex:
            self._check()
            saved = self.data.get("mail_scan", {}).get(scope, {})
            return deepcopy(saved) if saved.get("policy") == policy else {}

    @property
    def library(self):
        directory = self.data.get("library_directory")
        return Path(directory) if directory else None

    @property
    def workspace(self):
        if self.root.name == WORKSPACE_DATABASE:
            return self.root.parent
        return self.library or self.root

    def copy_workspace(self, revision, workspace):
        workspace = Path(workspace).resolve()
        target = workspace / WORKSPACE_DATABASE
        if not workspace.is_dir() or target.is_symlink() or (target.exists() and is_directory_link(target)):
            raise UserError("请选择可访问的工作区目录。")
        if target.exists():
            raise UserError("所选目录已包含工作区数据，请重新打开已有工作区。")
        with self.mutex:
            self._check()
            if revision != self.data["revision"]:
                raise UserError("工作区已更新，请刷新后重试。")
            with TemporaryDirectory(prefix=f"{WORKSPACE_DATABASE}-", dir=workspace) as directory:
                staging = Path(directory) / "database"
                clone = Store(staging)
                try:
                    with clone.change(clone.data["revision"], keep_history=False):
                        clone.data = deepcopy(self.data)
                        clone.data.pop("library_directory", None)
                        originals = {}
                        for (previous, _), (file, _) in zip(self._file_references(), clone._file_references(), strict=True):
                            key = previous.get("storage"), previous["path"], previous["hash"]
                            if key not in originals:
                                original, payload = self._read_original(previous)
                                relative = (previous["path"] if previous.get("mail_review")
                                            else f"objects/{file['hash']}{original.suffix.lower()}")
                                clone._ensure_original(clone._object_path(relative), payload, file["hash"])
                                originals[key] = relative
                            file["path"] = originals[key]
                            file.pop("storage", None)
                    self._check()
                finally:
                    clone.close()
                staging.rename(target)
        return target

    def occupied(self) -> dict:
        return {i["id"]: i["active_batch_id"] for i in self.data["invoices"]
                if i.get("active_batch_id") and i.get("reimbursement_status") in OCCUPIED_STATUSES}

    def _verify(self, item, *, cache=None, index=None):
        read = self._read_original if index is None else lambda file: index.read_original(self, file)
        item["verification"] = verify_record(item, read, cache=cache)
        item["reviewed"] = item["verification"]["passed"]

    def _upgrade_invoices(self):
        legacy = {i["id"]: b for b in self.data["batches"] if b["status"] != "cancelled" for i in b["items"]}
        for item in self.data["invoices"]:
            if "reimbursement_status" not in item:
                batch = legacy.get(item["id"])
                status = {"confirmed": "reserved"}.get(batch["status"], batch["status"]) if batch else "available"
                item["reimbursement_status"] = status
                item["active_batch_id"] = batch["id"] if batch and status in OCCUPIED_STATUSES else None
            if "verification" not in item:
                self._verify(item)

    def invoice(self, invoice_id: str) -> dict:
        item = next((i for i in self.data["invoices"] if i["id"] == invoice_id), None)
        if not item:
            raise UserError("发票不存在。")
        return item

    def set_company(self, revision: int, name: str, tax_id: str):
        name = name.strip()
        tax_id = re.sub(r"\s+", "", tax_id).upper()
        if not name and not tax_id:
            raise UserError("请至少填写公司抬头或税号。")
        if tax_id and not re.fullmatch(r"[A-Z0-9]{15,20}", tax_id):
            raise UserError("请核对公司税号格式。")
        with self.change(revision):
            self.data["company"] = {"name": name, "tax_id": tax_id}

    def reset(self, revision: int):
        with self.mutex:
            self._check()
            if revision != self.data["revision"]:
                raise UserError("页面资料已更新，请刷新后重试。")
            history = self.root / "history"
            def check_entry(path):
                if path.is_symlink() or (path.is_dir() and is_directory_link(path)):
                    raise UserError(f"资料库内部存在链接，已停止初始化：{path}")
                try:
                    check_delete_access(path)
                except OSError as exc:
                    raise UserError(f"文件被占用或没有删除权限，请关闭相关程序后重试：{path}") from exc
            # 清理范围限于数据库管理的目录，禁止沿链接清理。
            self._check_history_directory(history)
            if history.exists():
                check_entry(history)
                for base, dirs, files in os.walk(history, followlinks=False):
                    for name in dirs + files:
                        check_entry(Path(base) / name)
            with self.change(revision, keep_history=False):
                library = self.library
                self.data = {"schema": 4 if library else 3, "revision": revision, "company": {"name": "", "tax_id": ""},
                             "invoices": [], "batches": [], "import_runs": []}
                if library:
                    self.data["library_directory"] = str(library)
            try:
                self._check()
                self._check_history_directory(history)
                if history.exists():
                    shutil.rmtree(history)
                history.mkdir()
            except (OSError, UserError) as exc:
                raise UserError(f"发票索引已初始化，旧文件清理尚未完成。请处理以下问题后再次恢复初始化：{exc}") from exc

    def _check_history_directory(self, path):
        if (path.resolve() != path or path.is_symlink()
                or (path.exists() and (not path.is_dir() or is_directory_link(path)))):
            raise UserError("资料库内部目录无效或包含链接，已停止初始化。")

    def _object_path(self, relative: str) -> Path:
        path = (self.root / relative).resolve()
        if not path.is_relative_to((self.root / "objects").resolve()):
            raise UserError("原件路径无效，请检查工作区索引。")
        return path

    @staticmethod
    def _library_path(directory, relative):
        path = Path(relative)
        destination = (directory / path).resolve()
        if path.anchor or not destination.is_relative_to(directory) or destination == directory:
            raise UserError("原件路径超出发票资料库。")
        return destination

    def _original_path(self, file):
        if file.get("storage") == "library":
            if not self.library:
                raise UserError("请配置发票资料库路径。")
            return self._library_path(self.library, file["path"])
        return self._object_path(file["path"])

    @staticmethod
    def _write_original(destination, data):
        with destination.open("xb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())

    def _ensure_original(self, destination: Path, data: bytes, digest: str):
        if destination.exists():
            if fingerprint(destination.read_bytes()) != digest:
                raise UserError("存储原件内容已改变，请先检查同步或文件完整性；现有文件未被覆盖。")
            return
        self._write_original(destination, data)

    def _save_library_file(self, directory, name, data, digest, subfolder=""):
        # 保留扩展名，用后缀编号避让内容不同的同名原件。
        name = Path(name)
        suffix = name.suffix.lower()
        stem = safe_name(name.stem).rstrip(". ") or "invoice"
        if re.fullmatch(r"(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])", stem.partition(".")[0]):
            stem = "_" + stem
        if subfolder:
            self._library_path(directory, subfolder).mkdir(exist_ok=True)
        for number in count(1):
            filename = f"{stem}_{number}{suffix}" if number > 1 else stem + suffix
            relative = Path(subfolder) / filename
            destination = self._library_path(directory, relative)
            if destination.exists():
                if destination.is_file() and destination.stat().st_size == len(data) and fingerprint(destination.read_bytes()) == digest:
                    return relative.as_posix()
                continue
            try:
                self._write_original(destination, data)
            except FileExistsError:
                continue
            return relative.as_posix()

    def _file_references(self):
        for item in self.data["invoices"]:
            for file in item["files"]:
                yield file, item["sources"]
        for batch in self.data["batches"]:
            for item in batch["items"]:
                for file in item["files"]:
                    yield file, ()
        for report in self.data["import_runs"]:
            for row in report["items"]:
                if file := row.get("mail_object"):
                    yield file, ()
        for review in self.data.get("mail_reviews", {}).values():
            if file := review.get("review_object"):
                yield file, ()

    def set_library(self, revision, directory):
        directory = Path(directory).expanduser().resolve()
        if not directory.is_dir():
            raise UserError("请选择已有的发票文件夹。")
        if directory.is_relative_to(self.root) or self.root.is_relative_to(directory):
            raise UserError("发票资料库与系统数据库必须使用互不包含的文件夹。")
        with self.mutex:
            self._check()
            if self.library == directory and revision == self.data["revision"]:
                return revision
            with self.change(revision):
                mapped = {}
                for file, sources in self._file_references():
                    if file.get("mail_review"):
                        continue
                    key = file.get("storage"), file["path"], file["hash"]
                    if key not in mapped:
                        mapped[key] = self._copy_to_library(directory, file, sources)
                    file.update(path=mapped[key], storage="library")
                self.data["library_directory"] = str(directory)
                self.data["schema"] = max(self.data["schema"], 4)
            return self.data["revision"]

    def _copy_to_library(self, directory, file, sources):
        if file.get("storage") == "library":
            candidate = self._library_path(directory, file["path"])
            if candidate.is_file() and fingerprint(candidate.read_bytes()) == file["hash"]:
                return file["path"]
        for source in sources:
            candidate = Path(source)
            if not candidate.is_absolute() or not candidate.is_relative_to(directory):
                continue
            if (safe_name(candidate.name) != file.get("name") or file_format(candidate) != file_format(file["path"])
                    or not candidate.is_file()):
                continue
            relative = candidate.relative_to(directory)
            path = self._library_path(directory, relative)
            if fingerprint(path.read_bytes()) == file["hash"]:
                return relative.as_posix()
        original, data = self._read_original(file)
        name = file.get("name", original.name)
        if Path(name).suffix.lower() != original.suffix.lower():
            name += original.suffix
        return self._save_library_file(directory, name, data, file["hash"],
                                       MAIL_ORIGINALS if original.suffix.lower() == ".eml" else "")

    def _ingest(self, index, name, data, digest, source, local_path=None, *, analyze_only=False, extracted=None,
                mail_target=None, replace_mail_original=False):
        suffix = Path(name).suffix.lower()
        if extracted is not None and not analyze_only:
            index.evidence[digest, suffix] = extracted
        existing = (index.analyses if analyze_only else index.originals).get(digest)
        if existing:
            for file in existing["files"]:
                if not analyze_only and file["hash"] == digest:
                    destination = self._original_path(file)
                    if local_path and file.get("storage") == "library" and not destination.exists():
                        if file_format(local_path) != file_format(file["path"]):
                            raise UserError("同内容文件的扩展名与索引原件不一致，请恢复原件格式后导入。")
                        relative = local_path.relative_to(self.library).as_posix()
                        previous_path = file["path"]
                        for reference, _ in self._file_references():
                            if reference.get("storage") == "library" and reference["path"] == previous_path and reference["hash"] == digest:
                                reference["path"] = relative
                    else:
                        self._ensure_original(destination, data, digest)
            if source not in existing["sources"]:
                existing["sources"].append(source)
            # 重新导入可以补充尚未通过自动核验的空字段。
            if not existing.get("reviewed") and existing.get("reimbursement_status") not in OCCUPIED_STATUSES:
                file = next((f for f in existing["files"] if f["hash"] == digest), None)
                if file:
                    extracted = original_evidence(file, lambda entry: index.read_original(self, entry), cache=index.evidence)
                elif extracted is None:
                    extracted = extract(data, suffix)
                for field in FIELDS:
                    if not existing.get(field) and extracted.get(field):
                        existing[field] = extracted[field]
            self._verify(existing, cache=index.evidence, index=index)
            index.update_identity(existing)
            return "duplicates", existing["id"]
        if extracted is None:
            extracted = extract(data, suffix)
        if analyze_only:
            extracted = {key: value for key, value in extracted.items() if key != "raw_text"}
        new = {"id": uid(), "number": "", "code": "", "date": "", "buyer_name": "", "buyer_tax_id": "",
               "seller_name": "", "amount_cents": 0, "note": "", "reviewed": False, "archived": False, "created": now(),
               "files": [], "sources": [source], **extracted}
        new.update(reimbursement_status="available", active_batch_id=None, status_events=[])
        key = _record_identity(new)
        target = mail_target if mail_target is not None else index.match_identity(key)
        conflicting_target = None
        if (key is None and not new.get("multiple_invoice_identifiers") and (probe := identity(new))
                and any(item["id"] != new["id"] and _record_identity(item) for item in index.matching_invoices(probe))):
            # 未确认 OCR 记录不抢占已核验记录，但必须提示同票号可能重复。
            new["warnings"].append(SUSPECT_DUPLICATE_WARNING)
        if target and not replace_mail_original and _conflicting_fields(new, target):
            new["warnings"].append("与已存在的同票号记录字段冲突，已停止选用；请修正字段或移出错误记录。")
            new["duplicate_of"] = target["id"]
            conflicting_target = target
            target = None
        recipient = target if target is not None else new
        if analyze_only:
            recipient.setdefault("mail_xml", []).append(
                {"name": safe_name(name), "hash": digest, "fields": {field: extracted[field] for field in FIELDS if extracted.get(field)},
                 "warnings": extracted["warnings"]})
            self.data["schema"] = max(self.data["schema"], 5)
        else:
            if self.library:
                relative = (local_path.relative_to(self.library).as_posix() if local_path else
                            self._save_library_file(self.library, name, data, digest))
            else:
                relative = f"objects/{digest}{suffix}"
                self._ensure_original(self._object_path(relative), data, digest)
            file = {"id": uid(), "name": safe_name(name), "path": relative, "hash": digest, "size": len(data)}
            if self.library:
                file["storage"] = "library"
            if extracted.get("recognition_version"):
                file["recognition"] = extracted
            if replace_mail_original:
                self._replace_mail_original(index, recipient, new)
            recipient["files"].append(file)
            index.evidence[digest, suffix] = extracted
        if conflicting_target and conflicting_target.get("reimbursement_status") not in OCCUPIED_STATUSES:
            conflicting_target["warnings"].append("发现同票号的冲突文件，请检查新导入记录。")
        if target:
            if source not in target["sources"]:
                target["sources"].append(source)
            for field in FIELDS:
                if not target.get(field) and new.get(field):
                    target[field] = new[field]
            self._verify(target, cache=index.evidence, index=index)
            index.update_identity(target)
            index.add_file(target, digest, analyze_only=analyze_only)
            return "merged", target["id"]
        self._verify(new, cache=index.evidence, index=index)
        self.data["invoices"].append(new)
        index.update_identity(new)
        index.add_file(new, digest, analyze_only=analyze_only)
        return "imported", new["id"]

    @staticmethod
    def _import_report(kind, source, scanned=0):
        return {"id": uid(), "at": now(), "kind": kind, "source": source, "scanned": scanned,
                "imported": 0, "merged": 0, "duplicates": 0, "errors": [], "notes": [],
                "duplicate_files": [], "items": []}

    def _record_import(self, index, report, row, payload, digest, source, name, local_path=None, *,
                       mail_attachment=False, extracted=None, mail_target=None, replace_mail_original=False):
        suffix = Path(name).suffix.lower()
        analyze_only = mail_attachment and suffix == ".xml"
        outcome, invoice_id = self._ingest(index, name, payload, digest, source, local_path,
                                          analyze_only=analyze_only, extracted=extracted, mail_target=mail_target,
                                          replace_mail_original=replace_mail_original)
        report[outcome] += 1
        entry = {**row, "result": outcome, "invoice_id": invoice_id}
        if analyze_only:
            entry["xml_analysis"] = True
        report["items"].append(entry)
        return entry

    @staticmethod
    def _record_import_error(report, row, error):
        report["errors"].append({"file": row["file"], "error": str(error)})
        report["items"].append({**row, "result": "error", "detail": str(error)})

    @staticmethod
    def _record_import_note(report, row, note):
        report["notes"].append(f"{row['file']}: {note}")
        report["items"].append({**row, "result": "note", "detail": note})

    def _save_import_report(self, report):
        invoice_rows = [row for row in report["items"] if row.get("invoice_id")]
        if invoice_rows:
            conflicts = self.duplicate_ids()
            invoices = {i["id"]: i for i in self.data["invoices"]}
            for row in invoice_rows:
                item = invoices[row["invoice_id"]]
                issues = item["verification"]["issues"][:]
                if item["id"] in conflicts:
                    issues.insert(0, "同票字段冲突。")
                if detail := row.get("detail"):
                    issues.insert(0, detail)
                row["detail"] = "\n".join(issues)
        if report["items"] or report["scanned"]:
            self.data["import_runs"].append(deepcopy(report))

    def _mail_archive_target(self, index, key):
        if cached := index.mail_targets.get(key):
            item, file = cached
            if index.cached_file(self._original_path(file), file["hash"])[2] is not None:
                return item, True
            index.mail_targets.pop(key, None)
        pending = None
        for item in index.matching_invoices(key):
            for file in item["files"]:
                if file_format(file["path"]) == ".xml":
                    continue
                candidate = {**item, "files": [file], "mail_xml": (), "ocr_unconfirmed": False}
                if verify_record(candidate, lambda entry: index.read_original(self, entry),
                                 cache=index.evidence)["passed"]:
                    index.mail_targets[key] = item, file
                    return item, True
            if pending is None and _protects_identity(item):
                pending = item
        return pending, False

    @staticmethod
    def _replace_mail_original(index, target, new):
        retained = []
        for file in target["files"]:
            if file_format(file["path"]) == ".xml":
                retained.append(file)
            else:
                index.remove_file(target, file["hash"])
        target["files"] = retained
        for field in FIELDS:
            target[field] = new[field] or (target.get(field, new[field]) if retained else new[field])
        for field in ("ocr_unconfirmed", "raw_text", "method", "recognition_version"):
            target.pop(field, None)
            if field in new:
                target[field] = new[field]
        target["warnings"] = new["warnings"]
        analyses = []
        for entry in target.get("mail_xml", ()):
            if all(comparable(field, value) == comparable(field, target.get(field, ""))
                   for field, value in entry["fields"].items()):
                analyses.append(entry)
            else:
                index.remove_file(target, entry["hash"])
        if "mail_xml" in target:
            target["mail_xml"] = analyses

    @staticmethod
    def _record_mail_duplicate(report, row, target, source):
        if source not in target["sources"]:
            target["sources"].append(source)
        report["duplicates"] += 1
        report["items"].append({**row, "result": "duplicates", "invoice_id": target["id"]})

    def _queue_mail_review(self, report, row, name, payload, digest, reason, *, evidence=None, context=""):
        review_id = _mail_review_key(row, digest)
        reviews = self.data.setdefault("mail_reviews", {})
        prior = reviews.get(review_id, {})
        if prior.get("result", "review") != "review":
            return
        review = {**prior, **row, "result": "review", "detail": reason}
        if context:
            review["review_context"] = context
        if payload is not None:
            suffix = Path(name).suffix.lower()
            relative = f"objects/review-{digest}{suffix}"
            if suffix not in SUPPORTED:
                raise UserError("待审核附件格式无效。")
            self._ensure_original(self._object_path(relative), payload, digest)
            previous = prior.get("review_object")
            if previous and previous["path"] != relative:
                self.data.setdefault("review_cleanup", {})[previous["path"]] = previous["hash"]
            review["review_object"] = {"name": safe_name(name), "path": relative, "hash": digest, "mail_review": True}
            review.pop("review_xml", None)
            if suffix == ".xml" and evidence and not evidence["warnings"]:
                review["review_xml"] = {field: evidence[field] for field in FIELDS if evidence.get(field)}
        reviews[review_id] = review
        self.data["schema"] = 6
        report["items"].append({"file": "待审核材料", "result": "review", "review_id": review_id,
                                **{key: row[key] for key in ("imap_uid", "received") if key in row}})

    def _close_mail_review(self, review_id, result, *, invoice_id=None):
        reviews = self.data["mail_reviews"]
        review = reviews[review_id]
        closed = {"result": result, "file": review["file"] if invoice_id else "已排除材料"}
        if invoice_id:
            closed["invoice_id"] = invoice_id
        if original := review.get("review_object"):
            self.data.setdefault("review_cleanup", {})[original["path"]] = original["hash"]
        reviews[review_id] = closed

    def _release_review_files(self):
        with self.mutex:
            cleanup = self.data.get("review_cleanup", {})
            if not cleanup:
                return
            self._check()
            used = {entry["review_object"]["path"] for entry in self.data.get("mail_reviews", {}).values()
                    if entry.get("review_object")}
            for relative, digest in cleanup.items():
                if relative in used or not re.fullmatch(r"objects/review-[0-9a-f]{64}\.(?:pdf|ofd|xml|png|jpe?g)", relative):
                    continue
                original = self.root / relative
                if (original.is_file() and original.resolve() == original and original.name.startswith(f"review-{digest}")):
                    try:
                        if fingerprint(original.read_bytes()) == digest:
                            original.unlink()
                    except PermissionError:
                        # 预览程序关闭后，下次处理邮件审核时再次清理。
                        continue

    def _complete_mail_reviews(self, index, report):
        reviews = self.data.get("mail_reviews", {})
        if not reviews:
            return
        archived = [row for row in report["items"] if row.get("invoice_id")]
        if not archived:
            return
        for row in archived:
            keys = [_mail_review_key(row)]
            if row.get("mail_part"):
                keys.append(_mail_review_key({**row, "mail_part": "body"}))
            for key in keys:
                if reviews.get(key, {}).get("result") == "review":
                    self._close_mail_review(key, "reviewed", invoice_id=row["invoice_id"])
        identities = {key for row in archived if row.get("invoice_id")
                      and (key := index.identity_keys.get(row["invoice_id"]))}
        for review_id, review in reviews.items():
            if review.get("result") != "review" or not (fields := review.get("review_xml")):
                continue
            key = identity(fields)
            if key not in identities or not (target := index.match_identity(key)):
                continue
            if not target["verification"]["passed"] or _conflicting_fields(fields, target):
                continue
            original = review["review_object"]
            allowed = {field: value for field, value in fields.items()
                       if field not in {"buyer_tax_id", "code"} or target.get(field)}
            source = " / ".join(review[field] for field in ("mailbox", "folder", "message_id") if review.get(field))
            if source and source not in target["sources"]:
                target["sources"].append(source)
            if index.analyses.get(original["hash"]) is not target:
                for field, value in allowed.items():
                    if not target.get(field):
                        target[field] = value
                target.setdefault("mail_xml", []).append(
                    {"name": original["name"], "hash": original["hash"], "fields": allowed, "warnings": []})
                index.add_file(target, original["hash"], analyze_only=True)
            self._verify(target, cache=index.evidence, index=index)
            self._close_mail_review(review_id, "reviewed", invoice_id=target["id"])

    def decide_mail_review(self, revision, review_id, *, original=None, dismiss=False):
        with self.change(revision):
            review = self.data.get("mail_reviews", {}).get(review_id)
            if not review or review["result"] != "review":
                raise UserError("待审核记录已更新，请重新打开导入记录。")
            if dismiss:
                self._close_mail_review(review_id, "dismissed")
            else:
                if original is None:
                    if not (file := review.get("review_object")):
                        raise UserError("请先从原邮件获取发票原件。")
                    path, data = self._read_original(file)
                    name = file["name"]
                else:
                    path = Path(original)
                    if path.stat().st_size > MAX_BYTES:
                        raise UserError("单个文件不能超过 20 MB。")
                    data = path.read_bytes()
                    name = path.name
                suffix = path.suffix.lower()
                if suffix not in SUPPORTED - {".xml"}:
                    raise UserError("请选择可核验的 PDF、OFD 或发票图片。")
                evidence = extract(data, suffix)
                expected = identity(review.get("review_xml", {}))
                if expected and identity(evidence) != expected:
                    raise UserError("所选原件与待审核材料的发票号码或代码不一致。")
                digest = fingerprint(data)
                if suffix == ".ofd":
                    evidence["mail_invoice_title"] = has_invoice_title(inspect_mail_document(data, suffix, allow_ocr=True)[0])
                report = self._import_report("mail", "人工核验")
                index = _ImportIndex(self.data["invoices"])
                source = " / ".join(review[key] for key in ("mailbox", "folder", "message_id") if review.get(key))
                self._record_mail_payload(index, report, {"file": name}, data, digest, source, name, evidence, online=True)
                invoice_id = report["items"][0]["invoice_id"]
                self._close_mail_review(review_id, "reviewed", invoice_id=invoice_id)
                self._complete_mail_reviews(index, report)
                self._save_import_report(report)
        self._release_review_files()

    def _store_mail_checkpoint(self, checkpoint, report):
        scope, policy, validity = (checkpoint[key] for key in ("scope", "policy", "uidvalidity"))
        scans = self.data.setdefault("mail_scan", {})
        saved = scans.get(scope, {})
        messages = saved.get("messages", {}) if saved.get("uidvalidity") == validity and saved.get("policy") == policy else {}
        failed = {str(row["imap_uid"]) for row in report["items"] if row["result"] == "error" and row.get("imap_uid")}
        for uid, entry in checkpoint["messages"].items():
            if entry and uid not in failed:
                saved_entry = messages.get(uid)
                if not saved_entry or saved_entry[0] != entry[0] or saved_entry[1] != "deep":
                    messages[uid] = list(entry)
            else:
                messages.pop(uid, None)
        scans[scope] = {"policy": policy, "uidvalidity": validity, "messages": messages}
        self.data["schema"] = 6

    def _record_mail_payload(self, index, report, row, payload, digest, source, name, evidence, *, online=False):
        if evidence.get("multiple_invoice_identifiers"):
            raise UserError(MULTIPLE_INVOICE_IDENTIFIERS_WARNING)
        if not (key := identity(evidence)):
            raise UserError("无法可靠确认发票号码或代码。" + " ".join(evidence["warnings"]))
        suffix = file_format(name)
        titled = bool(evidence.get("mail_invoice_title") or has_invoice_title(evidence))
        if suffix != ".xml":
            index.evidence.setdefault((digest, Path(name).suffix.lower()), evidence)
        target, usable = self._mail_archive_target(index, key)
        conflicts = _conflicting_fields(target, evidence) if target is not None else []
        if suffix == ".xml":
            if online and not usable:
                raise UserError("邮件 XML 待补充可核验的发票原件。")
            if conflicts or evidence["warnings"]:
                detail = f"同票字段冲突（{'、'.join(conflicts)}）。" if conflicts else " ".join(evidence["warnings"])
                raise UserError(detail)
            if usable:
                # 邮件 XML 仅补充能够由现有原件支持的税号及发票代码。
                evidence = {**evidence}
                for field in ("buyer_tax_id", "code"):
                    if not target.get(field):
                        evidence.pop(field, None)
            return self._record_import(index, report, row, payload, digest, source, name,
                                       mail_attachment=True, extracted=evidence, mail_target=target)
        if usable:
            if conflicts:
                raise UserError(f"同票字段冲突（{'、'.join(conflicts)}）。")
            # 同封邮件已有合格 PDF 时跳过 OFD 是既有策略，保留这条明确路径。
            if evidence.get("mail_ofd_skipped") or not (evidence.get("warnings") or evidence.get("ocr_unconfirmed")):
                return self._record_mail_duplicate(report, row, target, source)
            # 新证据本身带字段缺失或核验异常警告时，不能静默当作无异常重复件。
            raise UserError("同票新证据存在核验异常：" + (" ".join(evidence["warnings"])
                                                        or "字段未通过自动核验，请核对原件。"))
        if online and not titled:
            # 标题缺失但同票号已有合格原件时已在上面按重复处理，两条路径结论一致。
            raise UserError("候选原件缺少可确认的发票标题。")

        verified = None
        if online:
            standalone = {"amount_cents": 0, **evidence, "files": [{"name": name, "hash": digest}]}
            verified = verify_record(standalone, lambda _: (Path(name), payload), cache=index.evidence)
            if not verified["passed"]:
                raise UserError("候选原件尚未通过发票核验。" + " ".join(verified["issues"]))

        replace = False
        if target:
            has_layout = any(file_format(file["path"]) != ".xml" for file in target["files"])
            if has_layout or not target["files"]:
                if verified is None:
                    standalone = {"amount_cents": 0, **evidence, "files": [{"name": name, "hash": digest}]}
                    verified = verify_record(standalone, lambda _: (Path(name), payload), cache=index.evidence)
                replace = verified["passed"] and not (conflicts and target["files"])
                if conflicts and not replace:
                    raise UserError(f"同票字段冲突（{'、'.join(conflicts)}）。")
                if has_layout and not replace and index.originals.get(digest) is not target:
                    raise UserError("已有同票原件待核查，新附件也未能独立通过核验。")
                if replace and conflicts:
                    row = {**row, "detail": f"先前邮件 XML 存在同票字段冲突（{'、'.join(conflicts)}），已按原件核验。"}
            elif conflicts:
                raise UserError(f"同票字段冲突（{'、'.join(conflicts)}）。")
        return self._record_import(index, report, row, payload, digest, source, name, mail_attachment=True,
                                   extracted=evidence, mail_target=target, replace_mail_original=replace)

    def _import_mail_row(self, index, report, row, *, local_eml=None):
        metadata = {key: value for key, value in row.items()
                    if key not in {"attachment", "error", "name", "payload", "mail_evidence", "mail_digest",
                                   "mail_hint", "mail_review", "mail_retry", "mail_context"}}
        name = row.get("name", row["file"])
        error = row.get("error")
        payload = extracted = None
        digest = ""
        if not error:
            try:
                extracted = row.get("mail_evidence")
                if local_eml is None:
                    source = " / ".join(row[key] for key in ("mailbox", "folder", "message_id") if row.get(key))
                    if path := row.get("payload"):
                        payload, digest = index.read_staged(path, row.get("mail_digest"))
                    if reason := row.get("mail_review"):
                        return self._queue_mail_review(report, metadata, name, payload, digest, reason,
                                                       context=row.get("mail_context", ""))
                else:
                    source = f"{local_eml} / {name}"
                    payload = row["payload"]
                    digest = fingerprint(payload)
                suffix = file_format(name)
                if suffix == ".ofd":
                    extracted = self._mail_ofd_evidence(index, report, payload, digest, extracted)
                    if not has_invoice_content(extracted) and not row.get("mail_hint"):
                        return
                elif extracted is None:
                    extracted = extract(payload, Path(name).suffix.lower())
                # 本地 .eml 与邮箱路径共用同一判定：标题、发票内容或带齐字段的 XML 才
                # 值得记录，普通账单/回单等静默跳过而不是报错。
                if not row.get("mail_hint"):
                    related = (has_required_fields(extracted) if suffix == ".xml"
                               else has_invoice_title(extracted) or has_invoice_content(extracted)
                               or extracted.get("mail_invoice_title"))
                    if not related:
                        return
                return self._record_mail_payload(index, report, metadata, payload, digest, source, name, extracted,
                                                 online=local_eml is None)
            except (UserError, OSError) as exc:
                error = str(exc)
                if (local_eml is None and payload is not None and extracted and
                        (has_invoice_title(extracted) or extracted.get("mail_invoice_title")
                         or row.get("mail_hint") and has_invoice_content(extracted)
                         or file_format(name) == ".xml" and has_required_fields(extracted))):
                    return self._queue_mail_review(report, metadata, name, payload, digest, error, evidence=extracted)
        if local_eml is None and row.get("subject"):
            error += " 可按主题到邮箱中查看原邮件。"
        self._record_import_error(report, metadata, error)

    def _mail_ofd_evidence(self, index, report, payload, digest, text):
        if text is None:
            text = inspect_mail_document(payload, ".ofd", allow_ocr=True)[0]
        expected = identity(text)
        if expected and not text.get("multiple_invoice_identifiers") and has_required_fields(text):
            _, usable = self._mail_archive_target(index, expected)
            if usable:
                # 同封邮件已有合格 PDF：OFD 不再跑 OCR，直接按重复处理。
                return {**text, "mail_ofd_skipped": True}
        observed = index.evidence.get((digest, ".ofd"))
        if observed is None:
            if screening := report.get("mail_screening"):
                screening["ocr_files"] += 1
            observed = extract(payload, ".ofd")
            index.evidence[digest, ".ofd"] = observed
        if text.get("multiple_invoice_identifiers") or observed.get("multiple_invoice_identifiers"):
            raise UserError(MULTIPLE_INVOICE_IDENTIFIERS_WARNING)
        if _conflicting_fields(text, observed):
            raise UserError("OFD 文字层与图像识别的发票字段不一致。")
        if expected and not identity(observed):
            observed = {**observed, "number": text["number"], "code": text["code"]}
        return {**observed, "mail_invoice_title": has_invoice_title(text)}

    def _import_mail_files(self, index, report, rows, progress=None, *, local_eml=None):
        first = len(report["items"])
        positions = []
        ordered = sorted(enumerate(rows), key=lambda entry: MAIL_FILE_PRIORITY.get(_mail_file_suffix(entry[1]), 0))
        for number, (position, row) in enumerate(ordered, 1):
            if progress:
                progress(f"正在核验邮件文件 {number} / {len(rows)}…")
            previous = len(report["items"])
            self._import_mail_row(index, report, row, local_eml=local_eml)
            if len(report["items"]) > previous:
                positions.append(position)
        report["items"][first:] = [entry for _, entry in sorted(zip(positions, report["items"][first:], strict=True))]

    def import_mail(self, revision, source, scanned, rows, progress=None, *, screening=None, checkpoint=None):
        result = self._import_report("mail", source, scanned)
        started = perf_counter()
        if screening is not None:
            result["mail_screening"] = dict(screening)
        if not rows and not scanned:
            return result
        with self.change(revision):
            if rows:
                has_candidates = any(row.get("payload") and not (row.get("error") or row.get("mail_review")) for row in rows)
                index = _ImportIndex(self.data["invoices"] if has_candidates else ())
                self._import_mail_files(index, result, rows, progress)
                self._complete_mail_reviews(index, result)
            if screening is not None:
                recorded = {row["imap_uid"] for row in result["items"] if row.get("received") and row.get("imap_uid")}
                result["mail_screening"]["filtered"] = scanned - len(recorded)
                result["mail_screening"]["archive_seconds"] = round(perf_counter() - started, 2)
            if checkpoint is not None and checkpoint.get("uidvalidity"):
                self._store_mail_checkpoint(checkpoint, result)
            self._save_import_report(result)
        self._release_review_files()
        return result

    def import_folder(self, revision: int, directory: str, recursive: bool = False, progress=None) -> dict:
        source = Path(directory).expanduser().resolve()
        if not source.is_dir():
            raise UserError("导入目录不存在。")
        if self._is_intake_excluded(source):
            raise UserError("所选目录由程序管理。请选择存放待导入发票的目录。")
        files = []
        if recursive:
            for base, dirs, names in os.walk(source, followlinks=False):
                base = Path(base)
                dirs[:] = [d for d in dirs if not is_directory_link(base / d)
                           and not self._is_intake_excluded(base / d)
                           and not _is_legacy_database(base / d)]
                files.extend(base / n for n in names if Path(n).suffix.lower() in SUPPORTED | {".eml"})
                if len(files) > 500:
                    raise UserError("单次最多扫描 500 个文件，请缩小导入范围。")
        else:
            files = [p for p in source.iterdir() if p.suffix.lower() in SUPPORTED | {".eml"} and p.is_file()]
        if len(files) > 500:
            raise UserError("单次最多扫描 500 个文件，请缩小导入范围。")
        return self._import_local_files(revision, files, self._import_report("directory", str(source), len(files)),
                                        progress, directory=source)

    def _is_intake_excluded(self, directory):
        if self.root.name.lower() == WORKSPACE_DATABASE and directory.is_relative_to(self.root):
            return True
        for folder in (directory, *directory.parents):
            name = folder.name.lower()
            if name in INTAKE_EXCLUDED_DIRECTORIES or name.startswith(f"{WORKSPACE_DATABASE}-"):
                return True
            if name in ARCHIVE_DIRECTORIES and (folder.parent == self.root or _is_legacy_database(folder.parent)):
                return True
        return False

    def import_files(self, revision, paths, progress=None):
        files = list(dict.fromkeys(Path(path).expanduser().absolute() for path in paths))
        if not 1 <= len(files) <= 500:
            raise UserError("请选择 1 至 500 个发票文件。")
        report = self._import_report("files", f"选择的 {len(files)} 个文件", len(files))
        report["source_directory"] = str(files[0].parent)
        return self._import_local_files(revision, files, report, progress)

    def _import_local_files(self, revision, files, result, progress, *, directory=None):
        if not files:
            return result
        source_hashes = {}
        skipped_eml = 0
        with self.change(revision):
            lookup = _ImportIndex(self.data["invoices"])
            for number, path in enumerate(sorted(files), 1):
                if progress:
                    progress(f"正在核验本地文件 {number} / {len(files)}…")
                row = {"file": str(path.relative_to(directory) if directory else path)}
                try:
                    resolved = path.resolve()
                    if path.is_symlink() or (directory and not resolved.is_relative_to(directory)):
                        raise UserError("跳过符号链接或导入目录外的文件。")
                    if self._is_intake_excluded(resolved.parent):
                        raise UserError("跳过工作区内部的归档文件。")
                    suffix = path.suffix.lower()
                    if suffix not in SUPPORTED | {".eml"}:
                        raise UserError("此文件格式不受支持。")
                    # .eml 只是容器，42MB 的邮件也可能夹带一张有效小发票；容器按邮箱单封
                    # 上限校验，解包后的附件再逐个执行 20MB 限制。
                    if path.stat().st_size > (MAX_MESSAGE_BYTES if suffix == ".eml" else MAX_BYTES):
                        raise UserError("邮件文件超过 80 MB。" if suffix == ".eml" else "文件超过 20 MB。")
                    data = path.read_bytes()
                    if suffix == ".eml":
                        attachments = eml_attachments(data)
                        rows = []
                        for attachment in attachments:
                            attachment_row = {"file": f"{row['file']} / {attachment['name']}",
                                              "name": attachment["name"], "payload": attachment["payload"]}
                            if attachment["oversized"]:
                                # payload 只用于核验；导入记录要写入索引 JSON，不能保留 bytes。
                                metadata = {key: value for key, value in attachment_row.items()
                                            if key not in {"name", "payload"}}
                                self._record_import_error(result, metadata, "文件超过 20 MB。")
                            else:
                                rows.append(attachment_row)
                        if rows:
                            self._import_mail_files(lookup, result, rows, local_eml=path)
                        if not attachments:
                            skipped_eml += 1
                    else:
                        digest = fingerprint(data)
                        local = resolved if self.library and resolved.is_relative_to(self.library) else None
                        self._record_import(lookup, result, row, data, digest, str(path), path.name, local)
                        if directory:
                            source_hashes.setdefault(digest, []).append(path)
                except (UserError, OSError) as exc:
                    self._record_import_error(result, row, exc)
            if skipped_eml:
                # 日常邮件没有发票附件属于常态，汇总成一条提示，避免逐封刷屏。
                self._record_import_note(result, {"file": "本地 EML"},
                                         f"{skipped_eml} 封邮件未发现支持的发票附件，已跳过。")
            conflicts = self.duplicate_ids()
            for item in self.data["invoices"]:
                if item["id"] in conflicts:
                    continue
                for file in item["files"]:
                    digest = file["hash"]
                    paths = source_hashes.get(digest, ())
                    if len(paths) < 2:
                        continue
                    candidates = [p for p in paths if file_format(p) == file_format(file["path"])]
                    if not candidates:
                        continue
                    keep = candidates[0]
                    if file.get("storage") == "library":
                        # 引用只能更新为同一资料库内的路径。
                        keep = next((p for p in candidates if p.is_relative_to(self.library)), keep)
                    result["duplicate_files"].extend({"keep": str(keep), "remove": str(path), "hash": digest}
                                                     for path in paths if path != keep)
            self._save_import_report(result)
        return result

    def review(self, revision: int, invoice_id: str, fields: dict):
        cleaned = validate_invoice(fields)
        with self.change(revision):
            item = self.invoice(invoice_id)
            if item.get("reimbursement_status") in OCCUPIED_STATUSES:
                raise UserError("该票已选入批次、已提交或已报销，无法修改字段。请在发票详情中将状态改为已退回或可用。")
            key = _record_identity({**item, **cleaned})
            if key and any(i["id"] != invoice_id and _protects_identity(i) and _record_identity(i) == key
                           for i in self.data["invoices"]):
                raise UserError("存在同号码与代码的发票，请修正错误票号或移出错误记录。")
            item.update(cleaned)
            self._verify(item)

    def recognize_again(self, revision: int, invoice_id: str):
        with self.change(revision):
            item = self.invoice(invoice_id)
            if item.get("archived") or item.get("reimbursement_status") in OCCUPIED_STATUSES:
                raise UserError("请对未占用且仍在资料中的发票重新识别。")
            if not item["files"]:
                raise UserError("请补充发票原件后再重新识别。")
            cache = {}
            sources = [original_evidence(file, self._read_original, refresh=True, cache=cache) for file in item["files"]]
            for field in FIELDS:
                if values := [source[field] for source in sources if source.get(field)]:
                    if len({comparable(field, value) for value in values}) == 1:
                        item[field] = values[0]
            item["raw_text"] = "\n\n".join(source["raw_text"] for source in sources)[:30000]
            item["method"] = " / ".join(dict.fromkeys(source["method"] for source in sources))
            item["warnings"] = [warning for source in sources for warning in source.get("warnings", [])]
            for flag in ("ocr_unconfirmed", "multiple_invoice_identifiers"):
                item[flag] = any(source.get(flag) for source in sources)
            self._verify(item, cache=cache)
            key = _record_identity(item)
            if not key or not item["reviewed"] or item.get("status_events"):
                return item["id"]
            matches = [other for other in self.data["invoices"] if other is not item
                       and _protects_identity(other) and _record_identity(other) == key]
            if len(matches) != 1:
                return item["id"]
            target, = matches
            merged = deepcopy(target)
            for field in FIELDS:
                if not merged.get(field) and item.get(field):
                    merged[field] = item[field]
            merged["files"].extend(item["files"])
            mail_xml = {entry["hash"]: entry for invoice in (target, item) for entry in invoice.get("mail_xml", ())}
            if mail_xml:
                merged["mail_xml"] = list(mail_xml.values())
            merged["sources"] = list(dict.fromkeys([*target["sources"], *item["sources"]]))
            if item.get("note") and not merged.get("note"):
                merged["note"] = item["note"]
            elif item.get("note") and item["note"] != merged["note"]:
                merged["note"] += "\n" + item["note"]
            self._verify(merged, cache=cache)
            if not merged["reviewed"]:
                return item["id"]
            target.update(merged)
            self.data["invoices"].remove(item)
            for report in self.data["import_runs"]:
                for row in report["items"]:
                    if row.get("invoice_id") == invoice_id:
                        row["invoice_id"] = target["id"]
            return target["id"]

    def archive(self, revision: int, invoice_id: str, archived: bool):
        with self.change(revision):
            item = self.invoice(invoice_id)
            if archived and item.get("reimbursement_status") in OCCUPIED_STATUSES:
                raise UserError("该票已选入批次、已提交或已报销，不能移出候选。请先将状态改为已退回或可用。")
            item["archived"] = archived
            self._verify(item)

    def duplicate_ids(self) -> set[str]:
        groups = {}
        for item in self.data["invoices"]:
            key = _record_identity(item)
            if key and _protects_identity(item):
                groups.setdefault(key, []).append(item["id"])
        return {i for group in groups.values() if len(group) > 1 for i in group}

    def recommend(self, target: str, force_ids=None, exclude_ids=None) -> dict:
        with self.mutex:
            self._check()
            duplicates = self.duplicate_ids()
            company = self.data["company"]
            available = [item for item in self.data["invoices"] if eligible_invoice(
                item, company_match=company_matches(item, company), duplicate=item["id"] in duplicates)]
            return choose_invoices(available, money(target), force_ids, exclude_ids)

    def create_batch(self, revision: int, ids: list[str], target: str, declared: str, request_id: str) -> str:
        if not re.fullmatch(r"[a-f0-9]{32}", request_id):
            raise UserError("批次请求标识无效，请刷新页面。")
        with self.mutex:
            self._check()
            prior = next((b for b in self.data["batches"] if b["request_id"] == request_id), None)
            if prior:
                return prior["id"]
            with self.change(revision):
                selected = set(ids)
                if not ids or len(ids) != len(selected):
                    raise UserError("请选择至少一张发票，且不能重复选择。")
                duplicates = self.duplicate_ids()
                company = self.data["company"]
                by_id = {item["id"]: item for item in self.data["invoices"] if item["id"] in selected}
                if len(by_id) != len(ids):
                    raise UserError("发票不存在。")
                items = [by_id[invoice_id] for invoice_id in ids]
                for item in items:
                    self._verify(item)
                if any(not eligible_invoice(item, company_match=company_matches(item, company), duplicate=item["id"] in duplicates)
                       for item in items):
                    raise UserError("所选发票状态或公司信息已变化，请重新选票。")
                total = sum(i["amount_cents"] for i in items)
                goal, claim = money(target), money(declared)
                if total < goal or claim > total:
                    raise UserError("票面合计必须达到目标，实际申报额不能超过票面合计。")
                batch_id = uid()
                self.data["batches"].append({"id": batch_id, "request_id": request_id, "created": now(), "status": "confirmed",
                    "company": deepcopy(self.data["company"]), "target_cents": goal, "declared_cents": claim,
                    "total_cents": total, "items": deepcopy(items), "events": [{"status": "confirmed", "at": now()}]})
                for item in items:
                    item["reimbursement_status"] = "reserved"
                    item["active_batch_id"] = batch_id
                    item.setdefault("status_events", []).append({"status": "reserved", "at": now(), "batch_id": batch_id})
                return batch_id

    def batch(self, batch_id: str) -> dict:
        batch = next((b for b in self.data["batches"] if b["id"] == batch_id), None)
        if not batch:
            raise UserError("报销批次不存在。")
        return batch

    def set_invoice_status(self, revision: int, invoice_id: str, status: str):
        if status not in INVOICE_STATUSES:
            raise UserError("发票状态无效。")
        with self.change(revision):
            item = self.invoice(invoice_id)
            if status == "reserved" and not item.get("active_batch_id"):
                raise UserError("请通过按额度选票将发票加入批次。")
            if status == item["reimbursement_status"]:
                return
            previous_batch = item.get("active_batch_id")
            item["reimbursement_status"] = status
            if status in {"available", "returned"}:
                item["active_batch_id"] = None
            item.setdefault("status_events", []).append({"status": status, "at": now(), "batch_id": previous_batch})

    def _read_original(self, file):
        path = self._original_path(file)
        if not path.is_file():
            raise UserError("找不到发票原件。请检查文件位置和同步状态。")
        data = path.read_bytes()
        if fingerprint(data) != file["hash"]:
            raise UserError("原件校验失败。请恢复原件后重试。")
        return path, data

    def original(self, file: dict) -> Path:
        return self._read_original(file)[0]

    def export(self, batch_id: str, destination: str | Path | None = None) -> Path:
        """Build the ZIP atomically, either in the workspace or at the caller's path."""
        with self.mutex:
            self._check()
            batch = self.batch(batch_id)
            if batch["status"] == "cancelled":
                raise UserError("已取消批次不能导出。")
            originals = [(n, item, f, self.original(f)) for n, item in enumerate(batch["items"], 1) for f in item["files"]]
            csv_buffer = io.StringIO(newline="")
            writer = csv.writer(csv_buffer)
            writer.writerow(["序号", "发票号码", "发票代码", "开票日期", "购买方", "购买方税号", "价税合计", "文件数"])
            # 明细按开票日期与票号稳定排序，方便财务按时间顺序人工核对；不改变选票逻辑与原件顺序。
            ordered = sorted(batch["items"], key=lambda item: (item.get("date", ""), item.get("number", ""), item["id"]))
            for n, item in enumerate(ordered, 1):
                writer.writerow([n, csv_cell(item["number"]), csv_cell(item["code"]), item["date"], csv_cell(item["buyer_name"]),
                                 csv_cell(item["buyer_tax_id"]), yuan(item["amount_cents"]), len(item["files"])])
            if destination is None:
                path = self.root / "exports" / f"batch-{batch_id}-{uid()}.zip"
            else:
                path = Path(destination).expanduser().resolve()
                if path.is_dir():
                    raise UserError("请选择导出的 ZIP 文件路径，而不是文件夹。")
                if not path.parent.is_dir():
                    raise UserError("导出目录不存在，请重新选择保存位置。")
            # 临时文件与目标同目录，os.replace 才能保持原子替换。
            temp = path.with_name(f".export-{uid()}.tmp")
            try:
                with zipfile.ZipFile(temp, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                    archive.writestr("明细.csv", csv_buffer.getvalue().encode("utf-8-sig"))
                    used_names = {"明细.csv"}
                    for n, item, f, original in originals:
                        name = safe_name(f["name"])
                        counter = 1
                        while name.casefold() in used_names:
                            name = f"{Path(safe_name(f['name'])).stem}_{counter}{Path(f['name']).suffix}"
                            counter += 1
                        used_names.add(name.casefold())
                        archive.write(original, name)
                os.replace(temp, path)
            finally:
                temp.unlink(missing_ok=True)
            return path
