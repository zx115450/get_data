"""GUI 小部件与布局辅助。"""
import os
import tkinter as tk

# 使用 ttkbootstrap 主题，可选：darkly / flatly / litera / minty / pulse / superhero 等
_DEFAULT_THEME = os.getenv("GUI_THEME", "flatly")


class ToolTip:
    """简单悬浮提示，使用固定深色样式，跨主题稳定。"""

    def __init__(self, widget, text, delay=400):
        self.widget = widget
        self.text = text
        self.delay = delay
        self.tip = None
        self._after_id = None
        widget.bind("<Enter>", self._on_enter)
        widget.bind("<Leave>", self._on_leave)
        widget.bind("<Destroy>", self._on_leave)

    def _on_enter(self, _event=None):
        self._after_id = self.widget.after(self.delay, self._show)

    def _on_leave(self, _event=None):
        if self._after_id:
            self.widget.after_cancel(self._after_id)
            self._after_id = None
        self._hide()

    def _show(self):
        x, y, _, _ = self.widget.bbox("insert") or (0, 0, 0, 0)
        x += self.widget.winfo_rootx() + 20
        y += self.widget.winfo_rooty() + 24
        self.tip = tk.Toplevel(self.widget)
        self.tip.wm_overrideredirect(True)
        self.tip.wm_geometry(f"+{x}+{y}")
        tk.Label(
            self.tip, text=self.text, justify="left",
            background="#2b2b2b", foreground="#f0f0f0",
            relief="solid", borderwidth=1,
            font=("Segoe UI", 9), padx=6, pady=4,
        ).pack()

    def _hide(self):
        if self.tip:
            self.tip.destroy()
            self.tip = None


# 与后端 runner 阶段文案对齐，用于顶部阶段条
STAGES = [
    ("std", "准备标程"),
    ("agent", "Agent 写 gen/validator"),
    ("check", "校验产物"),
    ("batch", "批量生成 in/out"),
    ("pack", "打包 zip"),
]

# 窗口/布局断点（宽）
_BP_WIDE = 1280
_BP_NORMAL = 1100
_BP_COMPACT = 960
_BP_NARROW = 820
_MIN_W, _MIN_H = 760, 560


def _fit_to_screen(
    prefer_w: int,
    prefer_h: int,
    screen_w: int,
    screen_h: int,
    *,
    margin: int = 48,
    min_w: int = _MIN_W,
    min_h: int = _MIN_H,
) -> tuple[int, int, int, int]:
    """按屏幕裁剪尺寸并居中，返回 (w, h, x, y)。"""
    max_w = max(min_w, screen_w - margin)
    max_h = max(min_h, screen_h - margin)
    w = max(min_w, min(int(prefer_w), max_w))
    h = max(min_h, min(int(prefer_h), max_h))
    x = max(0, (screen_w - w) // 2)
    y = max(0, (screen_h - h) // 2)
    return w, h, x, y


def _try_enable_dpi_awareness() -> None:
    """Windows 下尽量开启 DPI 感知，避免高分屏控件发糊/错位。"""
    if os.name != "nt":
        return
    try:
        import ctypes
        # Per-monitor v2 → 系统感知，依次尝试
        for api in (
            lambda: ctypes.windll.shcore.SetProcessDpiAwareness(2),
            lambda: ctypes.windll.user32.SetProcessDPIAware(),
        ):
            try:
                api()
                return
            except Exception:
                continue
    except Exception:
        pass
