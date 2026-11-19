from collections import Counter
from copy import deepcopy
from pathlib import Path
import argparse
import csv
import hashlib
import io
import json
import os
import sys
import tempfile
from time import perf_counter
import zipfile

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from invoice_workbench.domain import UserError, yuan
from invoice_workbench.filesystem import is_directory_link
from invoice_workbench.labels import selectable
from invoice_workbench.store import Store, uid
from invoice_workbench.workflow import import_directory, remove_duplicate_files


def hashes(source):
    result = {}
    for base, dirs, names in os.walk(source, followlinks=False):
        dirs[:] = [d for d in dirs if not is_directory_link(Path(base) / d)]
        for name in names:
            path = Path(base) / name
            if not path.is_symlink():
                result[path.relative_to(source).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def check_export(store, batch_id):
    batch = store.batch(batch_id)
    before = deepcopy(store.data)
    with zipfile.ZipFile(store.export(batch_id)) as archive:
        names = archive.namelist()
        assert all("/" not in n and "\\" not in n for n in names), "导出包含子目录"
        assert len(names) == len({n.casefold() for n in names}), "导出文件名重复"
        assert len(names) == 1 + sum(len(i["files"]) for i in batch["items"]), "导出文件数量不匹配"
        records = list(csv.DictReader(io.StringIO(archive.read("明细.csv").decode("utf-8-sig"))))
        assert [r["价税合计"] for r in records] == [yuan(i["amount_cents"]) for i in batch["items"]], "CSV 金额不匹配"
        assert [r["发票号码"] for r in records] == [i["number"] for i in batch["items"]], "CSV 票号不匹配"
        actual = Counter(hashlib.sha256(archive.read(n)).hexdigest() for n in names if n != "明细.csv")
        expected = Counter(f["hash"] for i in batch["items"] for f in i["files"])
        assert actual == expected, "导出原件哈希不匹配"
    assert store.data == before, "导出改变了发票状态"


def check_business_flow(store, records, local_root):
    verified = [i for i in records if i["verification"]["passed"] and not i["duplicate"]]
    result = {"selection_export_and_return_checked": False, "copied_duplicates_deleted": 0}
    if not verified:
        return result
    company = verified[0]
    store.set_company(store.data["revision"], company["buyer_name"], company["buyer_tax_id"])
    candidates = [i for i in store.view()["invoices"] if selectable(i)]
    total = yuan(sum(i["amount_cents"] for i in candidates))
    selection = store.recommend(total)
    assert set(selection["ids"]) == {i["id"] for i in candidates}, "选票记录不匹配"
    batch_id = store.create_batch(store.data["revision"], selection["ids"], total, total, uid())
    history = deepcopy(store.batch(batch_id))
    check_export(store, batch_id)
    returned = candidates[0]
    store.set_invoice_status(store.data["revision"], returned["id"], "submitted")
    store.set_invoice_status(store.data["revision"], returned["id"], "returned")
    reuse = store.recommend(yuan(returned["amount_cents"]))
    assert reuse["ids"] == [returned["id"]], "退回发票无法单独再次选用"
    store.create_batch(store.data["revision"], reuse["ids"], yuan(returned["amount_cents"]), yuan(returned["amount_cents"]), uid())
    assert store.batch(batch_id) == history, "退回改变了历史批次"
    result["selection_export_and_return_checked"] = True
    # 删除验证仅操作本次创建的样本副本。
    file = returned["files"][0]
    payload = store.original(file).read_bytes()
    source_copy = local_root / "sample-copies"
    source_copy.mkdir()
    suffix = Path(file["name"]).suffix
    for name in ("a", "b", "c"):
        (source_copy / f"{name}{suffix}").write_bytes(payload)
    report = import_directory(store, store.data["revision"], source_copy)
    assert len(report["duplicate_files"]) == 2, "样本副本识别数量不匹配"
    result["copied_duplicates_deleted"] = remove_duplicate_files(store, store.data["revision"], report)
    remaining = list(source_copy.iterdir())
    assert len(remaining) == 1 and remaining[0].read_bytes() == payload, "清理后保留的原件不匹配"
    return result


def validate(source):
    source = source.expanduser().resolve()
    if not source.is_dir():
        raise UserError("指定的业务验证目录不存在。")
    before = hashes(source)
    test_root = ROOT / ".test-tmp"
    existed = test_root.exists()
    test_root.mkdir(exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(prefix="business-validation-", dir=test_root) as directory:
            local_root = Path(directory)
            store = Store(local_root / "workspace")
            try:
                started = perf_counter()
                first = import_directory(store, store.data["revision"], source)
                import_seconds = round(perf_counter() - started, 2)
                assert not first["errors"], first["errors"]
                records = store.view()["invoices"]
                assert records, "没有导入发票"
                for item in records:
                    for file in item["files"]:
                        store.original(file)
                repeat = import_directory(store, store.data["revision"], source)
                assert not repeat["errors"], repeat["errors"]
                assert len(store.data["invoices"]) == len(records) and repeat["imported"] == 0, "重复导入新增了记录"
                checks = check_business_flow(store, records, local_root)
                workspace = store.root
            finally:
                store.close()
            reopened = Store(workspace)
            try:
                assert len(reopened.view()["invoices"]) == len(records), "重新打开后记录数量不匹配"
            finally:
                reopened.close()
            result = {"source_files": len(before), "invoice_records": len(records), "import_seconds": import_seconds,
                      "automatically_verified": sum(i["verification"]["passed"] for i in records),
                      "verification_issues": sum(not i["verification"]["passed"] for i in records),
                      "source_duplicate_candidates": len(first["duplicate_files"]),
                      "repeat_import_added": repeat["imported"], **checks}
    finally:
        unchanged = before == hashes(source)
        if not existed and test_root.exists() and not any(test_root.iterdir()):
            test_root.rmdir()
        if not unchanged:
            raise AssertionError("业务源目录的文件内容或路径发生变化")
    result["source_bytes_and_paths_unchanged"] = unchanged
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="只读验证指定业务目录，临时副本与工作区自动清理。")
    parser.add_argument("--source", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(validate(args.source), ensure_ascii=False))
