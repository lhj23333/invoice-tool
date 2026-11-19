from datetime import date, datetime, timedelta
import os
import tkinter as tk
from tkinter import messagebox, ttk

from .domain import UserError
from .mail import date_bounds
from .theme import fit_window
from .widgets import table_scrollers


IMPORT_RESULTS = {"imported": "新增", "merged": "归并", "duplicates": "重复", "error": "失败", "note": "提示"}


def import_summary(report):
    unit = "封邮件" if report["kind"] == "mail" else "个文件"
    pending = {row["invoice_id"] for row in report["items"] if row.get("invoice_id") and row.get("detail")}
    return (f"读取 {report['scanned']} {unit} · 新增 {report['imported']} 张 · 归并 {report['merged']} 个 · "
            f"已有 {report['duplicates']} 个重复文件 · 核验异常 {len(pending)} 张 · 失败 {len(report['errors'])} 项")


class MailScopeDialog(tk.Toplevel):
    def __init__(self, owner, folders, done):
        super().__init__(owner)
        self.title("从邮箱收票")
        self.transient(owner)
        self.resizable(False, False)
        padding = owner.theme.space.medium
        panel = ttk.Frame(self, padding=padding)
        panel.pack(fill="both", expand=True)
        panel.columnconfigure(1, weight=1)
        ttk.Label(panel, text="邮件文件夹").grid(row=0, column=0, sticky="w", padx=(0, padding))
        folder = ttk.Combobox(panel, values=[name for _, name in folders], state="readonly", width=33)
        folder.grid(row=0, column=1, sticky="ew")
        folder.current(0)
        today = date.today()
        dates = []
        for row, (label, value) in enumerate((("开始日期", today - timedelta(days=6)), ("结束日期", today)), 1):
            ttk.Label(panel, text=label).grid(row=row, column=0, sticky="w", pady=(padding, 0))
            entry = ttk.Entry(panel)
            entry.insert(0, value.isoformat())
            entry.grid(row=row, column=1, sticky="ew", pady=(padding, 0))
            dates.append(entry)
        ttk.Label(panel, text="按本机日期，包含起止当天（YYYY-MM-DD）", style="Muted.TLabel").grid(
            row=3, columnspan=2, sticky="w", pady=(padding, 0))

        def submit():
            first, last = (entry.get().strip() for entry in dates)
            try:
                bounds = date_bounds(first, last)
            except UserError as exc:
                messagebox.showerror("日期无效", str(exc), parent=self)
                return
            folder_id, name = folders[folder.current()]
            self.destroy()
            done(folder_id, f"{name} · {first} 至 {last}", bounds)

        ttk.Button(panel, text="开始收票", style="Primary.TButton", command=submit).grid(row=4, column=1,
                                                                                  sticky="e", pady=(padding, 0))
        self.grab_set()


class ImportHistory(tk.Toplevel):
    def __init__(self, owner, reports, selected=None):
        super().__init__(owner)
        self.title("导入记录")
        fit_window(self, owner.theme, 850, 540, 560, 360)
        self.transient(owner)
        padding = owner.theme.space.medium
        panel = ttk.Frame(self, padding=padding)
        panel.pack(fill="both", expand=True)
        labels = [f"{datetime.fromisoformat(report['at']).astimezone():%Y-%m-%d %H:%M} · {report['source']}"
                  for report in reports]
        selector = ttk.Combobox(panel, values=labels, state="readonly", width=1)
        selector.pack(fill="x")
        summary = tk.StringVar(self)
        ttk.Label(panel, textvariable=summary, style="Muted.TLabel").pack(anchor="w", pady=(padding, 0))
        rows = ttk.Frame(panel)
        rows.pack(fill="both", expand=True, pady=padding)
        tree = ttk.Treeview(rows, columns=("result", "file", "subject"), show="headings", selectmode="browse")
        for key, label, width in (("result", "结果", 86), ("file", "文件或链接", 280), ("subject", "邮件主题", 250)):
            tree.heading(key, text=label)
            tree.column(key, width=owner.theme.px(width), stretch=key != "result")
        table_scrollers(rows, tree)
        detail = tk.Text(panel, height=5, wrap="word", state="disabled")
        detail.pack(fill="x")
        current = {}
        workspace = owner.store

        def open_mail():
            original = current.get("mail_object")
            if not original:
                return
            try:
                if workspace is not owner.store or not owner.session_ok:
                    raise UserError("请重新打开此资料库，再查看原邮件。")
                os.startfile(str(workspace.original(original)))
            except (UserError, OSError) as exc:
                messagebox.showerror("无法打开原邮件", str(exc), parent=self)

        open_button = ttk.Button(panel, text="打开原邮件", command=open_mail, state="disabled")
        open_button.pack(anchor="e", pady=(padding, 0))

        def show_detail(_=None):
            index = selector.current()
            selection = tree.selection()
            current.clear()
            if index >= 0 and selection:
                current.update(reports[index]["items"][int(selection[0])])
            message = [current[key] for key in ("mailbox", "folder", "received", "sender", "subject", "detail") if current.get(key)]
            if current.get("xml_analysis"):
                message.append("仅保留 XML 字段分析结果。")
            detail.configure(state="normal")
            detail.delete("1.0", "end")
            detail.insert("1.0", "\n".join(message))
            detail.configure(state="disabled")
            open_button.configure(state="normal" if current.get("mail_object") else "disabled")

        def show_report(_=None):
            tree.delete(*tree.get_children())
            if reports:
                report = reports[selector.current()]
                summary.set(import_summary(report))
                for index, row in enumerate(report["items"]):
                    label = IMPORT_RESULTS[row["result"]]
                    if row.get("invoice_id") and row.get("detail"):
                        label += " · 异常"
                    tree.insert("", "end", iid=str(index), values=(label, row["file"], row.get("subject", "")))
                if tree.get_children():
                    tree.selection_set(tree.get_children()[0])
            else:
                summary.set("暂无导入记录。")
            show_detail()

        selector.bind("<<ComboboxSelected>>", show_report)
        tree.bind("<<TreeviewSelect>>", show_detail)
        if reports:
            selector.current(next((i for i, report in enumerate(reports) if report["id"] == selected), 0))
        show_report()
