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
    info_bg: str = "#E9EDFB"
    info: str = "#34489B"


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
        return self.badge_strip((text, tone))

    def badge_strip(self, *badges):
        if badges not in self.status_images:
            font = self.status_font
            height = self.px(24) if font else self.space.medium
            widths = [round(font.getlength(text)) + 2 * self.space.small if font else self.space.large
                      for text, _ in badges]
            gap = self.space.tiny
            image = Image.new("RGBA", (sum(widths) + gap * (len(widths) - 1), height))
            draw = ImageDraw.Draw(image)
            x = 0
            for (text, tone), width in zip(badges, widths):
                draw.rounded_rectangle((x, 0, x + width - 1, height - 1), radius=height // 2,
                                       fill=getattr(COLORS, tone + "_bg"))
                if font:
                    draw.text((x + width / 2, height / 2), text, font=font, anchor="mm",
                              fill=getattr(COLORS, tone))
                x += width + gap
            self.status_images[badges] = ImageTk.PhotoImage(image, master=self.root)
        return self.status_images[badges]

    def _rounded_layout(self, root, name, background, border, states=(), *, kind="Button", capsule=False, backdrop=None):
        radius = self.px(11 if capsule else 7)
        stretch_border = radius + 1
        minimum = 2 * stretch_border + self.px(4)
        # ttk 会平铺图像中心。增大平铺区域，同时维持控件原有的最小尺寸。
        size = 2 * stretch_border + self.px(64)

        def picture(fill, outline):
            image = Image.new("RGBA", (size, size), backdrop or (0, 0, 0, 0))
            ImageDraw.Draw(image).rounded_rectangle(
                (0, 0, size - 1, size - 1), radius=radius,
                fill=fill, outline=outline, width=1)
            photo = ImageTk.PhotoImage(image, master=root)
            self.images.append(photo)
            return photo

        self.style.element_create(name, "image", picture(background, border),
                                  *((state, picture(fill, outline)) for state, fill, outline in states),
                                  border=stretch_border, padding=0, sticky="nsew", width=minimum, height=minimum)
        if kind == "Frame":
            return [(name, {"sticky": "nsew"})]
        children = [(f"{kind}.label", {"sticky": "nsew"})]
        if kind == "Menubutton":
            children.insert(0, ("Menubutton.indicator", {"side": "right"}))
        return [(name, {"sticky": "nsew", "children": [
            (f"{kind}.padding", {"sticky": "nsew", "children": children})]})]

    def _configure(self, root):
        c, s, p = COLORS, self.style, self.px
        tiny, small, medium, large, extra_large = self.space
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
        s.layout("SettingsCard.TFrame", self._rounded_layout(root, "Workbench.settingsCard", c.canvas, c.line,
                                                            kind="Frame"))
        s.layout("Capsule.Segments.TFrame", self._rounded_layout(root, "Workbench.settingsSegments",
                                                                c.occupied_bg, c.line, kind="Frame", capsule=True))

        regular = self._rounded_layout(root, "Workbench.button", c.canvas, c.line, [
            ("disabled", c.surface, c.line), ("pressed", c.selected, c.line),
            ("focus", c.canvas, c.primary), ("active", c.hover, c.line)])
        primary_states = [
            ("disabled", c.line, c.line), ("pressed", c.primary, c.primary),
            ("focus", c.primary_hover, c.secondary), ("active", c.primary_hover, c.primary_hover)]
        primary = self._rounded_layout(root, "Workbench.primary", c.primary, c.primary, primary_states)
        menu = self._rounded_layout(root, "Workbench.menu", c.primary, c.primary, primary_states, kind="Menubutton")
        nav = self._rounded_layout(root, "Workbench.nav", c.sidebar, c.sidebar, [
            ("pressed", c.selected, c.selected), ("focus", c.sidebar, c.secondary),
            ("active", c.selected, c.selected)], backdrop=c.sidebar)
        selected_nav = self._rounded_layout(root, "Workbench.selectedNav", c.selected, c.selected, [
            ("focus", c.selected, c.secondary), ("active", c.line, c.line)], backdrop=c.sidebar)
        segment_states = [
            ("disabled", c.sidebar, c.sidebar), (("focus", "selected"), c.canvas, c.secondary),
            ("selected", c.canvas, c.line),
            ("focus", c.sidebar, c.secondary), ("active", c.selected, c.selected)]
        segment = self._rounded_layout(root, "Workbench.segment", c.sidebar, c.sidebar, segment_states,
                                       backdrop=c.sidebar)
        capsule_segment = self._rounded_layout(root, "Workbench.settingsSegment", c.occupied_bg, c.occupied_bg,
                                               segment_states, capsule=True, backdrop=c.occupied_bg)
        for name, layout in (("TButton", regular), ("Primary.TButton", primary),
                             ("Primary.TMenubutton", menu),
                             ("Nav.TButton", nav), ("Selected.Nav.TButton", selected_nav),
                             ("Pill.TRadiobutton", segment)):
            s.layout(name, layout)
            s.configure(name, padding=(medium, small), anchor="center", borderwidth=0)
            s.map(name, foreground=[("disabled", c.muted)])
        s.configure("Primary.TButton", foreground=c.canvas)
        s.map("Primary.TButton", foreground=[("disabled", c.secondary), ("!disabled", c.canvas)])
        s.configure("Primary.TMenubutton", width=0, foreground=c.canvas, arrowsize=p(10),
                    arrowcolor=c.canvas, arrowpadding=(small, 0, 0, 0))
        s.map("Primary.TMenubutton", foreground=[("disabled", c.secondary)],
              arrowcolor=[("disabled", c.secondary)])
        s.configure("Nav.TButton", anchor="w", foreground=c.secondary, padding=(large, medium))
        s.configure("Selected.Nav.TButton", foreground=c.text, font=(FONT, 10, "bold"))
        s.configure("Pill.TRadiobutton", padding=(medium, p(6)), foreground=c.secondary)
        s.configure("Compact.Pill.TRadiobutton", padding=(small, p(3)))
        s.map("Pill.TRadiobutton", foreground=[("disabled", c.muted), ("selected", c.text)])
        s.layout("Capsule.Pill.TRadiobutton", capsule_segment)
        s.configure("Capsule.Pill.TRadiobutton", padding=(small, p(5)))
        s.configure("Danger.TButton", foreground=c.error)
        s.configure("Quiet.TButton", width=0, padding=(small, p(6)), foreground=c.secondary)
        s.layout("SearchClear.TButton", [("Button.border", {"sticky": "nsew", "children": [
            ("Button.padding", {"sticky": "nsew", "children": [("Button.label", {"sticky": "nsew"})]})]})])
        s.configure("SearchClear.TButton", width=0, padding=(small, 0), foreground=c.secondary,
                    background=c.surface, borderwidth=0, relief="flat")
        s.map("SearchClear.TButton", background=[("disabled", c.surface), ("pressed", c.selected),
                                                ("active", c.occupied_bg), ("focus", c.occupied_bg)])
        s.layout("Link.TButton", self._rounded_layout(root, "Workbench.link", c.canvas, c.canvas, [
            ("focus", c.canvas, c.info), ("active", c.info_bg, c.info_bg)]))
        s.configure("Link.TButton", width=0, padding=(small, p(3)), foreground=c.info, font=(FONT, 9, "underline"))
        s.layout("Metric.TButton", self._rounded_layout(root, "Workbench.metric", c.canvas, c.canvas, [
            ("pressed", c.selected, c.selected), ("focus", c.canvas, c.secondary),
            ("active", c.hover, c.hover)]))
        s.layout("Selected.Metric.TButton", self._rounded_layout(root, "Workbench.selectedMetric", c.info_bg, c.info_bg, [
            ("focus", c.info_bg, c.info), ("active", c.info_bg, c.info)]))
        s.configure("Metric.TButton", width=0, anchor="w", padding=(small, tiny), foreground=c.secondary)
        s.configure("Selected.Metric.TButton", font=(FONT, 10, "bold"), foreground=c.info)
        s.configure("Metric.TLabel", font=(FONT, 10), padding=(small, tiny), foreground=c.secondary)
        disclosure = self._rounded_layout(root, "Workbench.disclosure", c.surface, c.surface, [
            ("pressed", c.selected, c.selected), ("focus", c.surface, c.secondary),
            ("active", c.selected, c.selected)])
        s.layout("Disclosure.TButton", disclosure)
        s.configure("Disclosure.TButton", anchor="w", padding=(small, p(6)), foreground=c.secondary)
        for tone in ("occupied", "info", "success", "warning", "error"):
            background = getattr(c, tone + "_bg")
            style = f"{tone}.StatusPill.TLabel"
            s.layout(style, self._rounded_layout(root, f"Workbench.status.{tone}", background, background,
                                                kind="Label", capsule=True))
            s.configure(style, foreground=getattr(c, tone), font=(FONT, 9), padding=(small, 0))
            s.configure(f"{tone}.SettingsFeedback.TLabel", background=background,
                        foreground=getattr(c, tone), font=(FONT, 9), padding=(small, p(5)))
        for tone in ("occupied", "warning"):
            background, foreground = getattr(c, tone + "_bg"), getattr(c, tone)
            style = f"{tone}.ActionPill.TButton"
            s.layout(style, self._rounded_layout(root, f"Workbench.action.{tone}", background, background, [
                ("pressed", c.selected, foreground), ("focus", background, foreground),
                ("active", background, foreground)], capsule=True))
            s.configure(style, width=0, foreground=foreground, font=(FONT, 9), padding=(medium, p(3)))
        for name in ("Compact.Primary.TButton", "Compact.Danger.TButton"):
            s.configure(name, padding=(p(4), p(5)))
        s.configure("Compact.TButton", padding=(small, p(3)))
        s.configure("Compact.Quiet.TButton", padding=(small, p(2)))

        s.configure("TEntry", padding=small, relief="flat", borderwidth=1,
                    fieldbackground=c.surface, insertcolor=c.text)
        s.map("TEntry", bordercolor=[("focus", c.secondary)],
              fieldbackground=[("readonly", c.sidebar), ("disabled", c.sidebar)],
              foreground=[("disabled", c.muted), ("readonly", c.secondary)])
        s.configure("Search.TEntry", background=c.surface, borderwidth=0,
                    bordercolor=c.surface, lightcolor=c.surface, darkcolor=c.surface)
        s.map("Search.TEntry", bordercolor=[("focus", c.surface)])
        s.configure("Compact.Search.TEntry", padding=(small, p(4)))
        s.configure("Amount.TEntry", font=(MONO, 22), padding=(medium, small))
        s.configure("TCombobox", padding=small, relief="flat", borderwidth=1,
                    arrowsize=p(12), fieldbackground=c.surface, background=c.surface)
        s.map("TCombobox", fieldbackground=[("readonly", c.surface)],
              bordercolor=[("focus", c.secondary)],
              foreground=[("disabled", c.muted), ("readonly", c.text)],
              selectbackground=[("readonly", c.surface)], selectforeground=[("readonly", c.text)])
        s.configure("Compact.TCombobox", padding=(small, p(4)))
        s.configure("Treeview", rowheight=max(p(36), font.nametofont("TkDefaultFont", root=root).metrics("linespace") + medium),
                    background=c.canvas, fieldbackground=c.canvas, foreground=c.text,
                    borderwidth=0, relief="flat", indent=0)
        s.layout("Treeview", [("Treeview.treearea", {"sticky": "nsew"})])
        s.configure("Treeview.Heading", padding=(small, p(10)), background=c.surface,
                    foreground=c.secondary, font=(FONT, 9), relief="flat", borderwidth=0)
        s.map("Treeview.Heading", background=[("active", c.sidebar)])
        s.map("Treeview", background=[("selected", c.selected)], foreground=[("selected", c.text)])
        s.configure("Invoice.Treeview", font=(MONO, 10), rowheight=p(40))
        s.configure("BatchHistory.Treeview", rowheight=p(40))
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


def wrap_label(label, *, inset=0):
    width = None

    def resize(event):
        nonlocal width
        available = event.width - inset
        if available > 1 and available != width:
            width = available
            label.configure(wraplength=width)

    label.bind("<Configure>", resize, add="+")


def fit_window(window, theme, width, height, minimum_width, minimum_height, *, parent=None):
    p = theme.px
    screen_width, screen_height = window.winfo_screenwidth(), window.winfo_screenheight()
    available_width = screen_width - p(48)
    available_height = screen_height - p(80)
    width, height = min(p(width), available_width), min(p(height), available_height)
    geometry = f"{width}x{height}"
    if parent is not None:
        parent.update_idletasks()
        cx = parent.winfo_rootx() + parent.winfo_width() // 2
        cy = parent.winfo_rooty() + parent.winfo_height() // 2
        x, y = cx - width // 2, cy - height // 2
        if 0 <= cx < screen_width and 0 <= cy < screen_height:
            x = min(max(0, x), screen_width - width)
            y = min(max(0, y), screen_height - height)
        geometry += f"+{x}+{y}"
    window.geometry(geometry)
    window.minsize(min(p(minimum_width), width), min(p(minimum_height), height))
