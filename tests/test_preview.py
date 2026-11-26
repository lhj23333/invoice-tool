import contextlib
import io
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from invoice_workbench.gui import Workbench
from invoice_workbench.preview import LOADING_TEXT, read_preview_image

SAMPLES = Path(__file__).resolve().parent.parent / "samples"


class PreviewAsyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.app = Workbench(self.root / "workspace", self.root / "settings.json", preview_async=True)
        self.app.withdraw()
        self.app.update()
        store = self.app.store
        store.import_folder(store.data["revision"], SAMPLES, recursive=True)
        self.app.reload()
        self.app.update()

    def tearDown(self):
        if self.app is not None:
            self.app.dirty = False
            self.app.close_app()
        self.temp.cleanup()

    def pump(self, predicate, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.update()
            if predicate():
                return
            time.sleep(0.005)
        self.fail("预览未在超时内完成")

    def wait_event(self, event, timeout=10):
        self.pump(event.is_set, timeout)

    def show_pdf(self):
        """先同步打开 PDF 预览，再清空缓存，让后续渲染真正进入后台。"""
        self.app.preview.async_enabled = False
        item = next(i for i in self.app.snapshot["invoices"] if any(f["name"].endswith(".pdf") for f in i["files"]))
        self.app.load_invoice(item)
        self.app.details.select(1)
        self.app.update()
        self.app.file_choice.current(next(n for n, f in enumerate(item["files"]) if f["name"].endswith(".pdf")))
        self.app.preview_file_changed()
        self.assertIsNotNone(self.app.image_reference)
        self.app.preview.async_enabled = True
        self.app.preview.clear()
        return item

    def canvas_texts(self):
        canvas = self.app.preview_canvas
        return [canvas.itemcget(item, "text") for item in canvas.find_all() if canvas.type(item) == "text"]

    def test_loading_placeholder_then_background_paint(self):
        self.show_pdf()
        gate, started = threading.Event(), threading.Event()
        real = read_preview_image

        def slow(path):
            started.set()
            self.assertTrue(gate.wait(10))
            return real(path)

        with patch("invoice_workbench.preview.read_preview_image", slow):
            self.app.render_preview()
            self.wait_event(started)
            self.assertIsNone(self.app.image_reference)
            self.assertIn(LOADING_TEXT, self.canvas_texts())
            gate.set()
            self.pump(lambda: self.app.preview.pending == 0 and self.app.image_reference is not None)
        self.assertTrue(self.app.preview_canvas.find_all())
        self.assertIsNotNone(self.app.image_reference)
        self.assertNotIn(LOADING_TEXT, self.canvas_texts())

    def test_stale_generation_result_is_discarded(self):
        self.show_pdf()
        png_item = next(i for i in self.app.snapshot["invoices"] if any(f["name"].endswith(".png") for f in i["files"]))
        real = read_preview_image
        release_a, release_b, started_a, started_b = (threading.Event() for _ in range(4))

        def slow(path):
            if path.suffix.lower() == ".pdf":
                started_a.set()
                self.assertTrue(release_a.wait(10))
            else:
                started_b.set()
                self.assertTrue(release_b.wait(10))
            return real(path)

        with patch("invoice_workbench.preview.read_preview_image", slow):
            self.app.render_preview()           # A：后台读取 PDF
            self.wait_event(started_a)
            self.app.load_invoice(png_item)     # B：切换到另一张发票，A 的结果随即过期
            self.assertGreaterEqual(self.app.preview.pending, 1)
            release_a.set()
            self.wait_event(started_b)
            self.pump(lambda: self.app.preview.pending == 1)
            # A 的结果被丢弃：画布仍停留在加载提示，没有闪回旧图片。
            self.assertIsNone(self.app.image_reference)
            self.assertIn(LOADING_TEXT, self.canvas_texts())
            release_b.set()
            self.pump(lambda: self.app.preview.pending == 0)
        self.assertIsNotNone(self.app.image_reference)
        self.assertEqual(self.app.preview_key, self.app.store.original(png_item["files"][0]))
        self.assertNotIn(LOADING_TEXT, self.canvas_texts())

    def test_rapid_renders_coalesce_into_one_pending_task(self):
        self.show_pdf()
        real = read_preview_image
        gate, started, calls = threading.Event(), threading.Event(), []

        def slow(path):
            calls.append(path)
            if len(calls) == 1:
                started.set()
                self.assertTrue(gate.wait(10))
            return real(path)

        with patch("invoice_workbench.preview.read_preview_image", slow):
            for _ in range(12):
                self.app.render_preview()
            self.wait_event(started)
            gate.set()
            self.pump(lambda: self.app.preview.pending == 0)
        # 第一张正在渲染时，其余请求被合并，只再渲染最后一张。
        self.assertLessEqual(len(calls), 2)
        self.assertGreaterEqual(len(calls), 1)
        self.assertIsNotNone(self.app.image_reference)

    def test_switching_workspace_invalidates_preview_before_old_store_closes(self):
        self.show_pdf()
        generation = self.app.preview_generation
        old = self.app.store
        observed = {}
        close = old.close

        def record():
            observed["generation"] = self.app.preview_generation
            observed["store"] = self.app.store
            close()

        old.close = record
        try:
            self.app.open_library(self.root / "second-workspace")
        finally:
            del old.close
        # 旧会话关闭之前，旧工作区的预览请求必须已经作废。
        self.assertGreater(observed["generation"], generation)
        self.assertIsNot(observed["store"], old)

    def test_switching_workspace_binds_pending_render_to_previous_store(self):
        self.show_pdf()
        workspace = self.app.store.root
        real = read_preview_image
        started, release, paths = threading.Event(), threading.Event(), []

        def slow(path):
            paths.append(path)
            if len(paths) == 1:
                started.set()
                self.assertTrue(release.wait(10))
            return real(path)

        with patch("invoice_workbench.preview.read_preview_image", slow):
            self.app.render_preview()
            self.wait_event(started)
            self.app.render_preview()  # 第二个请求在旧工作区里排队
            self.assertGreaterEqual(self.app.preview.pending, 2)
            generation = self.app.preview_generation
            self.app.open_library(self.root / "second-workspace")
            self.assertGreater(self.app.preview_generation, generation)
            release.set()
            self.pump(lambda: self.app.preview.pending == 0)
        # 排队请求仍按提交时的 store 解析原件，不会串到新工作区。
        self.assertEqual(len(paths), 2)
        self.assertTrue(all(path.is_relative_to(workspace) for path in paths))
        self.assertIsNone(self.app.image_reference)
        self.assertEqual(self.app.preview_canvas.find_all(), ())

    def test_close_app_during_render_finishes_without_traceback(self):
        self.show_pdf()
        real = read_preview_image
        started = threading.Event()

        def slow(path):
            started.set()
            time.sleep(0.15)  # 关闭请求到达时后台仍在解码
            return real(path)

        stderr = io.StringIO()
        with patch("invoice_workbench.preview.read_preview_image", slow), contextlib.redirect_stderr(stderr):
            self.app.render_preview()
            self.wait_event(started)
            began = time.monotonic()
            self.app.close_app()
            elapsed = time.monotonic() - began
            self.app = None
        # 关闭时短暂等待在途解码收尾，线程为 daemon，收尾期异常不打印到 stderr。
        self.assertGreaterEqual(elapsed, 0.1)
        self.assertLess(elapsed, 1)
        self.assertEqual(stderr.getvalue(), "")
        self.assertFalse(any(thread.name == "invoice-preview" and thread.is_alive()
                             for thread in threading.enumerate()))


if __name__ == "__main__":
    unittest.main()
