"""Phase 1 提取优化回归：归一化比对、附页免 OCR、XML 汇总金额、OCR 视觉宽度、金额兜底。"""

import re
import unittest
from unittest import mock

from invoice_workbench.domain import comparable
from invoice_workbench.extract import _complete_pdf, extract, from_text
from invoice_workbench.recognition import _display_width, _text_lines
from tests.helpers import TAX
from tests.test_review_regressions import invoice_text, pdf_document

NUMBER = "26000000000000000001"
# 文字层与 OCR 只差全/半角括号和税号大小写。
LAYER_TEXT = ("Invoice Number: " + NUMBER + "\nInvoice Date: 2026-10-01\n"
              "Buyer\nName: 示例（深圳）科技有限公司\nTax ID: " + TAX + "\n"
              "Seller\nName: 合成样本商户")
OCR_TEXT = ("Invoice Number: " + NUMBER + "\nInvoice Date: 2026-10-01\n"
            "Buyer\nName: 示例(深圳)科技有限公司\nTax ID: " + TAX.lower() + "\n"
            "Seller\nName: 合成样本商户\nTotal Amount: 600.00")
APPENDIX_TEXT = ("销货清单\n序号 商品名称 规格型号 数量 单价 金额\n"
                 "1 Widget A 10 20.00 200.00\n2 Widget B 5 30.00 150.00\n"
                 "3 Widget C 2 100.00 200.00\n合计 550.00")
ASCII_APPENDIX = ("Packing List\nNo Item Name Model Qty Unit Price Amount\n"
                  "1 Widget A 10 20.00 200.00\n2 Widget B 5 30.00 150.00\n"
                  "3 Widget C 2 100.00 200.00\nSubtotal 550.00")


def fake_ocr(pair, calls=None):
    pair = (pair, pair) if isinstance(pair, str) else pair

    def pages(data, indices):
        if calls is not None:
            calls.append(list(indices))
        return iter([pair for _ in indices])
    return pages


class ComparableFieldTests(unittest.TestCase):
    def test_width_whitespace_and_case_differences_compare_equal(self):
        self.assertEqual(comparable("buyer_name", "示例（深圳）科技有限公司"),
                         comparable("buyer_name", "示例 ( 深圳 ) 科技有限公司"))
        self.assertEqual(comparable("seller_name", "甲（乙）公司"), comparable("seller_name", "甲(乙)公司"))
        self.assertEqual(comparable("buyer_tax_id", "91440300ma0000000x"),
                         comparable("buyer_tax_id", " 9144 0300MA0000000X "))
        self.assertNotEqual(comparable("buyer_name", "示例（深圳）科技有限公司"),
                            comparable("buyer_name", "示例（广州）科技有限公司"))


class LayerOcrComparisonTests(unittest.TestCase):
    def complete(self, ocr, calls=None):
        with mock.patch("invoice_workbench.extract.recognize_pdf_pages", fake_ocr(ocr, calls)):
            return _complete_pdf(b"", [LAYER_TEXT], from_text(LAYER_TEXT))

    def test_harmless_width_difference_is_not_reported_as_conflict(self):
        # 缺少价税合计，文字层必然触发一次 OCR 交叉核验。
        calls = []
        parsed, ocr = self.complete(OCR_TEXT, calls)
        self.assertEqual(calls, [[0]])
        self.assertTrue(ocr)
        self.assertEqual(parsed["warnings"], [])
        self.assertFalse(parsed.get("ocr_unconfirmed"))
        self.assertEqual(parsed["amount_cents"], 60000)
        self.assertEqual(parsed["buyer_name"], "示例（深圳）科技有限公司")
        self.assertEqual(parsed["buyer_tax_id"], TAX)

    def test_real_difference_is_still_reported(self):
        parsed, _ = self.complete(OCR_TEXT.replace("2026-10-01", "2026-10-02"))
        self.assertIn("第 1 页文字层与图像识别结果不一致。", parsed["warnings"])


class MultiPageOcrTests(unittest.TestCase):
    def test_pdf_with_packing_list_page_needs_no_page_ocr(self):
        pages = [invoice_text(NUMBER), ASCII_APPENDIX]
        with mock.patch("invoice_workbench.extract.recognize_pdf_pages",
                        side_effect=AssertionError("纯文字附页不应触发 OCR")):
            parsed = extract(pdf_document(pages), ".pdf")
        self.assertEqual(parsed["warnings"], [])
        self.assertNotIn("recognition_version", parsed)
        self.assertEqual(parsed["amount_cents"], 60000)
        self.assertEqual(parsed["buyer_name"], "Example Company")
        self.assertEqual(parsed["number"], NUMBER)

    def test_chinese_appendix_is_skipped_without_ocr(self):
        parts = [invoice_text(NUMBER), APPENDIX_TEXT]
        with mock.patch("invoice_workbench.extract.recognize_pdf_pages",
                        side_effect=AssertionError("纯文字附页不应触发 OCR")):
            parsed, ocr = _complete_pdf(b"", parts, from_text("\n".join(parts)))
        self.assertFalse(ocr)
        self.assertEqual(parsed["warnings"], [])
        self.assertEqual(parsed["amount_cents"], 60000)

    def test_plain_text_pdf_never_renders_pages(self):
        with mock.patch("invoice_workbench.extract.recognize_pdf_pages",
                        side_effect=AssertionError("完整文字层不应触发 OCR")):
            parsed = extract(pdf_document([invoice_text(NUMBER)]), ".pdf")
        self.assertEqual(parsed["amount_cents"], 60000)
        self.assertNotIn("recognition_version", parsed)

    def test_sparse_scan_page_still_runs_ocr(self):
        calls = []
        pages = [invoice_text(NUMBER), "Scanned invoice footer"]
        with mock.patch("invoice_workbench.extract.recognize_pdf_pages", fake_ocr(("", ""), calls)):
            extract(pdf_document(pages), ".pdf")
        self.assertEqual(calls, [[1]])


def xml_document(body):
    return ("<Invoice>" + body + "</Invoice>").encode("utf-8")


XML_HEAD = ("<InvoiceNumber>" + NUMBER + "</InvoiceNumber><IssueDate>2026-10-01</IssueDate>"
            "<BuyerInformation><BuyerName>示例科技有限公司</BuyerName>"
            "<BuyerTaxID>" + TAX + "</BuyerTaxID></BuyerInformation>")


class XmlAmountTests(unittest.TestCase):
    def test_detail_line_amounts_never_override_summary_total(self):
        for container, detail in (("Items", "Item"), ("GoodsList", "Line"), ("Details", "Detail")):
            with self.subTest(container=container):
                body = (XML_HEAD + "<" + container + ">"
                        + "".join("<" + detail + "><AmountWithTax>" + amount + "</AmountWithTax></" + detail + ">"
                                  for amount in ("100.00", "500.00"))
                        + "</" + container + "><TotalWithTax>600.00</TotalWithTax>")
                parsed = extract(xml_document(body), ".xml")
                self.assertEqual(parsed["amount_cents"], 60000)
                self.assertEqual(parsed["warnings"], [])

    def test_detail_amount_is_fallback_only_without_summary(self):
        body = XML_HEAD + "<Items><Item><AmountWithTax>100.00</AmountWithTax></Item></Items>"
        parsed = extract(xml_document(body), ".xml")
        self.assertEqual(parsed["amount_cents"], 10000)

    def test_digital_invoice_item_information_is_not_the_total(self):
        data = xml_document(
            "<EInvoiceData>" + XML_HEAD
            + "<IssuItemInformation><ItemName>A</ItemName><AmountWithTax>100.00</AmountWithTax></IssuItemInformation>"
            + "<IssuItemInformation><ItemName>B</ItemName><AmountWithTax>500.00</AmountWithTax></IssuItemInformation>"
            + "<TotalTax-includedAmount>600.00</TotalTax-includedAmount></EInvoiceData>")
        parsed = extract(data, ".xml")
        self.assertEqual(parsed["amount_cents"], 60000)
        self.assertEqual(parsed["warnings"], [])

    def test_abbreviated_detail_containers_are_recognised(self):
        for container in ("FPMX", "Mx", "GoodsInfo", "ItemList", "Spmx"):
            with self.subTest(container=container):
                body = (XML_HEAD + "<" + container + "><AmountWithTax>100.00</AmountWithTax></" + container + ">"
                        + "<TotalWithTax>600.00</TotalWithTax>")
                parsed = extract(xml_document(body), ".xml")
                self.assertEqual(parsed["amount_cents"], 60000)
                self.assertEqual(parsed["warnings"], [])

    def test_inconsistent_summary_amounts_are_warned(self):
        body = XML_HEAD + "<TotalWithTax>600.00</TotalWithTax><TotalTaxIncludedAmount>601.00</TotalTaxIncludedAmount>"
        parsed = extract(xml_document(body), ".xml")
        self.assertEqual(parsed["amount_cents"], 60000)
        self.assertTrue(any("不一致" in warning for warning in parsed["warnings"]), parsed["warnings"])


class AmountFallbackTests(unittest.TestCase):
    def test_currency_integer_and_one_decimal_amounts(self):
        for text, cents in (("价税合计（小写）¥1002", 100200),
                            ("价税合计（小写）￥600", 60000),
                            ("价税合计（小写）¥1,002.5", 100250),
                            ("Total Amount: ¥ 600", 60000)):
            with self.subTest(text=text):
                self.assertEqual(from_text(text)["amount_cents"], cents)

    def test_two_decimal_amount_is_never_truncated(self):
        self.assertEqual(from_text("价税合计（小写）¥1002.00")["amount_cents"], 100200)
        self.assertEqual(from_text("价税合计（小写）￥1002.50")["amount_cents"], 100250)

    def test_bare_integer_without_currency_is_not_guessed(self):
        self.assertNotIn("amount_cents", from_text("价税合计（小写）1002"))

    def test_negative_currency_amount_is_still_rejected(self):
        for text in ("价税合计（小写）-¥600", "价税合计（小写）¥-600"):
            with self.subTest(text=text):
                parsed = from_text(text)
                self.assertNotIn("amount_cents", parsed)
                self.assertTrue(parsed["warnings"])


class OcrLayoutTests(unittest.TestCase):
    def test_cjk_and_ascii_rows_align_to_the_same_visual_column(self):
        cells = [(10.0, 0.0, 20.0, 1.0, "名称:"), (10.0, 140.0, 20.0, 1.0, "ABC"),
                 (40.0, 0.0, 20.0, 1.0, "Name:"), (40.0, 140.0, 20.0, 1.0, "XYZ")]
        cjk, ascii = _text_lines(cells, 0).splitlines()
        # 一列 = 0.5 倍行高：第 14 个视觉列，宽字符按 2 列计，"名称:" 占 5 列，需 9 个空格。
        self.assertEqual(cjk, "名称:" + " " * 9 + "ABC")
        self.assertEqual(ascii, "Name:" + " " * 9 + "XYZ")
        self.assertEqual(_display_width(cjk[:cjk.index("ABC")]), ascii.index("XYZ"))

    def test_long_chinese_buyer_keeps_room_for_seller_column(self):
        buyer = "深圳市前海示例科技发展有限公司"
        cells = [(10.0, 0.0, 20.0, 1.0, "购 名称:"),
                 (10.0, 100.0, 20.0, 1.0, buyer),
                 (10.0, 460.0, 20.0, 1.0, "销 名称:"),
                 (10.0, 560.0, 20.0, 1.0, "销售公司")]
        line = _text_lines(cells, 0)
        # 长中文抬头后仍须保留 ≥3 空格的双栏间隙，否则 from_text 无法切分左右栏。
        self.assertRegex(line, re.escape(buyer) + r"[ \t]{3,}销 名称")
        parsed = from_text(line)
        self.assertEqual(parsed["buyer_name"], buyer)
        self.assertEqual(parsed["seller_name"], "销售公司")

    def test_visual_width_of_mixed_text(self):
        self.assertEqual(_display_width("名称:"), 5)
        self.assertEqual(_display_width("Name:"), 5)
        self.assertEqual(_display_width("（￥）"), 6)


if __name__ == "__main__":
    unittest.main()
