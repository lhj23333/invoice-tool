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
import threading
import uuid
import zipfile

from .domain import UserError, choose_invoices, company_matches, eligible_invoice, identity, money, validate_invoice, yuan
from .extract import MAX_BYTES, MULTIPLE_INVOICE_IDENTIFIERS_WARNING, SUPPORTED, eml_attachments, extract, file_format
from .filesystem import check_delete_access, is_directory_link
from .session import WriterGuard, process_running
from .verification import FIELDS, comparable, original_evidence, verify_record

INVOICE_STATUSES = {"available", "reserved", "submitted", "reimbursed", "returned"}
OCCUPIED_STATUSES = {"reserved", "submitted", "reimbursed"}
SKIPPED_MAIL_OFD = "同一封邮件已有 PDF 发票文件，已跳过 OFD。"
MAIL_ORIGINALS = ".invoice-mail"


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


def _record_identity(item: dict) -> str | None:
    # 旧索引中的单文件记录可能只有提取警告，仍可据此识别多票原件。
    if item.get("multiple_invoice_identifiers") or (
        len(item["files"]) == 1 and MULTIPLE_INVOICE_IDENTIFIERS_WARNING in item.get("warnings", ())
    ):
        return None
    if item.get("ocr_unconfirmed") and item.get("reimbursement_status") not in OCCUPIED_STATUSES:
        return None
    return identity(item)


def _protects_identity(item):
    return not item.get("archived") or item.get("reimbursement_status") in OCCUPIED_STATUSES


class _ImportIndex:
    def __init__(self, invoices):
        self.positions = {}
        self.identity_keys = {}
        self.identities = {}
        self.originals = {}
        self.analyses = {}
        self.evidence = {}
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
        current = _record_identity(item) if _protects_identity(item) else None
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
        group = self.identities.get(key)
        return group[min(group)] if group else None

    def add_file(self, item, digest, *, analyze_only=False):
        indexes = (self.analyses,) if analyze_only else (self.originals, self.analyses)
        for index in indexes:
            previous = index.get(digest)
            if previous is None or self.positions[item["id"]] < self.positions[previous["id"]]:
                index[digest] = item


def _mail_message_key(row):
    if message_id := row.get("message_id"):
        return row.get("mailbox"), row.get("folder"), message_id
    return row.get("original_message")


def _mail_file_suffix(row):
    # 下载失败时记录的是链接标签，只有实际取得的文件和已知附件可以参与格式筛选。
    if row.get("payload") is not None or row.get("attachment"):
        return Path(row.get("name", row["file"])).suffix.lower()
    return ""


class Store:
    """One active local writer; cloud drive synchronization remains sequential."""

    def __init__(self, directory: str | Path, recover_lock: bool = False):
        self.root = Path(directory).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.state_path = self.root / "invoices.json"
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
            self._write_marker()
        except Exception as exc:
            self.guard.close()
            if isinstance(exc, FileExistsError):
                raise UserError("此资料库有未结束的会话。关闭其他电脑上的工具并等同步完成后，点击“重新连接”；异常退出可在该入口恢复会话。") from None
            raise
        try:
            for folder in ("objects", "history", "exports"):
                (self.root / folder).mkdir(exist_ok=True)
            if self.state_path.exists():
                raw = self.state_path.read_bytes()
                self.data = json.loads(raw)
                if self.data.get("schema") not in {1, 2, 3, 4, 5} or not isinstance(self.data.get("invoices"), list) or not isinstance(self.data.get("batches"), list):
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
            raise UserError("检测到资料被外部修改或网盘同步覆盖。已停止写入，请退出并处理冲突副本后重新打开。")
        # Common sync conflict copies are not silently treated as a separate empty library.
        conflicts = [p for p in self.root.glob("invoices*.json") if p.name != "invoices.json"]
        if conflicts:
            raise UserError("发现发票索引的其他副本，请先核对网盘冲突文件，避免覆盖使用状态。")
        try:
            lock = json.loads(self.lock_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            # The local OS guard is still held and the index has not changed.
            # Restore only an absent marker, never corrupt/replaced metadata.
            try:
                self._write_marker()
                lock = {"session": self.lock_id}
            except FileExistsError:
                raise UserError("资料会话已变化，请点击“重新连接”核对状态。") from None
        except (OSError, ValueError):
            raise UserError("资料会话标记无法读取。请等同步完成后点击“重新连接”。") from None
        if not isinstance(lock, dict) or lock.get("session") != self.lock_id:
            raise UserError("另一会话已接管资料目录，本次已停止写入。请等同步完成后重新连接。")
        return raw

    def _save(self, *, keep_history=True):
        previous = self._check()
        new_data = deepcopy(self.data)
        new_data["revision"] += 1
        raw = json.dumps(new_data, ensure_ascii=False, indent=2).encode("utf-8")
        if keep_history and previous is not None:
            history = self.root / "history" / f"{self.data['revision']:08d}-{uid()}.json"
            history.write_bytes(previous)
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
            return deepcopy(self.data["import_runs"][::-1])

    @property
    def library(self):
        directory = self.data.get("library_directory")
        return Path(directory) if directory else None

    def occupied(self) -> dict:
        return {i["id"]: i["active_batch_id"] for i in self.data["invoices"]
                if i.get("active_batch_id") and i.get("reimbursement_status") in OCCUPIED_STATUSES}

    def _verify(self, item, *, cache=None):
        item["verification"] = verify_record(item, self._read_original, cache=cache)
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

    def _ingest(self, index, name, data, digest, source, local_path=None, *, analyze_only=False):
        suffix = Path(name).suffix.lower()
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
                    extracted = original_evidence(file, self._read_original, cache=index.evidence)
                else:
                    extracted = extract(data, suffix)
                for field in FIELDS:
                    if not existing.get(field) and extracted.get(field):
                        existing[field] = extracted[field]
            self._verify(existing, cache=index.evidence)
            index.update_identity(existing)
            return "duplicates", existing["id"]
        extracted = extract(data, suffix)
        if analyze_only:
            extracted.pop("raw_text", None)
        new = {"id": uid(), "number": "", "code": "", "date": "", "buyer_name": "", "buyer_tax_id": "",
               "seller_name": "", "amount_cents": 0, "note": "", "reviewed": False, "archived": False, "created": now(),
               "files": [], "sources": [source], **extracted}
        new.update(reimbursement_status="available", active_batch_id=None, status_events=[])
        key = _record_identity(new)
        target = index.match_identity(key)
        conflicting_target = None
        if target and any(new.get(field) and target.get(field) and comparable(field, new[field]) != comparable(field, target[field])
                          for field in FIELDS):
            new["warnings"].append("与已存在的同票号记录字段冲突，已停止选用；请修正字段或移出错误记录。")
            new["duplicate_of"] = target["id"]
            conflicting_target = target
            target = None
        recipient = target if target is not None else new
        if analyze_only:
            recipient.setdefault("mail_xml", []).append(
                {"name": safe_name(name), "hash": digest, "fields": {field: extracted[field] for field in FIELDS if extracted.get(field)},
                 "warnings": extracted["warnings"]})
            self.data["schema"] = 5
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
            self._verify(target, cache=index.evidence)
            index.update_identity(target)
            index.add_file(target, digest, analyze_only=analyze_only)
            return "merged", target["id"]
        self._verify(new, cache=index.evidence)
        self.data["invoices"].append(new)
        index.update_identity(new)
        index.add_file(new, digest, analyze_only=analyze_only)
        return "imported", new["id"]

    @staticmethod
    def _import_report(kind, source, scanned=0):
        return {"id": uid(), "at": now(), "kind": kind, "source": source, "scanned": scanned,
                "imported": 0, "merged": 0, "duplicates": 0, "errors": [], "notes": [],
                "duplicate_files": [], "items": []}

    def _record_import(self, index, report, row, payload, digest, source, name, local_path=None, *, mail_attachment=False):
        suffix = Path(name).suffix.lower()
        analyze_only = mail_attachment and suffix == ".xml"
        outcome, invoice_id = self._ingest(index, name, payload, digest, source, local_path, analyze_only=analyze_only)
        report[outcome] += 1
        entry = {**row, "result": outcome, "invoice_id": invoice_id}
        if analyze_only:
            entry["xml_analysis"] = True
        elif mail_attachment and suffix == ".pdf":
            entry["file_hash"] = digest
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
        conflicts = self.duplicate_ids()
        invoices = {i["id"]: i for i in self.data["invoices"]}
        for row in report["items"]:
            item = invoices.get(row.get("invoice_id"))
            if item:
                issues = item["verification"]["issues"][:]
                if item["id"] in conflicts:
                    issues.insert(0, "同票字段冲突。")
                row["detail"] = "\n".join(issues)
        if report["items"] or report["scanned"]:
            self.data["import_runs"].append(deepcopy(report))

    def _available_mail_pdf(self, item, row):
        digest = row.get("file_hash")
        original_name = safe_name(row["file"])
        for file in item["files"]:
            if file_format(file["path"]) != ".pdf" or file.get("recognition", {}).get("ocr_unconfirmed"):
                continue
            if (digest and file["hash"] != digest) or (not digest and file["name"] != original_name):
                continue
            try:
                self.original(file)
            except (UserError, OSError):
                continue
            return True
        return False

    def _historical_mail_pdfs(self, keys):
        if not keys:
            return set()
        invoices = {item["id"]: item for item in self.data["invoices"]
                    if _protects_identity(item) and item["verification"]["passed"]}
        found = set()
        for report in reversed(self.data["import_runs"]):
            if report["kind"] != "mail":
                continue
            for row in report["items"]:
                key = _mail_message_key(row)
                if key not in keys or key in found or file_format(row["file"]) != ".pdf":
                    continue
                item = invoices.get(row.get("invoice_id"))
                if item and self._available_mail_pdf(item, row):
                    found.add(key)
            if found == keys:
                break
        return found

    def _import_mail_row(self, index, report, row, *, local_eml=None, skip_ofd=False):
        metadata = {key: value for key, value in row.items()
                    if key not in {"attachment", "error", "name", "payload", "original_message", "xml_in_message"}}
        if skip_ofd:
            note = SKIPPED_MAIL_OFD + ("可按主题在邮箱中查看附件。" if local_eml is None else "")
            self._record_import_note(report, metadata, note)
            return
        name = row.get("name", row["file"])
        error = row.get("error")
        if not error:
            try:
                if local_eml is None:
                    source = " / ".join(row[key] for key in ("mailbox", "folder", "message_id") if row.get(key))
                    payload = row["payload"].read_bytes()
                else:
                    source = f"{local_eml} / {name}"
                    payload = row["payload"]
                return self._record_import(index, report, metadata, payload, fingerprint(payload), source, name, mail_attachment=True)
            except (UserError, OSError) as exc:
                error = str(exc)
        if row.get("xml_in_message"):
            error += " 可按主题到邮箱中查看原邮件。"
        elif original := row.get("original_message"):
            try:
                data = original.read_bytes()
                digest = fingerprint(data)
                if self.library:
                    relative = self._save_library_file(self.library, f"{digest}.eml", data, digest, MAIL_ORIGINALS)
                    metadata["mail_object"] = {"path": relative, "hash": digest, "storage": "library"}
                else:
                    relative = f"objects/{digest}.eml"
                    self._ensure_original(self._object_path(relative), data, digest)
                    metadata["mail_object"] = {"path": relative, "hash": digest}
            except (UserError, OSError):
                error += " 原邮件副本保存失败，请在邮箱中按主题查找。"
        self._record_import_error(report, metadata, error)

    def _import_mail_files(self, index, report, rows, progress=None, *, local_eml=None):
        first = len(report["items"])
        positions = []
        ofd_rows = []
        pdf_rows = {}
        for position, row in enumerate(rows):
            suffix = _mail_file_suffix(row)
            key = local_eml or _mail_message_key(row)
            if suffix == ".ofd":
                ofd_rows.append((position, row, key))
                continue
            if progress:
                progress(f"正在核验邮件文件 {len(report['items']) - first + 1} / {len(rows)}…")
            entry = self._import_mail_row(index, report, row, local_eml=local_eml)
            positions.append(position)
            if suffix == ".pdf" and key is not None and entry:
                pdf_rows.setdefault(key, []).append(entry)
        ofd_messages = {key for _, _, key in ofd_rows if key is not None}
        pdf_messages = {key for key in ofd_messages
                        if any(self._available_mail_pdf(index.originals[entry["file_hash"]], entry)
                               for entry in pdf_rows.get(key, ()))}
        pdf_messages.update(self._historical_mail_pdfs({key for key in ofd_messages - pdf_messages if isinstance(key, tuple)}))
        # 先核验 PDF，然后按原顺序显示每个邮件文件的结果。
        for number, (position, row, key) in enumerate(ofd_rows, len(positions) + 1):
            if progress:
                progress(f"正在核验邮件文件 {number} / {len(rows)}…")
            self._import_mail_row(index, report, row, local_eml=local_eml, skip_ofd=key in pdf_messages)
            positions.append(position)
        if ofd_rows:
            ordered = [None] * len(rows)
            for position, entry in zip(positions, report["items"][first:], strict=True):
                ordered[position] = entry
            report["items"][first:] = ordered

    def import_mail(self, revision, source, scanned, rows, progress=None):
        result = self._import_report("mail", source, scanned)
        if not rows and not scanned:
            return result
        with self.change(revision):
            self._import_mail_files(_ImportIndex(self.data["invoices"]), result, rows, progress)
            self._save_import_report(result)
        return result

    def import_folder(self, revision: int, directory: str, recursive: bool = False, progress=None) -> dict:
        source = Path(directory).expanduser().resolve()
        if not source.is_dir():
            raise UserError("导入目录不存在。")
        if self.library and (not source.is_relative_to(self.library) or source.is_relative_to(self.library / MAIL_ORIGINALS)):
            raise UserError("请选择当前发票资料库或其子文件夹。")
        # 扫描时跳过数据库管理的目录，避免重复导入工具保存的文件。
        managed = {self.root / p for p in ("objects", "exports", "history", "backups")}
        skipped = {p for p in managed if not source.is_relative_to(p)}
        result = self._import_report("directory", str(source))
        source_hashes = {}
        files = []
        if recursive:
            for base, dirs, names in os.walk(source, followlinks=False):
                dirs[:] = [d for d in dirs if not is_directory_link(Path(base) / d)
                           and d not in {".git", "node_modules", ".venv", "__pycache__", MAIL_ORIGINALS}
                           and (Path(base) / d).resolve() not in skipped]
                files.extend(Path(base) / n for n in names if Path(n).suffix.lower() in SUPPORTED | {".eml"})
                if len(files) > 500:
                    raise UserError("单次最多扫描 500 个文件，请缩小导入范围。")
        else:
            files = [p for p in source.iterdir() if p.suffix.lower() in SUPPORTED | {".eml"} and p.is_file()]
        if len(files) > 500:
            raise UserError("单次最多扫描 500 个文件，请缩小导入范围。")
        result["scanned"] = len(files)
        if not files:
            return result
        with self.change(revision):
            lookup = _ImportIndex(self.data["invoices"])
            for number, path in enumerate(sorted(files), 1):
                if progress:
                    progress(f"正在核验本地文件 {number} / {len(files)}…")
                row = {"file": str(path.relative_to(source))}
                try:
                    resolved = path.resolve()
                    if not resolved.is_relative_to(source) or path.is_symlink():
                        raise UserError("跳过指向导入目录外的文件。")
                    if any(resolved.is_relative_to(p) for p in skipped):
                        continue
                    if path.stat().st_size > MAX_BYTES:
                        raise UserError("文件超过 20 MB。")
                    data = path.read_bytes()
                    if path.suffix.lower() == ".eml":
                        rows = [{"file": f"{row['file']} / {name}", "name": name, "payload": payload}
                                for name, payload in eml_attachments(data)]
                        if rows:
                            self._import_mail_files(lookup, result, rows, local_eml=path)
                        else:
                            self._record_import_note(result, row, "本地 EML 未发现支持的附件。正文链接可从邮箱收票，或手动下载后导入。")
                    else:
                        digest = fingerprint(data)
                        self._record_import(lookup, result, row, data, digest, str(path), path.name, resolved if self.library else None)
                        source_hashes.setdefault(digest, []).append(path)
                except (UserError, OSError) as exc:
                    self._record_import_error(result, row, exc)
            conflicts = self.duplicate_ids()
            for item in self.data["invoices"]:
                if item["id"] in conflicts:
                    continue
                for file in item["files"]:
                    digest = file["hash"]
                    paths = source_hashes.get(digest, ())
                    if len(paths) < 2:
                        continue
                    keep = next((p for p in paths if file_format(p) == file_format(file["path"])), None)
                    if keep is not None:
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
            raise UserError("原票尚未同步或路径无效，请等网盘完成文件下载。")
        data = path.read_bytes()
        if fingerprint(data) != file["hash"]:
            raise UserError("原票校验失败，文件可能被修改；请恢复原件后重试。")
        return path, data

    def original(self, file: dict) -> Path:
        return self._read_original(file)[0]

    def export(self, batch_id: str) -> Path:
        with self.mutex:
            self._check()
            batch = self.batch(batch_id)
            if batch["status"] == "cancelled":
                raise UserError("已取消批次不能导出。")
            originals = [(n, item, f, self.original(f)) for n, item in enumerate(batch["items"], 1) for f in item["files"]]
            csv_buffer = io.StringIO(newline="")
            writer = csv.writer(csv_buffer)
            writer.writerow(["序号", "发票号码", "发票代码", "开票日期", "购买方", "购买方税号", "价税合计", "文件数"])
            for n, item in enumerate(batch["items"], 1):
                writer.writerow([n, csv_cell(item["number"]), csv_cell(item["code"]), item["date"], csv_cell(item["buyer_name"]),
                                 csv_cell(item["buyer_tax_id"]), yuan(item["amount_cents"]), len(item["files"])])
            path = self.root / "exports" / f"batch-{batch_id}-{uid()}.zip"
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
