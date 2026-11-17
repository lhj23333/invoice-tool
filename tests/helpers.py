from pathlib import Path
import tempfile
import uuid

from invoice_workbench.store import Store

BUYER = "示例科技有限公司"
TAX = "91440300MA0000000X"


def xml_invoice(number="26000000000000000001", amount="600.00", buyer=BUYER, tax=TAX, date="2026-10-01", extra=""):
    return f'''<?xml version="1.0" encoding="UTF-8"?>
<Invoice>
  <InvoiceNumber>{number}</InvoiceNumber><IssueDate>{date}</IssueDate>
  <BuyerInformation><BuyerName>{buyer}</BuyerName><BuyerTaxID>{tax}</BuyerTaxID></BuyerInformation>
  <SellerName>合成样本商户</SellerName><TotalWithTax>{amount}</TotalWithTax>{extra}
</Invoice>'''.encode("utf-8")


def write_invoice(folder: Path, name="invoice.xml", **fields):
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_bytes(xml_invoice(**fields))
    return path


def review_all(store):
    for item in list(store.data["invoices"]):
        fields = {**item, "amount": f"{item['amount_cents'] / 100:.2f}"}
        store.review(store.data["revision"], item["id"], fields)


class StoreFixture:
    """Shared isolated workspace setup and helpers, without test cases."""
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.input = self.root / "input"
        self.input.mkdir()
        self.store = Store(self.root / "library")
        self.store.set_company(self.rev(), BUYER, TAX)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def rev(self):
        return self.store.data["revision"]

    def import_one(self, **fields):
        write_invoice(self.input, **fields)
        self.store.import_folder(self.rev(), str(self.input))
        return self.store.data["invoices"][0]

    def make_batch(self, amount="600.00", target="500.00"):
        self.import_one(amount=amount)
        review_all(self.store)
        ids = [i["id"] for i in self.store.data["invoices"]]
        return self.store.create_batch(self.rev(), ids, target, target, uuid.uuid4().hex)

