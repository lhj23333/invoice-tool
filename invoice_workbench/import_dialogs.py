from datetime import date, datetime, timedelta
import os
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from .domain import UserError
from .extract import SUPPORTED
from .mail import date_bounds
from .theme import COLORS, FONT, wrap_label
from .widgets import ScrollableFrame, SegmentedControl, SegmentedPages, StatusPill, WorkbenchDialog, hover_rows, table_scrollers


IMPORT_RESULTS = {"imported": "新增", "merged": "归并", "duplicates": "重复", "error": "失败", "note": "提示",
                  "review": "待审核", "reviewed": "已确认", "dismissed": "已排除"}
RESULT_TONES = {"imported": "success", "merged": "info", "duplicates": "occupied", "error": "error", "note": "info",
                "review": "warning", "reviewed": "success", "dismissed": "occupied"}
MAIL_MODE, LOCAL_MODE = "从邮箱收票", "从本地导入"
HISTORY_FILTERS = ("全部", "仅看异常", "待审核", "邮箱收票", "本地导入")


def import_summary(report):
    unit = "封邮件" if report["kind"] == "mail" else "个文件"
    pending = {row["invoice_id"] for row in report["items"] if row.get("invoice_id") and row.get("detail")}
    screened = ""
    if stats := report.get("mail_screening"):
        screened = (f" · 候选 {stats['candidates']} 封 · 已过滤 {stats['filtered']} 封"
                    f" · 待深度扫描 {stats['deferred']} 个附件")
        if stats["partial_bodies"]:
            screened += f" · {stats['partial_bodies']} 段正文未读完"
        if stats.get("reused"):
            screened += f" · 复用 {stats['reused']} 封"
    reviews = len({row["review_id"] for row in report["items"] if row["result"] == "review"})
    if reviews:
        screened += f" · 待审核 {reviews} 项"
    return (f"读取 {report['scanned']} {unit}{screened} · 新增 {report['imported']} 张 · 归并 {report['merged']} 份 · "
            f"已有 {report['duplicates']} 项 · 核验异常 {len(pending)} 张 · 失败 {len(report['errors'])} 项")


class MailCredentialDialog(WorkbenchDialog):
    def __init__(self, owner, address, provider=None):
        super().__init__(owner, "邮箱登录")
        self.result = None
        space = self.theme.space
        account = ttk.Label(self.body, text=address, style="Section.TLabel", width=1)
        account.pack(fill="x")
        wrap_label(account)
        provider = provider or "邮箱服务商"
        hint = ttk.Label(self.body, text=f"请使用 {provider} 提供的凭证，仅在本次运行期间保留。",
                         style="Muted.TLabel", width=1, justify="left")
        hint.pack(fill="x", pady=(space.tiny, space.medium))
        wrap_label(hint)
        ttk.Label(self.body, text="客户端授权码或应用专用密码").pack(anchor="w", pady=(0, space.tiny))
        field = ttk.Frame(self.body)
        field.pack(fill="x")
        self.visibility = ttk.Button(field, text="显示", command=self._toggle_visibility)
        self.visibility.pack(side="right", padx=(space.small, 0))
        self.entry = ttk.Entry(field, width=1, show="*")
        self.entry.pack(side="left", fill="x", expand=True)
        self.error = ttk.Label(self.body, style="error.SettingsFeedback.TLabel", width=1)
        self.add_actions("连接邮箱", self._accept)
        self.entry.bind("<Return>", self._accept)
        self.show(500, 300, 390, 285, focus=self.entry, modal=True)

    def _toggle_visibility(self):
        hidden = bool(self.entry.cget("show"))
        self.entry.configure(show="" if hidden else "*")
        self.visibility.configure(text="隐藏" if hidden else "显示")

    def _accept(self, event=None):
        password = self.entry.get()
        if password.strip():
            self.result = password
            self.destroy()
        else:
            self.error.configure(text="请填写邮箱客户端授权码或应用专用密码。")
            self.error.pack(fill="x", pady=(self.theme.space.small, 0))
            self.entry.focus_set()
        return "break"


class ImportDialog(WorkbenchDialog):
    def __init__(self, owner, mode, *, workspace, recent, profile, provider, mail_notice):
        super().__init__(owner, "导入发票")
        space = self.theme.space
        self.workspace = workspace
        self.recent = recent
        self.files = ()
        self.scan_workspace = False
        self.profile = profile
        self.mail_notice = mail_notice
        self.connecting = False
        self.folders = ()
        self.pages = SegmentedPages(self.body, (MAIL_MODE, LOCAL_MODE), theme=self.theme, capsule=True)
        self.pages.pack(fill="both", expand=True)
        self.pages.bind("<<SegmentedPageChanged>>", self._changed)
        self.error = ttk.Label(self.body, style="error.SettingsFeedback.TLabel", width=1, justify="left")
        wrap_label(self.error, inset=2 * space.small)
        for page, build in zip(self.pages.frames, (lambda panel: self._mail_form(panel, provider, mail_notice),
                                                   lambda panel: self._local_form(panel, recent))):
            scroll = ScrollableFrame(page, theme=self.theme)
            scroll.pack(fill="both", expand=True)
            build(scroll.content)
            scroll.enable_scrolling()
        target = ttk.Frame(self.actions)
        target.pack(fill="x", pady=(0, space.small))
        self.change_target = ttk.Button(target, text="修改", style="Link.TButton",
                                        command=lambda: owner.edit_archive_target(self))
        self.change_target.pack(side="right")
        self.archive_target = ttk.Label(target, text=f"归档位置：{workspace}",
                                        style="Muted.TLabel", width=1, justify="left")
        self.archive_target.pack(side="left", fill="x", expand=True)
        wrap_label(self.archive_target)
        self.confirm = ttk.Button(self.actions, style="Primary.TButton", command=self._submit)
        self.confirm.pack(side="right")
        self.cancel = ttk.Button(self.actions, text="取消", command=self.destroy)
        self.cancel.pack(side="right", padx=(0, space.small))
        self.pages.select(self.pages.labels.index(mode))
        self._changed()
        self.show(660, 535, 500, 380, focus=self.pages.control.buttons[self.pages.current_index], modal=True)

    def _mail_form(self, panel, provider, notice):
        space = self.theme.space
        card = ttk.Frame(panel, style="SettingsCard.TFrame", padding=space.small)
        card.pack(fill="x", pady=(0, space.medium))
        settings = ttk.Frame(card)
        settings.pack(side="right", anchor="n")
        self.change_settings = ttk.Button(settings, text="更换设置", style="Quiet.TButton",
                                         command=lambda: self.master.edit_import_settings(self))
        self.change_settings.pack(side="top", anchor="e")
        self.refresh_folders = ttk.Button(settings, text="刷新文件夹", style="Quiet.TButton",
                                          command=lambda: self.master.connect_import_mail(self))
        account = ttk.Label(card, text=provider if self.profile else "邮箱配置", style="Section.TLabel", width=1)
        account.pack(fill="x")
        wrap_label(account)
        address = ttk.Label(card, text=self.master.settings.get("mail_address") or "尚未保存邮箱地址",
                            style="Muted.TLabel", width=1, justify="left")
        address.pack(fill="x")
        wrap_label(address)
        self.mail_status = StatusPill(card, text="○ 未连接")
        self.mail_status.pack(anchor="w", pady=(space.tiny, 0))
        if notice:
            note = ttk.Label(panel, text=notice, style="warning.SettingsFeedback.TLabel", width=1, justify="left")
            note.pack(fill="x", pady=(0, space.small))
            wrap_label(note, inset=2 * space.small)
        ttk.Label(panel, text="邮件文件夹").pack(anchor="w", pady=(0, space.tiny))
        self.folder_choice = ttk.Combobox(panel, state="disabled", width=1)
        self.folder_choice.pack(fill="x")
        self.folder_choice.set("连接邮箱后选择")
        today = date.today()
        self.today = today.isoformat()
        self.ranges = {"近 7 天": (today - timedelta(days=6)).isoformat(),
                       "近 30 天": (today - timedelta(days=29)).isoformat(),
                       "本月": today.replace(day=1).isoformat()}
        self.start_date = tk.StringVar(self, value=self.ranges["近 7 天"])
        self.end_date = tk.StringVar(self, value=self.today)
        self.presets = SegmentedControl(panel, (*self.ranges, "自定义"), theme=self.theme, capsule=True,
                                        command=self._apply_range)
        self.presets.pack(anchor="w", pady=(space.medium, space.small))
        dates_row = ttk.Frame(panel)
        dates_row.pack(fill="x", pady=(0, space.small))
        self.date_entries = []
        for column, (label, value) in enumerate((("开始日期", self.start_date), ("结束日期", self.end_date))):
            dates_row.columnconfigure(column, weight=1, uniform="date_fields")
            field = ttk.Frame(dates_row)
            field.grid(row=0, column=column, sticky="ew", padx=(0, space.medium if column == 0 else 0))
            ttk.Label(field, text=label).pack(anchor="w", pady=(0, space.tiny))
            entry = ttk.Entry(field, textvariable=value, width=1)
            entry.pack(fill="x")
            entry.bind("<Return>", self._submit)
            entry.bind("<MouseWheel>", lambda event, variable=value: self._scroll_date(event, variable))
            self.date_entries.append(entry)
        hint = ttk.Label(panel, text="可用滚轮调整日期，也可输入 YYYY-MM-DD。包含起止当天。",
                         style="info.SettingsFeedback.TLabel", width=1, justify="left")
        hint.pack(fill="x")
        wrap_label(hint, inset=2 * space.small)
        self.deep_scan = tk.BooleanVar(self, value=False)
        self.deep_scan_option = ttk.Checkbutton(panel, text="深度扫描无线索的图片和扫描件", variable=self.deep_scan)
        self.deep_scan_option.pack(anchor="w", pady=(space.small, 0))
        self.force_rescan = tk.BooleanVar(self, value=False)
        ttk.Checkbutton(panel, text="重新扫描已处理邮件", variable=self.force_rescan).pack(anchor="w", pady=(space.tiny, 0))
        self.start_date.trace_add("write", self._edit_dates)
        self.end_date.trace_add("write", self._edit_dates)

    def _local_form(self, panel, recent):
        space = self.theme.space
        ttk.Label(panel, text="导入来源", style="Section.TLabel").pack(anchor="w", pady=(0, space.tiny))
        self.directory = tk.StringVar(self)
        self.origin_text = tk.StringVar(self, value="请选择文件夹或文件")
        row = ttk.Frame(panel)
        row.pack(fill="x")
        self.choose_directory = ttk.Button(row, text="选择文件夹…", command=self._browse)
        self.choose_directory.pack(side="left")
        self.choose_files = ttk.Button(row, text="选择文件…", command=self._browse_files)
        self.choose_files.pack(side="left", padx=(space.small, 0))
        origin = ttk.Label(panel, textvariable=self.origin_text, style="Muted.TLabel", width=1, justify="left")
        origin.pack(fill="x", pady=(space.small, 0))
        wrap_label(origin)
        choices = ttk.Frame(panel)
        choices.pack(fill="x", pady=(space.small, space.medium))
        self.recent_directory = ttk.Button(choices, text="使用最近目录", style="Quiet.TButton",
                                           command=lambda: self._set_directory(recent),
                                           state="normal" if recent and Path(recent).is_dir() else "disabled")
        self.recent_directory.pack(side="left")
        self.scan_button = ttk.Button(choices, text="扫描当前工作区新增文件", style="Quiet.TButton",
                                      command=lambda: self._set_directory(self.workspace, scan_workspace=True))
        self.scan_button.pack(side="left", padx=(space.tiny, 0))
        formats = ttk.Frame(panel)
        formats.pack(fill="x")
        for name in ("PDF", "XML", "OFD", "PNG/JPG", "EML"):
            StatusPill(formats, text=name).pack(side="left", padx=(0, space.tiny))
        hint = ttk.Label(panel, text="扫描包含子文件夹。也可一次选择多个文件。",
                         style="Muted.TLabel", width=1, justify="left")
        hint.pack(fill="x", pady=(space.small, 0))
        wrap_label(hint)

    def _browse(self):
        path = filedialog.askdirectory(parent=self, title="选择发票文件夹",
                                       initialdir=self._initial_directory(), mustexist=True)
        if path:
            self._set_directory(path)

    def _initial_directory(self):
        candidates = (self.directory.get(), self.recent, self.workspace)
        return str(next((p for p in candidates if p and Path(p).is_dir()), Path.home()))

    def _browse_files(self):
        pattern = " ".join(f"*{ext}" for ext in sorted(SUPPORTED | {".eml"}))
        files = filedialog.askopenfilenames(parent=self, title="选择发票文件", initialdir=self._initial_directory(),
                                            filetypes=[("发票文件", pattern)])
        if files:
            self._set_files(files)

    def _set_files(self, files):
        self.files = tuple(files)
        self.scan_workspace = False
        self.directory.set("")
        first = Path(files[0]).name
        caption = first if len(files) == 1 else f"已选 {len(files)} 个文件 · {first} 等"
        self.origin_text.set(caption)
        self._changed()

    def _set_directory(self, path, *, scan_workspace=False):
        self.files = ()
        self.scan_workspace = scan_workspace or Path(path).resolve() == self.workspace
        self.directory.set(str(path))
        self.origin_text.set(f"扫描工作区 · {path}" if self.scan_workspace else str(path))
        self._changed()

    def _edit_dates(self, *_):
        self.presets.variable.set("自定义")
        self.error.pack_forget()

    @staticmethod
    def _scroll_date(event, variable):
        days = int(event.delta / 120)
        if days:
            try:
                current = date.fromisoformat(variable.get().strip())
            except ValueError:
                return "break"
            ordinal = min(date.max.toordinal(), max(date.min.toordinal(), current.toordinal() + days))
            if ordinal != current.toordinal():
                variable.set(date.fromordinal(ordinal).isoformat())
        return "break"

    def _apply_range(self):
        label = self.presets.variable.get()
        if label == "自定义":
            self.date_entries[0].focus_set()
        else:
            self.start_date.set(self.ranges[label])
            self.end_date.set(self.today)
            self.presets.variable.set(label)

    def update_mail(self, *, connecting=False, folders=(), message=None, prompt=""):
        self.connecting = connecting
        self.folders = folders
        self.mail_status.configure(text=prompt if connecting else "● 已连接" if folders else "○ 未连接")
        self.mail_status.set_tone("info" if connecting else "success" if folders else "occupied")
        if folders:
            self.folder_choice.configure(state="readonly", values=[name for _, name in folders])
            self.folder_choice.current(0)
            self.refresh_folders.pack(side="top", anchor="e")
        else:
            self.folder_choice.configure(state="disabled", values=())
            self.folder_choice.set("连接邮箱后选择")
            self.refresh_folders.pack_forget()
        self._changed()
        if message:
            self.show_error(message)

    def show_error(self, message):
        self.error.configure(text=message)
        self.error.pack(side="bottom", fill="x", pady=(self.theme.space.tiny, 0), before=self.pages)

    def _changed(self, event=None):
        self.error.pack_forget()
        if self.pages.current_index == 0:
            caption = "开始收票" if self.folders else "连接邮箱"
            disabled = not self.profile or not self.workspace.is_dir()
        else:
            caption = "开始导入"
            disabled = not self.workspace.is_dir() or not (self.directory.get() or self.files)
        self.confirm.configure(text=caption, state="disabled" if self.connecting or disabled else "normal")

    def _submit(self, event=None):
        if self.confirm.instate(["disabled"]):
            return "break"
        if self.pages.current_index == 1:
            source = Path(self.directory.get()).resolve() if self.directory.get() else None
            if source is not None and not source.is_dir():
                self.show_error("文件夹已不可用，请重新选择。")
            elif self.master.allow_discard(parent=self):
                self.destroy()
                self.master.start_import(source, files=self.files, scan_workspace=self.scan_workspace)
        elif not self.folders:
            self.master.connect_import_mail(self)
        else:
            first, last = self.start_date.get().strip(), self.end_date.get().strip()
            try:
                bounds = date_bounds(first, last)
            except UserError as exc:
                self.show_error(str(exc))
                return "break"
            if self.master.allow_discard(parent=self):
                folder, name = self.folders[self.folder_choice.current()]
                deep_scan = self.deep_scan.get()
                force_rescan = self.force_rescan.get()
                self.destroy()
                self.master.start_mail_import(folder, f"{name} · {first} 至 {last}", bounds,
                                              deep_scan=deep_scan, force_rescan=force_rescan)
        return "break"


class ImportHistory(WorkbenchDialog):
    def __init__(self, owner, reports, selected=None):
        super().__init__(owner, "导入记录")
        self.workspace = owner.store
        self.reports = {report["id"]: report for report in reports}
        self.visible_items = {}
        self.current_batch = None
        space, p = self.theme.space, self.theme.px
        toolbar = ttk.Frame(self.body)
        toolbar.pack(fill="x", pady=(0, space.medium))
        self.filters = SegmentedControl(toolbar, HISTORY_FILTERS, theme=self.theme, capsule=True, command=self._show_batches)
        self.filters.pack(side="left")
        self.search = tk.StringVar(self)
        searchbar = ttk.Frame(toolbar)
        searchbar.pack(side="right", fill="x", expand=True, padx=(space.medium, 0))
        ttk.Label(searchbar, text="搜索", style="Muted.TLabel").pack(side="left", padx=(0, space.small))
        ttk.Button(searchbar, text="×", style="Quiet.TButton", command=lambda: self.search.set("")).pack(side="right")
        self.search_entry = ttk.Entry(searchbar, textvariable=self.search, width=1)
        self.search_entry.pack(fill="x", expand=True)

        split = ttk.Frame(self.body)
        split.pack(fill="both", expand=True)
        split.columnconfigure(0, minsize=p(240), weight=1)
        split.columnconfigure(1, minsize=p(315), weight=3)
        split.rowconfigure(0, weight=1)
        batches = ttk.Frame(split)
        batches.grid(row=0, column=0, sticky="nsew", padx=(0, space.medium))
        self.batch_tree = ttk.Treeview(batches, columns=("time",), show=("tree", "headings"),
                                       selectmode="browse", height=2, style="BatchHistory.Treeview")
        self.batch_tree.heading("#0", text="导入批次")
        self.batch_tree.column("#0", width=p(165), minwidth=p(152), stretch=False)
        self.batch_tree.heading("time", text="时间")
        self.batch_tree.column("time", width=p(98), minwidth=p(84))
        table_scrollers(batches, self.batch_tree)
        hover_rows(self.batch_tree)
        right = ttk.Frame(split)
        right.grid(row=0, column=1, sticky="nsew")
        self.source = tk.StringVar(self)
        ttk.Entry(right, textvariable=self.source, state="readonly", width=1).pack(fill="x")
        self.summary = ttk.Label(right, style="Muted.TLabel", width=1, justify="left")
        self.summary.pack(fill="x", pady=(space.tiny, space.small))
        wrap_label(self.summary)
        detail_box = ttk.Frame(right, style="Surface.TFrame", padding=space.small)
        detail_box.pack(side="bottom", fill="x", pady=(space.small, 0))
        detail_header = ttk.Frame(detail_box, style="Surface.TFrame")
        detail_header.pack(fill="x", pady=(0, space.tiny))
        self.detail_status = StatusPill(detail_header)
        self.detail_status.pack(side="left")
        self.locate_button = ttk.Button(detail_header, text="定位到发票", command=self._locate, state="disabled")
        self.locate_button.pack(side="right")
        self.open_mail_button = ttk.Button(detail_header, text="打开原邮件", command=self._open_mail, state="disabled")
        self.open_mail_button.pack(side="right", padx=(0, space.small))
        detail_area = ttk.Frame(detail_box, style="Surface.TFrame")
        detail_area.pack(fill="x")
        self.detail = tk.Text(detail_area, height=3, width=1, wrap="word", state="disabled", relief="flat",
                              borderwidth=0, padx=space.small, pady=space.tiny, background=COLORS.surface,
                              foreground=COLORS.secondary, font=(FONT, 10))
        scroll = ttk.Scrollbar(detail_area, orient="vertical", command=self.detail.yview)
        scroll.pack(side="right", fill="y")
        self.detail.configure(yscrollcommand=scroll.set)
        self.detail.pack(fill="x", expand=True)
        self.review_actions = ttk.Frame(detail_box, style="Surface.TFrame")
        self.review_open = ttk.Button(self.review_actions, text="查看附件", command=self._open_review)
        self.review_verify = ttk.Button(self.review_actions, text="核验附件", command=self._resolve_review)
        supplement = ttk.Button(self.review_actions, text="补充原件", command=lambda: self._resolve_review(supplement=True))
        dismiss = ttk.Button(self.review_actions, text="排除", command=lambda: self._resolve_review(dismiss=True))
        for button in (self.review_open, self.review_verify, supplement, dismiss):
            button.pack(side="left", padx=(0, space.tiny))
        self.feedback = ttk.Label(detail_box, style="error.SettingsFeedback.TLabel", width=1, justify="left")
        wrap_label(self.feedback, inset=2 * space.small)
        rows = ttk.Frame(right)
        rows.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(rows, columns=("file", "subject"), show=("tree", "headings"), selectmode="browse", height=2)
        for key, label, width in (("#0", "结果", 94), ("file", "文件或链接", 230), ("subject", "邮件主题", 155)):
            self.tree.heading(key, text=label)
            self.tree.column(key, width=p(width), minwidth=p(80), stretch=key != "#0")
        table_scrollers(rows, self.tree)
        hover_rows(self.tree)

        self.count = ttk.Label(self.actions, style="Muted.TLabel")
        self.count.pack(side="left")
        close_button = ttk.Button(self.actions, text="关闭", command=self.destroy)
        close_button.pack(side="right")
        self.search.trace_add("write", lambda *_: self._show_batches())
        self.batch_tree.bind("<<TreeviewSelect>>", self._show_rows)
        self.tree.bind("<<TreeviewSelect>>", self._show_detail)
        self._show_batches(selected=selected)
        self.show(980, 575, 705, 390, focus=self.search_entry)

    @staticmethod
    def _has_issue(row):
        return row["result"] == "error" or bool(row.get("invoice_id") and row.get("detail"))

    def _result_style(self, row):
        result = row["result"]
        if result != "error" and self._has_issue(row):
            return f"{IMPORT_RESULTS[result]} · 异常", "warning"
        return IMPORT_RESULTS[result], RESULT_TONES[result]

    def _badges(self, *badges):
        if self.theme.status_font:
            return {"image": self.theme.badge_strip(*badges)}
        return {"text": " · ".join(text for text, _ in badges)}

    def _show_batches(self, *, selected=None):
        selected = selected or next(iter(self.batch_tree.selection()), None)
        self.batch_tree.delete(*self.batch_tree.get_children())
        self.visible_items.clear()
        query = self.search.get().strip().casefold()
        mode = self.filters.variable.get()
        shown_reviews = set()
        for report_id, report in self.reports.items():
            mail = report["kind"] == "mail"
            if (mode == "邮箱收票" and not mail) or (mode == "本地导入" and mail):
                continue
            time = datetime.fromisoformat(report["at"]).astimezone()
            report_match = query in f"{report['source']} {time:%Y-%m-%d %H:%M}".casefold()
            visible = {}
            for index, row in enumerate(report["items"]):
                if mode == "仅看异常" and not self._has_issue(row):
                    continue
                if mode == "待审核" and (row["result"] != "review" or row.get("review_id") in shown_reviews):
                    continue
                if report_match or any(query in str(row.get(key, "")).casefold()
                                       for key in ("file", "subject", "sender", "mailbox", "detail")):
                    visible[str(index)] = row
                    if mode == "待审核":
                        shown_reviews.add(row.get("review_id"))
            if not visible and (mode in {"仅看异常", "待审核"} or not report_match):
                continue
            self.visible_items[report_id] = visible
            issues = sum(self._has_issue(row) for row in report["items"])
            badges = self._badges(("邮件" if mail else "本地", "info" if mail else "occupied"),
                                  (f"入库 {report['imported']}", "success"),
                                  (f"异常 {issues}", "warning" if issues else "occupied"))
            self.batch_tree.insert("", "end", iid=report_id, values=(f"{time:%m-%d %H:%M}",), **badges)
        if self.visible_items:
            batch = selected if selected in self.visible_items else next(iter(self.visible_items))
            self.batch_tree.selection_set(batch)
            self.batch_tree.focus(batch)
            self.batch_tree.see(batch)
        self._show_rows()

    def _show_rows(self, event=None):
        batch = next(iter(self.batch_tree.selection()), None)
        if event is not None and batch == self.current_batch:
            return
        selected = next(iter(self.tree.selection()), None) if batch == self.current_batch else None
        self.current_batch = batch
        self.tree.delete(*self.tree.get_children())
        report = self.reports.get(batch)
        self.source.set(report["source"] if report else "")
        self.summary.configure(text=import_summary(report) if report else
                               "没有匹配的导入记录。" if self.reports else "暂无导入记录。")
        rows = self.visible_items.get(batch, {})
        for row_id, row in rows.items():
            self.tree.insert("", "end", iid=row_id, values=(row["file"], row.get("subject", "")),
                             **self._badges(self._result_style(row)))
        if rows:
            item = selected if selected in rows else next(iter(rows))
            self.tree.selection_set(item)
            self.tree.focus(item)
            self.tree.see(item)
        self.count.configure(text=f"{len(self.visible_items)} 个批次 · 当前 {len(rows)} 项明细")
        self._show_detail()

    def _show_detail(self, event=None):
        selected = next(iter(self.tree.selection()), None)
        row = self.visible_items.get(self.current_batch, {}).get(selected, {})
        self.current_row = row
        self.feedback.pack_forget()
        label, tone = self._result_style(row) if row else ("无明细", "occupied")
        self.detail_status.configure(text=label)
        self.detail_status.set_tone(tone)
        message = [row[key] for key in ("file", "mailbox", "folder", "received", "sender", "subject", "detail") if row.get(key)]
        if row.get("review_context"):
            message.append(row["review_context"])
        if row.get("xml_analysis"):
            message.append("仅保留 XML 字段分析结果。")
        self.detail.configure(state="normal")
        self.detail.delete("1.0", "end")
        self.detail.insert("1.0", "\n".join(message))
        self.detail.configure(state="disabled")
        same_workspace = self.workspace is self.master.store and self.master.session_ok
        self.review_actions.pack_forget()
        if same_workspace and row.get("result") == "review" and row.get("review_id"):
            self.review_actions.pack(fill="x", pady=(self.theme.space.tiny, 0))
            original = row.get("review_object")
            self.review_open.configure(state="normal" if original else "disabled")
            self.review_verify.configure(state="normal" if original and not original["path"].endswith(".xml") else "disabled")
        self.open_mail_button.configure(state="normal" if same_workspace and row.get("mail_object") else "disabled")
        can_locate = same_workspace and row.get("invoice_id") in self.master.invoices_by_id
        self.locate_button.configure(state="normal" if can_locate else "disabled")

    def _open_mail(self):
        original = self.current_row.get("mail_object")
        if not original:
            return
        try:
            if self.workspace is not self.master.store or not self.master.session_ok:
                raise UserError("请重新打开此资料库，再查看原邮件。")
            os.startfile(str(self.workspace.original(original)))
        except (UserError, OSError) as exc:
            self.feedback.configure(text=str(exc))
            self.feedback.pack(fill="x", pady=(self.theme.space.tiny, 0))

    def _open_review(self):
        if self.workspace is self.master.store and self.master.session_ok and (file := self.current_row.get("review_object")):
            try:
                os.startfile(str(self.workspace.original(file)))
            except (UserError, OSError) as exc:
                self._review_failed(exc)

    def _review_failed(self, error):
        if self.winfo_exists():
            self.feedback.configure(text=str(error))
            self.feedback.pack(fill="x", pady=(self.theme.space.tiny, 0))

    def _resolve_review(self, *, supplement=False, dismiss=False):
        if self.workspace is not self.master.store or not self.master.session_ok or self.master.busy:
            return
        review_id = self.current_row.get("review_id")
        if not review_id or self.current_row.get("result") != "review":
            return
        original = None
        if supplement:
            original = filedialog.askopenfilename(parent=self, title="选择发票原件",
                                                  filetypes=[("发票原件", "*.pdf *.ofd *.png *.jpg *.jpeg")])
            if not original:
                return
        if dismiss and not messagebox.askyesno("排除待审核材料", "将这项材料从待审核列表移除？", parent=self):
            return
        revision = self.master.snapshot["revision"]

        def refresh(_):
            if self.winfo_exists():
                self.reports = {report["id"]: report for report in self.workspace.import_history()}
                self._show_batches()

        self.master.task("正在处理待审核材料…",
                         lambda: self.workspace.decide_mail_review(revision, review_id, original=original, dismiss=dismiss),
                         refresh, failed=self._review_failed)

    def _locate(self):
        if self.workspace is self.master.store and self.master.locate_imported_invoice(
                self.current_row.get("invoice_id"), parent=self):
            self.destroy()
            self.master.tree.focus_set()
