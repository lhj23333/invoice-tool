from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
import queue
import shutil
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from . import __version__
from .domain import UserError, identity, yuan
from .labels import STATUS, state_label
from .selection import SelectionDialog
from .store import Store
from .workflow import contains_invoices, import_directory

APP_ROOT = Path(__file__).resolve().parent.parent
BG, WHITE, INK, MUTED, ACCENT = "#f4f6f8", "#ffffff", "#202a35", "#687687", "#167365"


class PageStack(ttk.Frame):
    """Independent pages selected by the sidebar; no tab strip or shared content."""
    def __init__(self, master, changed):
        super().__init__(master)
        self.frames = []
        self.current_index = None
        self.changed = changed
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)

    def add(self, frame):
        self.frames.append(frame)

    def select(self, index):
        if self.current_index is not None:
            self.frames[self.current_index].grid_remove()
        self.frames[index].grid(row=0, column=0, sticky="nsew")
        self.current_index = index
        self.changed(index)


class Workbench(tk.Tk):
    def __init__(self, data_dir: Path | None = None, settings_file: Path | None = None):
        super().__init__()
        self.title("发票工作台 · Invoice Workbench")
        width = min(1360, self.winfo_screenwidth() - 80)
        height = min(820, self.winfo_screenheight() - 110)
        self.geometry(f"{width}x{height}+20+20")
        self.minsize(min(1080, width), min(640, height))
        self.configure(bg=BG)
        from tkinter import font
        font.nametofont("TkDefaultFont").configure(family="Microsoft YaHei UI", size=10)
        self.settings_file = settings_file or APP_ROOT / "local-settings.json"
        try:
            self.settings = json.loads(self.settings_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.settings = {}
        self.store = None
        self.session_ok = False
        self.snapshot = {}
        self.busy = False
        self.events = queue.Queue()
        self.actions = []
        self.current_id = None
        self.dirty = False
        self.loading = False
        self.image_reference = None
        self.preview_generation = 0
        self.protocol("WM_DELETE_WINDOW", self.close_app)
        self._style()
        self._build()
        self.poll_id = self.after(80, self._poll)
        directory = data_dir or Path(self.settings.get("data_directory", str(APP_ROOT / "data")))
        import_source = None
        if not (Path(directory) / "invoices.json").exists() and contains_invoices(directory):
            # Older versions let users mistake an invoice folder for a workspace.
            # Never create index/session files in that source folder.
            import_source, directory = directory, APP_ROOT / "data"
        self.connection_directory = Path(directory).expanduser()
        try:
            self.open_library(directory)
        except (UserError, OSError, ValueError) as exc:
            messagebox.showerror("无法打开资料库", str(exc), parent=self)
            self.connection_error(str(exc))
        if import_source and self.session_ok:
            self.after_idle(lambda: self.start_import(import_source))

    def _style(self):
        s = ttk.Style(self)
        s.theme_use("clam")
        s.configure(".", font=("Microsoft YaHei UI", 10))
        s.configure("TFrame", background=BG)
        s.configure("Card.TFrame", background=WHITE)
        s.configure("TLabel", background=BG, foreground=INK)
        s.configure("Card.TLabel", background=WHITE, foreground=INK)
        s.configure("Muted.TLabel", background=BG, foreground=MUTED)
        s.configure("Title.TLabel", font=("Microsoft YaHei UI", 20, "bold"), foreground=INK)
        s.configure("Stat.TLabel", font=("Microsoft YaHei UI", 20, "bold"), background=WHITE)
        s.configure("TButton", padding=(12, 8), background=WHITE, foreground=INK)
        s.map("TButton", background=[("active", "#e9f0ee")])
        s.configure("Accent.TButton", background=ACCENT, foreground=WHITE)
        s.map("Accent.TButton", background=[("active", "#115b50"), ("disabled", "#b2bfc0")], foreground=[("disabled", WHITE)])
        s.configure("Nav.TButton", padding=(16, 12), anchor="w", background="#e8eeec", foreground=INK, borderwidth=0)
        s.map("Nav.TButton", background=[("active", "#dce7e2")])
        s.configure("Selected.Nav.TButton", background=ACCENT, foreground=WHITE)
        s.map("Selected.Nav.TButton", background=[("active", "#115b50")], foreground=[("active", WHITE)])
        s.configure("Treeview", rowheight=35, background=WHITE, fieldbackground=WHITE, borderwidth=0, font=("Microsoft YaHei UI", 10))
        s.configure("Treeview.Heading", padding=(8, 10), background="#eef2f4", font=("Microsoft YaHei UI", 10, "bold"))
        s.map("Treeview", background=[("selected", "#dcece7")], foreground=[("selected", INK)])
        s.configure("TEntry", padding=6)
        s.configure("TNotebook", background=WHITE, borderwidth=0)
        s.configure("TNotebook.Tab", padding=(15, 9))
        s.configure("TLabelframe", background=WHITE)
        s.configure("TLabelframe.Label", background=WHITE, foreground=MUTED)

    def button(self, parent, text, command, accent=False, **pack):
        b = ttk.Button(parent, text=text, command=command, style="Accent.TButton" if accent else "TButton")
        b.pack(**pack)
        self.actions.append(b)
        return b

    def _build(self):
        sidebar = tk.Frame(self, bg="#e8eeec", width=164)
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)
        tk.Label(sidebar, text="发票工作台", bg="#e8eeec", fg=ACCENT,
                 font=("Microsoft YaHei UI", 16, "bold")).pack(anchor="w", padx=18, pady=(25, 4))
        tk.Label(sidebar, text="INVOICE WORKBENCH", bg="#e8eeec", fg=MUTED,
                 font=("Microsoft YaHei UI", 8)).pack(anchor="w", padx=18, pady=(0, 28))
        self.page_specs = [
            ("发票资料", "导入已有发票，核对信息后按额度准备报销。", self._library_page),
            ("报销批次", "查看批次、导出报销材料，并更新报销进度。", self._batch_page),
            ("设置", "管理发票资料库与报销公司。", self._settings_page),
        ]
        self.nav_buttons = []
        for index, (title, _, _) in enumerate(self.page_specs):
            button = ttk.Button(sidebar, text=title, style="Nav.TButton",
                                command=lambda n=index: self.pages.select(n))
            button.pack(fill="x", padx=10, pady=4)
            self.nav_buttons.append(button)
        tk.Label(sidebar, text=f"本地处理 · 按需使用\nv{__version__}", justify="left", bg="#e8eeec", fg=MUTED,
                 font=("Microsoft YaHei UI", 9)).pack(side="bottom", anchor="w", padx=18, pady=20)
        main = ttk.Frame(self, padding=(20, 18, 20, 10))
        main.pack(side="left", fill="both", expand=True)
        header = ttk.Frame(main)
        header.pack(fill="x")
        self.page_title = tk.StringVar()
        self.page_hint = tk.StringVar()
        ttk.Label(header, textvariable=self.page_title, style="Title.TLabel").pack(side="left")
        ttk.Label(main, textvariable=self.page_hint, style="Muted.TLabel").pack(anchor="w", pady=(4, 16))
        self.pages = PageStack(main, self.page_changed)
        self.pages.pack(fill="both", expand=True)
        for _, _, build in self.page_specs:
            frame = ttk.Frame(self.pages)
            self.pages.add(frame)
            build(frame)
        footer = ttk.Frame(main)
        footer.pack(fill="x", pady=(10, 0))
        self.status_text = tk.StringVar(value="打开资料库…")
        ttk.Label(footer, textvariable=self.status_text, style="Muted.TLabel", wraplength=1000).pack(side="left", fill="x", expand=True)
        self.progress = ttk.Progressbar(footer, mode="indeterminate", length=100)
        self.reconnect_button = ttk.Button(footer, text="重新连接", command=self.reconnect_library)
        self.pages.select(0)

    def page_changed(self, index):
        title, hint, _ = self.page_specs[index]
        self.page_title.set(title)
        self.page_hint.set(hint)
        for n, button in enumerate(self.nav_buttons):
            button.configure(style="Selected.Nav.TButton" if n == index else "Nav.TButton")

    def _library_page(self, page):
        self.stats_frame = ttk.Frame(page)
        self.stats_frame.pack(fill="x", pady=(0, 14))
        self.stats = []
        for title in ("发票记录", "待核对 / 冲突", "可用票面合计", "已占用"):
            card = ttk.Frame(self.stats_frame, style="Card.TFrame", padding=(14, 8))
            card.pack(side="left", fill="x", expand=True, padx=(0, 8))
            ttk.Label(card, text=title, style="Card.TLabel").pack(anchor="w")
            var = tk.StringVar(value="—")
            ttk.Label(card, textvariable=var, style="Stat.TLabel").pack(anchor="w", pady=(4, 0))
            self.stats.append(var)
        toolbar = ttk.Frame(page)
        toolbar.pack(fill="x", pady=(0, 12))
        self.button(toolbar, "导入发票文件夹", self.import_directory, True, side="left")
        self.button(toolbar, "按额度选票", self.open_selection, side="left", padx=8)
        self.search = tk.StringVar()
        search = ttk.Entry(toolbar, textvariable=self.search, width=23)
        search.pack(side="right", padx=(8, 0))
        search.bind("<KeyRelease>", lambda e: self.fill_invoices())
        search.configure(takefocus=True)
        ttk.Label(toolbar, text="搜索票号 / 抬头").pack(side="right")
        filters = ttk.Frame(page)
        filters.pack(fill="x", pady=(0, 10))
        self.filter = tk.StringVar(value="全部")
        combo = ttk.Combobox(filters, textvariable=self.filter, values=["全部", "待核对", "可用", "已占用", "同票冲突", "公司不匹配", "已移出"], state="readonly", width=15)
        combo.pack(side="left")
        combo.bind("<<ComboboxSelected>>", lambda e: self.fill_invoices())
        self.list_summary = tk.StringVar(value="还没有发票，点击上方“导入发票文件夹”开始。")
        ttk.Label(filters, textvariable=self.list_summary, style="Muted.TLabel").pack(side="left", padx=12)
        splitter = self.splitter = ttk.Panedwindow(page, orient="horizontal")
        splitter.pack(fill="both", expand=True)
        left, right = ttk.Frame(splitter), ttk.Frame(splitter, style="Card.TFrame", padding=12)
        self.detail_pane = right
        self.detail_visible = False
        splitter.add(left, weight=6)
        columns = ("file", "number", "date", "amount", "state")
        self.tree = ttk.Treeview(left, columns=columns, show="headings", selectmode="browse")
        for name, title, width in zip(columns, ("文件名", "发票号码", "开票日期", "金额 / 元", "状态"), (190, 175, 100, 90, 90)):
            self.tree.heading(name, text=title)
            self.tree.column(name, width=width, minwidth=70, stretch=name == "file", anchor="e" if name == "amount" else "w")
        scrollbar = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        horizontal = ttk.Scrollbar(left, orient="horizontal", command=self.tree.xview)
        self.tree.configure(xscrollcommand=horizontal.set)
        horizontal.pack(side="bottom", fill="x")
        self.tree.pack(fill="both", expand=True)
        self.empty_hint = tk.Label(left, text="还没有发票\n\n点击上方“导入发票文件夹”即可添加。", bg=WHITE, fg=MUTED,
                                   font=("Microsoft YaHei UI", 11), justify="center")
        self.tree.bind("<<TreeviewSelect>>", self.select_invoice)
        self.tree.bind("<Double-1>", lambda e: self.show_detail() if self.current_id else None)
        self.tree.tag_configure("pending", foreground="#9b6c20")
        self.tree.tag_configure("occupied", foreground=MUTED)
        self.details_title = tk.StringVar(value="选择一张发票进行核对")
        detail_header = ttk.Frame(right, style="Card.TFrame")
        detail_header.pack(fill="x", pady=(0, 10))
        ttk.Button(detail_header, text="收起", command=self.hide_detail).pack(side="right")
        ttk.Label(detail_header, textvariable=self.details_title, style="Card.TLabel", font=("Microsoft YaHei UI", 12, "bold"), wraplength=360).pack(side="left", fill="x", expand=True)
        self.details = ttk.Notebook(right)
        self.details.pack(fill="both", expand=True)
        form_page = ttk.Frame(self.details, style="Card.TFrame")
        form_canvas = tk.Canvas(form_page, bg=WHITE, highlightthickness=0, width=360)
        form_scroll = ttk.Scrollbar(form_page, orient="vertical", command=form_canvas.yview)
        form_canvas.configure(yscrollcommand=form_scroll.set)
        form_scroll.pack(side="right", fill="y")
        form_canvas.pack(side="left", fill="both", expand=True)
        form = ttk.Frame(form_canvas, style="Card.TFrame", padding=10)
        form_window = form_canvas.create_window((0, 0), window=form, anchor="nw")
        form.bind("<Configure>", lambda e: form_canvas.configure(scrollregion=form_canvas.bbox("all")))
        form_canvas.bind("<Configure>", lambda e: form_canvas.itemconfigure(form_window, width=e.width))
        def scroll_form(event):
            widget = self.winfo_containing(event.x_root, event.y_root)
            while widget:
                if widget is form_page:
                    form_canvas.yview_scroll(-int(event.delta / 120), "units")
                    return "break"
                widget = getattr(widget, "master", None)
        self.bind_all("<MouseWheel>", scroll_form, add="+")
        preview = ttk.Frame(self.details, style="Card.TFrame", padding=8)
        text_page = ttk.Frame(self.details, style="Card.TFrame", padding=8)
        self.details.add(form_page, text="字段核对")
        self.details.add(preview, text="原票预览")
        self.details.add(text_page, text="提取文本")
        self.fields, self.entries = {}, []
        for row, (key, title) in enumerate((("number", "发票号码"), ("code", "发票代码"), ("date", "开票日期"), ("amount", "价税合计 / 元"), ("buyer_name", "购买方抬头"), ("buyer_tax_id", "购买方税号"), ("seller_name", "销售方名称"), ("note", "备注"))):
            ttk.Label(form, text=title, style="Card.TLabel").grid(row=row, column=0, sticky="w", pady=4)
            var = tk.StringVar()
            var.trace_add("write", self.mark_dirty)
            entry = ttk.Entry(form, textvariable=var)
            entry.grid(row=row, column=1, sticky="ew", padx=(12, 0), pady=4)
            self.fields[key] = var
            self.entries.append(entry)
        form.columnconfigure(1, weight=1)
        self.warning_text = tk.StringVar(value="导入文件后，关键字段将在这里显示。")
        ttk.Label(form, textvariable=self.warning_text, style="Card.TLabel", foreground="#9b6c20", wraplength=390).grid(row=8, column=0, columnspan=2, sticky="w", pady=(12, 6))
        self.review_button = ttk.Button(form, text="保存并标记已核对", command=self.save_review, style="Accent.TButton")
        self.actions.append(self.review_button)
        self.review_button.grid(row=9, column=0, columnspan=2, sticky="ew", pady=(4, 6))
        form_actions = ttk.Frame(form, style="Card.TFrame")
        form_actions.grid(row=10, column=0, columnspan=2, sticky="ew")
        self.button(form_actions, "合并同票文件", self.merge_invoice, side="left", padx=(0, 6))
        self.archive_button = self.button(form_actions, "移出候选", self.archive_invoice, side="left")
        filebar = ttk.Frame(preview, style="Card.TFrame")
        filebar.pack(fill="x")
        self.file_choice = ttk.Combobox(filebar, state="readonly", width=24)
        self.file_choice.pack(side="left", fill="x", expand=True)
        self.file_choice.bind("<<ComboboxSelected>>", lambda e: self.render_preview())
        self.button(filebar, "打开原件", self.open_original, side="right", padx=(6, 0))
        self.preview_canvas = tk.Canvas(preview, bg="#edf1f3", highlightthickness=0)
        self.preview_canvas.pack(fill="both", expand=True, pady=(8, 0))
        self.preview_canvas.bind("<Configure>", lambda e: self.render_preview() if self.current_id and self.details.index("current") == 1 else None)
        self.raw = tk.Text(text_page, wrap="word", bg=WHITE, fg=INK, relief="flat", font=("Microsoft YaHei UI", 10), state="disabled")
        self.raw.pack(fill="both", expand=True)
        self.details.bind("<<NotebookTabChanged>>", lambda e: self.render_preview() if self.details.index("current") == 1 else None)

    def _batch_page(self, page):
        actions = ttk.Frame(page)
        actions.pack(fill="x", pady=(0, 12))
        self.button(actions, "导出原票与明细", self.export_batch, True, side="left")
        for text, state in (("标记已提交", "submitted"), ("标记已报销", "reimbursed"), ("标记已退回", "returned"), ("取消并释放", "cancelled")):
            self.button(actions, text, lambda s=state: self.transition_batch(s), side="left", padx=(8, 0))
        ttk.Label(page, text="整票占用，超额不结转。导出不改变报销进度；退回保持占用，取消后释放。", style="Muted.TLabel").pack(anchor="w", pady=(0, 12))
        columns = ("time", "count", "target", "total", "declared", "status")
        self.batch_tree = ttk.Treeview(page, columns=columns, show="headings", selectmode="browse")
        for key, title, width in zip(columns, ("创建时间", "张数", "目标 / 元", "票面 / 元", "申报 / 元", "状态"), (200, 65, 130, 130, 130, 110)):
            self.batch_tree.heading(key, text=title)
            self.batch_tree.column(key, width=width, anchor="w")
        self.batch_tree.pack(fill="both", expand=True)
        self.batch_tree.bind("<<TreeviewSelect>>", self.show_batch)
        self.batch_info = tk.Text(page, height=7, wrap="word", state="disabled", relief="flat", bg=WHITE, font=("Microsoft YaHei UI", 10))
        self.batch_info.pack(fill="x", pady=(12, 0))

    def _settings_page(self, page):
        ttk.Label(page, text="发票资料库", font=("Microsoft YaHei UI", 14, "bold")).pack(anchor="w")
        self.directory_text = tk.StringVar()
        ttk.Label(page, textvariable=self.directory_text, wraplength=850).pack(anchor="w", pady=8)
        self.library_button = self.button(page, "切换资料库…", self.choose_library, anchor="w")
        ttk.Label(page, text="这里保存发票记录、原件副本和报销批次。添加已有发票，请在“发票资料”页导入文件夹。\n需要换电脑时，将资料库放入网盘；关闭当前窗口并等同步完成后，再在另一台电脑打开。", style="Muted.TLabel", wraplength=920).pack(anchor="w", pady=(12, 24))
        ttk.Label(page, text="报销公司", font=("Microsoft YaHei UI", 14, "bold")).pack(anchor="w")
        self.company_name, self.company_tax = tk.StringVar(), tk.StringVar()
        for title, var in (("购买方抬头", self.company_name), ("购买方税号", self.company_tax)):
            ttk.Label(page, text=title).pack(anchor="w", pady=(10, 4))
            ttk.Entry(page, textvariable=var, width=58).pack(anchor="w")
        self.button(page, "保存公司设置", self.save_company, True, anchor="w", pady=(12, 20))
        ttk.Label(page, text="填写税号时精确匹配税号；同时填写抬头时两项都须匹配。", style="Muted.TLabel").pack(anchor="w")
        ttk.Separator(page).pack(fill="x", pady=20)
        ttk.Label(page, text=f"版本 {__version__} · 本地文件整理阶段", font=("Microsoft YaHei UI", 12, "bold")).pack(anchor="w")
        ttk.Label(page, text="支持 PDF 文字层与部分 XML 字段提取、图片预览、人工核对、精确选票和批次导出。\n当前不连接 Outlook，不上传发票；图片 OCR 与扫描 PDF 识别将在真实样本验证后接入。", style="Muted.TLabel", wraplength=850).pack(anchor="w", pady=8)

    def _save_settings(self):
        self.settings_file.parent.mkdir(parents=True, exist_ok=True)
        self.settings_file.write_text(json.dumps(self.settings, ensure_ascii=False, indent=2), encoding="utf-8")

    def open_library(self, directory):
        directory = Path(directory).expanduser()
        if not self.store:
            self.connection_directory = directory
        if self.store and self.store.root == directory.resolve():
            if not self.reload():
                raise UserError(self.status_text.get())
            return
        new = Store(directory)
        try:
            new.view()
        except Exception:
            new.close()
            raise
        old = self.store
        self.store = new
        self.connection_directory = new.root
        if old:
            old.close()
        self.settings["data_directory"] = str(new.root)
        self._save_settings()
        self.current_id = None
        self.clear_detail()
        self.search.set("")
        self.filter.set("全部")
        self.reload()

    def choose_library(self):
        if self.busy or not self.allow_discard():
            return
        path = filedialog.askdirectory(title="打开已有资料库，或选择空文件夹新建", parent=self)
        if path:
            if not (Path(path) / "invoices.json").exists() and contains_invoices(path):
                # Selecting a source folder should do what the user intended.
                self.start_import(path)
                return
            try:
                self.open_library(path)
            except (UserError, OSError, ValueError) as exc:
                messagebox.showerror("无法打开资料库", str(exc), parent=self)

    def update_actions(self):
        for button in self.actions:
            enabled = not self.busy and (self.session_ok or button is self.library_button)
            button.configure(state="normal" if enabled else "disabled")
        editable = self.session_ok and not self.busy and bool(self.selected_item())
        if editable:
            item = self.selected_item()
            editable = not item.get("batch_id") and not item.get("archived")
        for entry in self.entries:
            entry.configure(state="normal" if editable else "readonly")
        self.review_button.configure(state="normal" if editable else "disabled")

    def connection_error(self, text):
        self.session_ok = False
        self.status_text.set(text)
        self.reconnect_button.pack(side="right", padx=(10, 0))
        self.update_actions()

    def reconnect_library(self):
        if self.busy or not self.allow_discard():
            return
        directory = self.store.root if self.store else self.connection_directory
        self.connection_directory = directory
        if self.store:
            self.store.close()
            self.store = None
        self.snapshot = {}
        self.clear_detail()
        self.fill_invoices()
        self.fill_batches()
        try:
            self.open_library(directory)
        except (UserError, OSError, ValueError) as exc:
            self.connection_error(str(exc))
            # Local live writers remain protected by the OS guard even during recovery.
            if "未结束的会话" in str(exc) and messagebox.askyesno(
                    "恢复资料连接", f"资料库：{directory}\n\n仅在其他电脑上的工具已关闭、网盘同步已完成时恢复。\n\n确认恢复此资料库的遗留会话？", parent=self):
                try:
                    Store(directory, recover_lock=True).close()
                    self.open_library(directory)
                except (UserError, OSError, ValueError) as error:
                    self.connection_error(str(error))
                    messagebox.showerror("暂时无法连接", str(error), parent=self)

    def reload(self):
        if not self.store or self.busy:
            return False
        try:
            self.snapshot = self.store.view()
        except (UserError, OSError, ValueError) as exc:
            self.connection_error(str(exc))
            return False
        self.session_ok = True
        self.reconnect_button.pack_forget()
        invoices = self.snapshot["invoices"]
        available = [i for i in invoices if state_label(i) == "可用"]
        values = [str(len(invoices)), str(sum(state_label(i) in {"待核对", "同票冲突"} for i in invoices)),
                  "¥ " + yuan(sum(i["amount_cents"] for i in available)), str(sum(bool(i.get("batch_id")) for i in invoices))]
        for var, value in zip(self.stats, values):
            var.set(value)
        self.directory_text.set(self.snapshot["data_directory"])
        self.company_name.set(self.snapshot["company"]["name"])
        self.company_tax.set(self.snapshot["company"]["tax_id"])
        self.fill_invoices()
        self.fill_batches()
        if self.current_id:
            item = next((i for i in invoices if i["id"] == self.current_id), None)
            if item:
                self.load_invoice(item)
            else:
                self.clear_detail()
        if not self.snapshot["company"].get("name") and not self.snapshot["company"].get("tax_id"):
            self.status_text.set("可以直接导入发票。按额度选票前，请在“设置”填写报销公司。")
        else:
            self.status_text.set("已就绪。新导入的发票需核对后参与选票。")
        self.update_actions()
        return True

    def fill_invoices(self):
        query = self.search.get().strip().lower()
        selected = self.current_id
        self.tree.delete(*self.tree.get_children())
        for item in sorted(self.snapshot.get("invoices", []), key=lambda i: (i.get("date", ""), i["created"]), reverse=True):
            status = state_label(item)
            if self.filter.get() != "全部" and self.filter.get() != status:
                continue
            if query and query not in " ".join(str(item.get(k, "")) for k in ("number", "buyer_name", "buyer_tax_id", "seller_name")).lower():
                continue
            self.tree.insert("", "end", iid=item["id"], values=(item["files"][0]["name"], item["number"] or "待填写票号", item["date"] or "—", yuan(item["amount_cents"]) if item["amount_cents"] else "待填写", status), tags=("occupied" if item.get("batch_id") else "pending" if status in {"待核对", "同票冲突"} else "normal",))
        if selected and self.tree.exists(selected):
            self.tree.selection_set(selected)
        count, total = len(self.tree.get_children()), len(self.snapshot.get("invoices", []))
        self.list_summary.set(f"显示 {count} / {total} 张 · 点击发票查看与核对" if total else "点击“导入发票文件夹”添加，原文件保持原样。")
        if count:
            self.empty_hint.place_forget()
        else:
            self.empty_hint.configure(text="没有匹配的发票\n\n请清空搜索或切换为“全部”。" if total else "还没有发票\n\n点击上方“导入发票文件夹”即可添加。")
            self.empty_hint.place(relx=0.5, rely=0.45, anchor="center")

    def mark_dirty(self, *_):
        if not self.loading and self.current_id:
            self.dirty = True

    def allow_discard(self):
        return not self.dirty or messagebox.askyesno("尚未保存", "当前字段修改尚未保存。放弃修改并继续？", parent=self)

    def select_invoice(self, event=None):
        selection = self.tree.selection()
        if not selection or selection[0] == self.current_id:
            return
        if not self.allow_discard():
            if self.current_id and self.tree.exists(self.current_id):
                self.tree.selection_set(self.current_id)
            return
        item = next((i for i in self.snapshot["invoices"] if i["id"] == selection[0]), None)
        if item:
            self.load_invoice(item)

    def clear_detail(self):
        self.hide_detail()
        self.current_id = None
        self.loading = True
        for var in self.fields.values():
            var.set("")
        self.loading = self.dirty = False
        self.details_title.set("选择一张发票进行核对")
        self.file_choice["values"] = []
        self.preview_generation += 1
        self.image_reference = None
        self.preview_canvas.delete("all")
        self.set_text(self.raw, "")

    def load_invoice(self, item):
        self.show_detail()
        self.loading = True
        self.current_id = item["id"]
        for key, var in self.fields.items():
            var.set(yuan(item["amount_cents"]) if key == "amount" and item["amount_cents"] else item.get(key, ""))
        self.loading = self.dirty = False
        self.details_title.set(f"{item['number'] or '待核对发票'} · {state_label(item)}")
        self.warning_text.set("\n".join(item.get("warnings", [])) or "请对照原票核对号码、日期、价税合计及购买方信息。")
        self.file_choice["values"] = [f["name"] for f in item["files"]]
        self.file_choice.current(0)
        self.set_text(self.raw, item.get("raw_text", "") or "此文件暂无可提取文字，请打开原件核对并填写字段。")
        self.archive_button.configure(text="恢复候选" if item.get("archived") else "移出候选")
        editable = not item.get("batch_id") and not item.get("archived") and not self.busy
        for entry in self.entries:
            entry.configure(state="normal" if editable else "readonly")
        self.review_button.configure(state="normal" if editable and not self.busy else "disabled")
        self.preview_generation += 1
        self.preview_canvas.delete("all")
        if self.details.index("current") == 1:
            self.render_preview()

    def show_detail(self):
        if not self.detail_visible:
            self.splitter.add(self.detail_pane, weight=4)
            self.detail_visible = True
            # Let Tk allocate the newly inserted pane before positioning its sash.
            # Otherwise its pending geometry pass can collapse the pane to 1 px.
            self.splitter.update_idletasks()
            if self.splitter.winfo_width() > 700:
                self.splitter.sashpos(0, int(self.splitter.winfo_width() * 0.60))

    def hide_detail(self):
        if self.detail_visible:
            self.splitter.forget(self.detail_pane)
            self.detail_visible = False

    @staticmethod
    def set_text(widget, text):
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", text)
        widget.configure(state="disabled")

    def selected_item(self):
        return next((i for i in self.snapshot.get("invoices", []) if i["id"] == self.current_id), None)

    def selected_file(self):
        item = self.selected_item()
        index = self.file_choice.current()
        return item["files"][index] if item and 0 <= index < len(item["files"]) else None

    def render_preview(self):
        file = self.selected_file()
        self.preview_generation += 1
        generation = self.preview_generation
        self.image_reference = None
        self.preview_canvas.delete("all")
        if not file or not self.store:
            return
        try:
            path = self.store.original(file)
            from PIL import Image, ImageTk
            suffix = path.suffix.lower()
            if suffix == ".pdf":
                import pypdfium2 as pdfium
                doc = pdfium.PdfDocument(str(path))
                try:
                    page = doc[0]
                    try:
                        bitmap = page.render(scale=min(1.4, 1400 / max(page.get_size())))
                        try:
                            image = bitmap.to_pil().copy()
                        finally:
                            bitmap.close()
                    finally:
                        page.close()
                finally:
                    doc.close()
            elif suffix in {".png", ".jpg", ".jpeg"}:
                with Image.open(path) as source:
                    image = source.copy()
            else:
                self.preview_canvas.create_text(16, 20, anchor="nw", text="此格式可通过“打开原件”查看。\n提取内容位于“提取文本”。", fill=MUTED, width=340)
                return
            image.thumbnail((max(300, self.preview_canvas.winfo_width() - 20), max(220, self.preview_canvas.winfo_height() - 30)))
            if generation == self.preview_generation:
                self.image_reference = ImageTk.PhotoImage(image)
                self.preview_canvas.create_image(10, 20, anchor="nw", image=self.image_reference)
                if suffix == ".pdf":
                    self.preview_canvas.create_text(10, 2, anchor="nw", text="首页预览 · 多页票据请打开原件", fill=MUTED)
        except ImportError:
            self.preview_canvas.create_text(16, 20, anchor="nw", text="预览依赖未安装，请使用“打开原件”。\n或运行 setup.ps1 安装预览组件。", fill=MUTED, width=340)
        except Exception as exc:
            self.preview_canvas.create_text(16, 20, anchor="nw", text=f"无法生成预览，请打开原件核对。\n{exc}", fill=MUTED, width=340)

    def open_original(self):
        file = self.selected_file()
        if file and self.store:
            try:
                path = self.store.original(file)
                import os
                os.startfile(str(path))
            except (UserError, OSError) as exc:
                messagebox.showerror("无法打开原件", str(exc), parent=self)

    def task(self, description, work, done=None, *, preserve_edits=True):
        if self.busy:
            return
        self.busy = True
        self.pending_edits = (self.current_id, {k: v.get() for k, v in self.fields.items()}) if self.dirty else None
        self.preserve_edits = preserve_edits
        self.status_text.set(description)
        for b in self.actions:
            b.configure(state="disabled")
        for entry in self.entries:
            entry.configure(state="readonly")
        self.progress.pack(side="right", padx=(10, 0))
        self.progress.start(12)

        def run():
            try:
                self.events.put(("done", (work(), done)))
            except Exception as exc:
                logging.exception("Operation failed: %s", description)
                self.events.put(("error", exc))
        threading.Thread(target=run, daemon=True).start()

    def _poll(self):
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "progress":
                    self.status_text.set(payload)
                    continue
                self.busy = False
                self.progress.stop()
                self.progress.pack_forget()
                self.reload()
                self.update_actions()
                if self.pending_edits and (kind == "error" or self.preserve_edits):
                    if self.pending_edits[0] == self.current_id:
                        for key, value in self.pending_edits[1].items():
                            self.fields[key].set(value)
                self.pending_edits = None
                if kind == "error":
                    messagebox.showerror("操作未完成", str(payload), parent=self)
                    if self.session_ok:
                        self.status_text.set("操作未完成；请核对提示后重试。")
                else:
                    result, callback = payload
                    if callback:
                        callback(result)
        except queue.Empty:
            pass
        self.poll_id = self.after(100, self._poll)

    def import_directory(self):
        if not self.session_ok or self.busy or not self.allow_discard():
            return
        source = filedialog.askdirectory(title="选择发票文件夹（自动包含子文件夹）", parent=self,
                                         initialdir=self.settings.get("last_import_directory", str(Path.home())))
        if not source:
            return
        self.start_import(source)

    def start_import(self, source):
        if not self.session_ok or self.busy:
            return
        store, revision = self.store, self.snapshot["revision"]
        self.pages.select(0)
        self.current_id = None
        self.clear_detail()
        self.search.set("")
        self.filter.set("全部")
        self.settings["last_import_directory"] = str(Path(source).resolve())
        self._save_settings()
        def work():
            return import_directory(store, revision, source, lambda msg: self.events.put(("progress", msg)))
        self.task("正在读取发票…", work, self.import_done)

    def import_done(self, report):
        text = f"读取 {report['scanned']} 个支持的文件 · 新增 {report['imported']} 张 · 归并 {report['merged']} 个 · 已有 {report['duplicates']} 个"
        if report["errors"]:
            detail = text + "\n\n" + "\n".join(f"{e['file']}：{e['error']}" for e in report["errors"][:8])
            messagebox.showwarning("部分文件未导入", detail, parent=self)
        if report["notes"]:
            messagebox.showinfo("导入提示", "\n".join(report["notes"][:5]), parent=self)
        if not report["scanned"]:
            text = "没有找到支持的文件。支持 PDF、XML、PNG、JPG、OFD 和 EML，自动包含子文件夹。"
        elif report["errors"]:
            text += f" · 失败 {len(report['errors'])} 个"
        else:
            text += "。请选择发票核对信息。"
        self.status_text.set(text)
        children = self.tree.get_children()
        if children:
            self.tree.selection_set(children[0])
            self.select_invoice()

    def save_company(self):
        if self.store:
            name, tax = self.company_name.get(), self.company_tax.get()
            revision = self.snapshot["revision"]
            self.task("保存公司设置…", lambda: self.store.set_company(revision, name, tax))

    def save_review(self):
        item = self.selected_item()
        if item and self.store:
            fields = {k: v.get() for k, v in self.fields.items()}
            invoice_id, revision = item["id"], self.snapshot["revision"]
            self.task("保存核对结果…", lambda: self.store.review(revision, invoice_id, fields), preserve_edits=False)

    def archive_invoice(self):
        item = self.selected_item()
        if not item or not self.store or not self.allow_discard():
            return
        archived = not item.get("archived", False)
        if not messagebox.askyesno("候选状态", "移出候选后保留原件和记录，可随时恢复。" if archived else "恢复后需要重新核对，是否继续？", parent=self):
            return
        revision, item_id = self.snapshot["revision"], item["id"]
        self.task("更新候选状态…", lambda: self.store.archive(revision, item_id, archived), preserve_edits=False)

    def merge_invoice(self):
        item = self.selected_item()
        if not item or not self.store or not self.allow_discard():
            return
        candidates = [i for i in self.snapshot["invoices"] if i["id"] != item["id"] and not i.get("batch_id") and not i.get("archived")]
        if not candidates:
            messagebox.showinfo("合并同票文件", "没有可合并的其他发票记录。", parent=self)
            return
        dialog = tk.Toplevel(self)
        dialog.title("合并同票文件")
        dialog.geometry("600x245")
        dialog.transient(self)
        dialog.grab_set()
        ttk.Label(dialog, text="仅在确认两条记录是同一张发票时合并。\n当前记录的文件会归入所选目标，保留目标字段，随后重新核对。", wraplength=550).pack(anchor="w", padx=20, pady=18)
        labels = [f"{i['number'] or '待填写票号'} | ¥{yuan(i['amount_cents'])} | {i['buyer_name']} | {i['id'][:6]}" for i in candidates]
        choice = ttk.Combobox(dialog, values=labels, state="readonly", width=70)
        choice.pack(fill="x", padx=20)
        same = next((n for n, i in enumerate(candidates) if identity(i) == identity(item) and identity(i)), 0)
        choice.current(same)
        def commit():
            target_id = candidates[choice.current()]["id"]
            dialog.destroy()
            revision, source_id = self.snapshot["revision"], item["id"]
            def done(_):
                if self.tree.exists(target_id):
                    self.tree.selection_set(target_id)
                    self.select_invoice()
            self.task("合并同票文件…", lambda: self.store.merge(revision, source_id, target_id), done, preserve_edits=False)
        ttk.Button(dialog, text="确认是同一张发票，合并", command=commit).pack(anchor="e", padx=20, pady=22)

    def open_selection(self):
        if not self.store or self.busy or not self.allow_discard():
            return
        if not self.snapshot["company"].get("name") and not self.snapshot["company"].get("tax_id"):
            self.pages.select(2)
            messagebox.showinfo("先设置公司", "请先填写购买方抬头或税号。", parent=self)
            return
        SelectionDialog(self)

    def fill_batches(self):
        previous = self.batch_tree.selection()
        self.batch_tree.delete(*self.batch_tree.get_children())
        for batch in reversed(self.snapshot.get("batches", [])):
            from datetime import datetime
            created = datetime.fromisoformat(batch["created"]).astimezone().strftime("%Y-%m-%d %H:%M")
            self.batch_tree.insert("", "end", iid=batch["id"], values=(created, len(batch["items"]), yuan(batch["target_cents"]), yuan(batch["total_cents"]), yuan(batch["declared_cents"]), STATUS[batch["status"]]))
        if previous and self.batch_tree.exists(previous[0]):
            self.batch_tree.selection_set(previous[0])

    def selected_batch(self):
        selection = self.batch_tree.selection()
        return next((b for b in self.snapshot.get("batches", []) if selection and b["id"] == selection[0]), None)

    def show_batch(self, event=None):
        batch = self.selected_batch()
        if batch:
            text = f"批次：{batch['id']}\n公司：{batch['company']['name']}   税号：{batch['company']['tax_id']}\n" + "\n".join(f"{i['number']}  {i['date']}  ¥{yuan(i['amount_cents'])}" for i in batch["items"])
            self.set_text(self.batch_info, text)

    def export_batch(self):
        batch = self.selected_batch()
        if not batch or not self.store:
            messagebox.showinfo("选择批次", "请先选择一个报销批次。", parent=self)
            return
        dest = filedialog.asksaveasfilename(title="保存原票与明细 ZIP", initialfile=f"报销材料-{batch['id'][:8]}.zip", defaultextension=".zip", filetypes=[("ZIP", "*.zip")], parent=self)
        if not dest:
            return
        store = self.store
        def work():
            built = store.export(batch["id"])
            if built.resolve() != Path(dest).resolve():
                shutil.copy2(built, dest)
            return dest
        self.task("校验原票并生成报销材料…", work, lambda p: messagebox.showinfo("导出完成", f"已保存：\n{p}\n\n报销状态保持不变。", parent=self))

    def transition_batch(self, status):
        batch = self.selected_batch()
        if not batch or not self.store:
            return
        prompt = "取消后整张发票恢复可用，历史记录仍保留。确认取消？" if status == "cancelled" else f"将批次标记为“{STATUS[status]}”？"
        if not messagebox.askyesno("更新报销进度", prompt, parent=self):
            return
        revision = self.snapshot["revision"]
        self.task("更新报销进度…", lambda: self.store.transition(revision, batch["id"], status))

    def close_app(self):
        if self.busy:
            messagebox.showinfo("正在处理", "请等待当前处理完成后关闭窗口，确保发票记录保存完整。", parent=self)
            return
        if not self.allow_discard():
            return
        if self.store:
            self.store.close()
        self.after_cancel(self.poll_id)
        self.destroy()


def main():
    parser = argparse.ArgumentParser(description="个人发票工作台 · 本地 Windows GUI")
    parser.add_argument("--data-dir", type=Path, help="资料目录，可设为网盘专用目录")
    parser.add_argument("--settings-file", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--recover-lock", action="store_true", help="确认旧会话已退出后，手动恢复遗留会话锁")
    args = parser.parse_args()
    if args.recover_lock:
        directory = args.data_dir
        if directory is None:
            try:
                settings = json.loads((args.settings_file or APP_ROOT / "local-settings.json").read_text(encoding="utf-8"))
                directory = Path(settings["data_directory"])
            except (OSError, ValueError, KeyError):
                directory = APP_ROOT / "data"
        prompt = tk.Tk()
        prompt.withdraw()
        accepted = messagebox.askyesno("恢复遗留会话", "请确认所有电脑上的工具均已退出，网盘已同步完成。\n\n恢复会话锁不会合并同步冲突。确认恢复？", parent=prompt)
        prompt.destroy()
        if not accepted:
            return
        Store(directory, recover_lock=True).close()
    app = Workbench(args.data_dir, args.settings_file)
    try:
        app.mainloop()
    finally:
        if app.store:
            app.store.close()


if __name__ == "__main__":
    main()
