import os
from pathlib import Path
import tempfile
import time
import tkinter as tk
from tkinter import ttk
import unittest

from invoice_workbench.gui import Workbench
from invoice_workbench.selection import SelectionDialog
from invoice_workbench.store import uid
from invoice_workbench.theme import COLORS, enable_dpi_awareness
from .helpers import BUYER, TAX, write_invoice
from . import test_review_fixes


class InteractionTests(unittest.TestCase):
    answer_dialog = test_review_fixes.WorkspaceReconnectTests.answer_dialog

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.app = Workbench(self.root / "workspace", self.root / "settings.json")
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.close_app)
        self.errors = []
        self.app.report_callback_exception = lambda *error: self.errors.append(error)
        source = self.root / "source"
        write_invoice(source, "a.xml", amount="1002.50")
        write_invoice(source, "b.xml", amount="600.00", number="26000000000000000002")
        store = self.app.store
        store.set_company(store.data["revision"], BUYER, TAX)
        store.import_folder(store.data["revision"], str(source))
        self.app.reload()
        self.app.update()
        self.clock = 1000

    def close_app(self):
        self.app.dirty = False
        self.app.close_app()
        self.assertEqual(self.errors, [])

    def wait_task(self):
        deadline = time.monotonic() + 10
        while self.app.busy and time.monotonic() < deadline:
            self.app.update()
            time.sleep(0.01)
        self.assertFalse(self.app.busy)
        self.app.update()

    def dialog(self):
        dialog = SelectionDialog(self.app)
        self.addCleanup(lambda: dialog.destroy() if dialog.winfo_exists() else None)
        self.app.update()
        return dialog

    def click_cell(self, dialog, item_id, column, *, double=False):
        x, y, width, height = dialog.tree.bbox(item_id, column)
        x, y = x + width // 2, y + height // 2
        self.clock += 1000
        for offset in ((0, 100) if double else (0,)):
            dialog.tree.event_generate("<ButtonPress-1>", x=x, y=y, time=self.clock + offset)
            dialog.tree.event_generate("<ButtonRelease-1>", x=x, y=y, time=self.clock + offset + 1)
        self.app.update()

    def test_filter_pills_and_live_search_share_the_same_result(self):
        self.app.filter_control.buttons[2].invoke()
        self.assertEqual(len(self.app.tree.get_children()), 2)
        self.app.search_entry.focus_force()
        self.app.update()
        self.app.search.set("000000000002")
        self.app.search_entry.event_generate("<KeyRelease>", keysym="2")
        self.app.update()
        self.assertEqual(len(self.app.tree.get_children()), 1)
        self.app.filter_control.buttons[1].invoke()
        self.assertEqual(self.app.tree.get_children(), ())
        self.app.clear_search()
        self.app.filter_control.buttons[0].invoke()
        self.assertEqual(len(self.app.tree.get_children()), 2)

    def test_filter_clears_clean_details_when_the_invoice_leaves_the_list(self):
        tree = self.app.tree
        first, second = tree.get_children()
        for filter_invoice in (lambda: self.app.filter_control.buttons[1].invoke(),
                               lambda: self.search_for_invoice(second)):
            tree.selection_set(first)
            self.app.select_invoice()
            self.assertTrue(self.app.detail_visible)
            filter_invoice()
            self.app.update()
            self.assertEqual(tree.selection(), ())
            self.assertIsNone(self.app.current_id)
            self.assertFalse(self.app.detail_visible)
            self.assertEqual(self.app.fields["number"].get(), "")
            self.assertTrue(self.app.review_button.instate(("disabled",)))
            self.assertTrue(self.app.archive_button.instate(("disabled",)))
            self.app.filter_control.buttons[0].invoke()
            self.app.clear_search()

    def search_for_invoice(self, item_id):
        self.app.search_entry.focus_force()
        self.app.update()
        self.app.search.set(self.app.tree.set(item_id, "number"))
        self.app.search_entry.event_generate("<KeyRelease>", keysym="2")
        self.app.update()

    def test_hover_returns_when_refresh_recreates_the_row_under_the_pointer(self):
        tree = self.app.tree
        first = tree.get_children()[0]
        x, y, width, height = tree.bbox(first, "#0")
        x, y = x + width // 2, y + height // 2
        tree.event_generate("<Motion>", x=x, y=y)
        self.assertTrue(tree.tag_has("hover", first))
        self.app.filter_control.buttons[0].invoke()
        tree.event_generate("<Motion>", x=x + 1, y=y)
        self.assertTrue(tree.tag_has("hover", first))
        tree.event_generate("<Leave>")
        self.assertFalse(tree.tag_has("hover", first))

    @unittest.skipUnless(os.name == "nt", "需要 Windows 原生消息框")
    def test_keyboard_navigation_keeps_unsaved_fields_when_discard_is_declined(self):
        first, second = self.app.tree.get_children()
        self.app.tree.selection_set(first)
        self.app.select_invoice()
        self.app.fields["note"].set("尚未保存的核对备注")
        self.assertIn("有未保存", self.app.warning_text.get())
        self.answer_dialog(lambda: self.app.move_invoice(1), "尚未保存", 7)
        self.app.update()
        self.assertEqual(self.app.current_id, first)
        self.assertEqual(self.app.tree.focus(), first)
        self.assertEqual(self.app.fields["note"].get(), "尚未保存的核对备注")
        self.answer_dialog(lambda: self.app.move_invoice(1), "尚未保存", 6)
        self.app.update()
        self.assertEqual(self.app.current_id, second)
        self.assertFalse(self.app.dirty)

    @unittest.skipUnless(os.name == "nt", "需要 Windows 原生消息框")
    def test_declining_navigation_after_search_hides_the_current_invoice(self):
        first, second = self.app.tree.get_children()
        self.app.tree.selection_set(first)
        self.app.select_invoice()
        self.app.fields["note"].set("尚未保存的核对备注")
        self.search_for_invoice(second)
        self.assertEqual(self.app.tree.get_children(), (second,))
        self.assertIn("已被筛选隐藏", self.app.details_title.get())
        self.app.reload()
        self.assertIn("已被筛选隐藏", self.app.details_title.get())
        self.assertTrue(self.app.dirty)
        self.app.clear_search()
        self.assertEqual(self.app.tree.selection(), (first,))
        self.assertNotIn("已被筛选隐藏", self.app.details_title.get())
        self.search_for_invoice(second)
        self.answer_dialog(lambda: self.app.move_invoice(1), "尚未保存", 7)
        self.app.update()
        self.assertEqual(self.app.tree.selection(), ())
        self.assertEqual(self.app.tree.focus(), "")
        self.assertEqual(self.app.current_id, first)
        self.assertEqual(self.app.fields["note"].get(), "尚未保存的核对备注")
        self.answer_dialog(lambda: self.app.move_invoice(1), "尚未保存", 6)
        self.app.update()
        self.assertEqual(self.app.current_id, second)
        self.assertEqual(self.app.tree.selection(), (second,))
        self.assertNotIn("已被筛选隐藏", self.app.details_title.get())

    def test_saving_a_filtered_draft_closes_details_and_saves_the_correct_invoice(self):
        first, second = self.app.tree.get_children()
        self.app.tree.selection_set(first)
        self.app.select_invoice()
        note = "筛选后保存的核对备注"
        self.app.fields["note"].set(note)
        self.search_for_invoice(second)
        self.assertIn("已被筛选隐藏", self.app.details_title.get())
        self.app.review_button.invoke()
        self.wait_task()
        self.assertEqual(self.app.store.invoice(first)["note"], note)
        self.assertEqual(self.app.store.invoice(second)["note"], "")
        self.assertIsNone(self.app.current_id)
        self.assertFalse(self.app.dirty)
        self.assertFalse(self.app.detail_visible)

    def test_checkbox_single_and_double_click_adjust_one_time_without_changing_constraints(self):
        dialog = self.dialog()
        dialog.calculate()
        self.wait_task()
        selected = dialog.selected_ids[0]
        self.click_cell(dialog, selected, "pick")
        self.assertNotIn(selected, dialog.selected_ids)
        self.assertEqual(dialog.source_text.get(), "手动微调中")
        self.assertIn("缺口", dialog.difference_text.get())
        self.assertEqual(str(dialog.confirm.cget("state")), "disabled")
        self.click_cell(dialog, selected, "pick", double=True)
        self.assertIn(selected, dialog.selected_ids)
        self.assertEqual(dialog.constraints[selected], "自动")
        self.assertEqual(dialog.difference_text.get(), "超额 +¥2.50")
        self.assertEqual(str(dialog.confirm.cget("state")), "normal")

    def test_constraint_single_click_menu_and_reset_invalidate_the_plan(self):
        dialog = self.dialog()
        dialog.calculated(self.app.store.recommend(dialog.target.get()))
        item_id = dialog.tree.get_children()[0]
        self.click_cell(dialog, item_id, "constraint")
        self.assertEqual(dialog.constraints[item_id], "必选")
        self.assertIsNone(dialog.result)
        self.assertIn("请重新计算", dialog.notice_text.get())
        self.assertEqual(dialog.calculate_button.cget("style"), "Primary.TButton")
        self.click_cell(dialog, item_id, "constraint", double=True)
        self.assertEqual(dialog.constraints[item_id], "排除")
        dialog.menu_item = item_id
        dialog.menu.invoke(1)
        self.assertEqual(dialog.constraints[item_id], "必选")
        dialog.reset_button.invoke()
        self.assertTrue(all(state == "自动" for state in dialog.constraints.values()))
        self.assertEqual(str(dialog.confirm.cget("state")), "disabled")
        self.click_cell(dialog, item_id, "number", double=True)
        self.assertEqual(dialog.constraints[item_id], "必选")

    def test_shortfall_and_stale_plan_cannot_be_committed_directly(self):
        dialog = self.dialog()
        dialog.target.set("2000.00")
        dialog.calculate()
        self.wait_task()
        self.assertEqual(dialog.result["shortfall_cents"], 39750)
        self.assertEqual(dialog.source_text.get(), "未生成方案")
        self.assertIn("当前可用发票不足以凑齐目标额度", dialog.notice_text.get())
        item_id = next(i["id"] for i in dialog.candidates if i["amount_cents"] == 100250)
        self.click_cell(dialog, item_id, "pick")
        self.assertEqual(dialog.source_text.get(), "手动微调中")
        self.assertEqual(dialog.difference_text.get(), "缺口 -¥997.50")
        self.assertIn("当前可用发票不足以凑齐目标额度", dialog.notice_text.get())
        self.assertTrue(dialog.confirm.instate(("disabled",)))
        track, progress = dialog.progress_canvas.find_all()
        self.assertLess(dialog.progress_canvas.coords(progress)[2], dialog.progress_canvas.coords(track)[2])
        before = self.app.store.state_path.read_bytes()
        dialog.commit()
        self.assertEqual(self.app.store.state_path.read_bytes(), before)
        dialog.target.set("1000.00")
        dialog.calculated(self.app.store.recommend("1000.00"))
        self.assertEqual(dialog.source_text.get(), "算法最优解")
        dialog.target.set("1001.00")
        dialog.commit()
        self.assertEqual(self.app.store.state_path.read_bytes(), before)

    def test_condition_generation_rejects_old_result_even_if_target_is_changed_back(self):
        dialog = self.dialog()
        generation = dialog.condition_version
        result = self.app.store.recommend("1000.00")
        dialog.target.set("600.00")
        dialog.target.set("1000.00")
        dialog.calculated(result, generation)
        self.assertIsNone(dialog.result)
        self.assertEqual(dialog.selected_ids, [])
        self.assertEqual(str(dialog.confirm.cget("state")), "disabled")

    def test_closing_dialog_during_real_background_calculation_is_safe(self):
        dialog = self.dialog()
        dialog.calculate()
        dialog.destroy()
        self.wait_task()
        self.assertTrue(self.app.session_ok)
        self.assertEqual(self.errors, [])

    def test_workspace_change_after_calculation_keeps_dialog_disabled(self):
        dialog = self.dialog()
        dialog.calculate()
        deadline = time.monotonic() + 10
        while self.app.events.empty() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertFalse(self.app.events.empty())
        # Tk 尚未处理计算结果，此时从当前 Store 会话以外修改索引。
        state_path = self.app.store.state_path
        state_path.write_bytes(state_path.read_bytes() + b"\n")
        self.wait_task()
        deadline = time.monotonic() + 10
        while dialog.watch_id is not None and time.monotonic() < deadline:
            self.app.update()
            time.sleep(0.01)
        self.assertIsNone(dialog.watch_id)
        self.assertFalse(self.app.session_ok)
        self.assertIn("请关闭此窗口并重新选票", dialog.notice_text.get())
        self.assertTrue(dialog.calculate_button.instate(["disabled"]))
        self.assertTrue(dialog.confirm.instate(["disabled"]))
        dialog.calculate()
        self.assertFalse(self.app.busy)
        self.assertIsNone(dialog.result)
        self.assertIn("请关闭此窗口并重新选票", dialog.notice_text.get())

    def test_invalid_declared_amount_disables_confirmation(self):
        dialog = self.dialog()
        dialog.calculated(self.app.store.recommend("1000.00"))
        dialog.declared.set("2000.00")
        self.assertEqual(str(dialog.confirm.cget("state")), "disabled")
        dialog.declared.set("invalid")
        self.assertFalse(dialog.valid_selection())
        dialog.declared.set("900.00")
        self.assertTrue(dialog.valid_selection())

    def test_inspector_actions_remain_visible_for_an_incomplete_image(self):
        samples = Path(__file__).resolve().parent.parent / "samples" / "image-preview"
        self.app.store.import_folder(self.app.snapshot["revision"], samples)
        self.app.reload()
        item = next(i for i in self.app.snapshot["invoices"] if not i["verification"]["passed"])
        p = self.app.theme.px
        self.app.geometry(f"{p(1080)}x{p(640)}")
        self.app.load_invoice(item)
        self.app.update()
        self.assertTrue(self.app.review_button.winfo_ismapped())
        self.assertTrue(self.app.archive_button.winfo_ismapped())
        self.assertGreater(self.app.form_scroll.canvas.winfo_height(), p(48), {
            str(widget): (widget.winfo_width(), widget.winfo_height())
            for widget in (self.app, self.app.pages, self.app.splitter, self.app.detail_pane,
                           self.app.details, self.app.review_banner, self.app.review_actions)})

    def test_editing_a_scrolled_field_keeps_the_dirty_banner_and_save_visible(self):
        item = self.app.snapshot["invoices"][0]
        self.app.tree.selection_set(item["id"])
        self.app.select_invoice()
        self.app.update()
        canvas = self.app.form_scroll.canvas
        canvas.yview_moveto(1)
        self.app.update()
        self.assertGreater(canvas.yview()[0], 0)
        self.app.entries[list(self.app.fields).index("note")].insert(0, "核对备注")
        self.app.update()
        banner = self.app.review_banner
        self.assertIn("有未保存的核对修改", self.app.warning_text.get())
        self.assertTrue(banner.winfo_ismapped())
        self.assertGreaterEqual(banner.winfo_rooty(), self.app.detail_pane.winfo_rooty())
        self.assertLessEqual(banner.winfo_rooty() + banner.winfo_height(), canvas.winfo_rooty())
        self.assertTrue(self.app.review_button.winfo_ismapped())

    def test_collapsing_preview_cancels_rendering_and_reopening_schedules_it(self):
        self.app.load_invoice(self.app.snapshot["invoices"][0])
        self.app.details.control.buttons[1].invoke()
        self.app.update()
        self.assertIsNotNone(self.app.preview_after)
        self.app.hide_detail()
        self.assertIsNone(self.app.preview_after)
        self.app.preview_canvas.master.event_generate("<Configure>")
        self.app.update()
        self.assertIsNone(self.app.preview_after)
        self.app.render_preview()
        self.assertEqual(self.app.preview_canvas.find_all(), ())
        self.app.show_detail()
        deadline = time.monotonic() + 5
        while self.app.preview_after is not None and time.monotonic() < deadline:
            self.app.update()
            time.sleep(0.01)
        self.assertIsNone(self.app.preview_after)
        self.assertTrue(self.app.preview_canvas.find_all())

    def test_preview_zoom_and_resize_keep_the_scroll_position_until_file_changes(self):
        samples = Path(__file__).resolve().parent.parent / "samples"
        self.app.open_library(self.root / "preview-workspace")
        self.app.store.import_folder(self.app.snapshot["revision"], samples, recursive=True)
        self.app.reload()
        item = next(i for i in self.app.snapshot["invoices"] if any(f["name"].endswith(".pdf") for f in i["files"]))
        self.app.load_invoice(item)
        self.app.details.control.buttons[1].invoke()
        self.app.update()
        self.app.file_choice.current(next(n for n, file in enumerate(item["files"]) if file["name"].endswith(".pdf")))
        self.app.preview_file_changed()
        self.assertIsNotNone(self.app.image_reference)
        key = self.app.preview_key
        width = self.app.image_reference.width()
        self.app.zoom_preview(1.25)
        self.assertGreater(self.app.image_reference.width(), width)
        self.assertEqual(self.app.preview_key, key)
        canvas = self.app.preview_canvas
        self.app.zoom_preview(1.25)
        canvas.xview_moveto(0.2)
        canvas.yview_moveto(0.25)
        x, y = canvas.xview()[0], canvas.yview()[0]
        self.assertGreater(x, 0)
        self.assertGreater(y, 0)
        self.app.zoom_preview(1.25)
        self.assertAlmostEqual(canvas.xview()[0], x, delta=0.01)
        self.assertAlmostEqual(canvas.yview()[0], y, delta=0.01)
        self.app.zoom_preview(1.25 / self.app.preview_zoom)
        canvas.xview_moveto(0.1)
        x, y = canvas.xview()[0], canvas.yview()[0]
        self.assertGreater(x, 0)
        old_scroll_width = int(canvas.cget("scrollregion").split()[2])
        old_image_width = self.app.image_reference.width()
        resized_width = self.app.winfo_width() + self.app.theme.px(400)
        self.app.maxsize(resized_width, self.app.winfo_height())
        self.app.geometry(f"{resized_width}x{self.app.winfo_height()}")
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            self.app.update()
            if (canvas.winfo_width() > old_scroll_width and self.app.preview_after is None
                    and self.app.image_reference.width() > old_image_width):
                break
            time.sleep(0.01)
        self.assertGreater(canvas.winfo_width(), old_scroll_width)
        self.assertGreater(self.app.image_reference.width(), old_image_width)
        self.assertIsNone(self.app.preview_after)
        self.assertEqual(self.app.preview_key, key)
        self.assertAlmostEqual(canvas.xview()[0], x, delta=0.01)
        self.assertAlmostEqual(canvas.yview()[0], y, delta=0.01)
        self.app.zoom_preview(None)
        self.assertLess(self.app.image_reference.width(), canvas.winfo_width())
        self.assertEqual(canvas.cget("background"), COLORS.occupied_bg)
        other_index = next(n for n, file in enumerate(item["files"]) if not file["name"].endswith(".pdf"))
        self.app.file_choice.current(other_index)
        self.app.preview_file_changed()
        self.assertNotEqual(self.app.preview_key, key)
        self.assertEqual(canvas.xview()[0], 0)
        self.assertEqual(canvas.yview()[0], 0)
        self.app.details.control.buttons[2].invoke()
        self.app.update()
        self.assertEqual(self.app.details.current_index, 2)


class DpiLayoutTests(unittest.TestCase):
    def assert_inside(self, widget, ancestor):
        self.assertTrue(widget.winfo_ismapped(), str(widget))
        left = widget.winfo_rootx() - ancestor.winfo_rootx()
        top = widget.winfo_rooty() - ancestor.winfo_rooty()
        self.assertGreaterEqual(left, 0, str(widget))
        self.assertGreaterEqual(top, 0, str(widget))
        self.assertLessEqual(left + widget.winfo_width(), ancestor.winfo_width(), str(widget))
        self.assertLessEqual(top + widget.winfo_height(), ancestor.winfo_height(), str(widget))

    def assert_first_row_visible(self, tree):
        row = tree.bbox(tree.get_children()[0])
        self.assertTrue(row, str(tree))
        self.assertLessEqual(row[1] + row[3], tree.winfo_height(), str(tree))

    def test_1366x768_limits_keep_core_controls_visible(self):
        enable_dpi_awareness()
        display = tk.Tk()
        display.withdraw()
        original_scaling = float(display.tk.call("tk", "scaling"))
        samples = Path(__file__).resolve().parents[1] / "samples"
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                for scale in (1.0, 1.25, 1.5, 2.0):
                    with self.subTest(scale=scale):
                        display.tk.call("tk", "scaling", scale * 96 / 72)
                        app = Workbench(root / f"workspace-{scale}", root / "settings.json")
                        try:
                            p = app.theme.px
                            size = (1366 - p(48), 768 - p(80))
                            app.minsize(1, 1)
                            app.maxsize(*size)
                            app.geometry(f"{size[0]}x{size[1]}+0+0")
                            app.update()
                            self.assertEqual((app.winfo_width(), app.winfo_height()), size)
                            store = app.store
                            store.set_company(store.data["revision"], BUYER, TAX)
                            store.import_folder(store.data["revision"], samples, recursive=True)
                            plan = store.recommend("1000.00")
                            batch_id = store.create_batch(store.data["revision"], plan["ids"], "1000.00", "1000.00", uid())
                            app.reload()

                            item = next(i for i in app.snapshot["invoices"] if not i["verification"]["passed"])
                            app.tree.selection_set(item["id"])
                            app.select_invoice()
                            app.update()
                            self.assert_first_row_visible(app.tree)
                            self.assertTrue(app.tree.item(item["id"], "image"))
                            self.assertEqual(app.tree.set(item["id"], "state"), "核验异常")
                            first = app.tree.get_children()[0]
                            for column in ("#0", "amount"):
                                x, _, width, _ = app.tree.bbox(first, column)
                                self.assertGreaterEqual(x, 0)
                                self.assertLessEqual(x + width, app.tree.winfo_width(), column)
                            for widget in (app.status_label, app.workspace_label, app.review_button, app.archive_button):
                                self.assert_inside(widget, app)
                            canvas = app.form_scroll.canvas
                            self.assertGreaterEqual(canvas.winfo_height(), p(48))
                            canvas.yview_moveto((app.status_choice.winfo_y() - p(12)) / canvas.bbox("all")[3])
                            app.update()
                            self.assert_inside(app.status_choice, canvas)
                            for widget in (*app.nav_buttons, *app.filter_control.buttons, *app.details.control.buttons,
                                           app.review_button, app.archive_button):
                                self.assert_inside(widget, app)
                                self.assertGreaterEqual(widget.winfo_width(), widget.winfo_reqwidth(), str(widget))

                            pdf = next(i for i in app.snapshot["invoices"] if any(f["name"].endswith(".pdf") for f in i["files"]))
                            app.tree.selection_set(pdf["id"])
                            app.select_invoice()
                            app.details.select(1)
                            app.file_choice.current(next(n for n, f in enumerate(pdf["files"]) if f["name"].endswith(".pdf")))
                            app.update()
                            app.preview_file_changed()
                            self.assert_inside(app.file_choice, app.detail_pane)
                            self.assert_inside(app.preview_canvas, app.detail_pane)
                            self.assertGreaterEqual(app.preview_canvas.winfo_height(), p(48))
                            self.assertIsNotNone(app.image_reference)

                            app.pages.select(1)
                            app.batch_tree.selection_set(batch_id)
                            app.show_batch()
                            app.update()
                            self.assert_first_row_visible(app.batch_tree)
                            self.assert_inside(app.batch_info, app.pages.frames[1])
                            self.assertGreaterEqual(app.batch_info.winfo_height(), p(40) - p(1))
                            self.assertIn(batch_id, app.batch_info.get("1.0", "end"))

                            dialog = SelectionDialog(app)
                            try:
                                width = min(p(960), size[0])
                                dialog.minsize(1, 1)
                                dialog.maxsize(width, size[1])
                                dialog.target.set("3000.00")
                                dialog.calculated(store.recommend("3000.00"))
                                for dialog_width in (width, 1184) if scale == 2.0 else (width,):
                                    dialog.geometry(f"{dialog_width}x{size[1]}+0+0")
                                    app.update()
                                    self.assertEqual(dialog.winfo_width(), dialog_width)
                                    tree = dialog.tree
                                    self.assert_first_row_visible(tree)
                                    first = tree.get_children()[0]
                                    for column in ("pick", "constraint", "amount"):
                                        x, _, column_width, _ = tree.bbox(first, column)
                                        self.assertGreaterEqual(x, 0, column)
                                        self.assertLessEqual(x + column_width, tree.winfo_width(), column)
                                    for widget in (dialog.target_entry, dialog.declared_entry, dialog.confirm,
                                                   dialog.calculate_button, dialog.notice, dialog.difference_badge,
                                                   dialog.progress_label, dialog.progress_canvas, dialog.interaction_hint):
                                        self.assert_inside(widget, dialog)
                                        self.assertGreaterEqual(widget.winfo_height(), widget.winfo_reqheight(), str(widget))
                            finally:
                                dialog.destroy()

                            store.state_path.write_bytes(store.state_path.read_bytes() + b"\n")
                            self.assertFalse(app.reload())
                            for index in range(3):
                                app.pages.select(index)
                                app.update()
                                self.assert_inside(app.status_label, app)
                                self.assert_inside(app.reconnect_button, app)
                                self.assertGreaterEqual(app.reconnect_button.winfo_width(), app.reconnect_button.winfo_reqwidth())
                            app.reconnect_button.invoke()
                            app.update()
                            self.assertTrue(app.session_ok)
                            self.assertFalse(app.reconnect_button.winfo_ismapped())
                        finally:
                            app.close_app()
        finally:
            display.tk.call("tk", "scaling", original_scaling)
            display.destroy()

    def test_windows_widgets_at_100_125_150_and_200_percent(self):
        enable_dpi_awareness()
        display = tk.Tk()
        display.withdraw()
        original_scaling = float(display.tk.call("tk", "scaling"))
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source = root / "source"
                write_invoice(source, amount="1002.50")
                for index in range(2, 14):
                    write_invoice(source, f"candidate-{index}.xml", number=f"26{index:018d}", amount="10.00")
                for scale in (1.0, 1.25, 1.5, 2.0):
                    with self.subTest(scale=scale):
                        display.tk.call("tk", "scaling", scale * 96 / 72)
                        app = Workbench(root / "workspace", root / "settings.json")
                        try:
                            p = app.theme.px
                            app.maxsize(p(1600), p(1100))
                            app.geometry(f"{p(1080)}x{p(640)}+0+0")
                            if not app.snapshot["invoices"]:
                                app.store.set_company(app.snapshot["revision"], BUYER, TAX)
                                app.store.import_folder(app.store.data["revision"], source)
                                app.reload()
                            app.load_invoice(app.snapshot["invoices"][0])
                            app.update()
                            self.assertAlmostEqual(app.theme.scale, scale, delta=0.02)
                            for widget in (*app.nav_buttons, *app.filter_control.buttons, app.search_entry,
                                           *app.details.control.buttons, app.review_button, app.archive_button):
                                self.assert_inside(widget, app)
                                self.assertGreaterEqual(widget.winfo_width(), widget.winfo_reqwidth(), str(widget))
                            self.assertGreater(app.form_scroll.canvas.winfo_height(), p(48))
                            self.assertGreaterEqual(int(ttk.Style(app).lookup("Treeview", "rowheight")), p(36))
                            scrollbars = [widget for widget in app.tree.master.winfo_children()
                                          if widget.winfo_class() == "TScrollbar"]
                            for scrollbar in scrollbars:
                                thickness = scrollbar.winfo_reqwidth() if str(scrollbar.cget("orient")) == "vertical" else scrollbar.winfo_reqheight()
                                self.assertGreaterEqual(thickness, p(8))
                            self.assertGreater(app.detail_pane.winfo_width(), p(340))
                            app.form_scroll.canvas.yview_moveto(1)
                            app.update()
                            self.assert_inside(app.review_button, app.detail_pane)
                            if scale == 1.25:
                                image_sample = Path(__file__).resolve().parent.parent / "samples" / "image-preview"
                                app.store.import_folder(app.snapshot["revision"], image_sample)
                                app.reload()
                                item = next(i for i in app.snapshot["invoices"] if not i["verification"]["passed"])
                                app.minsize(1, 1)
                                app.geometry("1306x668+0+0")
                                app.load_invoice(item)
                                app.update()
                                canvas = app.form_scroll.canvas
                                self.assertEqual((app.winfo_width(), app.winfo_height()), (1306, 668))
                                self.assertGreater(canvas.winfo_height(), p(48))
                                self.assert_inside(app.review_button, app.detail_pane)
                                self.assert_inside(app.archive_button, app.detail_pane)
                                canvas.yview_moveto((app.status_choice.winfo_y() - p(12)) / canvas.bbox("all")[3])
                                app.update()
                                self.assertGreater(canvas.yview()[0], 0)
                                self.assert_inside(app.status_choice, canvas)
                            dialog = SelectionDialog(app)
                            try:
                                dialog.maxsize(p(1200), p(900))
                                dialog.geometry(f"{p(840)}x{p(640)}+0+0")
                                dialog.calculated(app.store.recommend("1000.00"))
                                app.update()
                                for widget in (dialog.calculate_button, dialog.confirm, dialog.target_entry,
                                               dialog.declared_entry, dialog.difference_badge, dialog.tree):
                                    self.assert_inside(widget, dialog)
                                self.assertGreaterEqual(dialog.confirm.winfo_width(), dialog.confirm.winfo_reqwidth())
                                self.assertGreaterEqual(dialog.tree.winfo_height(), p(90))
                                dialog.target.set("2000.00")
                                dialog.calculated(app.store.recommend("2000.00"))
                                app.update()
                                self.assert_inside(dialog.notice, dialog)
                                self.assert_inside(dialog.confirm, dialog)
                                self.assertGreaterEqual(dialog.tree.winfo_height(), p(60))
                                if scale == 1.5:
                                    dialog.minsize(1, 1)
                                    dialog.geometry("1294x648+0+0")
                                    app.update()
                                    self.assertEqual((dialog.winfo_width(), dialog.winfo_height()), (1294, 648))
                                    self.assert_inside(dialog.tree, dialog)
                                    self.assert_inside(dialog.notice, dialog)
                                    self.assert_inside(dialog.confirm, dialog)
                                    dialog.tree.yview_moveto(1)
                                    app.update()
                                    last_row = dialog.tree.bbox(dialog.tree.get_children()[-1])
                                    self.assertTrue(last_row)
                                    self.assertLessEqual(last_row[1] + last_row[3], dialog.tree.winfo_height())
                            finally:
                                dialog.destroy()
                            app.pages.select(1)
                            app.update()
                            self.assert_inside(app.batch_tree, app)
                            self.assertGreater(app.batch_tree.winfo_height(), p(90))
                            app.pages.select(2)
                            app.update()
                            self.assert_inside(app.library_button, app)
                            self.assert_inside(app.reset_button, app)
                        finally:
                            app.dirty = False
                            app.close_app()
        finally:
            display.tk.call("tk", "scaling", original_scaling)
            display.destroy()
