"""Shared visual tokens and rounded surfaces for the desktop dashboard."""
import math
import tkinter as tk
from tkinter import font as tkfont

GAP = 12
RADIUS = 16
PADDING = 20
THEME_LABELS = {"system": "시스템 설정", "light": "라이트", "dark": "다크"}
PALETTES = {
    "light": {
        "bg": "#f3f5f9", "panel": "#ffffff", "card": "#ffffff",
        "fg": "#172238", "muted": "#59677d", "accent": "#236b59",
        "on_accent": "#ffffff", "border": "#dce2ec", "hover": "#eaf1ee",
        "input": "#f3f5f9", "warning": "#9c4617", "accent_hover": "#195343", "disabled": "#8793a6",
        "tags": ("#236b59", "#365dbc", "#945511", "#7849ac", "#b43f65"),
    },
    "dark": {
        "bg": "#10151f", "panel": "#1b2331", "card": "#1b2331",
        "fg": "#edf2fa", "muted": "#a6b3c7", "accent": "#7ce0bd",
        "on_accent": "#10271e", "border": "#344155", "hover": "#293b43",
        "input": "#121b28", "warning": "#ffbc8d", "accent_hover": "#a0ebd0", "disabled": "#728096",
        "tags": ("#7ce0bd", "#a1bdff", "#efc083", "#d0afff", "#ffa9c6"),
    },
}


def system_theme():
    """Windows app appearance; light is the fallback on other platforms."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as key:
            value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
        return "light" if value else "dark"
    except (ImportError, OSError):
        return "light"


def resolve_theme(mode):
    return system_theme() if mode == "system" else mode if mode in PALETTES else "light"


class TextHint:
    """Expose full text for controls whose viewport may be narrower than content."""
    def __init__(self, widget, text, colors):
        self.widget, self.text, self.colors = widget, text, colors
        self.timer = self.window = None
        widget.bind('<Enter>', self.schedule, add='+')
        widget.bind('<Motion>', self.schedule, add='+')
        for event in ('<Leave>', '<ButtonPress>', '<Destroy>'):
            widget.bind(event, self.hide, add='+')

    def schedule(self, event):
        self.hide()
        self.x, self.y = event.x, event.y
        self.timer = self.widget.after(500, self.show)

    def show(self):
        self.timer = None
        if not self.widget.winfo_exists():
            return
        value = self.text(self.x, self.y)
        if not value:
            return
        popup = self.window = tk.Toplevel(self.widget)
        popup.withdraw()
        popup.overrideredirect(True)
        popup.attributes('-topmost', True)
        width = min(700, self.widget.winfo_screenwidth() - 50)
        tk.Label(popup, text=value, font=('맑은 고딕', 11), justify='left',
                 bg=self.colors['panel'], fg=self.colors['fg'], padx=14, pady=10,
                 wraplength=width, highlightthickness=1, highlightbackground=self.colors['border']).pack()
        popup.update_idletasks()
        x = min(self.widget.winfo_rootx() + self.x + 12, self.widget.winfo_screenwidth() - popup.winfo_reqwidth() - 10)
        y = min(self.widget.winfo_rooty() + self.y + 24, self.widget.winfo_screenheight() - popup.winfo_reqheight() - 40)
        popup.geometry(f'+{max(0, x)}+{max(0, y)}')
        popup.deiconify()

    def hide(self, event=None):
        if self.timer is not None:
            self.widget.after_cancel(self.timer)
            self.timer = None
        if self.window is not None:
            self.window.destroy()
            self.window = None


class ThemedButton(tk.Canvas):
    """Rounded, keyboard-accessible button with explicit interaction states."""
    def __init__(self, parent, text, command, colors, accent=False):
        self.colors = colors
        self.options = {"text": text, "command": command, "state": "normal",
                        "bg": colors["accent"] if accent else colors["input"],
                        "fg": colors["on_accent"] if accent else colors["fg"]}
        self.hovered = self.pressed = self.focused = False
        self.font = tkfont.Font(root=parent, family="맑은 고딕", size=11)
        super().__init__(parent, bg=parent.cget("bg"), highlightthickness=0,
                         bd=0, takefocus=True, cursor="hand2",
                         width=self.font.measure(text) + 34, height=self.font.metrics("linespace") + 20)
        self.bind("<Configure>", lambda e: self.draw())
        self.bind("<Enter>", lambda e: self.interact(hover=True))
        self.bind("<Leave>", lambda e: self.interact(hover=False, pressed=False))
        self.bind("<ButtonPress-1>", self.press)
        self.bind("<ButtonRelease-1>", self.release)
        self.bind("<FocusIn>", lambda e: self.interact(focus=True))
        self.bind("<FocusOut>", lambda e: self.interact(focus=False))
        self.bind("<space>", lambda e: self.keyboard_invoke())
        self.bind("<Return>", lambda e: self.keyboard_invoke())
        self.draw()

    def interact(self, hover=None, pressed=None, focus=None):
        if hover is not None:
            self.hovered = hover
        if pressed is not None:
            self.pressed = pressed
        if focus is not None:
            self.focused = focus
        self.draw()

    def press(self, event):
        if self.options["state"] != "disabled":
            self.focus_set()
            self.interact(pressed=True)

    def release(self, event):
        invoke = self.pressed and 0 <= event.x < self.winfo_width() and 0 <= event.y < self.winfo_height()
        self.interact(pressed=False)
        if invoke:
            self.invoke()

    def keyboard_invoke(self):
        self.invoke()
        return "break"

    def invoke(self):
        if self.options["state"] != "disabled":
            return self.options["command"]()

    def configure(self, cnf=None, **kwargs):
        if isinstance(cnf, dict):
            kwargs.update(cnf)
            cnf = None
        changed = False
        for key in tuple(kwargs):
            if key in self.options:
                self.options[key] = kwargs.pop(key)
                changed = True
        result = super().configure(cnf, **kwargs) if cnf is not None or kwargs else None
        if changed:
            super().configure(cursor="arrow" if self.options["state"] == "disabled" else "hand2")
            self.draw()
        return result

    config = configure

    def cget(self, key):
        return self.options[key] if key in self.options else super().cget(key)

    def draw(self):
        self.delete("button")
        c = self.colors
        disabled = self.options["state"] == "disabled"
        primary = self.options["bg"] == c["accent"]
        fill = c["input"] if disabled else self.options["bg"]
        if (self.hovered or self.pressed) and not disabled:
            fill = c["accent_hover"] if primary else c["hover"]
        foreground = c["disabled"] if disabled else self.options["fg"]
        outline = c["accent"] if self.focused and not disabled else c["border"]
        if primary and not disabled:
            outline = fill
        w = max(2, self.winfo_width() if self.winfo_width() > 1 else int(super().cget("width")))
        h = max(2, self.winfo_height() if self.winfo_height() > 1 else int(super().cget("height")))
        radius = min(9, w / 2, h / 2)
        points = []
        for cx, cy, start in ((w-radius-1, radius+1, -90), (w-radius-1, h-radius-1, 0),
                              (radius+1, h-radius-1, 90), (radius+1, radius+1, 180)):
            for step in range(7):
                angle = math.radians(start + step * 15)
                points.extend((cx + radius * math.cos(angle), cy + radius * math.sin(angle)))
        self.create_polygon(points, fill=fill, outline=outline, width=1, tags="button")
        self.create_text(w / 2, h / 2 + (1 if self.pressed else 0), text=self.options["text"],
                         fill=foreground, font=self.font, tags="button")
        if self.focused and not disabled:
            self.create_line(12, h-6, w-12, h-6, fill=foreground if primary else c["accent"], tags="button")


class RoundedPanel(tk.Canvas):
    """A single radius and inset shared by sections, cards, and dialogs."""
    def __init__(self, parent, *, fill, border, padding=PADDING, **kwargs):
        super().__init__(parent, bg=parent.cget("bg"), highlightthickness=0,
                         bd=0, height=1, **kwargs)
        self.fill = fill
        self.border = border
        self.radius = RADIUS
        self.padding = padding
        self.body = tk.Frame(self, bg=fill)
        self.window = self.create_window(padding, padding, anchor="nw", window=self.body)
        self.bind("<Configure>", self._layout)
        self.body.bind("<Configure>", self._request_height)

    def _request_height(self, event):
        requested = self.body.winfo_reqheight() + self.padding * 2
        if int(self.cget("height")) != requested:
            self.configure(height=requested)

    def _layout(self, event):
        self._request_height(event)
        w, h = event.width - 1, event.height - 1
        r = min(self.radius, max(0, w / 2), max(0, h / 2))
        points = []
        for cx, cy, start in ((w-r, r, -90), (w-r, h-r, 0),
                              (r, h-r, 90), (r, r, 180)):
            for step in range(9):
                angle = math.radians(start + step * 90 / 8)
                points.extend((cx + r * math.cos(angle), cy + r * math.sin(angle)))
        if self.find_withtag("surface"):
            self.coords("surface", *points)
        else:
            self.create_polygon(points, fill=self.fill, outline=self.border,
                                width=1, tags="surface")
            self.tag_lower("surface")
        self.itemconfigure(self.window, width=max(1, event.width-self.padding*2),
                           height=max(1, event.height-self.padding*2))

    def set_border(self, color):
        self.itemconfigure("surface", outline=color)
