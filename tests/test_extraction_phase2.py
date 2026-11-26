"""Phase 2 回归：OCR 金额大小写交叉校验、OFD 结构层提取、并排买卖方版面、同票号未确认提示。"""

from io import BytesIO
from pathlib import Path
import unittest
from unittest import mock
from zipfile import ZIP_DEFLATED, ZipFile

from invoice_workbench.domain import UserError
from invoice_workbench.extract import (AMOUNT_UNCONFIRMED_WARNING, _ofd_structured_values, extract, from_recognition,
                                      from_text, inspect_mail_document)
from invoice_workbench.labels import selectable
from invoice_workbench.recognition import (AMOUNT_SCORE_FLOOR, EVIDENCE_VERSION, amount_cross_checked,
                                           chinese_amount, ofd_document)
from tests.helpers import BUYER, TAX, StoreFixture, write_invoice

NUMBER = "25442000000012345682"
INVOICE_TEXT = ("电子发票（增值税专用发票）\n"
                f"发票号码：{NUMBER} 开票日期：2025年11月11日\n"
                "购买方信息\n"
                f"名称：{BUYER}\n"
                f"统一社会信用代码/纳税人识别号：{TAX}\n"
                "销售方信息\n"
                "名称：深圳市云帆数据服务有限公司\n"
                "价税合计（大写）伍佰伍拾伍圆整（小写)¥555.00\n")
RELIABLE_TEXT = INVOICE_TEXT.replace("555.00", "□□□.□□")


def ocr_pages(raw, reliable, low_lines):
    return [(raw, reliable, low_lines)]


class ChineseAmountTests(unittest.TestCase):
    def test_uppercase_amounts_convert_exactly_to_cents(self):
        for text, cents in (("伍佰伍拾伍圆整", 55500),
                            ("叁佰壹拾捌圆整", 31800),
                            ("壹仟零贰拾圆整", 102000),
                            ("壹佰贰拾捌元伍角肆分", 12854),
                            ("壹拾壹万壹仟伍佰玖拾捌圆叁角玖分", 11159839),
                            ("壹万肆仟肆佰陆拾肆圆壹角壹分", 1446411),
                            ("壹亿零壹圆零壹分", 10000000101)):
            with self.subTest(text=text):
                self.assertEqual(chinese_amount(text), cents)

    def test_malformed_uppercase_amount_is_rejected(self):
        for text in ("伍佰伍拾伍", "伍佰伍拾伍圆叁", "abc圆整", "伍佰圆伍角份", ""):
            with self.subTest(text=text):
                self.assertIsNone(chinese_amount(text))
        self.assertEqual(chinese_amount("拾圆整"), 1000)


class AmountCrossCheckTests(unittest.TestCase):
    def test_amount_accepted_when_uppercase_matches_below_score_floor(self):
        parsed = from_recognition(ocr_pages(INVOICE_TEXT, RELIABLE_TEXT, [(0.93, INVOICE_TEXT.splitlines()[-1])]),
                                  "图片本地识别")
        self.assertEqual(parsed["amount_cents"], 55500)
        self.assertEqual(parsed["warnings"], [])
        self.assertFalse(parsed.get("ocr_unconfirmed"))

    def test_amount_blocked_when_uppercase_disagrees(self):
        raw = INVOICE_TEXT.replace("¥555.00", "¥655.00")
        parsed = from_recognition(ocr_pages(raw, RELIABLE_TEXT, [(0.95, raw.splitlines()[-1])]), "图片本地识别")
        self.assertEqual(parsed["amount_cents"], 65500, "候选金额应保留给人工核对")
        self.assertTrue(parsed["ocr_unconfirmed"])
        self.assertIn(AMOUNT_UNCONFIRMED_WARNING, parsed["warnings"])

    def test_amount_blocked_when_uppercase_missing(self):
        raw = INVOICE_TEXT.replace("价税合计（大写）伍佰伍拾伍圆整", "价税合计（小写）")
        parsed = from_recognition(ocr_pages(raw, RELIABLE_TEXT, [(0.95, raw.splitlines()[-1])]), "图片本地识别")
        self.assertTrue(parsed["ocr_unconfirmed"])
        self.assertIn(AMOUNT_UNCONFIRMED_WARNING, parsed["warnings"])

    def test_amount_blocked_when_line_score_too_low(self):
        low = AMOUNT_SCORE_FLOOR - 0.04
        self.assertFalse(amount_cross_checked(INVOICE_TEXT, 55500, [(low, INVOICE_TEXT.splitlines()[-1])]))
        parsed = from_recognition(ocr_pages(INVOICE_TEXT, RELIABLE_TEXT, [(low, INVOICE_TEXT.splitlines()[-1])]),
                                  "图片本地识别")
        self.assertEqual(parsed["amount_cents"], 55500)
        self.assertTrue(parsed["ocr_unconfirmed"])
        self.assertIn(AMOUNT_UNCONFIRMED_WARNING, parsed["warnings"])

    def test_other_low_confidence_fields_still_block(self):
        raw = INVOICE_TEXT.replace(BUYER, "海南麒麟云服科技□限公司")
        reliable = INVOICE_TEXT.replace(BUYER, "海南麒麟云服科技□限公司")
        parsed = from_recognition(ocr_pages(raw, reliable, [(0.93, raw.splitlines()[3])]), "图片本地识别")
        self.assertEqual(parsed["amount_cents"], 55500)
        self.assertTrue(parsed["ocr_unconfirmed"])
        self.assertFalse(parsed.get("buyer_name"))


class SideBySideLayoutTests(unittest.TestCase):
    def test_same_line_buyer_and_seller_headers_split_left_column(self):
        text = ("           发票号码：25442000000012345694   开票日期：2025年11月10日\n"
                "购买方信息                        销售方信息\n"
                "名称：海南麒麟云服科技有限公司              名称：深圳市云帆数据服务有限公司\n"
                "统一社会信用代码/纳税人识别号：             统一社会信用代码/纳税人识别号：\n"
                "91460300MAA92DJN95           91440300MA5G12345A\n"
                "      项目名称          规格型号   单位  数量    单价    金额    税率   税额\n"
                "                 价税合计（大写）壹仟零贰拾圆整（小写）￥1,020.00\n")
        parsed = from_text(text)
        self.assertEqual(parsed["buyer_name"], "海南麒麟云服科技有限公司")
        self.assertEqual(parsed["buyer_tax_id"], "91460300MAA92DJN95")
        self.assertEqual(parsed["seller_name"], "深圳市云帆数据服务有限公司")
        self.assertEqual(parsed["amount_cents"], 102000)

    def test_vertical_label_layout_keeps_buyer_tax_id_instead_of_clearing_it(self):
        text = ("发票代码:   042001700112\n"
                "发票号码:   10077287\n"
                "开票日期:   2018年03月23日\n"
                "购   名\u3000\u3000\u3000\u3000称:    武汉市车城物流有限公司                                  密  034-+48123<-5+>276<9*<<0-7/0\n"
                "买   纳税人识别号:    914201007483062457                              63/393>*057<8865-*><84930854\n"
                "    地 址、电 话:   武汉经济技术开发区车城大道7号 84289348                     码  56+8>4576+8313/8/8*02+*/356+\n"
                "方   开户行及账号:    中国农业银行股份有限公司武汉开发区支行 17-071201040004598       区  50+7>-+25*010640190128*40614\n"
                "        项目名称                车牌号          类型    通行日期起\n"
                "     价税合计（大写）            壹佰贰拾捌元伍角肆分                                             （小写）    ￥128.54\n")
        parsed = from_text(text)
        self.assertEqual(parsed["buyer_name"], "武汉市车城物流有限公司")
        self.assertEqual(parsed["buyer_tax_id"], "914201007483062457")

    def test_empty_buyer_column_still_ignores_seller_tax_id(self):
        text = ("购  名称:张三                                        销  名称:销售公司\n"
                "买                                                  售\n"
                "信  统一社会信用代码/纳税人识别号:                   信  统一社会信用代码/纳税人识别号:91330100MA0000000Y\n"
                "项目名称 规格型号")
        parsed = from_text(text)
        self.assertEqual(parsed["buyer_name"], "张三")
        self.assertFalse(parsed.get("buyer_tax_id"))


def ofd_package(*, custom="", page="", template="", tags=None, attachment=b"", extra=()):
    """构造最小 OFD 包：OFD.xml 的 CustomDatas、页面/模板文字层与可选结构化附件。"""
    document = ('<?xml version="1.0" encoding="UTF-8"?>'
                '<ofd:OFD xmlns:ofd="http://www.ofdspec.org/2016" DocType="OFD" Version="1.0"><ofd:DocBody><ofd:DocInfo>'
                f"{custom}</ofd:DocInfo><ofd:DocRoot>Doc_0/Document.xml</ofd:DocRoot></ofd:DocBody></ofd:OFD>")
    with BytesIO() as buffer:
        with ZipFile(buffer, "w", ZIP_DEFLATED) as archive:
            archive.writestr("OFD.xml", document)
            archive.writestr("Doc_0/Document.xml", '<ofd:Document xmlns:ofd="http://www.ofdspec.org/2016"/>')
            archive.writestr("Doc_0/Pages/Page_0/Content.xml",
                             '<?xml version="1.0" encoding="UTF-8"?><ofd:Page xmlns:ofd="http://www.ofdspec.org/2016">'
                             f"<ofd:Content>{page}</ofd:Content></ofd:Page>")
            if template:
                archive.writestr("Doc_0/Tpls/Tpl_0/Content.xml",
                                 '<?xml version="1.0" encoding="UTF-8"?><ofd:Page xmlns:ofd="http://www.ofdspec.org/2016">'
                                 f"<ofd:Content>{template}</ofd:Content></ofd:Page>")
            if tags is not None:
                archive.writestr("Doc_0/Tags/CustomTags.xml",
                                 '<?xml version="1.0" encoding="UTF-8"?><ofd:CustomTags xmlns:ofd="http://www.ofdspec.org/2016">'
                                 '<ofd:CustomTag TypeID="fp"><ofd:FileLoc>Tag_Invoice.xml</ofd:FileLoc></ofd:CustomTag>'
                                 "</ofd:CustomTags>")
                archive.writestr("Doc_0/Tags/Tag_Invoice.xml",
                                 '<?xml version="1.0" encoding="UTF-8"?><ofd:root xmlns:ofd="http://www.ofdspec.org/2016">'
                                 f"{tags}</ofd:root>")
            if attachment:
                archive.writestr("Doc_0/Attachs/Attachments.xml",
                                 '<?xml version="1.0" encoding="UTF-8"?><ofd:Attachments xmlns:ofd="http://www.ofdspec.org/2016">'
                                 '<ofd:Attachment Name="original_invoice" Format="xml"><ofd:FileLoc>original_invoice.xml'
                                 "</ofd:FileLoc></ofd:Attachment></ofd:Attachments>")
                archive.writestr("Doc_0/Attachs/original_invoice.xml", attachment)
            for name, payload in extra:
                archive.writestr(name, payload)
        return buffer.getvalue()


def text_object(identity, x, y, text, size="3.175"):
    return (f'<ofd:TextObject ID="{identity}" Boundary="{x} {y} 40 5" Font="1" Size="{size}">'
            f'<ofd:TextCode X="0" Y="3.5">{text}</ofd:TextCode></ofd:TextObject>')


FULL_CUSTOM = ('<ofd:CustomDatas>'
               '<ofd:CustomData Name="发票代码">050001700111</ofd:CustomData>'
               '<ofd:CustomData Name="发票号码">99999951</ofd:CustomData>'
               '<ofd:CustomData Name="开票日期">2020年10月10日</ofd:CustomData>'
               '<ofd:CustomData Name="价税合计">318.00</ofd:CustomData>'
               '<ofd:CustomData Name="购买方名称">科游迈利有限公司</ofd:CustomData>'
               '<ofd:CustomData Name="购买方纳税人识别号">9150010807MGZHI002</ofd:CustomData>'
               '<ofd:CustomData Name="销售方名称">杭州融易算企业管理咨询有限公司</ofd:CustomData>'
               "</ofd:CustomDatas>")


class OfdStructureTests(unittest.TestCase):
    def test_custom_datas_provide_all_fields_without_rendering(self):
        with mock.patch("invoice_workbench.extract.recognize_ofd",
                        side_effect=AssertionError("结构化字段完整时不应渲染页面")):
            parsed = extract(ofd_package(custom=FULL_CUSTOM), ".ofd")
        self.assertEqual(parsed["method"], "OFD 结构提取")
        self.assertEqual(parsed["warnings"], [])
        self.assertEqual((parsed["number"], parsed["code"]), ("99999951", "050001700111"))
        self.assertEqual(parsed["date"], "2020-10-10")
        self.assertEqual(parsed["amount_cents"], 31800)
        self.assertEqual(parsed["buyer_name"], "科游迈利有限公司")
        self.assertEqual(parsed["buyer_tax_id"], "9150010807MGZHI002")
        self.assertEqual(parsed["seller_name"], "杭州融易算企业管理咨询有限公司")
        self.assertEqual(parsed["recognition_version"], EVIDENCE_VERSION)

    def test_custom_tag_layer_labels_page_text_objects(self):
        custom = ('<ofd:CustomDatas>'
                  '<ofd:CustomData Name="发票号码">24112000000048542163</ofd:CustomData>'
                  '<ofd:CustomData Name="开票日期">2024年05月28日</ofd:CustomData>'
                  '<ofd:CustomData Name="购买方纳税人识别号">91460300MAA92DJN95</ofd:CustomData>'
                  "</ofd:CustomDatas>")
        page = "".join([text_object("6922", 170, 10, "24112000000048542163"),
                        text_object("6924", 20, 33, "海南麒麟云服科技有限公司"),
                        text_object("6935", 155, 96, "¥"),
                        text_object("6936", 158, 96, "4449.00")])
        tags = ("<ofd:InvoiceNo><ofd:ObjectRef PageRef='1'>6922</ofd:ObjectRef></ofd:InvoiceNo>"
                "<ofd:Buyer><ofd:BuyerName><ofd:ObjectRef PageRef='1'>6924</ofd:ObjectRef></ofd:BuyerName>"
                "</ofd:Buyer><ofd:TaxInclusiveTotalAmount><ofd:ObjectRef PageRef='1'>6935</ofd:ObjectRef>"
                "<ofd:ObjectRef PageRef='1'>6936</ofd:ObjectRef></ofd:TaxInclusiveTotalAmount>")
        with mock.patch("invoice_workbench.extract.recognize_ofd",
                        side_effect=AssertionError("CustomTag 完整时不应渲染页面")):
            parsed = extract(ofd_package(custom=custom, page=page, tags=tags), ".ofd")
        self.assertEqual(parsed["method"], "OFD 结构提取")
        self.assertEqual(parsed["amount_cents"], 444900)
        self.assertEqual(parsed["buyer_name"], "海南麒麟云服科技有限公司")
        self.assertEqual(parsed["buyer_tax_id"], "91460300MAA92DJN95")

    def test_pretax_total_is_not_used_as_tax_inclusive_amount(self):
        custom = ('<ofd:CustomDatas>'
                  '<ofd:CustomData Name="发票号码">24112000000048542163</ofd:CustomData>'
                  '<ofd:CustomData Name="开票日期">2024年05月28日</ofd:CustomData>'
                  '<ofd:CustomData Name="合计金额">4197.17</ofd:CustomData>'
                  '<ofd:CustomData Name="合计税额">251.83</ofd:CustomData>'
                  "</ofd:CustomDatas>")
        values = _ofd_structured_values(ofd_document(ofd_package(custom=custom)))
        self.assertNotIn("amount_cents", values)

    def test_embedded_invoice_xml_wins_over_custom_datas(self):
        attachment = ('<?xml version="1.0" encoding="utf-8"?><eInvoice><InvoiceCode>050001700111</InvoiceCode>'
                      "<InvoiceNo>99999951</InvoiceNo><IssueDate>2020年10月10日</IssueDate>"
                      "<Buyer><BuyerName>科游迈利有限公司</BuyerName><BuyerTaxID>9150010807MGZHI002</BuyerTaxID></Buyer>"
                      "<TaxInclusiveTotalAmount>418.00</TaxInclusiveTotalAmount>"
                      "<GoodsInfos><GoodsInfo><Amount>300.00</Amount></GoodsInfo></GoodsInfos></eInvoice>")
        parsed = extract(ofd_package(custom=FULL_CUSTOM, attachment=attachment.encode("utf-8")), ".ofd")
        self.assertEqual(parsed["amount_cents"], 41800, "应使用内置结构化 XML 的价税合计")
        self.assertEqual(parsed["number"], "99999951")

    def test_mail_inspection_uses_structure_without_ocr(self):
        data = ofd_package(custom=FULL_CUSTOM)
        with mock.patch("invoice_workbench.extract.recognize_ofd",
                        side_effect=AssertionError("邮件筛选不应触发页面渲染")):
            evidence, deferred, ran_ocr = inspect_mail_document(data, ".ofd", allow_ocr=False)
        self.assertFalse(deferred)
        self.assertFalse(ran_ocr)
        self.assertEqual(evidence["amount_cents"], 31800)

    def test_dtd_and_zip_slip_are_rejected(self):
        dtd = ('<?xml version="1.0"?><!DOCTYPE x [<!ENTITY e "bad">]>'
               '<ofd:OFD xmlns:ofd="http://www.ofdspec.org/2016"><ofd:DocBody><ofd:DocInfo>'
               '<ofd:CustomDatas><ofd:CustomData Name="发票号码">99999951</ofd:CustomData></ofd:CustomDatas>'
               "</ofd:DocInfo></ofd:DocBody></ofd:OFD>")
        data = ofd_package(custom='<ofd:CustomDatas><ofd:CustomData Name="发票号码">99999951</ofd:CustomData>'
                                   "</ofd:CustomDatas>")
        with ZipFile(BytesIO(data), "a") as archive:
            archive.writestr("Doc_0/Attachs/evil.xml", dtd)
        self.assertEqual(ofd_document(data)["custom"], {"发票号码": "99999951"},
                         "含 DTD 的部件必须被忽略而不是解析")
        with self.assertRaises(UserError):
            ofd_document(ofd_package(extra=[("../escape.xml", "<x/>")]))


class SuspectDuplicateTests(StoreFixture, unittest.TestCase):
    def evidence(self, **overrides):
        evidence = {"number": "26000000000000000001", "code": "", "date": "2026-10-01", "buyer_name": BUYER,
                    "buyer_tax_id": TAX, "seller_name": "", "amount_cents": 60000, "raw_text": "本地识别",
                    "method": "图片本地识别", "warnings": ["原件有字段未达到本地识别可信度要求（98%）。"],
                    "recognition_version": EVIDENCE_VERSION, "ocr_unconfirmed": True}
        evidence.update(overrides)
        return evidence

    def test_unconfirmed_record_warns_about_existing_same_number(self):
        write_invoice(self.input)
        self.store.import_folder(self.rev(), str(self.input))
        confirmed, = self.store.view()["invoices"]

        image = self.input / "scan.png"
        image.write_bytes(b"\x89PNG\r\n\x1a\nsynthetic")
        with mock.patch("invoice_workbench.store.extract", side_effect=lambda data, suffix: self.evidence()):
            report = self.store.import_folder(self.rev(), str(self.input))
        self.assertEqual(report["imported"], 1)
        invoices = self.store.view()["invoices"]
        self.assertEqual(len(invoices), 2)
        pending = next(item for item in invoices if item["id"] != confirmed["id"])
        self.assertTrue(pending["ocr_unconfirmed"])
        self.assertIn("疑似与已有同票号记录重复，请核对原件后处理。", pending["warnings"])
        self.assertTrue(any("疑似与已有同票号记录重复" in issue for issue in pending["verification"]["issues"]))
        self.assertFalse(pending["verification"]["passed"])
        self.assertFalse(selectable(pending))
        record = self.store.invoice(confirmed["id"])
        self.assertTrue(record["verification"]["passed"])
        self.assertEqual(record["reimbursement_status"], "available")


if __name__ == "__main__":
    unittest.main()
