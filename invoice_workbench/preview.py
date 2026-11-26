"""原票预览面板：后台解码原件，主线程只做 Tk 绘制。

预览的读盘、SHA-256 校验和 PDF/图片解码都在单个后台线程内完成。工作线程
只保留最新一次请求，快速切换发票或拖动分隔条不会堆积渲染任务；过期的结果
按 generation 令牌在主线程丢弃，旧发票图片不会覆盖新发票。

ImageTk.PhotoImage 和所有 Canvas 操作严格留在主线程执行。
"""

from __future__ import annotations

from pathlib import Path
import queue
import threading
import tkinter as tk

from PIL import Image, ImageTk
import pypdfium2 as pdfium

from .theme import COLORS, FONT

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg"}
LOADING_TEXT = "正在加载预览…"
UNSUPPORTED_TEXT = "此格式请使用“打开原件”查看。\n文字内容位于“提取文本”。"
FAILED_TEXT = "无法生成预览，请打开原件核对。\n{}"
_COALESCED = object()  # 工作线程合并掉的旧请求，仅用于归还请求计数


def read_preview_image(path: Path) -> Image.Image | None:
    """解码原件首页；不支持的文件格式返回 None。仅在后台线程调用。"""
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        document = pdfium.PdfDocument(str(path))
        try:
            page = document[0]
            try:
                bitmap = page.render(scale=min(3, 1800 / max(page.get_size())))
                try:
                    return bitmap.to_pil().copy()
                finally:
                    bitmap.close()
            finally:
                page.close()
        finally:
            document.close()
    if suffix in IMAGE_SUFFIXES:
        with Image.open(path) as source:
            return source.copy()
    return None


class PreviewPane:
    """预览状态机：加载、缩放、滚动位置保持和过期结果丢弃。"""

    def __init__(self, master, canvas, *, theme, zoom_text, is_active, current_file, resolver):
        self.master = master
        self.canvas = canvas
        self.theme = theme
        self.zoom_text = zoom_text
        self._is_active = is_active
        self._current_file = current_file
        # resolver() 返回绑定当次资料会话的解析函数，避免工作线程读取已被替换的 store。
        self._resolver = resolver
        # 默认同步加载，保证无事件循环的脚本和单元测试结果确定；
        # Workbench.mainloop 启动后会切换为后台加载。
        self.async_enabled = False
        self.image = None
        self.photo = None
        self.key = None
        self.zoom = 1.0
        self.fit = True
        self.after_id = None
        self.generation = 0
        self.resize_position = None
        self._loaded_key = None
        self._position = (0.0, 0.0)
        self._requests = queue.Queue()
        self._results = queue.Queue()
        self._worker = None
        self._worker_lock = threading.Lock()
        self._poll_id = None
        self._outstanding = 0
        self._closed = False

    @property
    def pending(self):
        """尚未回到主线程的预览请求数。"""
        return self._outstanding

    def active(self):
        return bool(self._is_active())

    def scroll_position(self):
        return self.canvas.xview()[0], self.canvas.yview()[0]

    def cancel(self):
        self.generation += 1
        if self.after_id is not None:
            try:
                self.master.after_cancel(self.after_id)
            except tk.TclError:
                pass
            self.after_id = None

    def schedule(self, *, resizing=False):
        self.cancel()
        if self._closed or not self.active():
            return
        # 在画布随外框改变尺寸之前记录位置，避免 Tk 按旧滚动范围提前限制位置。
        if resizing and self.photo is not None and self.resize_position is None:
            self.resize_position = self.scroll_position()
        generation = self.generation
        self.after_id = self.master.after(100, lambda: self._debounced(generation))

    def _debounced(self, generation):
        self.after_id = None
        if generation == self.generation:
            self.render()

    def file_changed(self):
        self.zoom = 1.0
        self.fit = True
        self.render()

    def zoom_by(self, factor):
        if factor is None:
            self.fit = True
        self.zoom = 1.0 if factor is None else max(0.25, min(3.0, self.zoom * factor))
        self.render()

    def reset_zoom(self):
        self.fit = False
        self.zoom = 1.0
        self.render()

    def clear(self):
        self.cancel()
        self.image = self.photo = self.key = None
        self.zoom = 1.0
        self.fit = True
        self.resize_position = None
        self._loaded_key = None
        self.canvas.delete("all")
        self.canvas.configure(scrollregion=(0, 0, 0, 0))

    def hide(self):
        self.cancel()
        self.resize_position = None
        self.canvas.delete("all")

    def render(self):
        self.cancel()
        if self._closed or not self.active():
            return
        file = self._current_file()
        x, y = self.resize_position or self.scroll_position()
        self.resize_position = None
        self.photo = None
        self.canvas.delete("all")
        self.zoom_text.set(f"{self.zoom:.0%}")
        if not file:
            self.canvas.configure(scrollregion=(0, 0, 0, 0))
            return
        self._position = (x, y)
        if self._file_key(file) == self._loaded_key:
            self._paint()  # 同一份原件：只做缩放和绘制
            return
        if not self.async_enabled:
            self._load_inline(file)
            return
        # 主线程只提示加载中，解码与校验交给后台线程。
        self._message(LOADING_TEXT, COLORS.secondary)
        self.canvas.configure(scrollregion=(0, 0, 0, 0))
        self._submit(file)

    def _load_inline(self, file):
        try:
            path = self._resolver()(file)
            image = read_preview_image(path)
        except Exception as exc:
            self._failed(exc)
            return
        self._adopt(file, path, image)

    def _submit(self, file):
        self._ensure_worker()
        self._outstanding += 1
        self._requests.put((self.generation, dict(file), self._resolver()))
        if self._poll_id is None:
            self._schedule_poll()

    def _ensure_worker(self):
        with self._worker_lock:
            if self._worker is not None:
                return
            self._worker = threading.Thread(target=self._work, name="invoice-preview", daemon=True)
            self._worker.start()

    def _work(self):
        while True:
            request = self._requests.get()
            if request is None:
                return
            # 只渲染最新一次请求，排队中的旧发票直接跳过。
            skipped = []
            while True:
                try:
                    newer = self._requests.get_nowait()
                except queue.Empty:
                    break
                if newer is None:
                    return
                skipped.append(request[0])
                request = newer
            for generation in skipped:
                self._results.put((generation, None, None, None, _COALESCED))
            generation, file, resolve = request
            path = image = error = None
            try:
                path = resolve(file)
                image = read_preview_image(path)
            except Exception as exc:  # 后台线程不能把异常抛给 Tk 事件循环
                error = exc
            if self._closed:
                return  # 关闭期间的收尾结果直接丢弃，既不绘制也不向 stderr 输出
            self._results.put((generation, file, path, image, error))

    def _schedule_poll(self):
        try:
            self._poll_id = self.master.after(20, self._receive)
        except tk.TclError:
            self._poll_id = None

    def _receive(self):
        self._poll_id = None
        while True:
            try:
                generation, file, path, image, error = self._results.get_nowait()
            except queue.Empty:
                break
            self._outstanding = max(0, self._outstanding - 1)
            if error is _COALESCED:
                continue
            if self._closed or generation != self.generation:
                continue  # 过期结果直接丢弃，旧图片不会覆盖新发票
            if error is not None:
                self._failed(error)
            else:
                self._adopt(file, path, image)
        if not self._closed and self._outstanding and self._poll_id is None:
            self._schedule_poll()

    @staticmethod
    def _file_key(file):
        # 内容哈希标识原件；不同发票中的同名文件不会误用同一张缓存。
        return (file.get("hash") or file.get("path"), file.get("name"))

    def _adopt(self, file, path, image):
        self.key = path
        self.image = image
        self._loaded_key = self._file_key(file) if file else None
        self._paint()

    def _failed(self, error):
        self.key = self.image = self.photo = None
        self._loaded_key = None
        self._message(FAILED_TEXT.format(error), COLORS.error)

    def _message(self, text, color):
        p = self.theme.px
        self.canvas.configure(scrollregion=(0, 0, 0, 0))
        self.canvas.create_text(p(16), p(20), anchor="nw", text=text, fill=color, font=(FONT, 10),
                                width=max(p(100), self.canvas.winfo_width() - p(32)), tags="preview-status")

    def _paint(self):
        p = self.theme.px
        image = self.image
        self.canvas.delete("preview-status")
        if image is None:
            self._message(UNSUPPORTED_TEXT, COLORS.secondary)
            return
        x, y = self._position
        base_width = max(p(100), self.canvas.winfo_width() - p(32)) if self.fit else image.width
        width = max(1, round(base_width * self.zoom))
        scaled = image.resize((width, max(1, round(width * image.height / image.width))), Image.Resampling.LANCZOS)
        self.photo = ImageTk.PhotoImage(scaled, master=self.master)
        left = max(p(16), (self.canvas.winfo_width() - width) // 2)
        self.canvas.create_rectangle(left + p(3), p(19), left + width + p(3), self.photo.height() + p(19),
                                     fill=COLORS.line, outline="")
        self.canvas.create_image(left, p(16), anchor="nw", image=self.photo)
        self.canvas.configure(scrollregion=(0, 0, max(self.canvas.winfo_width(), width + p(32)),
                                            self.photo.height() + p(32)))
        self.canvas.xview_moveto(x)
        self.canvas.yview_moveto(y)

    def close(self):
        """窗口关闭时停止后台线程并取消排队中的绘制回调。"""
        self._closed = True
        self.cancel()
        if self._poll_id is not None:
            try:
                self.master.after_cancel(self._poll_id)
            except tk.TclError:
                pass
            self._poll_id = None
        with self._worker_lock:
            worker, self._worker = self._worker, None
        if worker is not None:
            self._requests.put(None)
            # 线程是 daemon，短暂等待收尾即可，避免退出时仍在解码原件造成竞态或卡顿。
            worker.join(timeout=0.3)
