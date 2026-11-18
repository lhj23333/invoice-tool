from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import sys
from tkinter import font, ttk
from typing import NamedTuple

from PIL import Image, ImageDraw, ImageFont, ImageTk


@dataclass(frozen=True)
class Colors:
    sidebar: str = "#F7F7F8"
    canvas: str = "#FFFFFF"
    surface: str = "#FAFAFB"
    line: str = "#E5E7EB"
    text: str = "#111827"
    secondary: str = "#4B5563"
    muted: str = "#9CA3AF"
    primary: str = "#18181B"
    primary_hover: str = "#27272A"
    hover: str = "#F7F7F8"
    selected: str = "#EDEDEF"
    success_bg: str = "#ECFDF5"
    success: str = "#065F46"
    warning_bg: str = "#FFFBEB"
    warning: str = "#92400E"
    error_bg: str = "#FEF2F2"
    error: str = "#991B1B"
    occupied_bg: str = "#F3F4F6"
    occupied: str = "#4B5563"


COLORS = Colors()
FONT = "Microsoft YaHei UI"
MONO = "Consolas"


class Spacing(NamedTuple):
    tiny: int = 4
    small: int = 8
    medium: int = 12
    large: int = 16
    extra_large: int = 24


SPACE = Spacing()


def enable_dpi_awareness():
    if sys.platform == "win32":
        import ctypes
        # 进程已设置 DPI 模式时，Windows 保留已有设置。
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))


class Theme:
    def __init__(self, root):
        self.scale = float(root.tk.call("tk", "scaling")) / (96 / 72)
        self.space = Spacing(*(self.px(value) for value in SPACE))
        self.root = root
        self.images = []
        self.status_images = {}
        font_path = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / "msyh.ttc"
        self.status_font = ImageFont.truetype(str(font_path), self.px(13), index=1) if font_path.is_file() else None
        root.configure(background=COLORS.canvas)
        for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont"):
            font.nametofont(name, root=root).configure(family=FONT, size=10)
        font.nametofont("TkFixedFont", root=root).configure(family=MONO, size=10)
        root.option_add("*TCombobox*Listbox.font", (FONT, 10))
        self.style = ttk.Style(root)
        self.style.theme_use("clam")
        self._configure(root)

    def px(self, value):
        return max(1, round(value * self.scale)) if value else 0

    def status_badge(self, text, tone):
        key = (text, tone)
        if key not in self.status_images:
            font = self.status_font
            width = round(font.getlength(text)) + 2 * self.space.small if font else self.space.large
            height = self.px(24) if font else self.space.medium
            image = Image.new("RGBA", (width, height))
            draw = ImageDraw.Draw(image)
            draw.rounded_rectangle((0, 0, width - 1, height - 1), radius=height // 2,
                                   fill=getattr(COLORS, tone + "_bg"))
            if font:
                draw.text((width / 2, height / 2), text, font=font, anchor="mm",
                          fill=getattr(COLORS, tone))
            self.status_images[key] = ImageTk.PhotoImage(image, master=self.root)
        return self.status_images[key]

    def _button_element(self, root, name, background, border, states):
        size, radius = self.px(24), self.px(7)

        def picture(fill, outline):
            image = Image.new("RGBA", (size, size))
            ImageDraw.Draw(image).rounded_rectangle(
                (0, 0, size - 1, size - 1), radius=radius,
                fill=fill, outline=outline, width=1)
            photo = ImageTk.PhotoImage(image, master=root)
            self.images.append(photo)
            return photo

        self.style.element_create(name, "image", picture(background, border),
                                  *((state, picture(fill, outline)) for state, fill, outline in states),
                                  border=radius + 1, padding=0, sticky="nsew")
        return [(name, {"sticky": "nsew", "children": [
            ("Button.padding", {"sticky": "nsew", "children": [
                ("Button.label", {"sticky": "nsew"})]})]})]

    def _configure(self, root):
        c, s, p = COLORS, self.style, self.px
        _, small, medium, large, extra_large = self.space
        s.configure(".", font=(FONT, 10), background=c.canvas, foreground=c.text,
                    bordercolor=c.line, lightcolor=c.line, darkcolor=c.line)
        s.configure("TFrame", background=c.canvas)
        s.configure("Surface.TFrame", background=c.surface)
        s.configure("Sidebar.TFrame", background=c.sidebar)
        s.configure("TLabel", background=c.canvas, foreground=c.text)
        s.configure("Muted.TLabel", foreground=c.secondary)
        s.configure("Hint.TLabel", foreground=c.muted, font=(FONT, 9))
        s.configure("Title.TLabel", font=(FONT, 20, "bold"))
        s.configure("Section.TLabel", font=(FONT, 12, "bold"))
        s.configure("Stat.TLabel", font=(MONO, 18, "bold"))
        s.configure("Surface.TLabel", background=c.surface)
        s.configure("SurfaceMuted.TLabel", background=c.surface, foreground=c.secondary)
        s.configure("Sidebar.TLabel", background=c.sidebar, foreground=c.secondary, font=(FONT, 9))
        s.configure("TSeparator", background=c.line)

        regular = self._button_element(root, "Workbench.button", c.canvas, c.line, [
            ("disabled", c.surface, c.line), ("pressed", c.selected, c.line),
            ("focus", c.canvas, c.primary), ("active", c.hover, c.line)])
        primary = self._button_element(root, "Workbench.primary", c.primary, c.primary, [
            ("disabled", c.line, c.line), ("pressed", c.primary, c.primary),
            ("focus", c.primary_hover, c.secondary), ("active", c.primary_hover, c.primary_hover)])
        nav = self._button_element(root, "Workbench.nav", c.sidebar, c.sidebar, [
            ("pressed", c.selected, c.selected), ("focus", c.sidebar, c.secondary),
            ("active", c.selected, c.selected)])
        selected_nav = self._button_element(root, "Workbench.selectedNav", c.selected, c.selected, [
            ("focus", c.selected, c.secondary), ("active", c.line, c.line)])
        pills = self._button_element(root, "Workbench.pill", c.sidebar, c.sidebar, [
            ("disabled", c.sidebar, c.sidebar), ("selected", c.canvas, c.line),
            ("focus", c.sidebar, c.secondary), ("active", c.selected, c.selected)])
        for name, layout in (("TButton", regular), ("Primary.TButton", primary),
                             ("Nav.TButton", nav), ("Selected.Nav.TButton", selected_nav),
                             ("Pill.TRadiobutton", pills)):
            s.layout(name, layout)
            s.configure(name, padding=(medium, small), anchor="center", borderwidth=0)
            s.map(name, foreground=[("disabled", c.muted)])
        s.configure("Primary.TButton", foreground=c.canvas)
        s.map("Primary.TButton", foreground=[("disabled", c.secondary), ("!disabled", c.canvas)])
        s.configure("Nav.TButton", anchor="w", foreground=c.secondary, padding=(large, medium))
        s.configure("Selected.Nav.TButton", foreground=c.text, font=(FONT, 10, "bold"))
        s.configure("Pill.TRadiobutton", padding=(medium, p(6)), foreground=c.secondary)
        s.configure("Compact.Pill.TRadiobutton", padding=(small, p(6)))
        s.map("Pill.TRadiobutton", foreground=[("disabled", c.muted), ("selected", c.text)])
        s.configure("Danger.TButton", foreground=c.error)
        s.configure("Quiet.TButton", padding=(small, p(6)), foreground=c.secondary)
        for name in ("Compact.Primary.TButton", "Compact.Danger.TButton"):
            s.configure(name, padding=small)

        s.configure("TEntry", padding=small, relief="flat", borderwidth=1,
                    fieldbackground=c.surface, insertcolor=c.text)
        s.map("TEntry", bordercolor=[("focus", c.secondary)],
              fieldbackground=[("readonly", c.sidebar), ("disabled", c.sidebar)],
              foreground=[("disabled", c.muted), ("readonly", c.secondary)])
        s.configure("Amount.TEntry", font=(MONO, 22), padding=(medium, small))
        s.configure("TCombobox", padding=small, relief="flat", borderwidth=1,
                    arrowsize=p(12), fieldbackground=c.surface, background=c.surface)
        s.map("TCombobox", fieldbackground=[("readonly", c.surface)],
              bordercolor=[("focus", c.secondary)],
              foreground=[("disabled", c.muted), ("readonly", c.text)],
              selectbackground=[("readonly", c.surface)], selectforeground=[("readonly", c.text)])
        s.configure("Treeview", rowheight=max(p(36), font.nametofont("TkDefaultFont", root=root).metrics("linespace") + medium),
                    background=c.canvas, fieldbackground=c.canvas, foreground=c.text,
                    borderwidth=0, relief="flat", indent=0)
        s.layout("Treeview", [("Treeview.treearea", {"sticky": "nsew"})])
        s.configure("Treeview.Heading", padding=(small, p(10)), background=c.surface,
                    foreground=c.secondary, font=(FONT, 9), relief="flat", borderwidth=0)
        s.map("Treeview.Heading", background=[("active", c.sidebar)])
        s.map("Treeview", background=[("selected", c.selected)], foreground=[("selected", c.text)])
        for orient in ("Vertical", "Horizontal"):
            s.layout(f"{orient}.TScrollbar", [(f"{orient}.Scrollbar.trough", {
                "sticky": "ns" if orient == "Vertical" else "ew", "children": [
                    (f"{orient}.Scrollbar.thumb", {"sticky": "nsew", "expand": True})]})])
            s.configure(f"{orient}.TScrollbar", arrowsize=small, gripsize=0, sliderlength=extra_large, borderwidth=0,
                        relief="flat", background=c.line, troughcolor=c.canvas,
                        bordercolor=c.line, lightcolor=c.line, darkcolor=c.line)
            s.map(f"{orient}.TScrollbar", background=[("active", c.muted), ("pressed", c.secondary)])
        s.configure("TPanedwindow", background=c.line)
        s.configure("Sash", sashthickness=p(1))
        s.configure("Horizontal.TProgressbar", background=c.primary, troughcolor=c.line,
                    borderwidth=0, thickness=p(3))


def wrap_label(label):
    label.bind("<Configure>", lambda event: label.configure(wraplength=max(1, event.width)), add="+")


def fit_window(window, theme, width, height, minimum_width, minimum_height):
    p = theme.px
    available_width = window.winfo_screenwidth() - p(48)
    available_height = window.winfo_screenheight() - p(80)
    width, height = min(p(width), available_width), min(p(height), available_height)
    window.geometry(f"{width}x{height}")
    window.minsize(min(p(minimum_width), width), min(p(minimum_height), height))
