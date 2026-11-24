"""Countdown prompt shown before AI starts a new desktop control period.

The dialog runs in its own process: Tk needs a thread that owns its interpreter,
and a crash or hang there must not take the MCP server with it. Run as a script,
this file shows the dialog and reports the answer as its exit status, so the
child never writes to a stream shared with the stdio transport.

为什么不用 tk.Label / tk.Button 直接拼：
  那些是 Windows 原生控件的薄包装，出来的是灰色立体按钮、浅色对话框底、
  系统标题栏——和屏幕四周那圈蓝光、顶部那块深蓝面板完全是两套设计语言。
  用户看到的第一印象是"这什么年代的软件"。
  实测在 DPI 125% 下原生按钮还会糊。

现在的做法：整个窗口用一个 Canvas 手绘。圆角面板、深蓝渐变底、自绘按钮、
自绘关闭键、无边框。视觉上和 AI 控制指示器是同一个产品。
代价是按钮的悬停 / 按下状态得自己实现（见 _Button）。

无边框窗口有个副作用：拖不动、没有系统关闭按钮。所以这里手动实现
拖动（按住任意空白处移动窗口）和右上角关闭键（等价于"拒绝"）。
"""

import subprocess
import sys
import threading

# Distinct from 0/1, which a crashed interpreter can also return.
_EXIT_CODES = {"allow": 10, "deny": 11, "timeout": 12}
_lock = threading.Lock()
_process: subprocess.Popen | None = None


def ask(seconds: int) -> str:
    """Show the prompt and return allow, deny, timeout or error."""
    global _process
    with _lock:
        if _process is not None and _process.poll() is None:
            _process.kill()  # A cancelled request left its prompt open.
        process = _process = subprocess.Popen(
            # -I: never import modules from the user's working directory.
            [sys.executable, "-I", __file__, str(seconds)],
            # stdio transport: never share the MCP pipes.
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    try:
        code = process.wait(timeout=seconds + 15)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
        return "error"
    return next((answer for answer, value in _EXIT_CODES.items() if value == code), "error")


# ------------------------------------------------------------------ 配色
#
# 和 control_overlay_art.py 里的面板取同一套色：深蓝底 + 亮蓝描边 + 白字。
# 两边必须一起改，否则一个蓝一个灰，用户会以为是两个软件。
_BG_TOP = (10, 48, 98)        # 面板渐变的上缘
_BG_BOTTOM = (6, 32, 66)      # 面板渐变的下缘
_BORDER = (45, 145, 255)      # 和蓝光同一个蓝
_TITLE_FG = (248, 251, 255)
_BODY_FG = (190, 215, 245)
_MUTED_FG = (140, 168, 200)
_BTN_ALLOW_BG = (45, 145, 255)
_BTN_ALLOW_HOVER = (72, 165, 255)
_BTN_DENY_BG = (30, 52, 84)
_BTN_DENY_HOVER = (44, 70, 108)
_BTN_FG = (255, 255, 255)

_CORNER = 14                  # 面板圆角
_WIDTH = 420
_HEIGHT = 240


def _hex(rgb: tuple[int, int, int]) -> str:
    """(r, g, b) → Tk 认识的 '#rrggbb'。

    必须走这个函数。直接把元组传给 fill= 会报
    ``unknown color name "140 168 200"`` —— Tk 要的是字符串，
    而 Python 把元组 repr 成空格分隔的三个数。
    """
    return "#%02x%02x%02x" % rgb


def _font(face: str, size: int, weight: str = "normal"):
    """Tk 字体元组。负数字号表示像素，避免 Tk 按 72dpi 折算后变小。"""
    return (face, -size, weight) if weight != "normal" else (face, -size)


def _ui_face(root) -> str:
    """Pick a CJK-capable Tk family, falling back to the Latin default.

    Segoe UI carries no Han glyphs, so a Chinese label in it renders as boxes.
    思源黑体排第一：和蓝光面板用同一套字形，界面才像同一个产品。
    """
    import tkinter.font as tkfont

    try:
        families = set(tkfont.families(root))
    except Exception:
        return "Segoe UI"
    for face in (
        "Noto Sans SC", "Noto Sans SC Medium", "Source Han Sans SC",
        "Microsoft YaHei UI", "Microsoft YaHei", "DengXian", "SimHei", "SimSun",
    ):
        if face in families:
            return face
    return "Segoe UI"


def _vertical_gradient(width: int, height: int, top, bottom) -> "list[str]":
    """每行一个颜色，拼成 Canvas 的逐行矩形填充。

    Tk 的 Canvas 没有渐变，只能一行一行画。高度 240 就是 240 个矩形，
    一次性建好再复用，比每次重画快得多。
    """
    rows = []
    for y in range(height):
        t = y / max(1, height - 1)
        r = round(top[0] + (bottom[0] - top[0]) * t)
        g = round(top[1] + (bottom[1] - top[1]) * t)
        b = round(top[2] + (bottom[2] - top[2]) * t)
        rows.append(f"#{r:02x}{g:02x}{b:02x}")
    return rows


def _rounded_rect(canvas, x1, y1, x2, y2, radius, **kw):
    """Canvas 没有圆角矩形，用多边形加平滑逼近。"""
    points = [
        x1 + radius, y1, x2 - radius, y1, x2, y1,
        x2, y1 + radius, x2, y2 - radius, x2, y2,
        x2 - radius, y2, x1 + radius, y2, x1, y2,
        x1, y2 - radius, x1, y1 + radius, x1, y1,
    ]
    return canvas.create_polygon(points, smooth=True, **kw)


class _Button:
    """自绘按钮。

    Tk 的 tk.Button 是原生控件，改不了圆角和配色深度。这里用 Canvas 矩形
    自己画，鼠标进出时换背景色。text 用 Canvas 的 text item，坐标居中。
    """

    def __init__(self, canvas, x, y, w, h, text, face, *, bg, hover, fg,
                 command, bold=True):
        self.canvas = canvas
        self.command = command
        self.bg, self.hover = bg, hover
        self.rect = _rounded_rect(
            canvas, x, y, x + w, y + h, 8, fill=_hex(bg), outline=""
        )
        self.label = canvas.create_text(
            x + w / 2, y + h / 2, text=text,
            font=_font(face, 15, "bold" if bold else "normal"), fill=_hex(fg),
        )
        for item in (self.rect, self.label):
            canvas.tag_bind(item, "<Enter>", self._enter)
            canvas.tag_bind(item, "<Leave>", self._leave)
            canvas.tag_bind(item, "<Button-1>", self._click)

    def _enter(self, _event=None):
        self.canvas.itemconfigure(self.rect, fill=_hex(self.hover))
        self.canvas.configure(cursor="hand2")

    def _leave(self, _event=None):
        self.canvas.itemconfigure(self.rect, fill=_hex(self.bg))
        self.canvas.configure(cursor="")

    def _click(self, _event=None):
        self.command()


def _show(seconds: int) -> str:
    import ctypes
    import tkinter as tk

    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # Sharp text on scaled displays.
    except Exception:
        pass
    answer = "timeout"
    root = tk.Tk()

    def finish(value: str) -> None:
        nonlocal answer
        answer = value
        root.destroy()

    face = _ui_face(root)

    # 无边框：系统标题栏（那条灰色带最小化/关闭键的）是"简陋"感的最大来源。
    # 去掉之后整块都是可设计的画布。
    root.overrideredirect(True)
    root.attributes("-topmost", True)
    root.configure(bg=_hex(_BG_TOP))
    root.resizable(False, False)

    canvas = tk.Canvas(
        root, width=_WIDTH, height=_HEIGHT,
        highlightthickness=0, bd=0,
        bg=_hex(_BG_TOP),
    )
    canvas.pack()

    # 渐变底 + 圆角裁剪。
    # 做法：先用圆角多边形铺底色，再把渐变逐行画在上面，最后用四角的
    # 遮罩色盖掉溢出部分。Tk 没有裁剪区，只能靠"后画的盖住先画的"。
    _rounded_rect(canvas, 0, 0, _WIDTH, _HEIGHT, _CORNER, fill=_hex(_BG_TOP), outline="")
    for y, color in enumerate(_vertical_gradient(_WIDTH, _HEIGHT, _BG_TOP, _BG_BOTTOM)):
        canvas.create_line(0, y, _WIDTH, y, fill=color)
    # 再叠一次圆角边框，压住渐变溢出的直角
    _rounded_rect(
        canvas, 1, 1, _WIDTH - 1, _HEIGHT - 1, _CORNER,
        fill="", outline="#%02x%02x%02x" % _BORDER, width=2,
    )

    # 关闭键：无边框窗口没有系统关闭按钮，自己画一个。
    close = canvas.create_text(
        _WIDTH - 24, 22, text="✕", font=_font(face, 15), fill=_hex(_MUTED_FG)
    )
    canvas.tag_bind(close, "<Button-1>", lambda _e: finish("deny"))
    canvas.tag_bind(close, "<Enter>", lambda _e: (
        canvas.itemconfigure(close, fill=_hex(_TITLE_FG)), canvas.configure(cursor="hand2")
    ))
    canvas.tag_bind(close, "<Leave>", lambda _e: (
        canvas.itemconfigure(close, fill=_hex(_MUTED_FG)), canvas.configure(cursor="")
    ))

    canvas.create_text(
        _WIDTH / 2, 62, text="AI 即将接管这台电脑",
        font=_font(face, 22, "bold"), fill=_hex(_TITLE_FG),
    )

    countdown = canvas.create_text(
        _WIDTH / 2, 106, text="",
        font=_font(face, 17), fill=_hex(_BODY_FG),
    )

    canvas.create_text(
        _WIDTH / 2, 142, text="你不回应、继续用电脑，控制权就一直在你手上。",
        font=_font(face, 12), fill=_hex(_MUTED_FG),
    )

    # 按钮：允许在右（主操作），拒绝在左。和 Windows 对话框的习惯相反，
    # 但这里"允许"才是这个框存在的理由，放右侧更顺手。
    btn_w, btn_h = 150, 40
    gap = 16
    total = btn_w * 2 + gap
    left = (_WIDTH - total) / 2
    top = _HEIGHT - btn_h - 28

    _Button(
        canvas, left, top, btn_w, btn_h, "拒绝", face,
        bg=_BTN_DENY_BG, hover=_BTN_DENY_HOVER, fg=_BTN_FG,
        command=lambda: finish("deny"), bold=False,
    )
    _Button(
        canvas, left + btn_w + gap, top, btn_w, btn_h, "允许", face,
        bg=_BTN_ALLOW_BG, hover=_BTN_ALLOW_HOVER, fg=_BTN_FG,
        command=lambda: finish("allow"),
    )

    # 无边框窗口拖不动，手动实现：按住空白处移动整个窗口。
    # 只绑在画布上，按钮和关闭键有自己的 <Button-1>，不会被这里吃掉
    # （Tk 的事件先给具体 item，再给 canvas 本身）。
    drag = {"x": 0, "y": 0}

    def start_drag(event):
        drag["x"], drag["y"] = event.x_root, event.y_root

    def do_drag(event):
        dx = event.x_root - drag["x"]
        dy = event.y_root - drag["y"]
        root.geometry(f"+{root.winfo_x() + dx}+{root.winfo_y() + dy}")
        drag["x"], drag["y"] = event.x_root, event.y_root

    canvas.bind("<Button-1>", start_drag, add="+")
    canvas.bind("<B1-Motion>", do_drag, add="+")

    # Esc = 拒绝。有人习惯按 Esc 关框，给个明确语义而不是让倒计时跑完。
    root.bind("<Escape>", lambda _e: finish("deny"))

    def tick(left_s: int) -> None:
        if left_s <= 0:
            finish("timeout")
            return
        canvas.itemconfigure(countdown, text=f"{left_s} 秒后开始")
        root.after(1000, tick, left_s - 1)

    tick(seconds)
    root.update_idletasks()
    # 居中偏上（屏幕 1/3 处）：像系统提示，不像弹窗盖住工作区。
    x = (root.winfo_screenwidth() - _WIDTH) // 2
    y = (root.winfo_screenheight() - _HEIGHT) // 3
    root.geometry(f"+{x}+{y}")
    root.mainloop()
    return answer


if __name__ == "__main__":
    sys.exit(_EXIT_CODES[_show(int(sys.argv[1]))])
