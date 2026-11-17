"""Invoice selection dialog and asynchronous recommendation state."""
from __future__ import annotations

import tkinter as tk
from tkinter import messagebox, ttk
from typing import TYPE_CHECKING

from .domain import UserError, money, yuan
from .labels import state_label
from .store import uid

if TYPE_CHECKING:
    from .gui import Workbench


class SelectionDialog(tk.Toplevel):
    def __init__(self, owner: Workbench):
        super().__init__(owner)
        self.owner = owner
        self.title("按额度选票 · 整票占用，超额不结转")
        self.geometry("910x640")
        self.minsize(820, 570)
        self.transient(owner)
        self.grab_set()
        self.result = None
        self.revision = owner.snapshot["revision"]
        self.request_id = uid()
        self.candidates = [i for i in owner.snapshot["invoices"] if state_label(i) == "可用"]
        self.constraints = {i["id"]: "自动" for i in self.candidates}
        self.selected_ids = []
        panel = ttk.Frame(self, padding=20)
        panel.pack(fill="both", expand=True)
        inputs = ttk.Frame(panel)
        inputs.pack(fill="x")
        ttk.Label(inputs, text="目标金额 / 元").pack(side="left")
        self.target = tk.StringVar(value="1000.00")
        self.target.trace_add("write", lambda *_: self.invalidate())
        ttk.Entry(inputs, textvariable=self.target, width=15).pack(side="left", padx=10)
        ttk.Button(inputs, text="计算最小超额组合", command=self.calculate).pack(side="left")
        ttk.Label(panel, text="双击“约束”所在行，循环设置：自动 → 必选 → 排除。勾选框表示最终选择，可手动调整。", style="Muted.TLabel", wraplength=820).pack(anchor="w", pady=12)
        self.tree = ttk.Treeview(panel, columns=("pick", "number", "date", "amount", "constraint"), show="headings", selectmode="browse")
        for key, title, width in zip(("pick", "number", "date", "amount", "constraint"), ("选中", "发票号码", "开票日期", "金额 / 元", "约束"), (60, 260, 130, 130, 90)):
            self.tree.heading(key, text=title)
            self.tree.column(key, width=width, stretch=key == "number")
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<Double-1>", self.toggle)
        self.summary = tk.StringVar(value=f"可用 {len(self.candidates)} 张，合计 ¥{yuan(sum(i['amount_cents'] for i in self.candidates))}。")
        ttk.Label(panel, textvariable=self.summary, wraplength=820).pack(anchor="w", pady=14)
        bottom = ttk.Frame(panel)
        bottom.pack(fill="x")
        ttk.Label(bottom, text="实际申报额 / 元").pack(side="left")
        self.declared = tk.StringVar(value="1000.00")
        ttk.Entry(bottom, textvariable=self.declared, width=15).pack(side="left", padx=10)
        self.confirm = ttk.Button(bottom, text="确认批次并占用整张票", command=self.commit, state="disabled", style="Accent.TButton")
        self.confirm.pack(side="right")
        self.fill()

    def invalidate(self):
        self.result = None
        self.selected_ids = []
        if hasattr(self, "confirm"):
            self.confirm.configure(state="disabled")
            self.fill()
            self.summary.set("目标已修改，请重新计算。")

    def fill(self):
        selected = self.tree.selection()
        self.tree.delete(*self.tree.get_children())
        for i in self.candidates:
            self.tree.insert("", "end", iid=i["id"], values=("☑" if i["id"] in self.selected_ids else "☐", i["number"], i["date"], yuan(i["amount_cents"]), self.constraints[i["id"]]))
        if selected and self.tree.exists(selected[0]):
            self.tree.selection_set(selected[0])

    def toggle(self, event):
        if self.owner.busy:
            return
        item_id, col = self.tree.identify_row(event.y), self.tree.identify_column(event.x)
        if not item_id:
            return
        if col == "#1" and self.result is not None:
            if item_id in self.selected_ids:
                self.selected_ids.remove(item_id)
            else:
                self.selected_ids.append(item_id)
            self.result["optimal"] = False
            self.update_summary()
        else:
            options = ["自动", "必选", "排除"]
            self.constraints[item_id] = options[(options.index(self.constraints[item_id]) + 1) % 3]
            self.invalidate()
        self.fill()

    def calculate(self):
        if self.owner.busy:
            return
        target = self.target.get()
        forced = [i for i, state in self.constraints.items() if state == "必选"]
        excluded = [i for i, state in self.constraints.items() if state == "排除"]
        self.confirm.configure(state="disabled")
        self.owner.task("正在计算精确选票组合…", lambda: self.owner.store.recommend(target, forced, excluded), self.calculated)

    def calculated(self, result):
        if not self.winfo_exists():
            return
        try:
            if result["target_cents"] != money(self.target.get()):
                return
        except UserError:
            return
        self.result = result
        self.selected_ids = result["ids"][:]
        self.declared.set(yuan(result["target_cents"]))
        self.fill()
        self.update_summary()

    def update_summary(self):
        if self.result is None:
            return
        if self.result.get("shortfall_cents"):
            self.summary.set(f"可用 ¥{yuan(self.result['available_cents'])}，不足 ¥{yuan(self.result['shortfall_cents'])}。")
            self.confirm.configure(state="disabled")
            return
        total = sum(i["amount_cents"] for i in self.candidates if i["id"] in self.selected_ids)
        try:
            target = money(self.target.get())
        except UserError:
            self.confirm.configure(state="disabled")
            return
        valid = total >= target and all(i in self.selected_ids for i, s in self.constraints.items() if s == "必选") and all(i not in self.selected_ids for i, s in self.constraints.items() if s == "排除")
        suffix = "已证明超额最少" if self.result.get("optimal") else "手动调整，未重新证明最优"
        self.summary.set(f"选中 {len(self.selected_ids)} 张，票面 ¥{yuan(total)}，" + (f"超额 ¥{yuan(total - target)}。{suffix}。" if total >= target else f"尚缺 ¥{yuan(target - total)}。"))
        self.confirm.configure(state="normal" if valid else "disabled")

    def commit(self):
        if self.owner.busy:
            return
        ids, target, declared = self.selected_ids[:], self.target.get(), self.declared.get()
        total = sum(i["amount_cents"] for i in self.candidates if i["id"] in ids)
        if not messagebox.askyesno("确认整票占用", f"本次选中 {len(ids)} 张，票面合计 ¥{yuan(total)}。\n实际申报：{declared} 元。\n\n确认后整张票归属本批次，超额不结转。取消批次可释放。", parent=self):
            return
        revision, request_id, store, owner = self.revision, self.request_id, self.owner.store, self.owner
        self.destroy()
        def done(batch_id):
            owner.pages.select(1)
            owner.batch_tree.selection_set(batch_id)
            owner.show_batch()
            owner.status_text.set("批次已确认，所选发票已整票占用。可导出材料，并按实际进度更新状态。")
        owner.task("确认报销批次…", lambda: store.create_batch(revision, ids, target, declared, request_id), done)
