"""Screen tool — desktop capture and display metadata in one entry.

原来这里是三个工具：Snapshot（控件树 + 图）、Screenshot（只出图）、
DisplayInventory（显示器布局）。三者的参数是**包含关系** —— Screenshot 的六个参数
全在 Snapshot 里，DisplayInventory 一个参数都没有。合并只加了一个 mode，
参数表没有变长，工具数少两个。

mode 语义：

    full      控件树 + 截图，带可点坐标（原 Snapshot）
    fast      只出图，跳过控件树，快（原 Screenshot）
    displays  显示器布局 / DPI / 缩放（原 DisplayInventory，不需要截图）

**默认值是 full 而不是 fast**：调用方多半是"我要看着屏幕决定下一步"，取到坐标才有用。
要速度的人显式写 mode="fast"。
"""

import logging
from typing import Any, Literal

from mcp.types import ToolAnnotations
from windows_mcp.desktop.utils import as_bool as _as_bool
from windows_mcp.desktop.utils import as_int_list as _as_int_list
from windows_mcp.infrastructure import with_analytics
from fastmcp import Context

from windows_mcp.tools._snapshot_helpers import (
    _as_region,
    capture_desktop_state,
    build_snapshot_response,
)
from windows_mcp.desktop.screenshot_file import save_screenshot

logger = logging.getLogger(__name__)

# Populated by register(); exposed for backward-compatible test imports.
state_tool = None
screenshot_tool = None
display_inventory_tool = None


def _rect_to_dict(rect: Any) -> dict[str, int] | None:
    if rect is None:
        return None
    return {
        "left": rect.left,
        "top": rect.top,
        "right": rect.right,
        "bottom": rect.bottom,
        "width": rect.width(),
        "height": rect.height(),
    }


def _display_rows(desktop: Any) -> list[dict[str, object]]:
    return [
        {
            "index": display.index,
            "device": display.device_name,
            "primary": display.primary,
            "bounds": _rect_to_dict(display.rect),
            "work_area": _rect_to_dict(getattr(display, "work_rect", None)),
            "resolution": f"{display.rect.width()}x{display.rect.height()}",
            "orientation": getattr(display, "orientation", None),
            "effective_dpi": getattr(display, "effective_dpi", None),
            "scale": getattr(display, "scale", None),
        }
        for display in desktop.get_displays()
    ]


def register(mcp, *, get_desktop, get_analytics):
    global state_tool, screenshot_tool, display_inventory_tool

    @mcp.tool(
        name='Screen',
        description=(
            "看屏幕。Keywords: screenshot, screen capture, see screen, observe, look, inspect, "
            "UI elements, what's on screen, displays, DPI, monitor. "
            "Three modes: "
            "'full' (default) captures the whole desktop state — system language, focused/opened windows, "
            "interactive elements (buttons, text fields, links, menus with coordinates) and scrollable "
            "areas, plus the image; use this first before acting. "
            "'fast' skips UI tree extraction for speed and returns only the image with cursor position "
            "and window summaries; use it when you already know the coordinates. "
            "'displays' lists monitor layout and DPI metadata without capturing anything. "
            "Set use_vision=True to include the screenshot with cursor highlight; save_path implies it. "
            "Set use_annotation=False for a clean image without bounding boxes (default: True in full mode, "
            "False in fast mode). Set use_ui_tree=False in full mode for a faster screenshot-only pass. "
            "Set width_reference_line/height_reference_line to overlay a grid for spatial reasoning "
            "(requires the image). Set use_dom=True for browser content to get page elements instead of "
            "browser UI. "
            "Set display=[0] or display=[0,1] using zero-based active display indices; omit for the whole "
            "desktop. Set region=[left, top, right, bottom] in virtual-desktop pixels to capture only that "
            "rectangle — region wins over display when both are given, and an out-of-bounds region raises "
            "rather than silently capturing elsewhere. "
            "Set save_path to an absolute file path (or an existing directory for a timestamped name) to "
            "write the image as PNG; existing files are never overwritten, and the saved path is reported. "
            "Note: the returned image may be downscaled; when it is, multiply image coordinates by the "
            "ratio of original to displayed size to get real screen coordinates for Input."
        ),
        annotations=ToolAnnotations(
            title="Screen",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    @with_analytics(get_analytics(), "Screen-Tool", run_sync_in_thread=False)
    def screen_tool(
        mode: Literal['full', 'fast', 'displays'] = 'full',
        use_vision: bool | str = False,
        use_dom: bool | str = False,
        use_annotation: bool | str | None = None,
        use_ui_tree: bool | str = True,
        width_reference_line: int | None = None,
        height_reference_line: int | None = None,
        display: list[int] | str | None = None,
        region: list[int] | str | None = None,
        save_path: str | None = None,
        ctx: Context = None,
    ):
        act = (mode or 'full').strip().lower()

        if act == 'displays':
            # 纯元数据，不截图。给这里的其它截图参数会被忽略而不是报错 ——
            # 查显示器布局时顺手带着 display=[0] 是极自然的误用，报错只会打断流程。
            return _display_rows(get_desktop())

        if act not in ('full', 'fast'):
            raise ValueError(
                f"未知 mode {mode!r}。可用：full（控件树+图） / fast（只出图） / displays（显示器信息）"
            )

        fast = act == 'fast'
        # full 模式默认画标注框（上游 Snapshot 的行为）；fast 模式默认不画（上游 Screenshot 的行为）。
        # 用 None 当"没传"，才能保留两个原工具各自的默认值。
        annotate = not fast if use_annotation is None else _as_bool(use_annotation, 'use_annotation')
        full_mode = act == 'full'

        tool_label = "Screen fast" if fast else "Screen full"
        try:
            capture_result = capture_desktop_state(
                get_desktop(),
                # fast 模式必须出图；full 模式跟随 use_vision（save_path 会强制打开）。
                use_vision=True if fast else (_as_bool(use_vision, 'use_vision') or bool(save_path)),
                use_dom=False if fast else _as_bool(use_dom, 'use_dom'),
                use_annotation=annotate,
                use_ui_tree=False if fast else _as_bool(use_ui_tree, 'use_ui_tree'),
                width_reference_line=width_reference_line,
                height_reference_line=height_reference_line,
                display=_as_int_list(display, 'display'),
                region=_as_region(region),
                tool_name=f"{tool_label} tool",
            )
        except Exception as e:
            logger.warning(
                "Screen(%s) failed with display=%s region=%s use_vision=%s use_dom=%s",
                act,
                display,
                region,
                use_vision,
                use_dom,
                exc_info=True,
            )
            return [f'Error capturing desktop state ({act}): {str(e)}. Please try again.']

        saved_path = None
        if save_path:
            try:
                saved_path = save_screenshot(capture_result['screenshot_bytes'], save_path)
            except (OSError, ValueError) as e:
                return [f'Error saving screenshot: {e}']

        return build_snapshot_response(
            capture_result,
            include_ui_details=full_mode,
            saved_path=saved_path,
            # fast 模式不再自己写一句 note：_snapshot_helpers 里的 WINDOWS_NOT_LISTED
            # 已经把两处「Focused Window / Opened Windows」都替换成同一句带 mode 提示的
            # 文案了。之前这里再传一次，输出里会出现两条意思重复的说明。
            ui_detail_note=None,
        )

    # 保留两个旧名字给向后兼容的测试导入（上游有测试直接 import 这两个符号）。
    state_tool = screen_tool
    screenshot_tool = screen_tool
    display_inventory_tool = screen_tool
