from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
import csv
import hashlib
import io
import json
import os
import re
import socket
import threading
import uuid
import zipfile

from .domain import UserError, choose_invoices, company_matches, identity, money, validate_invoice, yuan
from .extract import MAX_BYTES, SUPPORTED, eml_attachments, extract
from .filesystem import is_directory_link
from .session import WriterGuard, process_running


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
                if self.data.get("schema") != 1 or not isinstance(self.data.get("invoices"), list) or not isinstance(self.data.get("batches"), list):
                    raise UserError("资料格式不兼容，请保留原文件并检查备份。")
                self.expected = fingerprint(raw)
            else:
                self.data = {"schema": 1, "revision": 0, "company": {"name": "", "tax_id": ""}, "invoices": [], "batches": []}
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
        current = fingerprint(self.state_path.read_bytes()) if self.state_path.exists() else None
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

    def _save(self):
        self._check()
        new_data = deepcopy(self.data)
        new_data["revision"] += 1
        raw = json.dumps(new_data, ensure_ascii=False, indent=2).encode("utf-8")
        if self.state_path.exists():
            history = self.root / "history" / f"{self.data['revision']:08d}-{uid()}.json"
            history.write_bytes(self.state_path.read_bytes())
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
    def change(self, revision: int):
        with self.mutex:
            self._check()
            if revision != self.data["revision"]:
                raise UserError("页面资料已更新，请刷新后重试。")
            before = deepcopy(self.data)
            try:
                yield
                self._save()
            except Exception:
                self.data = before
                raise

    def view(self) -> dict:
        with self.mutex:
            self._check()
            data = deepcopy(self.data)
            occupied = self.occupied()
            duplicates = self.duplicate_ids()
            for i in data["invoices"]:
                i["batch_id"] = occupied.get(i["id"])
                i["company_match"] = company_matches(i, data["company"])
                i["duplicate"] = i["id"] in duplicates
            data["data_directory"] = str(self.root)
            return data

    def occupied(self) -> dict:
        return {item["id"]: b["id"] for b in self.data["batches"] if b["status"] != "cancelled" for item in b["items"]}

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

    def _object_path(self, relative: str) -> Path:
        path = (self.root / relative).resolve()
        if not path.is_relative_to((self.root / "objects").resolve()):
            raise UserError("原件路径无效，请检查工作区索引。")
        return path

    def _ensure_original(self, relative: str, data: bytes, digest: str):
        destination = self._object_path(relative)
        if destination.exists():
            if fingerprint(destination.read_bytes()) != digest:
                raise UserError("存储原件内容已改变，请先检查同步或文件完整性；现有文件未被覆盖。")
            return
        # Restore missing objects without replacing existing files, including
        # objects arriving from cloud synchronization during import.
        with destination.open("xb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())

    def _ingest(self, name: str, data: bytes, source: str) -> str:
        suffix = Path(name).suffix.lower()
        digest = fingerprint(data)
        existing = next((i for i in self.data["invoices"] if any(f["hash"] == digest for f in i["files"])), None)
        if existing:
            for file in existing["files"]:
                if file["hash"] == digest:
                    self._ensure_original(file["path"], data, digest)
            if source not in existing["sources"]:
                existing["sources"].append(source)
            # Parser upgrades may fill missing fields on unreviewed records.
            # Confirmed or user-supplied values are never replaced on re-import.
            if not existing.get("reviewed") and existing["id"] not in self.occupied():
                extracted = extract(data, suffix)
                for field in ("number", "code", "date", "amount_cents", "buyer_name", "buyer_tax_id", "seller_name"):
                    if not existing.get(field) and extracted.get(field):
                        existing[field] = extracted[field]
            return "duplicates"
        extracted = extract(data, suffix)
        new = {"id": uid(), "number": "", "code": "", "date": "", "buyer_name": "", "buyer_tax_id": "",
               "seller_name": "", "amount_cents": 0, "note": "", "reviewed": False, "archived": False, "created": now(),
               "files": [], "sources": [source], **extracted}
        key = identity(new)
        target = next((i for i in self.data["invoices"] if key and identity(i) == key and not i.get("archived")), None)
        conflicts = []
        if target:
            conflicts = [k for k in ("amount_cents", "buyer_tax_id", "buyer_name", "date")
                         if new.get(k) and target.get(k) and new[k] != target[k]]
        if target and conflicts:
            new["warnings"].append("与已存在的同票号记录字段冲突，请核对原件后人工处理。")
            new["duplicate_of"] = target["id"]
            conflicting_target = target
            target = None
        relative = f"objects/{digest}{suffix}"
        self._ensure_original(relative, data, digest)
        file = {"id": uid(), "name": safe_name(name), "path": relative, "hash": digest, "size": len(data)}
        if conflicts and conflicting_target["id"] not in self.occupied():
            conflicting_target["reviewed"] = False
            conflicting_target["warnings"].append("发现同票号的冲突文件，请检查新导入记录。")
        if target:
            target["files"].append(file)
            if source not in target["sources"]:
                target["sources"].append(source)
            for field in ("number", "code", "date", "buyer_name", "buyer_tax_id", "seller_name", "amount_cents"):
                if not target.get(field) and new.get(field):
                    target[field] = new[field]
                    target["reviewed"] = False
            return "merged"
        new["files"].append(file)
        self.data["invoices"].append(new)
        return "imported"

    def import_folder(self, revision: int, directory: str, recursive: bool = False) -> dict:
        source = Path(directory).expanduser().resolve()
        if not source.is_dir():
            raise UserError("导入目录不存在。")
        # Scanning the library must not recursively ingest its own stored files.
        # An explicitly selected verified backup, however, is a valid import source.
        managed = {self.root / p for p in ("objects", "exports", "history", "backups")}
        skipped = {p for p in managed if not source.is_relative_to(p)}
        result = {"scanned": 0, "imported": 0, "merged": 0, "duplicates": 0, "errors": [], "notes": []}
        files = []
        if recursive:
            for base, dirs, names in os.walk(source, followlinks=False):
                dirs[:] = [d for d in dirs if not is_directory_link(Path(base) / d)
                           and d not in {".git", "node_modules", ".venv", "__pycache__"}
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
            for path in sorted(files):
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
                        attachments, notes = eml_attachments(data)
                        result["notes"].extend(f"{path.name}: {note}" for note in notes)
                        for name, payload in attachments:
                            try:
                                result[self._ingest(name, payload, str(path) + " / " + name)] += 1
                            except (UserError, OSError) as exc:
                                result["errors"].append({"file": f"{path.name} / {name}", "error": str(exc)})
                    else:
                        result[self._ingest(path.name, data, str(path))] += 1
                except (UserError, OSError) as exc:
                    result["errors"].append({"file": path.name, "error": str(exc)})
        return result

    def review(self, revision: int, invoice_id: str, fields: dict):
        cleaned = validate_invoice(fields)
        with self.change(revision):
            item = self.invoice(invoice_id)
            if invoice_id in self.occupied():
                raise UserError("该票已属于报销批次，无法修改。取消批次后可重新核对。")
            duplicates = [i for i in self.data["invoices"] if i["id"] != invoice_id and not i.get("archived") and identity(i) == identity(cleaned)]
            if duplicates:
                raise UserError("存在同号码与代码的发票，暂不能标记可用。请修正错误票号，或用“合并同票文件”归并。")
            item.update(cleaned)
            item["reviewed"] = True
            item["reviewed_at"] = now()

    def archive(self, revision: int, invoice_id: str, archived: bool):
        with self.change(revision):
            item = self.invoice(invoice_id)
            if invoice_id in self.occupied():
                raise UserError("该票已被报销批次占用，不能移出候选。")
            item["archived"] = archived
            item["reviewed"] = False

    def duplicate_ids(self) -> set[str]:
        groups = {}
        for item in self.data["invoices"]:
            key = identity(item)
            if key and not item.get("archived"):
                groups.setdefault(key, []).append(item["id"])
        return {i for group in groups.values() if len(group) > 1 for i in group}

    def merge(self, revision: int, source_id: str, target_id: str):
        with self.change(revision):
            if source_id == target_id:
                raise UserError("请选择另一张记录。")
            if {source_id, target_id} & self.occupied().keys():
                raise UserError("已被批次占用的发票不能合并。")
            source, target = self.invoice(source_id), self.invoice(target_id)
            target["files"].extend(source["files"])
            target["sources"] = sorted(set(source["sources"] + target["sources"]))
            target["reviewed"] = False
            target["warnings"].append("已手动合并文件；保留目标记录的字段，请重新核对。")
            self.data["invoices"].remove(source)

    def recommend(self, target: str, force_ids=None, exclude_ids=None) -> dict:
        with self.mutex:
            self._check()
            occupied = self.occupied()
            duplicates = self.duplicate_ids()
            available = [i for i in self.data["invoices"] if i["reviewed"] and not i.get("archived") and i["id"] not in occupied and i["id"] not in duplicates
                         and company_matches(i, self.data["company"])]
            return choose_invoices(available, money(target), force_ids, exclude_ids)

    def create_batch(self, revision: int, ids: list[str], target: str, declared: str, request_id: str) -> str:
        if not re.fullmatch(r"[a-f0-9]{32}", request_id):
            raise UserError("批次请求标识无效，请刷新页面。")
        with self.mutex:
            prior = next((b for b in self.data["batches"] if b["request_id"] == request_id), None)
            if prior:
                return prior["id"]
            with self.change(revision):
                if not ids or len(ids) != len(set(ids)):
                    raise UserError("请选择至少一张发票，且不能重复选择。")
                occupied = self.occupied()
                duplicates = self.duplicate_ids()
                items = [self.invoice(i) for i in ids]
                if any(i["id"] in occupied or i["id"] in duplicates or i.get("archived") or not i["reviewed"] or not company_matches(i, self.data["company"]) for i in items):
                    raise UserError("所选发票状态或公司信息已变化，请重新选票。")
                total = sum(i["amount_cents"] for i in items)
                goal, claim = money(target), money(declared)
                if total < goal or claim > total:
                    raise UserError("票面合计必须达到目标，实际申报额不能超过票面合计。")
                for i in items:
                    for f in i["files"]:
                        self.original(f)
                batch_id = uid()
                self.data["batches"].append({"id": batch_id, "request_id": request_id, "created": now(), "status": "confirmed",
                    "company": deepcopy(self.data["company"]), "target_cents": goal, "declared_cents": claim,
                    "total_cents": total, "items": deepcopy(items), "events": [{"status": "confirmed", "at": now()}]})
                return batch_id

    def batch(self, batch_id: str) -> dict:
        batch = next((b for b in self.data["batches"] if b["id"] == batch_id), None)
        if not batch:
            raise UserError("报销批次不存在。")
        return batch

    def transition(self, revision: int, batch_id: str, status: str):
        transitions = {"confirmed": {"submitted", "cancelled"}, "submitted": {"reimbursed", "returned", "cancelled"},
                       "returned": {"submitted", "cancelled"}, "reimbursed": set(), "cancelled": set()}
        with self.change(revision):
            batch = self.batch(batch_id)
            if status not in transitions[batch["status"]]:
                raise UserError("该报销状态不允许这样变更。")
            batch["status"] = status
            batch["events"].append({"status": status, "at": now()})

    def original(self, file: dict) -> Path:
        path = self._object_path(file["path"])
        if not path.is_file():
            raise UserError("原票尚未同步或路径无效，请等网盘完成文件下载。")
        if fingerprint(path.read_bytes()) != file["hash"]:
            raise UserError("原票校验失败，文件可能被修改；请恢复原件后重试。")
        return path

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
            path = self.root / "exports" / f"batch-{batch_id}.zip"
            temp = path.with_name(f".export-{uid()}.tmp")
            try:
                with zipfile.ZipFile(temp, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                    archive.writestr("明细.csv", csv_buffer.getvalue().encode("utf-8-sig"))
                    archive.writestr("批次记录.json", json.dumps(batch, ensure_ascii=False, indent=2))
                    archive.writestr("报销说明.txt", f"批次: {batch_id}\n目标: {yuan(batch['target_cents'])} 元\n票面: {yuan(batch['total_cents'])} 元\n申报: {yuan(batch['declared_cents'])} 元\n整张票归属本批次；超额不结转。导出不会更新报销进度。\n")
                    for n, item, f, original in originals:
                        archive.write(original, f"原票/{n:03d}_{item['number']}/{f['id'][:8]}_{safe_name(f['name'])}")
                os.replace(temp, path)
            finally:
                temp.unlink(missing_ok=True)
            return path
