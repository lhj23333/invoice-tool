from __future__ import annotations

import argparse
from datetime import datetime
import json
import logging
from pathlib import Path
import queue
from tempfile import NamedTemporaryFile
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from . import __version__
from .domain import UserError, yuan
from .filesystem import is_directory_link
from .import_dialogs import ImportDialog, ImportHistory, LOCAL_MODE, MAIL_MODE, MailCredentialDialog, import_summary
from .labels import INVOICE_IMPORT_GUIDE, INVOICE_STATUS, SETTINGS_HINT, selectable, state_label
from .mail import IMAPMailbox, domain_list, mailbox_profile
from .outlook_auth import OUTLOOK_IMAP_HOST, OutlookOAuth, configured_client_id, validate_client_id
from .preview import PreviewPane
from .selection import SelectionDialog
from .store import ARCHIVE_DIRECTORIES, INVOICE_INDEX, OCCUPIED_STATUSES, WORKSPACE_DATABASE, Store
from .theme import COLORS, FONT, MONO, Theme, enable_dpi_awareness, fit_window, wrap_label
from .widgets import (CollapsibleSection, ScrollableFrame, SegmentedControl, SegmentedPages,
                      StatusPill, hover_rows, table_scrollers)
from .workflow import contains_invoices, import_directory, import_mailbox, remove_duplicate_files

APP_ROOT = Path(__file__).resolve().parent.parent
SETTINGS_WIDTH = 780
GENERAL_SETTINGS, MAIL_SETTINGS, SYSTEM_SETTINGS = "常规与存储", "邮箱收票", "系统与重置"
MAIL_SETTINGS_HINT = "保存后，在「导入发票」中连接邮箱。"
MAIL_PROVIDER_NAMES = {
    "imap.qq.com": "QQ 邮箱",
    "imap.163.com": "网易 163 邮箱",
    "imap.126.com": "网易 126 邮箱",
    "imap.yeah.net": "网易 Yeah 邮箱",
    OUTLOOK_IMAP_HOST: "Outlook",
}


def _read_settings(path):
    try:
        settings = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return settings if isinstance(settings, dict) else {}


def _startup_workspace(data_dir, settings_file):
    default = (APP_ROOT / "data").resolve()
    directory = Path(data_dir).expanduser().resolve() if data_dir is not None else default
    config = settings_file or directory / "local-settings.json"
    settings = _read_settings(config)
    if settings_file is None and data_dir is None and "data_directory" not in settings:
        settings = _read_settings(APP_ROOT / "local-settings.json") | settings
    if data_dir is None and (saved := settings.get("data_directory")):
        directory = Path(saved).expanduser().resolve()
    if directory.name.lower() == WORKSPACE_DATABASE:
        return directory, config, settings, None
    database = directory / WORKSPACE_DATABASE
    if database.exists() or database.is_symlink():
        return database, config, settings, None
    if not (directory / INVOICE_INDEX).exists() and contains_invoices(directory):
        # 旧设置可能把发票原件目录当作工作区。该目录仅用于导入。
        return default, settings_file or default / "local-settings.json", settings, directory
    return directory, config, settings, None


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
        for text in (f"发现 {len(duplicates)} 份内容相同的副本。删除所列副本后，每组保留一份。", f"源目录：{source}"):
            label = ttk.Label(panel, text=text, style="Muted.TLabel")
            label.pack(fill="x", pady=(0, space.small))
            wrap_label(label)
        actions = ttk.Frame(panel)
        actions.pack(side="top", fill="x", pady=(0, space.medium))
        self.no_button = ttk.Button(actions, text="保留全部", command=self.destroy)
        self.no_button.pack(side="right")
        self.yes_button = ttk.Button(actions, text="删除所列副本", command=self.accept, style="Danger.TButton")
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
    def __init__(self, data_dir: Path | None = None, settings_file: Path | None = None, *, recover_lock=False,
                 preview_async=None):
        enable_dpi_awareness()
        super().__init__()
        self.store = None
        directory, self.settings_file, self.settings, import_source = _startup_workspace(data_dir, settings_file)
        if recover_lock:
            self.withdraw()
            accepted = messagebox.askyesno("恢复遗留会话", f"系统数据库：{directory}\n\n请确认其他设备已关闭此工作区、网盘已完成同步。\n\n确认恢复？", parent=self)
            if not accepted:
                self.destroy()
                return
            self.deiconify()
        self.theme = Theme(self)
        self.title("发票工作台 · Invoice Workbench")
        fit_window(self, self.theme, 1360, 820, 1080, 640)
        self.mailbox = None
        self.mail_folders = ()
        self.connecting_mail = False
        self.session_ok = False
        self.snapshot = {}
        self.invoices_by_id = {}
        self.busy = False
        self.task_cancelled = None
        self.events = queue.Queue()
        self.next_task_id = 0
        self.active_task_id = None
        self.actions = []
        self.current_id = None
        self.dirty = False
        self.loading = False
        self.selecting_invoice = False
        self.preview = None
        self.preview_async = preview_async
        self.import_dialog = None
        self.last_import_report = None
        self.protocol("WM_DELETE_WINDOW", self.close_app)
        self._build()
        self.poll_id = self.after(80, self._poll)
        self.connection_directory = directory
        try:
            self.open_library(directory, recover_lock=recover_lock)
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
            ("发票资料", "导入、核对和选用发票。", self._library_page),
            ("报销批次", "查看批次，导出发票原件和明细。", self._batch_page),
            ("设置", SETTINGS_HINT, self._settings_page),
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
        heading = ttk.Frame(main)
        heading.grid(row=0, column=0, sticky="ew")
        self.page_title_label = ttk.Label(heading, textvariable=self.page_title, style="Title.TLabel")
        self.page_title_label.pack(fill="x")
        self.page_hint_label = ttk.Label(heading, textvariable=self.page_hint, style="Muted.TLabel", width=1)
        self.page_hint_label.pack(fill="x", pady=(tiny, large))
        wrap_label(self.page_hint_label)
        self.pages = PageStack(main, self.page_changed)
        self.pages.grid(row=2, column=0, sticky="nsew")
        self.pages.bind("<Configure>", self._align_page_heading)
        for _, _, build in self.page_specs:
            frame = ttk.Frame(self.pages)
            self.pages.add(frame)
            build(frame)
        separator = ttk.Separator(main)
        separator.grid(row=3, column=0, sticky="ew", pady=(medium, 0))
        footer = ttk.Frame(main)
        footer.grid(row=4, column=0, sticky="ew", pady=(small, 0))
        footer.columnconfigure(0, weight=1)
        footer.rowconfigure(0, minsize=p(28))
        self.status_text = tk.StringVar(self, value="打开资料库…")
        self.status_label = ttk.Label(footer, textvariable=self.status_text, style="Hint.TLabel", width=1)
        self.status_label.grid(row=0, column=0, sticky="ew")
        wrap_label(self.status_label)
        self.progress = ttk.Progressbar(footer, mode="indeterminate", length=p(56))
        self.progress.grid(row=0, column=1, padx=(small, 0))
        self.task_pill = StatusPill(footer)
        self.task_pill.grid(row=0, column=2, padx=(small, 0))
        self.reconnect_button = ttk.Button(footer, text="重新连接", command=self.reconnect_library,
                                           style="warning.ActionPill.TButton")
        self.reconnect_button.grid(row=0, column=3, padx=(small, 0))
        self.cancel_task_button = ttk.Button(footer, text="取消邮箱连接", command=self.cancel_mail_connection,
                                             style="occupied.ActionPill.TButton")
        self.cancel_task_button.grid(row=0, column=4, padx=(small, 0))
        for control in (self.progress, self.task_pill, self.reconnect_button, self.cancel_task_button):
            control.grid_remove()
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
            footer.rowconfigure(0, minsize=p(24) if compact else p(28))
            if compact:
                brand_subtitle.pack_forget()
                heading.grid_remove()
            else:
                brand_subtitle.pack(before=self.nav_buttons[0], anchor="w", padx=large, pady=(0, extra_large))
                heading.grid()
            self.update_workspace_path()

        self.bind("<Configure>", arrange_window, add="+")

    def page_changed(self, index):
        title, hint, _ = self.page_specs[index]
        self.page_title.set(title)
        self.page_hint.set(hint)
        self._align_page_heading()
        for n, button in enumerate(self.nav_buttons):
            button.configure(style="Selected.Nav.TButton" if n == index else "Nav.TButton")
        self.schedule_preview()

    def _align_page_heading(self, event=None):
        margin = 0
        if self.pages.current_index == 2:
            margin = max(0, (self.pages.winfo_width() - self.theme.px(SETTINGS_WIDTH)) // 2)
        self.page_title_label.pack_configure(padx=margin)
        self.page_hint_label.pack_configure(padx=margin)

    def _library_page(self, page):
        p = self.theme.px
        tiny, small, medium, large, _ = self.theme.space
        page.columnconfigure(0, weight=1)
        page.rowconfigure(5, weight=1)
        self.stats_frame = ttk.Frame(page)
        self.stats_frame.grid(row=0, column=0, sticky="ew", pady=(0, large))
        self.stats = []
        stat_titles = ("发票记录", "待核对 / 冲突", "可用票面合计", "已占用")
        self.metric_buttons = {}
        for index, (title, target) in enumerate(zip(stat_titles, (None, "待核对", "可用", "已占用"))):
            if index:
                tk.Frame(self.stats_frame, width=1, background=COLORS.line).grid(
                    row=0, column=index * 2 - 1, sticky="ns", padx=small, pady=tiny)
            self.stats_frame.columnconfigure(index * 2, weight=2 if index == 2 else 1)
            var = tk.StringVar(self, value="—")
            caption = tk.StringVar(self, value=f"{title}\n—")
            var.trace_add("write", lambda *_, name=title, value=var, text=caption: text.set(f"{name}\n{value.get()}"))
            if target:
                metric = ttk.Button(self.stats_frame, textvariable=caption, style="Metric.TButton",
                                    command=lambda value=target: self.toggle_invoice_filter(value))
                self.metric_buttons[target] = metric
            else:
                metric = ttk.Label(self.stats_frame, textvariable=caption, style="Metric.TLabel")
            metric.grid(row=0, column=index * 2, sticky="nsew")
            self.stats.append(var)
        toolbar = ttk.Frame(page)
        toolbar.grid(row=1, column=0, sticky="ew", pady=(0, medium))
        self.selection_button = self.button(toolbar, "按额度选票", self.open_selection, True, side="left")
        self.intake_button = self.button(toolbar, "导入发票", self.open_import, side="left", padx=(small, 0))
        self.history_button = self.button(toolbar, "导入记录", self.open_import_history, side="right")
        self.history_button.configure(style="Quiet.TButton")

        filters = ttk.Frame(page)
        filters.grid(row=2, column=0, sticky="ew", pady=(0, small))
        filters.columnconfigure(1, weight=1)
        self.filter = tk.StringVar(self, value="全部")
        self.filter_control = SegmentedControl(filters, ("全部", "待核对", "可用", "已占用", "同票冲突"),
                                               variable=self.filter, command=self.fill_invoices, theme=self.theme)
        self.filter_control.grid(row=0, column=0, sticky="w")
        searchbar = ttk.Frame(filters)
        searchbar.grid(row=0, column=1, sticky="e", padx=(small, 0))
        ttk.Label(searchbar, text="搜索", style="Muted.TLabel").pack(side="left", padx=(0, small))
        search_field = tk.Frame(searchbar, background=COLORS.line, borderwidth=0, padx=1, pady=1)
        search_field.pack(side="left", fill="x", expand=True)
        self.search = tk.StringVar(self)
        self.search_entry = ttk.Entry(search_field, textvariable=self.search, width=22, style="Search.TEntry")
        self.clear_search_button = ttk.Button(search_field, text="×", style="SearchClear.TButton",
                                             command=self.clear_search)
        self.clear_search_button.pack(side="right", fill="y")
        self.search_entry.pack(side="left", fill="both", expand=True)
        for widget in (self.search_entry, self.clear_search_button):
            widget.bind("<FocusIn>", lambda event: search_field.configure(background=COLORS.secondary))
            widget.bind("<FocusOut>", lambda event: search_field.configure(background=COLORS.line))
        self.search.trace_add("write", lambda *_: self.fill_invoices())
        self.search_entry.bind("<Escape>", lambda event: self.clear_search())
        self.list_summary = tk.StringVar(self, value=INVOICE_IMPORT_GUIDE)
        summary = ttk.Label(page, textvariable=self.list_summary, style="Muted.TLabel", width=1)
        summary.grid(row=3, column=0, sticky="ew", pady=(0, small))
        wrap_label(summary)
        self.import_banner = ttk.Frame(page, style="Surface.TFrame", padding=(small, tiny))
        self.import_banner.grid(row=4, column=0, sticky="ew", pady=(0, small))
        self.import_banner.grid_remove()
        ttk.Button(self.import_banner, text="×", style="Quiet.TButton", command=self.dismiss_import_result).pack(side="right")
        self.import_result_link = ttk.Button(self.import_banner, text="查看导入记录", style="Quiet.TButton",
                                             command=lambda: self.open_import_history(self.last_import_report))
        self.import_result_link.pack(side="right", padx=(small, 0))
        self.import_result = ttk.Label(self.import_banner, width=1, justify="left")
        self.import_result.pack(side="left", fill="x", expand=True)
        wrap_label(self.import_result, inset=2 * small)
        splitter = self.splitter = ttk.Panedwindow(page, orient="horizontal")
        splitter.grid(row=5, column=0, sticky="nsew")
        left, right = ttk.Frame(splitter), ttk.Frame(splitter, padding=(large, 0, 0, 0))
        self.detail_pane = right
        self.detail_visible = False
        splitter.add(left, weight=6)
        columns = ("file", "number", "date", "amount", "state")
        self.tree = ttk.Treeview(left, columns=columns, displaycolumns=("amount", "number", "date", "file"),
                                 show=("tree", "headings"), selectmode="browse", style="Invoice.Treeview")
        self.tree.heading("#0", text="状态 / 来源")
        self.tree.column("#0", width=p(142), minwidth=p(142), stretch=False)
        for name, title, width in zip(columns[:-1], ("文件名", "发票号码", "开票日期", "金额 / 元"), (190, 190, 110, 100)):
            self.tree.heading(name, text=title)
            self.tree.column(name, width=p(82 if name == "amount" else width), minwidth=p(76),
                             stretch=name == "file", anchor="e" if name == "amount" else "w")
        table_scrollers(left, self.tree)
        hover_rows(self.tree)
        self.empty_card = ttk.Frame(self.tree, style="SettingsCard.TFrame", padding=(medium, small))
        ttk.Label(self.empty_card, text="没有匹配的发票", style="Section.TLabel", anchor="center").pack(fill="x")
        self.empty_clear = ttk.Button(self.empty_card, text="清除筛选", command=self.reset_invoice_filters)
        self.empty_clear.pack(pady=(small, 0))
        self.tree.bind("<<TreeviewSelect>>", self.select_invoice)
        self.tree.bind("<ButtonRelease-1>", lambda event: self.show_detail()
                       if self.current_id and self.tree.identify_row(event.y) == self.current_id else None)
        self.tree.bind("<Up>", lambda event: self.move_invoice(-1))
        self.tree.bind("<Down>", lambda event: self.move_invoice(1))
        self.details_title = tk.StringVar(self, value="选择一张发票查看详情")
        detail_header = ttk.Frame(right)
        detail_header.pack(fill="x", pady=(0, medium))
        self.detail_close = ttk.Button(detail_header, text="×", style="Quiet.TButton", command=self.hide_detail)
        self.detail_close.pack(side="right")
        self.detail_status = StatusPill(detail_header)
        self.detail_status.pack(side="right", padx=(tiny, 0))
        title = ttk.Label(detail_header, textvariable=self.details_title, font=(FONT, 10, "bold"), width=1)
        title.pack(side="left", fill="x", expand=True)
        wrap_label(title)
        self.details = SegmentedPages(right, ("字段核对", "原票预览", "提取文本"), theme=self.theme)
        self.details.pack(fill="both", expand=True)
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
        help_text = ttk.Label(form, text="改为「已退回」或「可用」可释放批次占用。核验通过且公司匹配后可再次选用。",
                              style="Hint.TLabel", width=1)
        help_text.grid(row=19, column=0, sticky="ew", padx=(0, small), pady=tiny)
        wrap_label(help_text)
        self.recognize_button = ttk.Button(form, text="重新识别原件", command=self.recognize_again, state="disabled")
        self.recognize_button.grid(row=20, column=0, sticky="w", pady=(small, 0))
        self.form_scroll.enable_scrolling()
        filebar = ttk.Frame(preview)
        filebar.pack(fill="x", pady=(0, small))
        self.file_choice = ttk.Combobox(filebar, state="readonly", width=1)
        self.original_button = self.button(filebar, "打开原件", self.open_original, side="right", padx=(small, 0))
        self.file_choice.pack(side="left", fill="x", expand=True)
        self.file_choice.bind("<<ComboboxSelected>>", self.preview_file_changed)
        zoom = ttk.Frame(preview)
        zoom.pack(fill="x", pady=(0, small))
        self.zoom_out_button = ttk.Button(zoom, text="−", width=3, command=lambda: self.zoom_preview(1 / 1.25))
        self.zoom_out_button.pack(side="left")
        self.zoom_reset_button = ttk.Button(zoom, text="100%", command=self.reset_preview_zoom)
        self.zoom_reset_button.pack(side="left", padx=tiny)
        self.zoom_in_button = ttk.Button(zoom, text="+", width=3, command=lambda: self.zoom_preview(1.25))
        self.zoom_in_button.pack(side="left")
        self.zoom_fit_button = ttk.Button(zoom, text="适应宽度", style="Quiet.TButton", command=lambda: self.zoom_preview(None))
        self.zoom_fit_button.pack(side="left", padx=(tiny, 0))
        self.zoom_text = tk.StringVar(self, value="100%")
        zoom_level = ttk.Label(zoom, textvariable=self.zoom_text, style="Hint.TLabel")
        zoom_level.pack(side="right")
        preview_hint = ttk.Label(preview, text="预览 PDF 首页。查看其余页面请打开原件。", style="Hint.TLabel")
        preview_hint.pack(anchor="w", pady=(0, small))
        preview_area = ttk.Frame(preview)
        preview_area.pack(fill="both", expand=True)
        self.preview_canvas = tk.Canvas(preview_area, bg=COLORS.occupied_bg, highlightthickness=0, width=1, height=1)
        table_scrollers(preview_area, self.preview_canvas)
        self.preview = PreviewPane(self, self.preview_canvas, theme=self.theme, zoom_text=self.zoom_text,
                                   is_active=self._preview_active, current_file=self.selected_file,
                                   resolver=self._preview_resolver)
        self.preview.async_enabled = self.preview_async is True
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
        layout = None

        def arrange_library(event):
            nonlocal compact, layout
            short = event.height < p(515)
            use_short_labels = short or event.width < p(650)
            compact_filters = event.width < p(650)
            small_metrics = event.height < p(400)
            if (short, use_short_labels, compact_filters, small_metrics) == layout:
                return
            compact, narrow = short, use_short_labels
            layout = (short, narrow, compact_filters, small_metrics)
            gap = tiny if compact else small
            toolbar.grid_configure(pady=(0, p(2) if compact else medium))
            filters.grid_configure(pady=(0, gap))
            detail_header.pack_configure(pady=(0, 0 if compact else medium))
            self.details.bar.pack_configure(pady=(0, p(2) if compact else medium))
            right.configure(padding=(small if narrow else large, 0, 0, 0))
            self.review_banner.configure(pady=p(2) if compact else tiny)
            self.review_banner.pack_configure(pady=0 if compact else (0, tiny))
            labels = ("核对", "预览", "文本") if compact_filters else self.details.labels
            self.details.control.set_compact(narrow, labels=labels)
            self.filter_control.set_compact(compact_filters)
            self.search_entry.configure(width=9 if compact_filters else 22,
                                        style="Compact.Search.TEntry" if small_metrics else "Search.TEntry")
            self.selection_button.configure(style="Compact.Primary.TButton" if small_metrics else "Primary.TButton")
            self.intake_button.configure(style="Compact.TButton" if small_metrics else "TButton")
            self.history_button.configure(style="Compact.Quiet.TButton" if small_metrics else "Quiet.TButton")
            self.review_button.configure(style="Compact.Primary.TButton" if narrow else "Primary.TButton")
            self.review_button.configure(text="保存并核验" if compact_filters else "保存字段并核验")
            self.archive_button.configure(style="Compact.Danger.TButton" if narrow else "Danger.TButton")
            self.review_actions.configure(padding=0 if compact else (0, tiny, 0, 0))
            self.detail_close.configure(style="Compact.Quiet.TButton" if compact else "Quiet.TButton")
            self.file_choice.configure(style="Compact.TCombobox" if compact else "TCombobox")
            for button in (self.original_button, self.zoom_in_button, self.zoom_out_button, self.zoom_reset_button):
                button.configure(style="Compact.TButton" if compact else "TButton")
            self.zoom_fit_button.configure(style="Compact.Quiet.TButton" if compact else "Quiet.TButton")
            filebar.pack_configure(pady=(0, p(2) if small_metrics else gap))
            zoom.pack_configure(pady=(0, p(2) if small_metrics else gap))
            if small_metrics:
                self.stats_frame.grid_remove()
                summary.grid_remove()
            else:
                self.stats_frame.grid()
                summary.grid()
            if compact:
                preview_hint.pack_forget()
                zoom_level.pack_forget()
            else:
                preview_hint.pack(before=preview_area, anchor="w", pady=(0, small))
                zoom_level.pack(side="right")

        page.bind("<Configure>", arrange_library)
        self.details.bind("<<SegmentedPageChanged>>", lambda event: self.schedule_preview())

    def _batch_page(self, page):
        p = self.theme.px
        space = self.theme.space
        page.columnconfigure(0, weight=1)
        page.rowconfigure(1, weight=2, minsize=p(80))
        actions = ttk.Frame(page)
        actions.grid(row=0, column=0, sticky="ew", pady=(0, space.small))
        self.batch_export_button = ttk.Button(actions, text="导出原票与明细", style="Primary.TButton",
                                              command=self.export_batch, state="disabled")
        self.batch_export_button.pack(side="right", padx=(space.small, 0))
        self.batch_summary = tk.StringVar(self)
        summary = ttk.Label(actions, textvariable=self.batch_summary, style="Muted.TLabel", width=1)
        summary.pack(side="left", fill="x", expand=True)
        wrap_label(summary)
        listing = ttk.Frame(page)
        listing.grid(row=1, column=0, sticky="nsew")
        self.batch_tree = self._batch_table(listing, (
            ("time", "创建时间", 150, 140, "w"),
            ("count", "张数", 48, 42, "e"),
            ("target", "目标 / 元", 80, 76, "e"),
            ("total", "票面 / 元", 80, 76, "e"),
            ("declared", "申报 / 元", 80, 76, "e"),
        ))
        self.batch_tree.bind("<<TreeviewSelect>>", self.show_batch)
        self.batch_details = ttk.Frame(page)
        self.batch_details.grid(row=2, column=0, sticky="nsew", pady=(space.small, 0))
        self.batch_details.grid_remove()
        metadata = ttk.Frame(self.batch_details, style="Surface.TFrame")
        metadata.pack(fill="x", pady=(0, space.tiny))
        self.batch_info = tk.Text(metadata, height=2, width=1, wrap="word", state="disabled", relief="flat",
                                  borderwidth=0, highlightthickness=0, bg=COLORS.surface, fg=COLORS.text,
                                  font=(FONT, 10), padx=space.small, pady=space.tiny)
        self.batch_info.tag_configure("caption", foreground=COLORS.secondary)
        self.batch_info.tag_configure("identifier", font=(MONO, 10))
        scroll = ttk.Scrollbar(metadata, orient="vertical", command=self.batch_info.yview)
        scroll.pack(side="right", fill="y")
        self.batch_info.configure(yscrollcommand=scroll.set)
        self.batch_info.pack(fill="x", expand=True)
        items = ttk.Frame(self.batch_details)
        items.pack(fill="both", expand=True)
        self.batch_items = self._batch_table(items, (
            ("number", "发票号码", 148, 140, "w"),
            ("date", "开票日期", 88, 82, "w"),
            ("amount", "金额 / 元", 68, 68, "e"),
            ("status", "当前状态", 76, 76, "w"),
            ("membership", "批次占用", 66, 64, "w"),
        ))

    def _batch_table(self, parent, columns):
        p = self.theme.px
        tree = ttk.Treeview(parent, columns=[column[0] for column in columns], show="headings",
                            selectmode="browse", height=2)
        for key, title, width, minimum, anchor in columns:
            tree.heading(key, text=title, anchor=anchor)
            tree.column(key, width=p(width), minwidth=p(minimum), anchor=anchor)
        table_scrollers(parent, tree)
        hover_rows(tree)
        return tree

    def _settings_page(self, page):
        labels = (GENERAL_SETTINGS, MAIL_SETTINGS, SYSTEM_SETTINGS)
        self.settings_pages = tabs = SegmentedPages(page, labels, theme=self.theme, capsule=True)
        tabs.pack(fill="both", expand=True)

        def center_settings(event):
            margin = max(0, (event.width - self.theme.px(SETTINGS_WIDTH)) // 2)
            tabs.pack_configure(padx=margin)

        page.bind("<Configure>", center_settings)
        space = self.theme.space
        mail_footer = ttk.Frame(tabs.frames[1], style="SettingsCard.TFrame", padding=(space.large, space.small))
        mail_footer.pack(side="bottom", fill="x", pady=(space.tiny, 0))
        builders = (self._general_settings, lambda panel: self._mail_settings(panel, mail_footer), self._system_settings)
        for frame, build in zip(tabs.frames, builders):
            scroll = ScrollableFrame(frame, theme=self.theme)
            scroll.pack(fill="both", expand=True)
            build(scroll.content)
            scroll.enable_scrolling()

    def open_settings(self, section):
        self.settings_pages.select(self.settings_pages.labels.index(section))
        self.pages.select(2)

    def _settings_card(self, panel, title):
        space = self.theme.space
        card = ttk.Frame(panel, style="SettingsCard.TFrame", padding=(space.large, self.theme.px(10)))
        card.pack(fill="x", pady=(0, space.medium))
        header = ttk.Frame(card)
        header.pack(fill="x")
        ttk.Label(header, text=title, style="Section.TLabel").pack(side="left")
        return card, header

    def _settings_field(self, parent, title, variable, *, surface=False, width=1):
        style = "SurfaceMuted.TLabel" if surface else "Muted.TLabel"
        ttk.Label(parent, text=title, style=style).pack(anchor="w", pady=(0, self.theme.space.tiny))
        entry = ttk.Entry(parent, textvariable=variable, width=width)
        entry.pack(fill="x")
        return entry

    def _general_settings(self, panel):
        space = self.theme.space
        library, header = self._settings_card(panel, "工作区存储位置")
        self.library_button = self.button(header, "修改位置…", self.choose_workspace, side="right")
        hint = ttk.Label(library, text="选择新目录会复制当前资料。选择已有工作区会打开其中的记录。",
                         style="Muted.TLabel", width=1, justify="left")
        hint.pack(fill="x", pady=(space.small, 0))
        wrap_label(hint)
        self.directory_text = tk.StringVar(self)
        directory = ttk.Label(library, textvariable=self.directory_text, style="Muted.TLabel", width=1)
        directory.pack(fill="x", pady=(space.small, 0))
        wrap_label(directory)
        self.legacy_storage_note = ttk.Label(library, style="Muted.TLabel", width=1, justify="left")
        wrap_label(self.legacy_storage_note)
        company, _ = self._settings_card(panel, "报销公司")
        self.company_name, self.company_tax = tk.StringVar(self), tk.StringVar(self)
        company_fields = ttk.Frame(company)
        company_fields.pack(fill="x", pady=(space.small, 0))
        fields = (("购买方抬头", self.company_name), ("购买方税号", self.company_tax))
        for index, (title, variable) in enumerate(fields):
            company_fields.columnconfigure(index, weight=1, uniform="company")
            field = ttk.Frame(company_fields)
            field.grid(row=0, column=index, sticky="ew", padx=(space.small if index else 0, 0))
            self._settings_field(field, title, variable)
        company_actions = ttk.Frame(company)
        company_actions.pack(fill="x", pady=(space.medium, 0))
        self.button(company_actions, "保存公司设置", self.save_company, True, side="right", padx=(space.small, 0))
        help_text = ttk.Label(company_actions, text="同时填写抬头和税号时，两项都须匹配。", style="Muted.TLabel", width=1)
        help_text.pack(side="left", fill="x", expand=True)
        wrap_label(help_text)

    def _system_settings(self, panel):
        space = self.theme.space
        database, _ = self._settings_card(panel, "系统数据库")
        self.database_text = tk.StringVar(self)
        path = ttk.Label(database, textvariable=self.database_text, style="Muted.TLabel", width=1)
        path.pack(fill="x", pady=(space.small, 0))
        wrap_label(path)
        reset, _ = self._settings_card(panel, "恢复初始化")
        notice = ttk.Label(reset, text="清空记录和设置。保留工作区位置、原件和导出文件。",
                           style="Muted.TLabel", width=1)
        notice.pack(fill="x", pady=(space.small, space.medium))
        wrap_label(notice)
        self.reset_button = self.button(reset, "恢复初始化…", self.reset_workspace, anchor="e")
        self.reset_button.configure(style="Danger.TButton")

    def _mail_settings(self, panel, footer):
        p = self.theme.px
        space = self.theme.space
        card, header = self._settings_card(panel, "IMAP 收票配置")
        self.mail_session_pill = StatusPill(header)
        self.mail_session_pill.pack(side="right", padx=(space.small, 0))
        ttk.Separator(card).pack(fill="x", pady=p(6))

        self.mail_address, self.mail_host, self.mail_port, self.mail_client_id, self.mail_blocked = (
            tk.StringVar(self) for _ in range(5))
        self.mail_fields = (self.mail_address, self.mail_host, self.mail_port, self.mail_client_id, self.mail_blocked)
        self._load_mail_fields()
        ttk.Label(card, text="邮箱地址").pack(anchor="w", pady=(0, space.tiny))
        address_line = ttk.Frame(card)
        address_line.pack(fill="x")
        address_line.columnconfigure(0, weight=1)
        self.mail_address_entry = ttk.Entry(address_line, width=1, textvariable=self.mail_address)
        self.mail_address_entry.grid(row=0, column=0, sticky="ew")
        for event in ("<KeyRelease>", "<FocusOut>"):
            self.mail_address_entry.bind(event, self._refresh_mail_provider)
        self.mail_provider_pill = StatusPill(address_line, width=1, anchor="w", justify="left")
        wrap_label(self.mail_provider_pill, inset=2 * space.small)
        self.mail_provider_pill.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(space.tiny, 0))
        inline = False

        def arrange_address(event):
            nonlocal inline
            beside = event.width >= p(580)
            if beside == inline:
                return
            inline = beside
            address_line.columnconfigure(1, minsize=p(275) if inline else 0)
            self.mail_provider_pill.grid_configure(row=0 if inline else 1, column=int(inline),
                                                   columnspan=1 if inline else 2,
                                                   padx=(space.small if inline else 0, 0),
                                                   pady=(0 if inline else space.tiny, 0))

        address_line.bind("<Configure>", arrange_address)
        self._mail_provider_kind = None
        self.mail_advanced = CollapsibleSection(card, "高级服务器设置", theme=self.theme)
        self.mail_advanced.pack(fill="x", pady=(space.small, 0))
        advanced = self.mail_advanced.content
        connection = ttk.Frame(advanced, style="Surface.TFrame", padding=(space.tiny, 0))
        connection.pack(fill="x")
        connection.columnconfigure(0, weight=1)
        host = ttk.Frame(connection, style="Surface.TFrame")
        host.grid(row=0, column=0, sticky="ew", padx=(0, space.small))
        self._settings_field(host, "IMAP 服务器（留空则自动识别）", self.mail_host, surface=True)
        port = ttk.Frame(connection, style="Surface.TFrame")
        port.grid(row=0, column=1, sticky="ew")
        self._settings_field(port, "SSL/TLS 端口", self.mail_port, surface=True, width=8)
        blocklist = ttk.Frame(advanced, style="Surface.TFrame", padding=(space.tiny, 0))
        blocklist.pack(fill="x", pady=(space.small, 0))
        self._settings_field(blocklist, "下载域名黑名单（逗号分隔）", self.mail_blocked, surface=True)
        oauth = ttk.Frame(advanced, style="Surface.TFrame", padding=(space.tiny, 0))
        oauth.pack(fill="x", pady=(space.small, space.tiny))
        self.mail_client_entry = self._settings_field(oauth, "Outlook OAuth 客户端 ID", self.mail_client_id, surface=True)
        oauth_hint = ttk.Label(oauth, style="SurfaceMuted.TLabel", width=1,
                               text="已提供客户端 ID 时可留空。")
        oauth_hint.pack(fill="x", pady=(space.tiny, 0))
        wrap_label(oauth_hint)

        self.mail_feedback = ttk.Label(footer, justify="left", width=1)
        self.mail_feedback.pack(fill="x")
        wrap_label(self.mail_feedback, inset=2 * space.small)
        actions = ttk.Frame(footer)
        actions.pack(fill="x", pady=(space.small, 0))
        self.mail_clear_button = ttk.Button(actions, text="清除本次登录凭证", command=self.disconnect_mail,
                                            state="disabled")
        self.mail_clear_button.pack(side="right")
        self.mail_save_button = self.button(actions, "保存收票设置", self.save_mail_settings, True,
                                            side="right", padx=(0, space.small))
        for variable in self.mail_fields:
            variable.trace_add("write", self._mail_fields_changed)
        for variable in (self.mail_address, self.mail_host, self.mail_port):
            variable.trace_add("write", self._refresh_mail_provider)
        self._refresh_mail_provider()
        self._mail_fields_changed()
        self._update_mail_session()

    def _save_settings(self, settings=None):
        path = self.settings_file
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, suffix=".tmp", delete=False)
        temporary_path = Path(temporary.name)
        try:
            with temporary:
                json.dump(self.settings if settings is None else settings, temporary, ensure_ascii=False, indent=2)
            temporary_path.replace(path)
        finally:
            temporary_path.unlink(missing_ok=True)

    def open_library(self, directory, *, recover_lock=False, require_existing=False):
        directory = Path(directory).expanduser()
        if self.store and self.store.root == directory.resolve():
            if not self.reload():
                raise UserError(self.status_text.get())
            return
        new = Store(directory, recover_lock=recover_lock, require_existing=require_existing)
        settings = self.settings | {"data_directory": str(new.root)}
        try:
            snapshot = new.view(include_import_runs=False)
            self._save_settings(settings)
        except Exception:
            new.close()
            raise
        old = self.store
        # 先让旧工作区的预览请求作废，再替换并关闭资料会话。
        self.cancel_preview_render()
        self.store = new
        self.connection_directory = new.root
        if old:
            old.close()
        self.settings = settings
        self.current_id = None
        self.clear_detail()
        self.dismiss_import_result()
        self.search.set("")
        self.filter.set("全部")
        self._show_snapshot(snapshot)

    def choose_workspace(self):
        if self.busy or not self.allow_discard():
            return
        directory = self.store.workspace if self.store else self.connection_directory
        if directory.name.lower() == WORKSPACE_DATABASE:
            directory = directory.parent
        path = filedialog.askdirectory(title="选择工作区存储位置", initialdir=str(directory) if directory.is_dir() else None,
                                       parent=self, mustexist=True)
        if path:
            self.change_workspace(path)

    def change_workspace(self, path):
        if self.busy:
            return
        store = self.store
        current_database = store.root if store else self.connection_directory
        path = Path(path).resolve()
        if WORKSPACE_DATABASE in {part.lower() for part in path.parts} or any(
                path.is_relative_to(current_database / directory) for directory in ARCHIVE_DIRECTORIES):
            messagebox.showerror("无法选择工作区", "请选择独立的工作区根目录。", parent=self)
            return
        database = path / WORKSPACE_DATABASE
        if database.is_symlink() or (database.exists() and is_directory_link(database)):
            messagebox.showerror("无法选择工作区", "工作区的数据库目录不能使用目录链接。", parent=self)
            return

        def open_workspace(directory):
            try:
                self.open_library(directory, require_existing=True)
            except (UserError, OSError, ValueError) as exc:
                messagebox.showerror("无法打开工作区", str(exc), parent=self)
                return
            self.status_text.set("工作区已打开。")

        if database.exists():
            open_workspace(database)
        elif (path / INVOICE_INDEX).is_file() and not (store and path == store.root and store.library):
            open_workspace(path)
        elif not self.session_ok:
            messagebox.showerror("无法打开工作区", "当前数据库未连接。请选择已有工作区，或重新连接后再复制记录。",
                                 parent=self)
        else:
            revision = self.snapshot["revision"]
            self.task("正在复制工作区资料…", lambda: store.copy_workspace(revision, path),
                      open_workspace, preserve_edits=False)

    def update_actions(self):
        enabled = not self.busy and self.session_ok
        for button in self.actions:
            available = not self.busy if button is self.library_button else enabled
            button.configure(state="normal" if available else "disabled")
        self.import_result_link.configure(state="normal" if enabled else "disabled")
        item = self.selected_item()
        editable = self.session_ok and not self.busy and bool(item)
        if editable:
            editable = item.get("reimbursement_status") not in OCCUPIED_STATUSES and not item.get("archived")
        for entry in self.entries:
            entry.configure(state="normal" if editable else "readonly")
        self.review_button.configure(state="normal" if editable else "disabled")
        can_recognize = editable and item["files"] and not item["verification"]["passed"]
        self.recognize_button.configure(state="normal" if can_recognize else "disabled")
        self.status_choice.configure(state="readonly" if self.session_ok and not self.busy and item else "disabled")
        archivable = (self.session_ok and not self.busy and item
                      and (item.get("archived") or item.get("reimbursement_status") not in OCCUPIED_STATUSES))
        self.archive_button.configure(state="normal" if archivable else "disabled")
        self.original_button.configure(state="normal" if self.session_ok and not self.busy and self.selected_file()
                                       else "disabled")
        self.update_batch_export()

    def connection_error(self, text):
        self.session_ok = False
        self.status_text.set(text)
        self.update_connection_status()
        self.update_actions()

    def update_connection_status(self):
        self.workspace_status.set("◌ 正在处理" if self.busy else "● 数据库已连接" if self.session_ok else "○ 数据库未连接")
        self.workspace_label.configure(foreground=COLORS.secondary if self.session_ok else COLORS.warning)
        self.status_label.configure(foreground=COLORS.text if self.busy else COLORS.secondary if self.session_ok else COLORS.error)
        self.cancel_task_button.configure(text="取消邮箱连接" if self.connecting_mail else "取消任务")
        self._update_mail_session()
        for control, visible in (
            (self.progress, self.busy),
            (self.task_pill, self.busy),
            (self.reconnect_button, not self.session_ok and not self.busy),
            (self.cancel_task_button, self.task_cancelled is not None),
        ):
            if visible:
                control.grid()
            else:
                control.grid_remove()

    def update_workspace_path(self):
        if not self.store:
            self.workspace_path.set("工作区未连接")
            return
        directory = self.store.workspace
        name = directory.name or str(directory)
        self.workspace_path.set(name if self.compact_layout else f"{name}\n{directory.parent}")

    def reconnect_library(self):
        if self.busy or not self.allow_discard():
            return
        directory = self.store.root if self.store else self.connection_directory
        self.connection_directory = directory
        self.cancel_preview_render()
        if self.store:
            self.store.close()
            self.store = None
        self.snapshot = {}
        self.invoices_by_id.clear()
        self.clear_detail()
        self.fill_invoices()
        self.fill_batches()
        try:
            self.open_library(directory)
        except (UserError, OSError, ValueError) as exc:
            self.connection_error(str(exc))
            # Local live writers remain protected by the OS guard even during recovery.
            if "未结束的会话" in str(exc) and messagebox.askyesno(
                    "恢复资料连接", f"系统数据库：{directory}\n\n请确认其他设备已关闭此工作区、网盘已完成同步。\n\n确认恢复？", parent=self):
                try:
                    self.open_library(directory, recover_lock=True)
                except (UserError, OSError, ValueError) as error:
                    self.connection_error(str(error))
                    messagebox.showerror("暂时无法连接", str(error), parent=self)

    def reload(self, *, discard_edits=False):
        if not self.store or self.busy:
            return False
        try:
            snapshot = self.store.view(include_import_runs=False)
        except (UserError, OSError, ValueError) as exc:
            self.connection_error(str(exc))
            return False
        self._show_snapshot(snapshot, discard_edits=discard_edits)
        return True

    def _show_snapshot(self, snapshot, *, discard_edits=False):
        self.snapshot = snapshot
        invoices = sorted(snapshot["invoices"], key=lambda item: (item.get("date", ""), item["created"]), reverse=True)
        self.invoices_by_id = {item["id"]: item for item in invoices}
        self.session_ok = True
        pending = occupied = available_total = 0
        for item in invoices:
            if item["reimbursement_status"] in OCCUPIED_STATUSES:
                occupied += 1
            elif selectable(item):
                available_total += item["amount_cents"]
            elif not item.get("archived"):
                pending += 1
        values = (str(len(invoices)), str(pending), "¥ " + yuan(available_total), str(occupied))
        for var, value in zip(self.stats, values):
            var.set(value)
        self.directory_text.set(str(self.store.workspace))
        if self.store.library and self.store.root.name != WORKSPACE_DATABASE:
            self.legacy_storage_note.configure(text="此工作区使用旧版存储方式。可通过「修改位置」迁移资料。")
            self.legacy_storage_note.pack(fill="x", pady=(self.theme.space.tiny, 0))
        else:
            self.legacy_storage_note.pack_forget()
        self.database_text.set(self.snapshot["data_directory"])
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
            self.status_text.set("按额度选票前，请在「设置」填写报销公司。")
        else:
            self.status_text.set("已就绪。")
        self.update_actions()
        self.update_connection_status()

    def clear_search(self):
        self.search.set("")
        self.search_entry.focus_set()

    def reset_invoice_filters(self):
        self.filter.set("全部")
        self.search.set("")

    def toggle_invoice_filter(self, target):
        self.filter.set("全部" if self.filter.get() == target else target)
        self.fill_invoices()

    def fill_invoices(self):
        query = self.search.get().strip().lower()
        active_filter = self.filter.get()
        for target, button in self.metric_buttons.items():
            button.configure(style="Selected.Metric.TButton" if active_filter == target else "Metric.TButton")
        self.clear_search_button.configure(state="normal" if self.search.get() else "disabled")
        selected = self.current_id
        self.tree.delete(*self.tree.get_children())
        for item in self.invoices_by_id.values():
            if query:
                documents = list(item.get("files", ())) + list(item.get("mail_xml", ()))
                haystack = " ".join(str(item.get(k) or "") for k in
                                    ("number", "buyer_name", "buyer_tax_id", "seller_name", "note"))
                haystack += " " + " ".join(str(document.get("name") or "") for document in documents)
                if query not in haystack.lower():
                    continue
            occupied = item.get("reimbursement_status") in OCCUPIED_STATUSES
            available = selectable(item)
            pending = not item.get("archived") and not occupied and not available
            matches = {"全部": True, "待核对": pending, "可用": available,
                       "已占用": occupied, "同票冲突": bool(item.get("duplicate"))}
            if not matches[active_filter]:
                continue
            status = state_label(item)
            tone = "occupied" if occupied or item.get("archived") else "error" if item.get("duplicate") else "warning" if pending else "success"
            from_mail = any("@" in source.partition(" / ")[0] and " / " in source
                            and not Path(source.partition(" / ")[0]).anchor for source in item["sources"])
            origin = "邮件" if from_mail else "本地"
            badges = {"image": self.theme.badge_strip((status, tone), (origin, "info" if from_mail else "occupied"))}
            if self.theme.status_font is None:
                badges = {"text": f"{status} · {origin}"}
            documents = item["files"] or item.get("mail_xml", ())
            filename = documents[0]["name"] if documents else "待补充原件"
            self.tree.insert("", "end", iid=item["id"],
                             values=(filename, item["number"] or "待填写票号", item["date"] or "—",
                                     yuan(item["amount_cents"]) if item["amount_cents"] else "待填写", status), **badges)
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
        self.list_summary.set(f"显示 {count} / {total} 张" if total else INVOICE_IMPORT_GUIDE)
        if total and not count:
            self.empty_card.place(relx=0.5, rely=0.55, anchor="center")
        else:
            self.empty_card.place_forget()

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
        detail_title = item["number"] or "待填写票号"
        if self.draft_is_filtered():
            detail_title += "\n已被筛选隐藏"
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
        self.detail_status.configure(text=state_label(item))
        if item.get("reimbursement_status") in OCCUPIED_STATUSES or item.get("archived"):
            self.detail_status.set_tone("occupied")
        else:
            self.detail_status.set_tone("error" if item.get("duplicate") else "warning" if messages else "success")
        background, foreground = getattr(COLORS, tone + "_bg"), getattr(COLORS, tone)
        self.review_banner.configure(background=background, foreground=foreground, activebackground=background,
                                      activeforeground=foreground, highlightbackground=background,
                                      cursor="hand2" if messages else "", takefocus=bool(messages))

    def show_review_issues(self):
        if self.review_messages:
            messagebox.showwarning("核验提示", "\n\n".join(self.review_messages), parent=self)

    def allow_discard(self, *, parent=None):
        return not self.dirty or messagebox.askyesno("尚未保存", "当前字段修改尚未保存。放弃修改并继续？",
                                                    parent=parent or self)

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
            item = self.invoices_by_id.get(selection[0])
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
        files = item["files"]
        self.file_choice["values"] = [f["name"] for f in files]
        if 0 <= file_index < len(files):
            self.file_choice.current(file_index)
        else:
            if files:
                self.file_choice.current(0)
            else:
                self.file_choice.set("")
            self.clear_preview()
        raw = item.get("raw_text") or ("已分析邮件 XML，请补充可核验的发票原件。" if not files and item.get("mail_xml")
                                    else "此文件暂无可提取文字，自动核验无法完成。")
        self.set_text(self.raw, raw)
        self.archive_button.configure(text="恢复候选" if item.get("archived") else "移出候选")
        self.loading = False
        self.dirty = keep_edits
        self.update_review_banner()
        self.update_actions()
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
            left = min(self.theme.px(300), max(self.theme.px(236), int(width * 0.35)), width)
            right = min(right, width - left)
            position = self.splitter.sashpos(0)
            self.splitter.sashpos(0, max(left, min(width - right, position)))

    def hide_detail(self):
        if self.detail_visible:
            self.detail_visible = False
            self.preview.hide()
            self.splitter.forget(self.detail_pane)

    @staticmethod
    def set_text(widget, text):
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", text)
        widget.configure(state="disabled")

    def selected_item(self):
        return self.invoices_by_id.get(self.current_id)

    def selected_file(self):
        item = self.selected_item()
        index = self.file_choice.current()
        return item["files"][index] if item and 0 <= index < len(item["files"]) else None

    def _preview_active(self):
        return (self.detail_visible and self.current_id is not None and self.pages.current_index == 0
                and self.details.current_index == 1)

    def _preview_resolver(self):
        """绑定当次请求的资料会话，工作线程不再读取可能已被替换的 self.store。"""
        store = self.store

        def resolve(file):
            if store is None:
                raise UserError("资料会话已关闭，请重新启动工具。")
            return store.original(file)

        return resolve

    def clear_preview(self):
        self.preview.clear()

    def preview_scroll_position(self):
        return self.preview.scroll_position()

    def cancel_preview_render(self):
        self.preview.cancel()

    def preview_is_active(self):
        return self.preview.active()

    def schedule_preview(self, *, resizing=False):
        self.preview.schedule(resizing=resizing)

    def preview_file_changed(self, event=None):
        self.preview.file_changed()

    def zoom_preview(self, factor):
        self.preview.zoom_by(factor)

    def reset_preview_zoom(self):
        self.preview.reset_zoom()

    def render_preview(self):
        self.preview.render()

    # 预览状态由 PreviewPane 持有，这里保留原有属性名供既有调用方和测试读取。
    @property
    def image_reference(self):
        return self.preview.photo

    @property
    def preview_image(self):
        return self.preview.image

    @property
    def preview_key(self):
        return self.preview.key

    @property
    def preview_zoom(self):
        return self.preview.zoom

    @property
    def preview_fit(self):
        return self.preview.fit

    @property
    def preview_after(self):
        return self.preview.after_id

    @property
    def preview_generation(self):
        return self.preview.generation

    @property
    def preview_resize_position(self):
        return self.preview.resize_position

    def open_original(self):
        file = self.selected_file()
        if file and self.store:
            try:
                path = self.store.original(file)
                import os
                os.startfile(str(path))
            except (UserError, OSError) as exc:
                messagebox.showerror("无法打开原件", str(exc), parent=self)

    def _task_channel(self):
        """分配递增的任务编号，返回只携带该编号的事件发送器。"""
        self.next_task_id += 1
        task_id = self.next_task_id
        return task_id, lambda kind, payload: self.events.put((task_id, kind, payload))

    def task(self, description, work, done=None, *, preserve_edits=True, cancelled=None, connecting_mail=False,
             failed=None, task_id=None, pill="处理中", reload_after=True):
        if self.busy:
            return
        if task_id is None:
            task_id = self._task_channel()[0]
        self.busy = True
        self.active_task_id = task_id
        self.task_cancelled = cancelled
        self.connecting_mail = connecting_mail
        self.preserve_edits = preserve_edits
        self.reload_after = reload_after
        self.status_text.set(description)
        self.task_pill.configure(text="连接邮箱" if self.connecting_mail else pill)
        self.update_connection_status()
        self.update_actions()
        self.progress.start(12)

        def run():
            try:
                result = work()
                if cancelled is None or not cancelled.is_set():
                    self.events.put((task_id, "done", (result, done)))
            except Exception as exc:
                if cancelled is None or not cancelled.is_set():
                    logging.exception("Operation failed: %s", description)
                    self.events.put((task_id, "error", (exc, failed)))
        threading.Thread(target=run, daemon=True).start()

    def finish_task(self):
        self.busy = False
        self.active_task_id = None
        self.task_cancelled = None
        self.connecting_mail = False
        self.reload_after = True
        self.progress.stop()
        self.update_connection_status()

    def cancel_mail_connection(self):
        if self.task_cancelled is None:
            return
        self.task_cancelled.set()
        # 旧任务编号立即失效，迟到的事件在 _poll 中会被丢弃。
        self.active_task_id = None
        self.finish_task()
        self.update_actions()
        self.status_text.set("已取消收票任务。")
        if self.import_dialog is not None:
            self.import_dialog.update_mail()

    def _poll(self):
        try:
            while True:
                task_id, kind, payload = self.events.get_nowait()
                if task_id != self.active_task_id:
                    continue
                if kind == "progress":
                    self.status_text.set(payload)
                    continue
                reload_after = self.reload_after
                self.finish_task()
                if reload_after or kind == "error":
                    # 失败时同步一次快照，确保断开或索引冲突能立即反映到界面。
                    self.reload(discard_edits=kind == "done" and not self.preserve_edits)
                else:
                    # 只读任务不改资料，跳过全量重载和列表重建；仍做一次轻量会话校验，
                    # 外部修改索引或冲突副本必须在结果落地前被发现。
                    try:
                        if self.store is None:
                            raise UserError("资料会话已关闭，请重新启动工具。")
                        self.store.verify()
                    except (UserError, OSError, ValueError) as exc:
                        self.connection_error(str(exc))
                    else:
                        if self.session_ok:
                            self.status_text.set("已就绪。")
                    self.update_actions()
                if kind == "error":
                    error, callback = payload
                    if callback:
                        callback(error)
                    else:
                        messagebox.showerror("操作未完成", str(error), parent=self)
                        if self.session_ok:
                            self.status_text.set("操作未完成，请核对提示后重试。")
                else:
                    result, callback = payload
                    if callback:
                        callback(result)
        except queue.Empty:
            pass
        self.poll_id = self.after(100, self._poll)

    def import_directory(self):
        self.open_import(LOCAL_MODE)

    def open_import(self, mode=LOCAL_MODE):
        if self.import_dialog is not None:
            self.import_dialog.pages.select(self.import_dialog.pages.labels.index(mode))
            self.import_dialog.lift()
            return
        if not self.session_ok or self.busy:
            return
        workspace = self.store.workspace
        address = self.settings.get("mail_address", "")
        profile = provider = None
        notice = ""
        if not workspace.is_dir():
            notice = "请先在设置中选择可用的工作区。"
        elif not address:
            notice = "请先在「邮箱收票」设置中保存邮箱地址。"
        if address:
            try:
                profile = mailbox_profile(address, self.settings.get("mail_host", ""), self.settings.get("mail_port", 993))
                provider = MAIL_PROVIDER_NAMES.get(profile[1], profile[1])
            except UserError as exc:
                if not notice:
                    notice = str(exc)
        dialog = ImportDialog(self, mode, workspace=workspace, recent=self.settings.get("last_import_directory"),
                              profile=profile, provider=provider, mail_notice=notice)
        self.import_dialog = dialog
        if self.mailbox is not None and self.mailbox.profile == profile:
            dialog.update_mail(folders=self.mail_folders)

        def closed(event):
            if event.widget is dialog:
                self.import_dialog = None
                if dialog.connecting:
                    self.cancel_mail_connection()

        dialog.bind("<Destroy>", closed, add="+")

    def edit_import_settings(self, dialog):
        workspace = self.store.workspace
        dialog.destroy()
        if not workspace.is_dir():
            self.open_settings(GENERAL_SETTINGS)
        else:
            self.open_settings(MAIL_SETTINGS)
            if dialog.mail_notice:
                if dialog.profile is None and self.settings.get("mail_address"):
                    self.mail_advanced.set_expanded(True)
                self._mail_feedback(dialog.mail_notice, tone="warning")
            self.mail_address_entry.focus_set()

    def edit_archive_target(self, dialog):
        dialog.destroy()
        self.open_settings(GENERAL_SETTINGS)

    def start_import(self, source=None, *, files=(), scan_workspace=False):
        if not self.session_ok or self.busy:
            return
        store, revision = self.store, self.snapshot["revision"]
        self.pages.select(0)
        self.clear_detail()
        self.reset_invoice_filters()
        self.dismiss_import_result()
        task_id, emit = self._task_channel()

        def work():
            def progress(message):
                emit("progress", message)

            if files:
                return store.import_files(revision, files, progress)
            return import_directory(store, revision, source, progress, scan_workspace=scan_workspace)
        self.task("正在读取发票…", work, self.import_done, failed=self.import_failed, task_id=task_id)

    def import_done(self, report):
        text = import_summary(report)
        issues = sum(row["result"] == "error" or bool(row.get("invoice_id") and row.get("detail"))
                     for row in report["items"])
        reviews = len({row["review_id"] for row in report["items"] if row["result"] == "review"})
        banner = f"本次新增 {report['imported']} 张 · 自动归并 {report['merged']} 份原件 · {issues} 条异常"
        if reviews:
            banner += f" · {reviews} 项待审核"
        if not report["scanned"] and report["kind"] == "directory":
            text = banner = "没有找到支持的文件。"
        elif not report["scanned"] and not report["items"]:
            reused = report.get("mail_screening", {}).get("reused", 0)
            banner = f"已复用 {reused} 封邮件的处理结果。" if reused else "所选日期范围内没有邮件。"
        elif stats := report.get("mail_screening"):
            if stats["deferred"]:
                banner += f" · {stats['deferred']} 个附件可用深度扫描补查"
            elif not report["items"]:
                banner = f"已筛查 {report['scanned']} 封邮件，未识别到发票。"
        if report["kind"] in {"directory", "files"}:
            recent = report.get("source_directory", report["source"])
            self.settings = self.settings | {"last_import_directory": recent}
            try:
                self._save_settings()
            except OSError:
                logging.exception("无法保存最近导入目录")
        self.show_import_result(banner, "warning" if issues or reviews else "success", report)
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

    def dismiss_import_result(self):
        self.last_import_report = None
        self.import_banner.grid_remove()

    def show_import_result(self, text, tone, report=None):
        self.last_import_report = report
        self.import_result.configure(text=text, style=f"{tone}.SettingsFeedback.TLabel")
        if report:
            self.import_result_link.pack(side="right", padx=(self.theme.space.small, 0), before=self.import_result)
        else:
            self.import_result_link.pack_forget()
        self.import_banner.grid()

    def import_failed(self, error):
        self.show_import_result(str(error), "error")
        self.status_text.set("导入未完成。")

    def open_import_history(self, report=None):
        if not self.store:
            return
        try:
            reports = self.store.import_history()
        except (UserError, OSError, ValueError) as exc:
            self.connection_error(str(exc))
            return
        if report and not any(row["id"] == report["id"] for row in reports):
            reports.insert(0, report)
        ImportHistory(self, reports, report["id"] if report else None)

    def locate_imported_invoice(self, invoice_id, *, parent):
        if not self.session_ok or self.busy or invoice_id not in self.invoices_by_id:
            return False
        if self.current_id != invoice_id and not self.allow_discard(parent=parent):
            return False
        self.pages.select(0)
        self.reset_invoice_filters()
        self.tree.selection_set(invoice_id)
        self.tree.focus(invoice_id)
        self.tree.see(invoice_id)
        if self.current_id != invoice_id:
            self.load_invoice(self.invoices_by_id[invoice_id])
        else:
            self.show_detail()
        self.details.select(0)
        return True

    def _saved_mail_fields(self):
        return (
            self.settings.get("mail_address", ""),
            self.settings.get("mail_host", ""),
            str(self.settings.get("mail_port", 993)),
            self.settings.get("mail_client_id", ""),
            ", ".join(self.settings.get("mail_blocked_domains", [])),
        )

    def _load_mail_fields(self):
        for field, value in zip(self.mail_fields, self._saved_mail_fields()):
            field.set(value)

    def _refresh_mail_provider(self, *_):
        address = self.mail_address.get().strip()
        host = self.mail_host.get().strip()
        port = self.mail_port.get().strip()
        local, _, domain = address.rpartition("@")
        provider = "输入邮箱地址后识别 IMAP 服务器"
        tone = "occupied"
        kind = None
        if local and "." in domain:
            try:
                default = mailbox_profile(address)
            except UserError:
                default = None
            try:
                profile = mailbox_profile(address, host, port) if host or port not in ("", "993") else default
            except UserError:
                profile = None
            provider_host = default[1] if default else ""
            effective = profile or default
            if effective and effective[1] == OUTLOOK_IMAP_HOST:
                provider, tone = "已识别：Outlook · OAuth2 登录", "info"
            elif provider_host:
                _, server, tls_port = profile or default
                prefix = "自定义" if profile and profile != default else "默认"
                name = MAIL_PROVIDER_NAMES.get(provider_host, provider_host)
                provider = f"已识别：{name} · {prefix} {server}:{tls_port}"
                tone = "info"
            elif profile:
                _, server, tls_port = profile
                provider, tone = f"其他邮箱 · {server}:{tls_port}", "info"
            else:
                provider, tone = "其他邮箱 · 请核对地址和 IMAP 配置", "warning"
            kind = "known" if provider_host else "other"
        self.mail_provider_pill.configure(text=provider)
        self.mail_provider_pill.set_tone(tone)
        if kind or not address:
            other = kind == "other"
            if other != (self._mail_provider_kind == "other"):
                self.mail_advanced.set_expanded(other)
            self._mail_provider_kind = kind

    def _mail_fields_changed(self, *_):
        values = tuple(field.get() for field in self.mail_fields)
        if values == self._saved_mail_fields():
            self._mail_feedback(MAIL_SETTINGS_HINT)
        else:
            self._mail_feedback("有未保存的收票设置", tone="warning")

    def _mail_feedback(self, text, *, tone="occupied"):
        self.mail_feedback.configure(text=text, style=f"{tone}.SettingsFeedback.TLabel")

    def _update_mail_session(self):
        if self.connecting_mail:
            text, tone = "正在连接邮箱…", "info"
        elif self.mailbox is not None:
            text, tone = "已连接", "success"
        else:
            text, tone = "未连接", "occupied"
        self.mail_session_pill.configure(text=text)
        self.mail_session_pill.set_tone(tone)
        can_clear = self.mailbox is not None and not self.busy
        self.mail_clear_button.configure(state="normal" if can_clear else "disabled")

    def save_mail_settings(self):
        if self.busy:
            return
        try:
            address = self.mail_address.get().strip()
            host = self.mail_host.get().strip()
            if address:
                address, _, port = mailbox_profile(address, host, self.mail_port.get().strip())
            else:
                host, port = "", 993
            client_id = self.mail_client_id.get().strip()
            if client_id:
                client_id = validate_client_id(client_id)
            blocked = domain_list(self.mail_blocked.get())
        except UserError as exc:
            self.mail_advanced.set_expanded(True)
            self._mail_feedback(str(exc), tone="error")
            return
        profile_settings = dict(mail_address=address, mail_host=host, mail_port=port, mail_client_id=client_id)
        settings = self.settings | profile_settings | {"mail_blocked_domains": blocked}
        try:
            self._save_settings(settings)
        except OSError:
            self._mail_feedback(f"无法写入本地设置文件。\n{self.settings_file}", tone="error")
            return
        if any(self.settings.get(key, "") != value for key, value in profile_settings.items()):
            self.mailbox = None
            self.mail_folders = ()
        self.settings = settings
        self._load_mail_fields()
        self._update_mail_session()
        self._mail_feedback("✓  收票设置已保存", tone="success")
        self.status_text.set("已保存收票设置。")

    def disconnect_mail(self):
        self.mailbox = None
        self.mail_folders = ()
        self._update_mail_session()
        self._mail_feedback("已清除本次邮箱登录凭证", tone="success")
        self.status_text.set("已清除本次邮箱登录凭证。")

    def connect_import_mail(self, dialog):
        if not self.session_ok or self.busy or not dialog.profile:
            return
        profile = dialog.profile
        address, host, _ = profile
        outlook = host == OUTLOOK_IMAP_HOST
        if self.mailbox and self.mailbox.profile == profile:
            mailbox = self.mailbox
        elif outlook:
            try:
                client_id = configured_client_id(APP_ROOT, local_client_id=self.settings.get("mail_client_id", ""))
                oauth = OutlookOAuth(address, client_id)
            except UserError as exc:
                dialog.show_error(str(exc))
                return
            mailbox = IMAPMailbox(profile, outlook_oauth=oauth)
        else:
            credential = MailCredentialDialog(dialog, address, MAIL_PROVIDER_NAMES.get(host))
            self.wait_window(credential)
            if not dialog.winfo_exists():
                return
            dialog.grab_set()
            password = credential.result
            if not password:
                dialog.confirm.focus_set()
                return
            mailbox = IMAPMailbox(profile, password)
        self.mailbox = None
        self.mail_folders = ()
        login_prompt = "请在浏览器完成 Microsoft 授权" if outlook else "正在连接邮箱…"
        dialog.update_mail(connecting=True, prompt=login_prompt)

        def choose(folders):
            self.mailbox = mailbox
            self.mail_folders = folders
            self._update_mail_session()
            dialog.update_mail(folders=folders)
            self.status_text.set("邮箱已连接。")

        def failed(error):
            dialog.update_mail(message=str(error))
            self.status_text.set("邮箱连接未完成。")

        cancelled = threading.Event()
        self.task(login_prompt, lambda: mailbox.folders(cancelled=cancelled), choose,
                  cancelled=cancelled, connecting_mail=True, failed=failed)

    def start_mail_import(self, folder, source, bounds, *, deep_scan=False, force_rescan=False):
        if not self.session_ok or self.busy or self.mailbox is None:
            return
        store, revision = self.store, self.snapshot["revision"]
        self.pages.select(0)
        if not store.workspace.is_dir():
            self.import_failed("请在设置中选择可访问的工作区存储位置。")
            return
        self.clear_detail()
        self.reset_invoice_filters()
        self.dismiss_import_result()
        mailbox = self.mailbox
        blocked = self.settings.get("mail_blocked_domains", [])
        cancelled = threading.Event()
        task_id, emit = self._task_channel()

        def work():
            return import_mailbox(store, revision, mailbox, folder, source, bounds, blocked,
                                  lambda message: emit("progress", message), deep_scan=deep_scan,
                                  force_rescan=force_rescan, cancelled=cancelled)

        self.task("正在查找发票邮件…", work, self.import_done, failed=self.import_failed,
                  cancelled=cancelled, task_id=task_id, pill="正在收票")

    def recognize_again(self):
        item = self.selected_item()
        if not item or not self.allow_discard():
            return
        store, revision = self.store, self.snapshot["revision"]

        def done(invoice_id):
            if self.tree.exists(invoice_id):
                self.tree.selection_set(invoice_id)
                self.select_invoice()
            self.status_text.set("已重新识别原件，请查看核验结果。")

        self.task("正在重新识别原件…", lambda: store.recognize_again(revision, item["id"]), done, preserve_edits=False)

    def save_company(self):
        if self.store:
            name, tax = self.company_name.get(), self.company_tax.get()
            revision = self.snapshot["revision"]
            self.task("保存公司设置…", lambda: self.store.set_company(revision, name, tax))

    def reset_workspace(self):
        if not self.session_ok or not self.store or self.busy:
            return
        directory = self.store.root
        prompt = (f"系统数据库：{directory}\n\n"
                  "清空发票索引、报销批次、导入记录和设置。\n"
                  "保留工作区位置、原件和导出文件。\n\n"
                  "此操作无法撤销。确认恢复初始化？")
        if not messagebox.askyesno("恢复初始化", prompt, parent=self, icon="warning", default="no"):
            return
        store, revision = self.store, self.snapshot["revision"]
        settings = {"data_directory": str(directory)}
        def work():
            store.reset(revision)
            self._save_settings(settings)
            return settings
        def done(settings):
            self.settings = settings
            self.mailbox = None
            self.mail_folders = ()
            self.dismiss_import_result()
            self._load_mail_fields()
            self._update_mail_session()
            self.current_id = None
            self.clear_detail()
            self.search.set("")
            self.filter.set("全部")
            self.fill_invoices()
            self.fill_batches()
            self.open_settings(SYSTEM_SETTINGS)
            self.status_text.set("系统数据库已恢复初始化。")
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
            self.open_settings(GENERAL_SETTINGS)
            messagebox.showinfo("先设置公司", "请先填写购买方抬头或税号。", parent=self)
            return
        SelectionDialog(self)

    def fill_batches(self):
        previous = self.batch_tree.selection()
        self.batch_tree.delete(*self.batch_tree.get_children())
        batches = self.snapshot.get("batches", [])
        for batch in reversed(batches):
            created = datetime.fromisoformat(batch["created"]).astimezone().strftime("%Y-%m-%d %H:%M")
            self.batch_tree.insert("", "end", iid=batch["id"], values=(created, len(batch["items"]), yuan(batch["target_cents"]), yuan(batch["total_cents"]), yuan(batch["declared_cents"])))
        if previous and self.batch_tree.exists(previous[0]):
            self.batch_tree.selection_set(previous[0])
        self.batch_summary.set(f"共 {len(batches)} 个批次" if batches else "暂无批次。可在「发票资料」按额度选票。")
        self.show_batch()

    def selected_batch(self):
        selection = self.batch_tree.selection()
        return next((b for b in self.snapshot.get("batches", []) if selection and b["id"] == selection[0]), None)

    def show_batch(self, event=None):
        batch = self.selected_batch()
        self.update_batch_export()
        info = self.batch_info
        info.configure(state="normal")
        info.delete("1.0", "end")
        if batch:
            company = batch["company"]
            info.insert("end", "批次编号  ", "caption", batch["id"], "identifier", "\n", (),
                        "报销公司  ", "caption", company["name"] or "—", (),
                        "    税号  ", "caption", company["tax_id"] or "—", "identifier")
        info.configure(state="disabled")
        self.batch_items.delete(*self.batch_items.get_children())
        page = self.batch_details.master
        if not batch:
            self.batch_details.grid_remove()
            page.rowconfigure(2, weight=0, minsize=0)
            return
        for item in batch["items"]:
            live = self.invoices_by_id.get(item["id"], {})
            status = INVOICE_STATUS.get(live.get("reimbursement_status"), "历史记录")
            membership = "占用中" if live.get("active_batch_id") == batch["id"] else "已释放"
            self.batch_items.insert("", "end", values=(item["number"], item["date"], yuan(item["amount_cents"]),
                                                        status, membership))
        page.rowconfigure(2, weight=3, minsize=self.theme.px(135))
        self.batch_details.grid()

    def update_batch_export(self):
        enabled = self.session_ok and not self.busy and bool(self.batch_tree.selection())
        self.batch_export_button.configure(state="normal" if enabled else "disabled")

    def export_batch(self):
        batch = self.selected_batch()
        if not batch or not self.store:
            messagebox.showinfo("选择批次", "请先选择一个报销批次。", parent=self)
            return
        dest = filedialog.asksaveasfilename(title="保存原票与明细 ZIP", initialfile=f"报销材料-{batch['id'][:8]}.zip", defaultextension=".zip", filetypes=[("ZIP", "*.zip")], parent=self)
        if not dest:
            return
        store = self.store
        self.task("正在导出报销材料…", lambda: store.export(batch["id"], destination=dest),
                  lambda p: messagebox.showinfo("导出完成", f"已保存到\n{p}", parent=self),
                  reload_after=False)

    def close_app(self):
        if self.busy and self.task_cancelled is None:
            messagebox.showinfo("正在处理", "请等待当前操作完成后关闭窗口。", parent=self)
            return
        if not self.allow_discard():
            return
        if self.task_cancelled is not None:
            self.task_cancelled.set()
            self.finish_task()
        self.preview.close()
        if self.store:
            self.store.close()
        self.mailbox = None
        self.after_cancel(self.poll_id)
        self.destroy()

    def mainloop(self, n=0):
        # 只有真正运行 Tk 事件循环时才启用后台预览；单步测试和脚本调用保持同步确定性。
        if self.preview_async is not False:
            self.preview.async_enabled = True
        try:
            return super().mainloop(n)
        finally:
            self.preview.async_enabled = False


def main():
    parser = argparse.ArgumentParser(description="个人发票工作台 · 本地 Windows GUI")
    parser.add_argument("--data-dir", type=Path, help="指定系统数据库目录")
    parser.add_argument("--settings-file", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--recover-lock", action="store_true", help="确认旧会话已退出后，手动恢复遗留会话锁")
    args = parser.parse_args()
    app = Workbench(args.data_dir, args.settings_file, recover_lock=args.recover_lock)
    try:
        app.mainloop()
    finally:
        if app.store:
            app.store.close()


if __name__ == "__main__":
    main()
