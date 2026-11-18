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

from PIL import Image, ImageTk
import pypdfium2 as pdfium

from . import __version__
from .domain import UserError, yuan
from .labels import INVOICE_STATUS, selectable, state_label
from .selection import SelectionDialog
from .store import OCCUPIED_STATUSES, Store
from .theme import COLORS, FONT, MONO, Theme, enable_dpi_awareness, fit_window, wrap_label
from .widgets import SegmentedControl, SegmentedPages, ScrollableFrame, hover_rows, table_scrollers
from .workflow import contains_invoices, import_directory, remove_duplicate_files

APP_ROOT = Path(__file__).resolve().parent.parent


class PageStack(ttk.Frame):
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


class DuplicateFilesDialog(tk.Toplevel):
    def __init__(self, owner, source, duplicates):
        super().__init__(owner)
        p = owner.theme.px
        space = owner.theme.space
        self.title("整理重复发票文件")
        fit_window(self, owner.theme, 900, 480, 650, 350)
        self.transient(owner)
        self.grab_set()
        self.accepted = False
        panel = ttk.Frame(self, padding=space.extra_large)
        panel.pack(fill="both", expand=True)
        for text in (f"发现 {len(duplicates)} 个内容完全相同的发票副本。每组保留一份原件，工作区副本已校验。", f"源目录：{source}"):
            label = ttk.Label(panel, text=text, style="Muted.TLabel")
            label.pack(fill="x", pady=(0, space.small))
            wrap_label(label)
        actions = ttk.Frame(panel)
        actions.pack(side="top", fill="x", pady=(0, space.medium))
        self.no_button = ttk.Button(actions, text="否，保留文件并继续", command=self.destroy)
        self.no_button.pack(side="right")
        self.yes_button = ttk.Button(actions, text="是，删除重复文件", command=self.accept, style="Danger.TButton")
        self.yes_button.pack(side="right", padx=(0, space.small))
        listing = ttk.Frame(panel)
        listing.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(listing, columns=("remove", "keep"), show="headings")
        from tkinter import font
        text_font = font.nametofont("TkDefaultFont")
        for key, title in (("remove", "将删除的重复文件"), ("keep", "保留的原件")):
            self.tree.heading(key, text=title)
            width = max(p(400), *(text_font.measure(str(Path(d[key]).relative_to(source))) + space.extra_large
                                 for d in duplicates))
            self.tree.column(key, width=width, minwidth=p(200), stretch=False)
        vertical = ttk.Scrollbar(listing, orient="vertical", command=self.tree.yview)
        horizontal = ttk.Scrollbar(listing, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        vertical.pack(side="right", fill="y")
        horizontal.pack(side="bottom", fill="x")
        self.tree.pack(fill="both", expand=True)
        for duplicate in duplicates:
            self.tree.insert("", "end", values=tuple(str(Path(duplicate[key]).relative_to(source)) for key in ("remove", "keep")))
        self.bind("<Escape>", lambda event: self.destroy())
        self.no_button.focus_set()
        self.update_idletasks()
        self.lift()

    def accept(self):
        self.accepted = True
        self.destroy()


class Workbench(tk.Tk):
    def __init__(self, data_dir: Path | None = None, settings_file: Path | None = None):
        enable_dpi_awareness()
        super().__init__()
        self.theme = Theme(self)
        self.title("发票工作台 · Invoice Workbench")
        fit_window(self, self.theme, 1360, 820, 1080, 640)
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
        self.selecting_invoice = False
        self.image_reference = None
        self.preview_generation = 0
        self.preview_image = None
        self.preview_key = None
        self.preview_zoom = 1.0
        self.preview_after = None
        self.preview_resize_position = None
        self.protocol("WM_DELETE_WINDOW", self.close_app)
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

    def button(self, parent, text, command, accent=False, **pack):
        b = ttk.Button(parent, text=text, command=command, style="Primary.TButton" if accent else "TButton")
        b.pack(**pack)
        self.actions.append(b)
        return b

    def _build(self):
        p = self.theme.px
        tiny, small, medium, large, extra_large = self.theme.space
        sidebar = self.sidebar = ttk.Frame(self, style="Sidebar.TFrame", width=p(164))
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)
        brand = tk.Label(sidebar, text="▣  发票工作台", bg=COLORS.sidebar, fg=COLORS.text, font=(FONT, 13, "bold"))
        brand.pack(anchor="w", padx=large, pady=(extra_large, tiny))
        brand_subtitle = ttk.Label(sidebar, text="INVOICE WORKBENCH", style="Sidebar.TLabel", font=(FONT, 7))
        brand_subtitle.pack(anchor="w", padx=large, pady=(0, extra_large))
        self.page_specs = [
            ("发票资料", "导入、核对与选票，集中处理每一张发票。", self._library_page),
            ("报销批次", "查看选票记录，导出发票文件与 CSV 明细。", self._batch_page),
            ("设置", "管理发票资料库与报销公司。", self._settings_page),
        ]
        self.nav_buttons = []
        for index, (title, _, _) in enumerate(self.page_specs):
            button = ttk.Button(sidebar, text=title, style="Nav.TButton",
                                command=lambda n=index: self.pages.select(n))
            button.pack(fill="x", padx=small, pady=tiny)
            self.nav_buttons.append(button)
        workspace = ttk.Frame(sidebar, style="Sidebar.TFrame", padding=large)
        workspace.pack(side="bottom", fill="x")
        self.workspace_status = tk.StringVar(self, value="○ 正在连接")
        self.workspace_path = tk.StringVar(self)
        self.workspace_label = ttk.Label(workspace, textvariable=self.workspace_status, style="Sidebar.TLabel")
        self.workspace_label.pack(anchor="w")
        path = ttk.Label(workspace, textvariable=self.workspace_path, style="Sidebar.TLabel")
        path.pack(fill="x", pady=small)
        wrap_label(path)
        ttk.Label(workspace, text=f"本地处理 · v{__version__}", style="Sidebar.TLabel").pack(anchor="w")
        ttk.Separator(self, orient="vertical").pack(side="left", fill="y")
        main = ttk.Frame(self, padding=(extra_large, extra_large, extra_large, medium))
        main.pack(side="left", fill="both", expand=True)
        main.columnconfigure(0, weight=1)
        main.rowconfigure(2, weight=1)
        self.page_title = tk.StringVar(self)
        self.page_hint = tk.StringVar(self)
        title = ttk.Label(main, textvariable=self.page_title, style="Title.TLabel")
        title.grid(row=0, column=0, sticky="w")
        hint = ttk.Label(main, textvariable=self.page_hint, style="Muted.TLabel")
        hint.grid(row=1, column=0, sticky="ew", pady=(tiny, large))
        wrap_label(hint)
        self.pages = PageStack(main, self.page_changed)
        self.pages.grid(row=2, column=0, sticky="nsew")
        for _, _, build in self.page_specs:
            frame = ttk.Frame(self.pages)
            self.pages.add(frame)
            build(frame)
        separator = ttk.Separator(main)
        separator.grid(row=3, column=0, sticky="ew", pady=(medium, 0))
        footer = ttk.Frame(main)
        footer.grid(row=4, column=0, sticky="ew", pady=(small, 0))
        footer.columnconfigure(0, weight=1)
        self.status_text = tk.StringVar(self, value="打开资料库…")
        self.status_label = ttk.Label(footer, textvariable=self.status_text, style="Hint.TLabel", width=1)
        self.status_label.grid(row=0, column=0, sticky="ew")
        wrap_label(self.status_label)
        self.progress = ttk.Progressbar(footer, mode="indeterminate", length=p(80))
        self.reconnect_button = ttk.Button(footer, text="重新连接", command=self.reconnect_library)
        self.pages.select(0)
        self.compact_layout = None

        def arrange_window(event):
            if event.widget is not self:
                return
            compact = event.height < p(560)
            if compact == self.compact_layout:
                return
            self.compact_layout = compact
            main.configure(padding=(small, small, small, tiny) if compact else (extra_large, extra_large, extra_large, medium))
            brand.pack_configure(pady=(medium if compact else extra_large, tiny))
            workspace.configure(padding=(large, small if compact else large))
            path.pack_configure(pady=tiny if compact else small)
            separator.grid_configure(pady=(tiny if compact else medium, 0))
            footer.grid_configure(pady=(tiny if compact else small, 0))
            if compact:
                brand_subtitle.pack_forget()
                title.grid_remove()
                hint.grid_remove()
            else:
                brand_subtitle.pack(before=self.nav_buttons[0], anchor="w", padx=large, pady=(0, extra_large))
                title.grid()
                hint.grid()
            self.update_workspace_path()

        self.bind("<Configure>", arrange_window, add="+")

    def page_changed(self, index):
        title, hint, _ = self.page_specs[index]
        self.page_title.set(title)
        self.page_hint.set(hint)
        for n, button in enumerate(self.nav_buttons):
            button.configure(style="Selected.Nav.TButton" if n == index else "Nav.TButton")
        self.schedule_preview()

    def _library_page(self, page):
        p = self.theme.px
        tiny, small, medium, large, _ = self.theme.space
        page.columnconfigure(0, weight=1)
        page.rowconfigure(4, weight=1)
        self.stats_frame = ttk.Frame(page)
        self.stats_frame.grid(row=0, column=0, sticky="ew", pady=(0, large))
        self.stats = []
        stat_titles = ("发票记录", "待核对 / 冲突", "可用票面合计", "已占用")
        for index, title in enumerate(stat_titles):
            if index:
                ttk.Separator(self.stats_frame, orient="vertical").grid(row=0, column=index * 2 - 1, sticky="ns", padx=large, pady=tiny)
            metric = ttk.Frame(self.stats_frame)
            metric.grid(row=0, column=index * 2, sticky="ew")
            self.stats_frame.columnconfigure(index * 2, weight=2 if index == 2 else 1)
            label = ttk.Label(metric, text=title, style="Hint.TLabel")
            label.pack(fill="x")
            wrap_label(label)
            var = tk.StringVar(self, value="—")
            ttk.Label(metric, textvariable=var, style="Stat.TLabel", width=1).pack(fill="x", pady=(tiny, 0))
            self.stats.append(var)
        toolbar = ttk.Frame(page)
        toolbar.grid(row=1, column=0, sticky="ew", pady=(0, medium))
        actions = ttk.Frame(toolbar)
        actions.grid(row=0, column=0, sticky="w", padx=(0, small))
        self.button(actions, "导入发票文件夹", self.import_directory, True, side="left")
        self.button(actions, "按额度选票", self.open_selection, side="left", padx=(small, 0))
        searchbar = ttk.Frame(toolbar)
        searchbar.grid(row=0, column=1, sticky="ew")
        toolbar.columnconfigure(1, weight=1)
        ttk.Label(searchbar, text="搜索", style="Muted.TLabel").pack(side="left", padx=(0, small))
        self.search = tk.StringVar(self)
        self.search_entry = ttk.Entry(searchbar, textvariable=self.search, width=1)
        self.search_entry.pack(side="left", fill="x", expand=True)
        self.search_entry.bind("<KeyRelease>", lambda event: self.fill_invoices())
        self.search_entry.bind("<Escape>", lambda event: self.clear_search())
        ttk.Button(searchbar, text="清空", style="Quiet.TButton", command=self.clear_search).pack(side="right", padx=(small, 0))
        def arrange_toolbar(event):
            wrap = event.width < actions.winfo_reqwidth() + p(150)
            searchbar.grid(row=int(wrap), column=0 if wrap else 1,
                           columnspan=2 if wrap else 1, sticky="ew", pady=(tiny if wrap else 0, 0))
        toolbar.bind("<Configure>", arrange_toolbar)
        filters = ttk.Frame(page)
        filters.grid(row=2, column=0, sticky="ew", pady=(0, small))
        self.filter = tk.StringVar(self, value="全部")
        self.filter_control = SegmentedControl(filters, ("全部", "待核对", "可用", "已占用", "同票冲突"),
                                               variable=self.filter, command=self.fill_invoices, theme=self.theme)
        self.filter_control.pack(side="left")

        def show_stats():
            text = "\n".join(f"{title}  {value.get()}" for title, value in zip(stat_titles, self.stats))
            messagebox.showinfo("发票统计", text, parent=self)

        stats_button = ttk.Button(filters, text="统计", style="Quiet.TButton", command=show_stats)
        self.list_summary = tk.StringVar(self, value="还没有发票，点击上方“导入发票文件夹”开始。")
        summary = ttk.Label(page, textvariable=self.list_summary, style="Hint.TLabel")
        summary.grid(row=3, column=0, sticky="ew", pady=(0, small))
        wrap_label(summary)
        splitter = self.splitter = ttk.Panedwindow(page, orient="horizontal")
        splitter.grid(row=4, column=0, sticky="nsew")
        left, right = ttk.Frame(splitter), ttk.Frame(splitter, padding=(large, 0, 0, 0))
        self.detail_pane = right
        self.detail_visible = False
        splitter.add(left, weight=6)
        columns = ("file", "number", "date", "amount", "state")
        self.tree = ttk.Treeview(left, columns=columns, displaycolumns=("amount", "number", "date", "file"),
                                 show=("tree", "headings"), selectmode="browse")
        self.tree.heading("#0", text="状态")
        self.tree.column("#0", width=p(92), minwidth=p(92), stretch=False)
        for name, title, width in zip(columns[:-1], ("文件名", "发票号码", "开票日期", "金额 / 元"), (190, 190, 110, 100)):
            self.tree.heading(name, text=title)
            self.tree.column(name, width=p(width), minwidth=p(80), stretch=name == "file", anchor="e" if name == "amount" else "w")
        table_scrollers(left, self.tree)
        hover_rows(self.tree)
        self.empty_hint = tk.Label(left, text="还没有发票\n\n点击上方“导入发票文件夹”即可添加。", bg=COLORS.canvas, fg=COLORS.secondary,
                                   font=(FONT, 11), justify="center")
        self.tree.bind("<<TreeviewSelect>>", self.select_invoice)
        self.tree.bind("<Double-1>", lambda e: self.show_detail() if self.current_id else None)
        self.tree.bind("<Up>", lambda event: self.move_invoice(-1))
        self.tree.bind("<Down>", lambda event: self.move_invoice(1))
        self.details_title = tk.StringVar(self, value="选择一张发票查看详情")
        detail_header = ttk.Frame(right)
        detail_header.pack(fill="x", pady=(0, medium))
        title = ttk.Label(detail_header, textvariable=self.details_title, font=(FONT, 11, "bold"), width=1)
        title.pack(side="left", fill="x", expand=True)
        wrap_label(title)
        self.details = SegmentedPages(right, ("字段核对", "原票预览", "提取文本"), theme=self.theme)
        self.details.pack(fill="both", expand=True)
        ttk.Button(self.details.bar, text="收起", style="Quiet.TButton", command=self.hide_detail).pack(
            side="right", before=self.details.control, padx=(tiny, 0))
        form_page, preview, text_page = self.details.frames
        self.warning_text = tk.StringVar(self)
        self.review_actions = ttk.Frame(form_page, padding=(0, tiny, 0, 0))
        self.review_actions.pack(side="bottom", fill="x")
        self.review_button = self.button(self.review_actions, "保存字段并核验", self.save_review, True, side="left")
        self.archive_button = self.button(self.review_actions, "移出候选", self.archive_invoice, side="left", padx=(small, 0))
        self.archive_button.configure(style="Danger.TButton")
        self.review_banner = tk.Button(form_page, textvariable=self.warning_text, anchor="w", justify="left",
                                       font=(FONT, 9), padx=small, pady=tiny, width=1,
                                       relief="flat", borderwidth=0, highlightthickness=1,
                                       highlightcolor=COLORS.secondary, command=self.show_review_issues)
        self.review_banner.bind("<Configure>", lambda event: self.review_banner.configure(wraplength=max(1, event.width - 2 * small)))
        self.review_banner.pack(fill="x", pady=(0, tiny))
        self.form_scroll = ScrollableFrame(form_page, theme=self.theme)
        self.form_scroll.pack(fill="both", expand=True)
        form = self.form_scroll.content
        self.fields, self.entries = {}, []
        for row, (key, title) in enumerate((("number", "发票号码"), ("code", "发票代码"), ("date", "开票日期"), ("amount", "价税合计 / 元"), ("buyer_name", "购买方抬头"), ("buyer_tax_id", "购买方税号"), ("seller_name", "销售方名称"), ("note", "备注"))):
            ttk.Label(form, text=title, style="Muted.TLabel").grid(row=row * 2 + 1, column=0, sticky="w", pady=tiny)
            var = tk.StringVar(self)
            var.trace_add("write", self.mark_dirty)
            entry = ttk.Entry(form, textvariable=var, width=1, justify="right" if key == "amount" else "left",
                              font=(MONO if key in {"amount", "number", "code", "date", "buyer_tax_id"} else FONT, 10))
            entry.grid(row=row * 2 + 2, column=0, sticky="ew", pady=(0, small), padx=(0, small))
            self.fields[key] = var
            self.entries.append(entry)
        form.columnconfigure(0, weight=1)
        ttk.Label(form, text="报销状态", style="Muted.TLabel").grid(row=17, column=0, sticky="w", pady=tiny)
        self.invoice_status = tk.StringVar(self)
        self.status_choice = ttk.Combobox(form, textvariable=self.invoice_status, values=list(INVOICE_STATUS.values()), state="disabled", width=1)
        self.status_choice.grid(row=18, column=0, sticky="ew", padx=(0, small), pady=(0, small))
        self.status_choice.bind("<<ComboboxSelected>>", self.change_invoice_status)
        help_text = ttk.Label(form, text="已退回或可用会释放当前批次占用。核验通过且公司匹配后可再次选用。\n同票文件在导入时自动归并。", style="Hint.TLabel", width=1)
        help_text.grid(row=19, column=0, sticky="ew", padx=(0, small), pady=tiny)
        wrap_label(help_text)
        self.form_scroll.enable_scrolling()
        filebar = ttk.Frame(preview)
        filebar.pack(fill="x", pady=(0, small))
        self.file_choice = ttk.Combobox(filebar, state="readonly", width=1)
        self.button(filebar, "打开原件", self.open_original, side="right", padx=(small, 0))
        self.file_choice.pack(side="left", fill="x", expand=True)
        self.file_choice.bind("<<ComboboxSelected>>", self.preview_file_changed)
        zoom = ttk.Frame(preview)
        zoom.pack(fill="x", pady=(0, small))
        ttk.Button(zoom, text="适应宽度", style="Quiet.TButton", command=lambda: self.zoom_preview(None)).pack(side="left")
        ttk.Button(zoom, text="−", width=3, command=lambda: self.zoom_preview(1 / 1.25)).pack(side="left", padx=tiny)
        ttk.Button(zoom, text="+", width=3, command=lambda: self.zoom_preview(1.25)).pack(side="left")
        self.zoom_text = tk.StringVar(self, value="100%")
        ttk.Label(zoom, textvariable=self.zoom_text, style="Hint.TLabel").pack(side="right")
        preview_hint = ttk.Label(preview, text="PDF 显示首页 · 多页票据可打开原件", style="Hint.TLabel")
        preview_hint.pack(anchor="w", pady=(0, small))
        preview_area = ttk.Frame(preview)
        preview_area.pack(fill="both", expand=True)
        self.preview_canvas = tk.Canvas(preview_area, bg=COLORS.occupied_bg, highlightthickness=0, width=1, height=1)
        table_scrollers(preview_area, self.preview_canvas)
        preview_area.bind("<Configure>", lambda event: self.schedule_preview(resizing=True))
        self.raw = tk.Text(text_page, wrap="word", bg=COLORS.surface, fg=COLORS.text, relief="flat", borderwidth=0,
                           font=(FONT, 10), state="disabled", width=1, height=1, padx=medium, pady=medium)
        raw_scroll = ttk.Scrollbar(text_page, orient="vertical", command=self.raw.yview)
        raw_scroll.pack(side="right", fill="y")
        self.raw.configure(yscrollcommand=raw_scroll.set)
        self.raw.pack(fill="both", expand=True)
        self.splitter.bind("<Configure>", self.constrain_detail)
        self.splitter.bind("<ButtonRelease-1>", self.constrain_detail)

        compact = False
        narrow = None

        def arrange_details():
            if compact and self.details.index("current") == 1 and not self.draft_is_filtered():
                detail_header.pack_forget()
            else:
                detail_header.pack(before=self.details, fill="x", pady=(0, tiny if compact else medium))

        def change_detail_page(event):
            arrange_details()
            self.schedule_preview()

        def arrange_library(event):
            nonlocal compact, narrow
            short = event.height < p(515)
            use_short_labels = short or event.width < p(650)
            if (short, use_short_labels) == (compact, narrow):
                return
            compact, narrow = short, use_short_labels
            gap = tiny if compact else small
            toolbar.grid_configure(pady=(0, tiny if compact else medium))
            filters.grid_configure(pady=(0, gap))
            arrange_details()
            self.details.bar.pack_configure(pady=(0, tiny if compact else medium))
            right.configure(padding=(small if narrow else large, 0, 0, 0))
            self.review_banner.configure(pady=p(2) if compact else tiny)
            self.review_banner.pack_configure(pady=0 if compact else (0, tiny))
            labels = ("核对", "预览", "文本") if narrow else self.details.labels
            for button, label in zip(self.details.control.buttons, labels):
                button.configure(text=label, style="Compact.Pill.TRadiobutton" if narrow else "Pill.TRadiobutton")
            self.review_button.configure(style="Compact.Primary.TButton" if narrow else "Primary.TButton")
            self.archive_button.configure(style="Compact.Danger.TButton" if narrow else "Danger.TButton")
            filebar.pack_configure(pady=(0, gap))
            zoom.pack_configure(pady=(0, gap))
            if compact:
                self.stats_frame.grid_remove()
                summary.grid_remove()
                stats_button.pack(side="right")
                preview_hint.pack_forget()
            else:
                self.stats_frame.grid()
                summary.grid()
                stats_button.pack_forget()
                preview_hint.pack(before=preview_area, anchor="w", pady=(0, small))

        page.bind("<Configure>", arrange_library)
        self.details.bind("<<NotebookTabChanged>>", change_detail_page)
        self.details_title.trace_add("write", lambda *_: arrange_details())

    def _batch_page(self, page):
        p = self.theme.px
        space = self.theme.space
        page.columnconfigure(0, weight=1)
        page.rowconfigure(2, weight=3, minsize=p(78))
        page.rowconfigure(3, weight=2, minsize=p(40))
        actions = ttk.Frame(page)
        actions.grid(row=0, column=0, sticky="ew", pady=(0, space.tiny))
        self.button(actions, "导出原票与明细", self.export_batch, True, side="left")
        hint = ttk.Label(page, text="整票占用，超额不结转。发票报销状态在详情中管理，这里保留批次历史。", style="Muted.TLabel")
        hint.grid(row=1, column=0, sticky="ew", pady=(0, space.small))
        wrap_label(hint)
        listing = ttk.Frame(page)
        listing.grid(row=2, column=0, sticky="nsew")
        columns = ("time", "count", "target", "total", "declared")
        self.batch_tree = ttk.Treeview(listing, columns=columns, show="headings", selectmode="browse", height=2)
        for key, title, width in zip(columns, ("创建时间", "张数", "目标 / 元", "票面 / 元", "申报 / 元"), (200, 65, 130, 130, 130)):
            self.batch_tree.heading(key, text=title)
            self.batch_tree.column(key, width=p(width), minwidth=p(60), anchor="w" if key == "time" else "e")
        table_scrollers(listing, self.batch_tree)
        hover_rows(self.batch_tree)
        self.batch_tree.bind("<<TreeviewSelect>>", self.show_batch)
        detail = ttk.Frame(page, style="Surface.TFrame")
        detail.grid(row=3, column=0, sticky="nsew", pady=(space.tiny, 0))
        self.batch_info = tk.Text(detail, height=2, width=1, wrap="word", state="disabled", relief="flat", borderwidth=0,
                                  bg=COLORS.surface, fg=COLORS.secondary, font=(FONT, 10), padx=space.large, pady=space.small)
        scroll = ttk.Scrollbar(detail, orient="vertical", command=self.batch_info.yview)
        scroll.pack(side="right", fill="y")
        self.batch_info.configure(yscrollcommand=scroll.set)
        self.batch_info.pack(fill="both", expand=True)

    def _settings_page(self, page):
        tiny, small, medium, large, _ = self.theme.space
        scroll = ScrollableFrame(page, theme=self.theme)
        scroll.pack(fill="both", expand=True)
        panel = scroll.content
        library = ttk.Frame(panel, style="Surface.TFrame", padding=large)
        library.pack(fill="x", pady=(0, large), padx=(0, medium))
        ttk.Label(library, text="发票资料库", style="Surface.TLabel", font=(FONT, 12, "bold")).pack(anchor="w")
        self.directory_text = tk.StringVar(self)
        directory = ttk.Label(library, textvariable=self.directory_text, style="SurfaceMuted.TLabel")
        directory.pack(fill="x", pady=small)
        wrap_label(directory)
        library_actions = ttk.Frame(library, style="Surface.TFrame")
        library_actions.pack(anchor="w")
        self.library_button = self.button(library_actions, "切换资料库…", self.choose_library, side="left")
        self.reset_button = self.button(library_actions, "恢复初始化…", self.reset_workspace, side="left", padx=(small, 0))
        self.reset_button.configure(style="Danger.TButton")
        help_text = ttk.Label(library, text="这里保存发票记录、原件副本和报销批次。已有发票请从“发票资料”导入。\n跨电脑使用时，关闭当前窗口并等待网盘同步完成，再在另一台电脑打开。", style="SurfaceMuted.TLabel")
        help_text.pack(fill="x", pady=(medium, 0))
        wrap_label(help_text)
        company = ttk.Frame(panel, style="Surface.TFrame", padding=large)
        company.pack(fill="x", pady=(0, large), padx=(0, medium))
        ttk.Label(company, text="报销公司", style="Surface.TLabel", font=(FONT, 12, "bold")).pack(anchor="w")
        self.company_name, self.company_tax = tk.StringVar(self), tk.StringVar(self)
        for title, var in (("购买方抬头", self.company_name), ("购买方税号", self.company_tax)):
            ttk.Label(company, text=title, style="SurfaceMuted.TLabel").pack(anchor="w", pady=(medium, tiny))
            ttk.Entry(company, textvariable=var, width=1).pack(fill="x")
        self.button(company, "保存公司设置", self.save_company, True, anchor="w", pady=(medium, small))
        help_text = ttk.Label(company, text="填写税号时精确匹配税号；同时填写抬头时两项都须匹配。", style="SurfaceMuted.TLabel")
        help_text.pack(fill="x")
        wrap_label(help_text)
        ttk.Label(panel, text=f"版本 {__version__} · 本地文件整理", style="Section.TLabel").pack(anchor="w")
        help_text = ttk.Label(panel, text="支持 PDF 文字层与部分 XML 字段提取、自动核验、重复文件整理、精确选票和批次导出。\n当前不连接 Outlook，不上传发票；图片、OFD 和扫描 PDF 无法自动核验的字段会明确提示。", style="Muted.TLabel")
        help_text.pack(fill="x", pady=small, padx=(0, medium))
        wrap_label(help_text)

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
            editable = item.get("reimbursement_status") not in OCCUPIED_STATUSES and not item.get("archived")
        for entry in self.entries:
            entry.configure(state="normal" if editable else "readonly")
        self.review_button.configure(state="normal" if editable else "disabled")
        self.status_choice.configure(state="readonly" if self.session_ok and not self.busy and self.selected_item() else "disabled")
        item = self.selected_item()
        archivable = (self.session_ok and not self.busy and item
                      and (item.get("archived") or item.get("reimbursement_status") not in OCCUPIED_STATUSES))
        self.archive_button.configure(state="normal" if archivable else "disabled")

    def connection_error(self, text):
        self.session_ok = False
        self.status_text.set(text)
        self.reconnect_button.grid(row=0, column=2, sticky="e", padx=(self.theme.space.small, 0))
        self.update_connection_status()
        self.update_actions()

    def update_connection_status(self):
        self.workspace_status.set("◌ 正在处理" if self.busy else "● 工作区已连接" if self.session_ok else "○ 工作区未连接")
        self.workspace_label.configure(foreground=COLORS.secondary if self.session_ok else COLORS.warning)
        self.status_label.configure(foreground=COLORS.text if self.busy else COLORS.muted if self.session_ok else COLORS.error)

    def update_workspace_path(self):
        directory = Path(self.snapshot.get("data_directory", self.connection_directory))
        self.workspace_path.set(directory.name if self.compact_layout else f"{directory.name}\n{directory.parent}")

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

    def reload(self, *, discard_edits=False):
        if not self.store or self.busy:
            return False
        try:
            self.snapshot = self.store.view()
        except (UserError, OSError, ValueError) as exc:
            self.connection_error(str(exc))
            return False
        self.session_ok = True
        self.reconnect_button.grid_remove()
        invoices = self.snapshot["invoices"]
        available = [i for i in invoices if selectable(i)]
        values = [str(len(invoices)), str(sum(not i.get("archived") and i["reimbursement_status"] not in OCCUPIED_STATUSES and not selectable(i) for i in invoices)),
                  "¥ " + yuan(sum(i["amount_cents"] for i in available)), str(sum(i["reimbursement_status"] in {"reserved", "submitted", "reimbursed"} for i in invoices))]
        for var, value in zip(self.stats, values):
            var.set(value)
        self.directory_text.set(self.snapshot["data_directory"])
        self.update_workspace_path()
        self.company_name.set(self.snapshot["company"]["name"])
        self.company_tax.set(self.snapshot["company"]["tax_id"])
        if discard_edits:
            self.dirty = False
        self.fill_invoices()
        self.fill_batches()
        if self.current_id:
            item = self.selected_item()
            if item:
                self.load_invoice(item, refresh=True)
            else:
                self.clear_detail()
        if not self.snapshot["company"].get("name") and not self.snapshot["company"].get("tax_id"):
            self.status_text.set("可以直接导入发票。按额度选票前，请在“设置”填写报销公司。")
        else:
            self.status_text.set("已就绪。自动核验通过且公司匹配的可用、已退回发票可参与选票。")
        self.update_actions()
        self.update_connection_status()
        return True

    def clear_search(self):
        self.search.set("")
        self.fill_invoices()
        self.search_entry.focus_set()

    def fill_invoices(self):
        query = self.search.get().strip().lower()
        selected = self.current_id
        self.tree.delete(*self.tree.get_children())
        for item in sorted(self.snapshot.get("invoices", []), key=lambda i: (i.get("date", ""), i["created"]), reverse=True):
            status = state_label(item)
            occupied = item.get("reimbursement_status") in OCCUPIED_STATUSES
            pending = not item.get("archived") and not occupied and not selectable(item)
            matches = {"全部": True, "待核对": pending, "可用": selectable(item),
                       "已占用": occupied, "同票冲突": bool(item.get("duplicate"))}
            if not matches[self.filter.get()]:
                continue
            if query and query not in " ".join(str(item.get(k, "")) for k in ("number", "buyer_name", "buyer_tax_id", "seller_name")).lower():
                continue
            tone = "occupied" if occupied or item.get("archived") else "error" if item.get("duplicate") else "warning" if pending else "success"
            badge = self.theme.status_badge(status, tone)
            self.tree.insert("", "end", iid=item["id"], image=badge, text="" if self.theme.status_font else status,
                             values=(item["files"][0]["name"], item["number"] or "待填写票号", item["date"] or "—",
                                     yuan(item["amount_cents"]) if item["amount_cents"] else "待填写", status))
        if selected:
            if self.tree.exists(selected):
                self.tree.selection_set(selected)
                self.tree.focus(selected)
            elif not self.dirty:
                self.clear_detail()
                self.update_actions()
            if self.current_id:
                self.update_review_banner()
        count, total = len(self.tree.get_children()), len(self.snapshot.get("invoices", []))
        self.list_summary.set(f"显示 {count} / {total} 张 · 点击发票查看详情与状态" if total else "点击“导入发票文件夹”添加，发现重复副本时可选择删除。")
        if count:
            self.empty_hint.place_forget()
        else:
            self.empty_hint.configure(text="没有匹配的发票\n\n请清空搜索或切换为“全部”。" if total else "还没有发票\n\n点击上方“导入发票文件夹”即可添加。")
            self.empty_hint.place(relx=0.5, rely=0.45, anchor="center")

    def mark_dirty(self, *_):
        if not self.loading and self.current_id and not self.dirty:
            self.dirty = True
            self.update_review_banner()

    def draft_is_filtered(self):
        return self.dirty and self.current_id is not None and not self.tree.exists(self.current_id)

    def update_review_banner(self):
        item = self.selected_item()
        if not item:
            self.review_messages = []
            self.warning_text.set("")
            return
        detail_title = f"{item['number'] or '字段不完整'} · {state_label(item)}"
        if self.draft_is_filtered():
            detail_title += " · 已被筛选隐藏"
        if detail_title != self.details_title.get():
            self.details_title.set(detail_title)
        verification = item.get("verification", {})
        messages = list(verification.get("issues", [])) if not verification.get("passed") else []
        if item.get("duplicate"):
            messages.insert(0, "存在同票冲突，请修正字段或移出错误记录。")
        if not item.get("company_match"):
            messages.append("购买方与报销公司不匹配，请核对公司设置。")
        self.review_messages = messages
        title = "有未保存的核对修改" if self.dirty else "存在同票冲突" if item.get("duplicate") else "字段需要核对" if messages else "自动核验通过"
        self.warning_text.set(title + (f" · 查看 {len(messages)} 项核验提示" if messages else ""))
        tone = "error" if item.get("duplicate") else "warning" if self.dirty or not verification.get("passed") or not item.get("company_match") else "success"
        background, foreground = getattr(COLORS, tone + "_bg"), getattr(COLORS, tone)
        self.review_banner.configure(background=background, foreground=foreground, activebackground=background,
                                      activeforeground=foreground, highlightbackground=background,
                                      cursor="hand2" if messages else "", takefocus=bool(messages))

    def show_review_issues(self):
        if self.review_messages:
            messagebox.showwarning("核验提示", "\n\n".join(self.review_messages), parent=self)

    def allow_discard(self):
        return not self.dirty or messagebox.askyesno("尚未保存", "当前字段修改尚未保存。放弃修改并继续？", parent=self)

    def select_invoice(self, event=None):
        if self.selecting_invoice:
            return
        selection = self.tree.selection()
        if not selection or selection[0] == self.current_id:
            return
        self.selecting_invoice = True
        try:
            if not self.allow_discard():
                if self.current_id and self.tree.exists(self.current_id):
                    self.tree.selection_set(self.current_id)
                    self.tree.focus(self.current_id)
                    self.tree.see(self.current_id)
                else:
                    self.tree.selection_remove(*self.tree.selection())
                    self.tree.focus("")
                return
            item = next((i for i in self.snapshot["invoices"] if i["id"] == selection[0]), None)
            if item:
                self.load_invoice(item)
        finally:
            self.selecting_invoice = False

    def move_invoice(self, direction):
        ids = self.tree.get_children()
        if not ids:
            return "break"
        selection = self.tree.selection()
        index = ids.index(selection[0]) if selection else (-1 if direction > 0 else len(ids))
        next_id = ids[max(0, min(len(ids) - 1, index + direction))]
        self.tree.selection_set(next_id)
        self.select_invoice()
        selected = self.tree.selection()
        if selected:
            self.tree.focus(selected[0])
            self.tree.see(selected[0])
        return "break"

    def clear_detail(self):
        self.hide_detail()
        self.current_id = None
        self.loading = True
        for var in self.fields.values():
            var.set("")
        self.details_title.set("选择一张发票查看详情")
        self.invoice_status.set("")
        self.status_choice.configure(state="disabled")
        self.file_choice["values"] = []
        self.clear_preview()
        self.set_text(self.raw, "")
        self.loading = self.dirty = False
        self.update_review_banner()

    def load_invoice(self, item, *, refresh=False):
        if not refresh:
            self.show_detail()
        self.loading = True
        keep_edits = refresh and self.dirty
        file_index = self.file_choice.current() if self.current_id == item["id"] else -1
        self.current_id = item["id"]
        if not keep_edits:
            for key, var in self.fields.items():
                var.set(yuan(item["amount_cents"]) if key == "amount" and item["amount_cents"] else item.get(key, ""))
        self.status_choice["values"] = [label for status, label in INVOICE_STATUS.items()
                                        if status != "reserved" or item.get("active_batch_id")]
        self.invoice_status.set(INVOICE_STATUS[item["reimbursement_status"]])
        self.status_choice.configure(state="readonly" if self.session_ok and not self.busy else "disabled")
        self.file_choice["values"] = [f["name"] for f in item["files"]]
        if 0 <= file_index < len(item["files"]):
            self.file_choice.current(file_index)
        else:
            self.file_choice.current(0)
            self.clear_preview()
        self.set_text(self.raw, item.get("raw_text", "") or "此文件暂无可提取文字，自动核验无法完成。")
        archivable = not self.busy and (item.get("archived") or item.get("reimbursement_status") not in OCCUPIED_STATUSES)
        self.archive_button.configure(text="恢复候选" if item.get("archived") else "移出候选",
                                      state="normal" if self.session_ok and archivable else "disabled")
        editable = item.get("reimbursement_status") not in OCCUPIED_STATUSES and not item.get("archived") and not self.busy
        for entry in self.entries:
            entry.configure(state="normal" if editable else "readonly")
        self.review_button.configure(state="normal" if editable and not self.busy else "disabled")
        self.loading = False
        self.dirty = keep_edits
        self.update_review_banner()
        if not refresh:
            self.form_scroll.canvas.yview_moveto(0)
        if self.preview_is_active():
            self.render_preview()

    def show_detail(self):
        if not self.detail_visible:
            self.splitter.add(self.detail_pane, weight=4)
            self.detail_visible = True
            # 新面板完成几何计算后再设置分隔条位置。
            self.splitter.update_idletasks()
            width = self.splitter.winfo_width()
            self.splitter.sashpos(0, max(1, width - self.theme.px(400)))
            self.constrain_detail()
            self.schedule_preview()

    def constrain_detail(self, event=None):
        if self.detail_visible:
            width = self.splitter.winfo_width()
            right = min(self.theme.px(360), int(width * 0.55))
            # 给状态、金额和滚动条保留至少 200 个逻辑像素。
            left = min(self.theme.px(280), max(self.theme.px(200), int(width * 0.35)), width - right)
            position = self.splitter.sashpos(0)
            self.splitter.sashpos(0, max(left, min(width - right, position)))

    def hide_detail(self):
        if self.detail_visible:
            self.detail_visible = False
            self.cancel_preview_render()
            self.preview_resize_position = None
            self.splitter.forget(self.detail_pane)

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

    def clear_preview(self):
        self.cancel_preview_render()
        self.preview_generation += 1
        self.preview_image = self.preview_key = None
        self.preview_zoom = 1.0
        self.preview_resize_position = None
        self.image_reference = None
        self.preview_canvas.delete("all")
        self.preview_canvas.configure(scrollregion=(0, 0, 0, 0))

    def preview_scroll_position(self):
        return self.preview_canvas.xview()[0], self.preview_canvas.yview()[0]

    def cancel_preview_render(self):
        if self.preview_after is not None:
            self.after_cancel(self.preview_after)
            self.preview_after = None

    def preview_is_active(self):
        return (self.detail_visible and self.current_id is not None and self.pages.current_index == 0
                and self.details.index("current") == 1)

    def schedule_preview(self, *, resizing=False):
        self.cancel_preview_render()
        if self.preview_is_active():
            # 在画布随外框改变尺寸之前记录位置，避免 Tk 按旧滚动范围提前限制位置。
            if resizing and self.image_reference is not None and self.preview_resize_position is None:
                self.preview_resize_position = self.preview_scroll_position()
            self.preview_after = self.after(100, self.render_preview)

    def preview_file_changed(self, event=None):
        self.preview_zoom = 1.0
        self.render_preview()

    def zoom_preview(self, factor):
        self.preview_zoom = 1.0 if factor is None else max(0.25, min(3.0, self.preview_zoom * factor))
        self.render_preview()

    def render_preview(self):
        self.cancel_preview_render()
        if not self.preview_is_active():
            return
        self.preview_generation += 1
        generation = self.preview_generation
        file = self.selected_file()
        x_position, y_position = self.preview_resize_position or self.preview_scroll_position()
        self.preview_resize_position = None
        self.image_reference = None
        self.preview_canvas.delete("all")
        self.zoom_text.set(f"{self.preview_zoom:.0%}")
        if not file or not self.store:
            self.preview_canvas.configure(scrollregion=(0, 0, 0, 0))
            return
        try:
            path = self.store.original(file)
            suffix = path.suffix.lower()
            if self.preview_key != path:
                x_position = y_position = 0.0
                self.preview_image = None
                if suffix == ".pdf":
                    doc = pdfium.PdfDocument(str(path))
                    try:
                        page = doc[0]
                        try:
                            bitmap = page.render(scale=min(3, 1800 / max(page.get_size())))
                            try:
                                self.preview_image = bitmap.to_pil().copy()
                            finally:
                                bitmap.close()
                        finally:
                            page.close()
                    finally:
                        doc.close()
                elif suffix in {".png", ".jpg", ".jpeg"}:
                    with Image.open(path) as source:
                        self.preview_image = source.copy()
                self.preview_key = path
            p = self.theme.px
            if self.preview_image is None:
                self.preview_canvas.configure(scrollregion=(0, 0, 0, 0))
                self.preview_canvas.create_text(p(16), p(20), anchor="nw", text="此格式请使用“打开原件”查看。\n文字内容位于“提取文本”。", fill=COLORS.secondary,
                                                font=(FONT, 10), width=max(p(100), self.preview_canvas.winfo_width() - p(32)))
                return
            width = max(p(100), round((self.preview_canvas.winfo_width() - p(32)) * self.preview_zoom))
            image = self.preview_image.resize((width, max(1, round(width * self.preview_image.height / self.preview_image.width))), Image.Resampling.LANCZOS)
            if generation == self.preview_generation:
                self.image_reference = ImageTk.PhotoImage(image, master=self)
                x = max(p(16), (self.preview_canvas.winfo_width() - width) // 2)
                self.preview_canvas.create_rectangle(x + p(3), p(19), x + width + p(3), image.height + p(19), fill=COLORS.line, outline="")
                self.preview_canvas.create_image(x, p(16), anchor="nw", image=self.image_reference)
                self.preview_canvas.configure(scrollregion=(0, 0, max(self.preview_canvas.winfo_width(), width + p(32)), image.height + p(32)))
                self.preview_canvas.xview_moveto(x_position)
                self.preview_canvas.yview_moveto(y_position)
        except Exception as exc:
            self.preview_key = self.preview_image = None
            self.preview_canvas.configure(scrollregion=(0, 0, 0, 0))
            self.preview_canvas.create_text(self.theme.px(16), self.theme.px(20), anchor="nw", text=f"无法生成预览，请打开原件核对。\n{exc}", fill=COLORS.error, font=(FONT, 10),
                                            width=max(100, self.preview_canvas.winfo_width() - self.theme.px(32)))

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
        self.preserve_edits = preserve_edits
        self.status_text.set(description)
        self.update_connection_status()
        for b in self.actions:
            b.configure(state="disabled")
        for entry in self.entries:
            entry.configure(state="readonly")
        self.status_choice.configure(state="disabled")
        self.progress.grid(row=0, column=1, sticky="e", padx=(self.theme.space.small, 0))
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
                self.progress.grid_remove()
                self.reload(discard_edits=kind == "done" and not self.preserve_edits)
                self.update_actions()
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
            text += "。已完成自动核验。"
        self.status_text.set(text)
        children = self.tree.get_children()
        if children:
            self.tree.selection_set(children[0])
            self.select_invoice()
        duplicates = report.get("duplicate_files", [])
        if duplicates:
            dialog = DuplicateFilesDialog(self, report["source"], duplicates)
            self.wait_window(dialog)
            if dialog.accepted:
                store, revision = self.store, self.snapshot["revision"]
                self.task("正在整理重复文件…", lambda: remove_duplicate_files(store, revision, report),
                          lambda count: self.status_text.set(text + f" · 已删除 {count} 个重复副本"))

    def save_company(self):
        if self.store:
            name, tax = self.company_name.get(), self.company_tax.get()
            revision = self.snapshot["revision"]
            self.task("保存公司设置…", lambda: self.store.set_company(revision, name, tax))

    def reset_workspace(self):
        if not self.session_ok or not self.store or self.busy:
            return
        directory = self.store.root
        prompt = (f"资料库：{directory}\n\n"
                  "将清空全部发票记录、报销批次、原件副本、历史记录、资料库内的导出文件、公司信息及其他配置。\n"
                  "资料库位置和导入源文件保留。尚未保存的修改将清除。\n\n"
                  "此操作无法撤销。确认恢复初始化？")
        if not messagebox.askyesno("恢复初始化", prompt, parent=self, icon="warning", default="no"):
            return
        store, revision, settings_file = self.store, self.snapshot["revision"], self.settings_file
        settings = {"data_directory": str(directory)}
        def work():
            store.reset(revision)
            settings_file.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
            return settings
        def done(settings):
            self.settings = settings
            self.current_id = None
            self.clear_detail()
            self.search.set("")
            self.filter.set("全部")
            self.fill_invoices()
            self.fill_batches()
            self.set_text(self.batch_info, "")
            self.pages.select(2)
            self.status_text.set("已恢复初始化。资料库已清空，保存位置保持不变。")
        self.task("正在恢复初始化…", work, done, preserve_edits=False)

    def save_review(self):
        item = self.selected_item()
        if item and self.store:
            fields = {k: v.get() for k, v in self.fields.items()}
            invoice_id, revision = item["id"], self.snapshot["revision"]
            self.task("保存字段并自动核验…", lambda: self.store.review(revision, invoice_id, fields), preserve_edits=False)

    def archive_invoice(self):
        item = self.selected_item()
        if not item or not self.store or not self.allow_discard():
            return
        archived = not item.get("archived", False)
        if not messagebox.askyesno("候选状态", "移出候选后保留原件和记录，可随时恢复。" if archived else "恢复候选并重新自动核验，是否继续？", parent=self):
            return
        revision, item_id = self.snapshot["revision"], item["id"]
        self.task("更新候选状态…", lambda: self.store.archive(revision, item_id, archived), preserve_edits=False)

    def change_invoice_status(self, event=None):
        item = self.selected_item()
        if not item or not self.store or self.busy or self.loading or not self.session_ok:
            return
        status = next((key for key, label in INVOICE_STATUS.items() if label == self.invoice_status.get()), None)
        if status is None or status == item["reimbursement_status"]:
            return
        if status == "reserved" and not item.get("active_batch_id"):
            self.invoice_status.set(INVOICE_STATUS[item["reimbursement_status"]])
            return
        invoice_id, revision = item["id"], self.snapshot["revision"]
        self.task("更新发票状态…", lambda: self.store.set_invoice_status(revision, invoice_id, status))

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
            self.batch_tree.insert("", "end", iid=batch["id"], values=(created, len(batch["items"]), yuan(batch["target_cents"]), yuan(batch["total_cents"]), yuan(batch["declared_cents"])))
        if previous and self.batch_tree.exists(previous[0]):
            self.batch_tree.selection_set(previous[0])
            self.show_batch()

    def selected_batch(self):
        selection = self.batch_tree.selection()
        return next((b for b in self.snapshot.get("batches", []) if selection and b["id"] == selection[0]), None)

    def show_batch(self, event=None):
        batch = self.selected_batch()
        if batch:
            current = {i["id"]: i for i in self.snapshot["invoices"]}
            lines = []
            for item in batch["items"]:
                live = current.get(item["id"], {})
                status = INVOICE_STATUS.get(live.get("reimbursement_status"), "历史记录")
                membership = "本批次占用" if live.get("active_batch_id") == batch["id"] else "已释放本批次占用"
                lines.append(f"{item['number']}  {item['date']}  ¥{yuan(item['amount_cents'])}  当前：{status}（{membership}）")
            text = f"批次：{batch['id']}\n公司：{batch['company']['name']}   税号：{batch['company']['tax_id']}\n" + "\n".join(lines)
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

    def close_app(self):
        if self.busy:
            messagebox.showinfo("正在处理", "请等待当前处理完成后关闭窗口，确保发票记录保存完整。", parent=self)
            return
        if not self.allow_discard():
            return
        if self.store:
            self.store.close()
        self.cancel_preview_render()
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
