from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from .theme import COLORS


class SegmentedControl(ttk.Frame):
    def __init__(self, master, labels, *, variable=None, command=None, theme):
        super().__init__(master, style="Sidebar.TFrame", padding=theme.space.tiny)
        self.variable = variable if variable is not None else tk.StringVar(self, value=labels[0])
        self.buttons = []
        for index, label in enumerate(labels):
            button = ttk.Radiobutton(self, text=label, value=label, variable=self.variable,
                                     style="Pill.TRadiobutton", command=command)
            button.grid(row=0, column=index, sticky="ew")
            self.columnconfigure(index, weight=1, uniform="segments")
            self.buttons.append(button)
            button.bind("<Right>", lambda event, n=index: self._move(n, 1))
            button.bind("<Left>", lambda event, n=index: self._move(n, -1))

    def _move(self, index, direction):
        button = self.buttons[(index + direction) % len(self.buttons)]
        button.focus_set()
        button.invoke()
        return "break"


class SegmentedPages(ttk.Frame):
    def __init__(self, master, labels, *, theme):
        super().__init__(master)
        self.labels = tuple(labels)
        self.variable = tk.StringVar(self, value=labels[0])
        self.bar = ttk.Frame(self)
        self.bar.pack(fill="x", pady=(0, theme.space.medium))
        self.control = SegmentedControl(self.bar, labels, variable=self.variable,
                                        command=self._changed, theme=theme)
        self.control.pack(side="left", fill="x", expand=True)
        self.body = ttk.Frame(self)
        self.body.pack(fill="both", expand=True)
        self.body.columnconfigure(0, weight=1)
        self.body.rowconfigure(0, weight=1)
        self.frames = [ttk.Frame(self.body) for _ in labels]
        self.current_index = 0
        self.frames[0].grid(row=0, column=0, sticky="nsew")

    def select(self, index):
        self.variable.set(self.labels[index])
        self._changed()

    def _changed(self):
        index = self.labels.index(self.variable.get())
        if index == self.current_index:
            return
        self.frames[self.current_index].grid_remove()
        self.current_index = index
        self.frames[index].grid(row=0, column=0, sticky="nsew")
        self.event_generate("<<SegmentedPageChanged>>", when="tail")


class ScrollableFrame(ttk.Frame):
    def __init__(self, master, *, theme, background=COLORS.canvas, style="TFrame"):
        super().__init__(master, style=style)
        self.canvas = tk.Canvas(self, background=background, highlightthickness=0,
                                width=1, height=1, yscrollincrement=theme.px(24))
        scroll = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        scroll.pack(side="right", fill="y")
        self.canvas.configure(yscrollcommand=scroll.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.content = ttk.Frame(self.canvas, style=style)
        window = self.canvas.create_window(0, 0, window=self.content, anchor="nw")
        self.content.bind("<Configure>", lambda event: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda event: self.canvas.itemconfigure(window, width=event.width))
        self.wheel_tag = f"InvoiceScroll:{self}"
        self.bind_class(self.wheel_tag, "<MouseWheel>", self._wheel)
        self.bind("<Map>", lambda event: self._tag_children(self))
        self.bind("<Destroy>", self._cleanup)

    def _tag_children(self, widget):
        for child in (widget, *widget.winfo_children()):
            tags = child.bindtags()
            if self.wheel_tag not in tags:
                child.bindtags((tags[0], self.wheel_tag, *tags[1:]))
            if child is not widget:
                self._tag_children(child)

    def enable_scrolling(self):
        self._tag_children(self)

    def _wheel(self, event):
        if self.canvas.yview() != (0.0, 1.0):
            self.canvas.yview_scroll(-int(event.delta / 120), "units")
        return "break"

    def _cleanup(self, event):
        if event.widget is self:
            self.unbind_class(self.wheel_tag, "<MouseWheel>")


def table_scrollers(parent, tree):
    parent.columnconfigure(0, weight=1)
    parent.rowconfigure(0, weight=1)
    tree.grid(row=0, column=0, sticky="nsew")
    vertical = ttk.Scrollbar(parent, orient="vertical", command=tree.yview)
    horizontal = ttk.Scrollbar(parent, orient="horizontal", command=tree.xview)
    vertical.grid(row=0, column=1, sticky="ns")
    horizontal.grid(row=1, column=0, sticky="ew")
    tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)


def hover_rows(tree):
    tree.tag_configure("hover", background=COLORS.hover)
    hovered = None

    def update(event=None):
        nonlocal hovered
        current = tree.identify_row(event.y) if event is not None else None
        if current == hovered and (not current or tree.tag_has("hover", current)):
            return
        if hovered and tree.exists(hovered):
            tree.item(hovered, tags=tuple(tag for tag in tree.item(hovered, "tags") if tag != "hover"))
        hovered = current
        if current:
            tree.item(current, tags=("hover", *tree.item(current, "tags")))

    tree.bind("<Motion>", update, add="+")
    tree.bind("<Leave>", lambda event: update(), add="+")
