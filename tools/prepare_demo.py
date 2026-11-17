"""Create an isolated demo library from the included synthetic samples."""
from pathlib import Path
import argparse
import json
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from invoice_workbench.domain import yuan
from invoice_workbench.store import Store
from invoice_workbench.workflow import import_directory


def prepare(directory: Path):
    directory = directory.resolve()
    if directory.exists():
        raise ValueError("Demo directory already exists. Choose a new directory; no existing data is overwritten.")
    store = Store(directory)
    try:
        report = import_directory(store, store.data["revision"], ROOT / "samples")
        store.set_company(store.data["revision"], "示例科技有限公司", "91440300MA0000000X")
        for item in list(store.data["invoices"]):
            if item.get("number") and item.get("amount_cents"):
                store.review(store.data["revision"], item["id"], {**item, "amount": yuan(item["amount_cents"])})
        print(json.dumps({"directory": str(directory), "report": report, "recommendation": store.recommend("1000.00")}, ensure_ascii=True))
    finally:
        store.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", type=Path, required=True)
    prepare(parser.parse_args().directory)
