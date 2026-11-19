from contextlib import closing
import ctypes
from ctypes import wintypes
from email.message import EmailMessage
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from uuid import uuid4

from invoice_workbench.domain import UserError
from invoice_workbench.extract import extract, from_text
from invoice_workbench.gui import Workbench
from invoice_workbench.outlook_auth import CLIENT_ID_ENV
from invoice_workbench.store import Store
from .helpers import BUYER, TAX, xml_invoice


ROOT = Path(__file__).resolve().parent.parent
TEST_ROOT = ROOT / ".test-tmp"


class AmountSignTests(unittest.TestCase):
    def test_negative_amount_keeps_its_sign_in_each_layout(self):
        labels = ("价税合计（小写）", "价税合计(小写): ", "小写）",
                  "小写）壹佰圆整" + " " * 60, "Total with Tax: ")
        for label in labels:
            for sign in ("-", "−", "－"):
                for currency in ("￥", "¥"):
                    for amount in (f"{sign}{currency}100.00", f"{currency}{sign}100.00",
                                   f"{sign} {currency} 100.00", f"{currency} {sign} 100.00"):
                        with self.subTest(text=label + amount):
                            parsed = from_text(label + amount)
                            self.assertNotIn("amount_cents", parsed)
                            self.assertTrue(any("红字发票" in warning for warning in parsed["warnings"]))

    def test_positive_amount_and_original_text_are_preserved(self):
        for label in ("价税合计（小写）", "小写）", "Total Amount: "):
            for amount in ("￥1,002.50", "¥ 1002.50", "1002.50"):
                text = label + amount
                with self.subTest(text=text):
                    parsed = from_text(text)
                    self.assertEqual(parsed["amount_cents"], 100250)
                    self.assertEqual(parsed["warnings"], [])
                    self.assertEqual(parsed["raw_text"], text)
        text = "价税合计（小写）−￥100.00"
        self.assertEqual(from_text(text)["raw_text"], text)
        text = "小写）壹仟零贰圆伍角" + " " * 60 + "￥1,002.50"
        self.assertEqual(from_text(text)["amount_cents"], 100250)

    def test_long_layout_requires_currency_and_keeps_negative_sign(self):
        prefix = "小写）壹佰圆整" + " " * 60
        self.assertNotIn("amount_cents", from_text(prefix + "100.00"))
        parsed = from_text(prefix + "-￥100.00")
        self.assertNotIn("amount_cents", parsed)
        self.assertTrue(parsed["warnings"])


class XmlEncodingTests(unittest.TestCase):
    def test_encoding_and_syntax_failures_are_user_errors(self):
        for payload in (b'<?xml version="1.0" encoding="unsupported"?><Invoice/>',
                        b'<?xml version="1.0" encoding="GB2312"?><Invoice/>', b"<Invoice>"):
            with self.subTest(payload=payload), self.assertRaises(UserError):
                extract(payload, ".xml")

    def test_bad_xml_does_not_discard_other_files_or_email_attachments(self):
        TEST_ROOT.mkdir(exist_ok=True)
        for encoding in ("unsupported", "GB2312"):
            for email in (False, True):
                with self.subTest(encoding=encoding, email=email), tempfile.TemporaryDirectory(dir=TEST_ROOT) as directory:
                    root = Path(directory)
                    source = root / "source"
                    source.mkdir()
                    valid = xml_invoice()
                    another = xml_invoice(number="26000000000000000002", amount="402.50")
                    invalid = f'<?xml version="1.0" encoding="{encoding}"?><Invoice/>'.encode("ascii")
                    if email:
                        message = EmailMessage()
                        message.set_content("合成发票导入验证")
                        for name, data in (("a-valid.xml", valid), ("z-invalid.xml", invalid),
                                           ("zz-valid.xml", another)):
                            message.add_attachment(data, maintype="application", subtype="xml", filename=name)
                        (source / "invoices.eml").write_bytes(message.as_bytes())
                    else:
                        (source / "a-valid.xml").write_bytes(valid)
                        (source / "z-invalid.xml").write_bytes(invalid)
                        (source / "zz-valid.xml").write_bytes(another)
                    before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in source.iterdir()}
                    store = Store(root / "workspace")
                    try:
                        result = store.import_folder(store.data["revision"], str(source))
                        self.assertEqual(result["imported"], 2)
                        self.assertEqual(len(result["errors"]), 1)
                        self.assertIn("z-invalid.xml", result["errors"][0]["file"])
                        self.assertIn("XML 编码无法解析", result["errors"][0]["error"])
                        self.assertEqual({item["amount_cents"] for item in store.data["invoices"]}, {60000, 40250})
                        self.assertEqual(len(list((store.root / "objects").iterdir())), 0 if email else 2)
                    finally:
                        store.close()
                    reopened = Store(root / "workspace")
                    try:
                        self.assertEqual(len(reopened.view()["invoices"]), 2)
                    finally:
                        reopened.close()
                    after = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in source.iterdir()}
                    self.assertEqual(before, after)


class BuildInputTests(unittest.TestCase):
    def test_missing_required_input_fails_before_creating_output(self):
        TEST_ROOT.mkdir(exist_ok=True)
        for missing in ("app.py", "samples"):
            with self.subTest(missing=missing), tempfile.TemporaryDirectory(dir=TEST_ROOT) as directory:
                root = Path(directory)
                for name in ("invoice_workbench", "tools", "tests", "samples"):
                    if name != missing:
                        shutil.copytree(ROOT / name, root / name, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
                for name in ("app.py", "README.md", "requirements.txt", "AGENTS.md", "setup.ps1", "start.ps1", "Install-ToSoft.ps1"):
                    if name != missing:
                        shutil.copy2(ROOT / name, root / name)
                output = root / "dist" / "bundle"
                result = subprocess.run([sys.executable, "-B", str(root / "tools" / "build_portable.py"),
                                         "--output", str(output), "--outlook-client-id", str(uuid4())], capture_output=True, timeout=30,
                                        env=dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1"))
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("构建缺少必需".encode(), result.stderr)
                self.assertIn(missing.encode(), result.stderr)
                self.assertFalse((root / "dist").exists())

    def test_missing_outlook_client_id_fails_before_creating_output(self):
        TEST_ROOT.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as directory:
            root = Path(directory)
            shutil.copytree(ROOT / "invoice_workbench", root / "invoice_workbench", ignore=shutil.ignore_patterns("__pycache__"))
            (root / "tools").mkdir()
            build = root / "tools" / "build_portable.py"
            shutil.copy2(ROOT / "tools" / build.name, build)
            env = dict(os.environ, PYTHONIOENCODING="utf-8")
            env.pop(CLIENT_ID_ENV, None)
            result = subprocess.run([sys.executable, "-B", str(build)], capture_output=True, timeout=30, env=env)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("发布构建需要 Outlook OAuth 客户端 ID".encode(), result.stderr)
            self.assertFalse((root / "dist").exists())


@unittest.skipUnless(sys.platform == "win32", "需要 Windows 原生消息框")
class WorkspaceReconnectTests(unittest.TestCase):
    def setUp(self):
        TEST_ROOT.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=TEST_ROOT)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.saved = self.root / "saved-workspace"
        saved = Store(self.saved)
        saved.close()
        self.saved_index = (self.saved / "invoices.json").read_bytes()
        self.settings_file = self.root / "settings.json"
        self.settings_file.write_text(json.dumps({"data_directory": str(self.saved)}), encoding="utf-8")

    def answer_dialog(self, action, title, response):
        # 操作当前进程真实的 Windows 消息框，不替换 GUI 或 Store 方法。
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        user32.EnumThreadWindows.argtypes = (wintypes.DWORD, callback_type, wintypes.LPARAM)
        user32.EnumThreadWindows.restype = wintypes.BOOL
        user32.GetWindowTextW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
        user32.GetWindowTextW.restype = ctypes.c_int
        user32.GetClassNameW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
        user32.GetClassNameW.restype = ctypes.c_int
        user32.EnumChildWindows.argtypes = (wintypes.HWND, callback_type, wintypes.LPARAM)
        user32.GetDlgItem.argtypes = (wintypes.HWND, ctypes.c_int)
        user32.GetDlgItem.restype = wintypes.HWND
        user32.IsWindowVisible.argtypes = (wintypes.HWND,)
        user32.IsWindowVisible.restype = wintypes.BOOL
        user32.IsWindowEnabled.argtypes = (wintypes.HWND,)
        user32.IsWindowEnabled.restype = wintypes.BOOL
        user32.PostMessageW.argtypes = (wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
        user32.PostMessageW.restype = wintypes.BOOL
        main_thread = threading.get_native_id()
        finished = threading.Event()
        answered = threading.Event()

        @callback_type
        def visit(window, parameter):
            caption = ctypes.create_unicode_buffer(256)
            user32.GetWindowTextW(window, caption, len(caption))
            if caption.value == title:
                if response == 1:
                    buttons = []
                    @callback_type
                    def find_button(child, parameter):
                        classname = ctypes.create_unicode_buffer(128)
                        user32.GetClassNameW(child, classname, len(classname))
                        if classname.value == "Button" and user32.IsWindowVisible(child):
                            buttons.append(child)
                        return True
                    user32.EnumChildWindows(window, find_button, 0)
                    # 确认提示只有一个按钮，控件编号由当前 Tk/Windows 分配。
                    button = buttons[0] if len(buttons) == 1 else None
                else:
                    button = user32.GetDlgItem(window, response)
                # 按钮创建并可用后发送真实点击；窗口关闭前允许再次发送。
                if button and user32.IsWindowVisible(button) and user32.IsWindowEnabled(button):
                    if user32.PostMessageW(button, 0x00F5, 0, 0):
                        answered.set()
                return False
            return True

        def respond():
            while not finished.wait(0.02):
                user32.EnumThreadWindows(main_thread, visit, 0)

        worker = threading.Thread(target=respond, daemon=True)
        worker.start()
        try:
            result = action()
        finally:
            finished.set()
            worker.join(timeout=2)
        self.assertTrue(answered.is_set(), f"未找到消息框：{title}")
        return result

    def open_app(self, directory, blocked=False):
        if blocked:
            app = self.answer_dialog(lambda: Workbench(directory, self.settings_file), "无法打开资料库", 1)
        else:
            app = Workbench(directory, self.settings_file)
        self.addCleanup(app.close_app)
        app.withdraw()
        return app

    def assert_saved_workspace_unchanged(self):
        self.assertEqual((self.saved / "invoices.json").read_bytes(), self.saved_index)

    def test_failed_startup_and_repeated_retries_keep_requested_workspace(self):
        requested = self.root / "requested-workspace"
        blocker = Store(requested)
        self.addCleanup(blocker.close)
        app = self.open_app(requested, blocked=True)
        self.assertIsNone(app.store)
        for _ in range(2):
            app.reconnect_library()
            self.assertIsNone(app.store)
            self.assertEqual(app.connection_directory, requested)
            self.assertEqual(json.loads(self.settings_file.read_text())["data_directory"], str(self.saved))
        blocker.close()
        app.reconnect_library()
        self.assertTrue(app.session_ok)
        self.assertEqual(app.store.root, requested)
        self.assertEqual(json.loads(self.settings_file.read_text())["data_directory"], str(requested))
        self.assert_saved_workspace_unchanged()

    def test_stale_session_decline_then_recovery_uses_requested_workspace(self):
        requested = self.root / "requested-workspace"
        store = Store(requested)
        store.close()
        marker = requested / ".invoice-session.json"
        marker.write_text(json.dumps({"session": "remote-session", "host": "other-test-host", "pid": 1}))
        original_marker = marker.read_bytes()
        app = self.open_app(requested, blocked=True)
        self.answer_dialog(app.reconnect_library, "恢复资料连接", 7)
        self.assertIsNone(app.store)
        self.assertEqual(marker.read_bytes(), original_marker)
        self.assertEqual(app.connection_directory, requested)
        self.answer_dialog(app.reconnect_library, "恢复资料连接", 6)
        self.assertTrue(app.session_ok)
        self.assertEqual(app.store.root, requested)
        recovered = list(requested.glob("recovered-session-*.json"))
        self.assertEqual(len(recovered), 1)
        self.assertEqual(recovered[0].read_bytes(), original_marker)
        self.assert_saved_workspace_unchanged()

    def test_failed_switch_keeps_current_workspace_and_success_updates_target(self):
        requested = self.root / "requested-workspace"
        blocker = Store(requested)
        self.addCleanup(blocker.close)
        app = self.open_app(self.saved)
        with self.assertRaises(UserError):
            app.open_library(requested)
        self.assertEqual(app.store.root, self.saved)
        self.assertEqual(app.connection_directory, self.saved)
        app.reconnect_library()
        self.assertEqual(app.store.root, self.saved)
        blocker.close()
        app.open_library(requested)
        app.reconnect_library()
        self.assertEqual(app.connection_directory, requested)
        self.assertEqual(app.store.root, requested)
        self.assert_saved_workspace_unchanged()

    def test_legacy_app_settings_open_and_recover_the_same_custom_workspace(self):
        application = self.root / "app"
        application.mkdir()
        shutil.copytree(ROOT / "invoice_workbench", application / "invoice_workbench",
                        ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copy2(self.settings_file, application / "local-settings.json")
        invoice = self.root / "invoice"
        invoice.mkdir()
        (invoice / "original.xml").write_bytes(xml_invoice())
        with closing(Store(self.saved)) as store:
            store.set_company(store.view()["revision"], BUYER, TAX)
            report = store.import_folder(store.view()["revision"], str(invoice))
            invoice_id = report["items"][0]["invoice_id"]
            store.create_batch(store.view()["revision"], [invoice_id], "500", "500", uuid4().hex)
            store.set_invoice_status(store.view()["revision"], invoice_id, "reimbursed")
        before = (self.saved / "invoices.json").read_bytes()
        script = '''
import json
from pathlib import Path
import sys
import unittest

from invoice_workbench.gui import Workbench
sys.path.append(sys.argv[1])
from tests.test_review_fixes import WorkspaceReconnectTests

workspace = Path(sys.argv[2])
def verify(app):
    try:
        assert app.store.root == workspace
        assert len(app.snapshot["batches"]) == 1
        invoice, = app.snapshot["invoices"]
        assert invoice["reimbursement_status"] == "reimbursed"
    finally:
        app.close_app()

verify(Workbench())
(Path("data") / "local-settings.json").unlink()
marker = workspace / ".invoice-session.json"
marker.write_text(json.dumps({"session": "legacy", "host": "remote-test", "pid": 1}), encoding="utf-8")
answer = WorkspaceReconnectTests.answer_dialog
verify(answer(unittest.TestCase(), lambda: Workbench(recover_lock=True), "恢复遗留会话", 6))
assert len(list(workspace.glob("recovered-session-*.json"))) == 1
assert not (Path("data") / "invoices.json").exists()
Path("local-settings.json").unlink()
verify(Workbench())
'''
        result = subprocess.run([sys.executable, "-B", "-c", script, str(ROOT), str(self.saved)], cwd=application,
                                capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        self.assertEqual((self.saved / "invoices.json").read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
