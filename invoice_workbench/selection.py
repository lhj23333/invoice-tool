from __future__ import annotations

import tkinter as tk
from tkinter import messagebox, ttk
from typing import TYPE_CHECKING

from .domain import UserError, money, yuan
from .labels import selectable
from .store import uid
from .theme import COLORS, FONT, MONO, fit_window, wrap_label
from .widgets import hover_rows, table_scrollers

if TYPE_CHECKING:
    from .gui import Workbench


CONSTRAINT_LABELS = {"自动": "○ 自动", "必选": "● 必选", "排除": "× 排除"}


class SelectionDialog(tk.Toplevel):
    def __init__(self, owner: Workbench):
        super().__init__(owner, background=COLORS.canvas)
        self.owner = owner
        self.theme = owner.theme
        p = self.theme.px
        tiny, small, medium, large, extra_large = self.theme.space
        self.title("按额度选票 · 整票占用，超额不结转")
        fit_window(self, self.theme, 960, 700, 840, 600)
        self.transient(owner)
        self.grab_set()
        self.result = None
        self.manually_adjusted = False
        self.store = owner.store
        self.revision = owner.snapshot["revision"]
        self.workspace_stale = False
        self.request_id = uid()
        self.condition_version = 0
        self.watch_id = None
        self.candidates = [i for i in owner.snapshot["invoices"] if selectable(i)]
        self.constraints = {i["id"]: "自动" for i in self.candidates}
        self.selected_ids = []
        self.target = tk.StringVar(self, value="1000.00")
        self.declared = tk.StringVar(self, value="1000.00")
        self.summary = tk.StringVar(self)
        self.progress_text = tk.StringVar(self)
        self.difference_text = tk.StringVar(self)
        self.source_text = tk.StringVar(self, value="等待计算")
        self.notice_text = tk.StringVar(self)
        panel = ttk.Frame(self, padding=(extra_large, medium))
        panel.pack(fill="both", expand=True)

        dashboard = ttk.Frame(panel, style="Surface.TFrame", padding=(medium, small))
        dashboard.pack(fill="x", pady=(0, small))
        dashboard.columnconfigure(1, weight=1)
        target_panel = ttk.Frame(dashboard, style="Surface.TFrame")
        target_panel.grid(row=0, column=0, sticky="w", padx=(0, extra_large))
        target_label = ttk.Label(target_panel, text="目标金额 / 元", style="SurfaceMuted.TLabel")
        target_label.pack(anchor="w", pady=(0, tiny))
        self.target_entry = ttk.Entry(target_panel, textvariable=self.target, width=13,
                                      font=(MONO, 22), style="Amount.TEntry", justify="right")
        self.target_entry.pack(fill="x")
        progress_panel = ttk.Frame(dashboard, style="Surface.TFrame")
        progress_panel.grid(row=0, column=1, sticky="ew")
        badges = ttk.Frame(progress_panel, style="Surface.TFrame")
        badges.pack(fill="x")
        self.source_badge = tk.Label(badges, textvariable=self.source_text, bg=COLORS.occupied_bg,
                                     fg=COLORS.secondary, font=(FONT, 9), padx=small, pady=tiny)
        self.source_badge.pack(side="left")
        self.difference_badge = tk.Label(badges, textvariable=self.difference_text, font=(FONT, 9), padx=small, pady=tiny)
        self.difference_badge.pack(side="left", padx=(tiny, 0))
        self.progress_label = ttk.Label(progress_panel, textvariable=self.progress_text, style="SurfaceMuted.TLabel")
        self.progress_label.pack(fill="x", pady=(tiny, 0))
        wrap_label(self.progress_label)
        self.progress_canvas = tk.Canvas(progress_panel, bg=COLORS.surface, highlightthickness=0, height=medium, width=1)
        self.progress_canvas.pack(fill="x")
        self.progress_canvas.bind("<Configure>", lambda event: self.draw_progress())

        bottom = ttk.Frame(panel)
        bottom.pack(side="bottom", fill="x", pady=(small, 0))
        summary = ttk.Label(bottom, textvariable=self.summary, style="Muted.TLabel")
        summary.pack(fill="x", pady=(0, tiny))
        wrap_label(summary)
        self.notice = tk.Label(bottom, textvariable=self.notice_text, anchor="w", justify="left",
                               font=(FONT, 9), bg=COLORS.warning_bg, fg=COLORS.warning,
                               padx=small, pady=tiny, width=1)
        self.notice.bind("<Configure>", lambda event: self.notice.configure(wraplength=max(1, event.width - 2 * small)))
        self.action_row = ttk.Frame(bottom)
        self.action_row.pack(fill="x")
        self.confirm = ttk.Button(self.action_row, text="确认批次并占用整票", command=self.commit, state="disabled")
        self.confirm.pack(side="right")
        self.calculate_button = ttk.Button(self.action_row, text="计算最小超额组合", command=self.calculate, style="Primary.TButton")
        self.calculate_button.pack(side="right", padx=small)
        ttk.Label(self.action_row, text="实际申报额 / 元", style="Muted.TLabel").pack(side="left")
        self.declared_entry = ttk.Entry(self.action_row, textvariable=self.declared, width=12, font=(MONO, 10), justify="right")
        self.declared_entry.pack(side="left", padx=(small, 0))

        tools = ttk.Frame(panel)
        tools.pack(fill="x", pady=(0, tiny))
        tools.columnconfigure(1, weight=1)
        self.reset_button = ttk.Button(tools, text="重置全部约束为自动", style="Quiet.TButton", command=self.reset_constraints)
        self.reset_button.grid(row=0, column=2, sticky="e")
        ttk.Label(tools, text=f"可用发票 {len(self.candidates)} 张", style="Muted.TLabel").grid(row=0, column=0, sticky="w")
        self.interaction_hint = ttk.Label(tools, text="点击复选框微调 · 点击约束列切换 · 右键指定",
                                          style="Hint.TLabel", width=1)
        self.interaction_hint.grid(row=1, column=0, columnspan=3, sticky="ew")
        wrap_label(self.interaction_hint)
        listing = ttk.Frame(panel)
        listing.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(listing, columns=("pick", "number", "date", "amount", "constraint"),
                                 displaycolumns=("pick", "constraint", "number", "date", "amount"),
                                 show="headings", selectmode="browse")
        for key, title, width in zip(("pick", "number", "date", "amount", "constraint"),
                                     ("选中", "发票号码", "开票日期", "金额 / 元", "约束"), (64, 180, 100, 100, 100)):
            self.tree.heading(key, text=title)
            self.tree.column(key, width=p(width), minwidth=p(width),
                             stretch=key == "number", anchor="e" if key == "amount" else "center" if key in {"pick", "constraint"} else "w")
        table_scrollers(listing, self.tree)
        hover_rows(self.tree)
        self.tree.tag_configure("必选", foreground=COLORS.success)
        self.tree.tag_configure("排除", foreground=COLORS.muted)
        self.tree.bind("<Button-1>", self.click)
        self.tree.bind("<Double-1>", self.toggle)
        self.tree.bind("<Button-3>", self.context_menu)
        self.tree.bind("<space>", self.keyboard_toggle)
        self.tree.bind("<c>", self.keyboard_constraint)
        compact = None

        def arrange_dialog(event):
            nonlocal compact
            short = event.height < p(465)
            if event.widget is not self or short == compact:
                return
            compact = short
            panel.configure(padding=(large, tiny) if compact else (extra_large, medium))
            dashboard.configure(padding=(small, tiny) if compact else (medium, small))
            dashboard.pack_configure(pady=(0, tiny if compact else small))
            target_panel.grid_configure(padx=(0, small if compact else extra_large))
            target_label.pack_configure(side="left" if compact else "top", padx=(0, small if compact else 0),
                                        pady=(0, 0 if compact else tiny))
            self.target_entry.configure(style="TEntry" if compact else "Amount.TEntry",
                                        font=(MONO, 12 if compact else 22))
            self.target_entry.pack_configure(side="left" if compact else "top")
            bottom.pack_configure(pady=(tiny if compact else small, 0))
            self.source_badge.configure(pady=0 if compact else tiny)
            self.difference_badge.configure(pady=0 if compact else tiny)
            self.progress_label.pack_configure(pady=(0 if compact else tiny, 0))
            self.progress_canvas.configure(height=small if compact else medium)
            self.interaction_hint.grid_configure(row=0 if compact else 1, column=1 if compact else 0,
                                                 columnspan=1 if compact else 3, padx=small if compact else 0)

        self.bind("<Configure>", arrange_dialog, add="+")
        self.menu = tk.Menu(self, tearoff=False, font=(FONT, 10), background=COLORS.canvas, foreground=COLORS.text)
        self.menu_item = None
        for state, label in CONSTRAINT_LABELS.items():
            self.menu.add_command(label=label, command=lambda value=state: self.set_constraint(self.menu_item, value))
        self.target.trace_add("write", lambda *_: self.invalidate())
        self.declared.trace_add("write", lambda *_: self.update_summary())
        self.bind("<Escape>", lambda event: self.destroy())
        self.fill()
        self.update_summary()
        self.target_entry.focus_set()

    def show_notice(self, text):
        self.notice_text.set(text)
        if text:
            self.notice.pack(fill="x", before=self.action_row, pady=(0, self.theme.space.tiny))
        else:
            self.notice.pack_forget()

    def invalidate(self):
        if self.workspace_stale:
            return
        self.condition_version += 1
        self.result = None
        self.selected_ids = []
        self.fill()
        self.update_summary()
        self.show_notice("条件已变更，请重新计算组合")

    def check_workspace(self):
        if self.workspace_stale:
            return False
        if (self.owner.session_ok and self.owner.store is self.store
                and self.owner.snapshot["revision"] == self.revision):
            return True
        self.workspace_stale = True
        self.result = None
        self.selected_ids = []
        self.fill()
        self.update_summary()
        self.calculate_button.configure(state="disabled", text="计算最小超额组合")
        self.reset_button.configure(state="disabled")
        self.target_entry.configure(state="readonly")
        self.declared_entry.configure(state="readonly")
        self.show_notice("工作区状态已变化，请关闭此窗口并重新选票。")
        return False

    def fill(self):
        selected, position = self.tree.selection(), self.tree.yview()
        for item in self.candidates:
            item_id = item["id"]
            values = ("☑" if item_id in self.selected_ids else "☐", item["number"], item["date"],
                      yuan(item["amount_cents"]), CONSTRAINT_LABELS[self.constraints[item_id]])
            if self.tree.exists(item_id):
                self.tree.item(item_id, values=values, tags=(self.constraints[item_id],))
            else:
                self.tree.insert("", "end", iid=item_id, values=values, tags=(self.constraints[item_id],))
        if selected:
            self.tree.selection_set(selected)
        if position:
            self.tree.yview_moveto(position[0])

    def click(self, event):
        item_id, column = self.tree.identify_row(event.y), self.tree.identify_column(event.x)
        if not item_id or not column:
            return
        column = self.tree.column(column, "id")
        if column not in {"pick", "constraint"}:
            return
        self.tree.selection_set(item_id)
        self.tree.focus(item_id)
        self.tree.focus_set()
        if column == "pick":
            self.toggle_selection(item_id)
        else:
            self.cycle_constraint(item_id)
        return "break"

    def toggle(self, event):
        item_id, column = self.tree.identify_row(event.y), self.tree.identify_column(event.x)
        # 第一次单击已处理显式操作列，双击只处理其他列的兼容行为。
        if item_id and column and self.tree.column(column, "id") not in {"pick", "constraint"}:
            self.cycle_constraint(item_id)
        return "break"

    def toggle_selection(self, item_id):
        if self.owner.busy or not self.check_workspace():
            return
        if self.result is None:
            self.show_notice("请先计算组合，再点击复选框微调方案。")
            return
        if item_id in self.selected_ids:
            self.selected_ids.remove(item_id)
        else:
            self.selected_ids.append(item_id)
        self.result["optimal"] = False
        self.manually_adjusted = True
        self.fill()
        self.update_summary()

    def cycle_constraint(self, item_id):
        options = tuple(CONSTRAINT_LABELS)
        self.set_constraint(item_id, options[(options.index(self.constraints[item_id]) + 1) % len(options)])

    def set_constraint(self, item_id, state):
        if self.owner.busy or not self.check_workspace() or item_id not in self.constraints or self.constraints[item_id] == state:
            return
        self.constraints[item_id] = state
        self.invalidate()

    def reset_constraints(self):
        if self.owner.busy or not self.check_workspace() or all(state == "自动" for state in self.constraints.values()):
            return
        self.constraints = dict.fromkeys(self.constraints, "自动")
        self.invalidate()

    def context_menu(self, event):
        item_id = self.tree.identify_row(event.y)
        if not item_id or self.owner.busy or not self.check_workspace():
            return
        self.tree.selection_set(item_id)
        self.tree.focus(item_id)
        self.menu_item = item_id
        try:
            self.menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.menu.grab_release()
            if self.winfo_exists():
                self.grab_set()
        return "break"

    def keyboard_toggle(self, event):
        if self.tree.selection():
            self.toggle_selection(self.tree.selection()[0])
        return "break"

    def keyboard_constraint(self, event):
        if self.tree.selection():
            self.cycle_constraint(self.tree.selection()[0])
        return "break"

    def calculate(self):
        if self.owner.busy or not self.check_workspace():
            return
        if self.watch_id is not None:
            self.after_cancel(self.watch_id)
            self.watch_id = None
        try:
            money(self.target.get())
        except UserError as exc:
            messagebox.showerror("请核对目标金额", str(exc), parent=self)
            self.target_entry.focus_set()
            return
        target, version = self.target.get(), self.condition_version
        forced = [i for i, state in self.constraints.items() if state == "必选"]
        excluded = [i for i, state in self.constraints.items() if state == "排除"]
        self.result = None
        self.selected_ids = []
        self.fill()
        self.update_summary()
        self.calculate_button.configure(state="disabled", text="正在计算组合…")
        self.show_notice("正在计算，可修改目标金额；条件变更后需重新计算。")
        self.owner.task("正在计算精确选票组合…", lambda: self.store.recommend(target, forced, excluded),
                        lambda result: self.calculated(result, version))
        self.watch_id = self.after(100, self.watch_task)

    def watch_task(self):
        self.watch_id = None
        if self.owner.busy:
            self.watch_id = self.after(100, self.watch_task)
        elif self.check_workspace():
            self.calculate_button.configure(state="normal", text="计算最小超额组合")
            if self.result is None:
                self.show_notice("条件已变更或计算未完成，请重新计算组合。")

    def calculated(self, result, version=None):
        if not self.winfo_exists() or not self.check_workspace():
            return
        if version is not None and version != self.condition_version:
            return
        try:
            if result["target_cents"] != money(self.target.get()):
                return
        except UserError:
            return
        self.result = result
        self.manually_adjusted = False
        self.selected_ids = result["ids"][:]
        if self.watch_id is not None:
            self.after_cancel(self.watch_id)
            self.watch_id = None
        self.declared.set(yuan(result["target_cents"]))
        self.calculate_button.configure(state="normal", text="计算最小超额组合")
        self.fill()
        self.update_summary()

    def selected_total(self):
        return sum(item["amount_cents"] for item in self.candidates if item["id"] in self.selected_ids)

    def valid_selection(self):
        if self.result is None:
            return False
        try:
            target, declared = money(self.target.get()), money(self.declared.get())
        except UserError:
            return False
        return (target == self.result["target_cents"] and bool(self.selected_ids)
                and self.selected_total() >= target and declared <= self.selected_total()
                and all(i in self.selected_ids for i, state in self.constraints.items() if state == "必选")
                and all(i not in self.selected_ids for i, state in self.constraints.items() if state == "排除"))

    def draw_progress(self):
        canvas, p = self.progress_canvas, self.theme.px
        canvas.delete("all")
        width, height = max(p(16), canvas.winfo_width()), canvas.winfo_height()
        start, end = p(4), width - p(4)
        canvas.create_line(start, height / 2, end, height / 2, fill=COLORS.line, width=p(6), capstyle="round")
        try:
            ratio = min(1.0, self.selected_total() / money(self.target.get()))
        except UserError:
            ratio = 0
        if ratio > 0:
            canvas.create_line(start, height / 2, start + (end - start) * ratio, height / 2,
                               fill=COLORS.success if ratio >= 1 else COLORS.primary, width=p(6), capstyle="round")

    def update_summary(self):
        total = self.selected_total()
        available = sum(i["amount_cents"] for i in self.candidates if self.constraints[i["id"]] != "排除")
        try:
            target = money(self.target.get())
        except UserError:
            target = None
        target_display = f"¥{yuan(target)}" if target is not None else "—"
        self.progress_text.set(f"已选 ¥{yuan(total)}  /  目标 {target_display}")
        tone = "success" if target is not None and total >= target else "warning"
        difference = total - target if target is not None else 0
        self.difference_text.set((f"超额 +¥{yuan(difference)}" if difference > 0 else "恰好达到目标" if difference == 0 else f"缺口 -¥{yuan(-difference)}") if target is not None else "请输入有效目标金额")
        self.difference_badge.configure(bg=getattr(COLORS, tone + "_bg"), fg=getattr(COLORS, tone))
        optimal = self.result is not None and self.result.get("optimal")
        if self.result is None:
            source = "等待计算"
        elif self.manually_adjusted:
            source = "手动微调中"
        else:
            source = "算法最优解" if optimal else "未生成方案"
        self.source_text.set(source)
        self.source_badge.configure(bg=COLORS.success_bg if optimal else COLORS.occupied_bg,
                                    fg=COLORS.success if optimal else COLORS.secondary)
        valid = self.valid_selection()
        self.confirm.configure(state="normal" if valid else "disabled", style="Primary.TButton" if valid else "TButton")
        self.calculate_button.configure(style="TButton" if valid else "Primary.TButton")
        self.summary.set(f"选中 {len(self.selected_ids)} 张 · 票面 ¥{yuan(total)} · {self.difference_text.get()} · 可用 ¥{yuan(available)}")
        if self.result is not None:
            if target is not None and available < target:
                self.show_notice(f"当前可用发票不足以凑齐目标额度，还缺 ¥{yuan(target - available)}。请调整目标或导入可用发票。")
            elif not valid:
                self.show_notice("当前选择未满足目标金额、必选 / 排除约束或申报金额要求，请调整或重新计算。")
            else:
                self.show_notice("")
        self.draw_progress()

    def commit(self):
        if self.owner.busy or not self.check_workspace():
            return
        if not self.valid_selection():
            self.show_notice("当前方案尚不可确认，请核对条件并重新计算组合。")
            return
        ids, target, declared = self.selected_ids[:], self.target.get(), self.declared.get()
        total = self.selected_total()
        if not messagebox.askyesno("确认整票占用", f"本次选中 {len(ids)} 张，票面合计 ¥{yuan(total)}。\n实际申报：{declared} 元。\n\n确认后整张票归属本批次，超额不结转。可在发票详情中修改状态；已退回或可用会释放该票。", parent=self):
            return
        revision, request_id, store, owner = self.revision, self.request_id, self.owner.store, self.owner
        self.destroy()

        def done(batch_id):
            owner.pages.select(1)
            owner.batch_tree.selection_set(batch_id)
            owner.show_batch()
            owner.status_text.set("批次已确认，所选发票已整票占用。可导出材料，并在发票详情中更新状态。")

        owner.task("确认报销批次…", lambda: store.create_batch(revision, ids, target, declared, request_id), done)

    def destroy(self):
        if self.watch_id is not None:
            self.after_cancel(self.watch_id)
            self.watch_id = None
        super().destroy()
