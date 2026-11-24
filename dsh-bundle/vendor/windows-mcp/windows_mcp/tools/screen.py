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
import threading
from typing import Any, Literal

from mcp.types import ToolAnnotations
from windows_mcp.desktop.frame_cache import get_frame_cache
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


# ---------------------------------------------------------------- 帧去重
# 「同一会话上一帧的哈希」——对应 pilot 的 lastFrameHashes（lib/index.js:1480）。
#
# 为什么不是全局一个 key：Screen 支持 display / region / 标注框 / grid，四个参数
# 组合出的是**不同的图**。全桌面没变不代表某个 region 没变，标了框的图和没标框的
# 图也不是同一张（框的颜色是 random 生成的，标框模式下连"像素一样"都很难成立）。
# 所以 key 里要带会话、显示区域、region、标注框、grid、模式。
_FRAME_HASH_STATE: dict[tuple, str] = {}
_FRAME_HASH_LOCK = threading.Lock()

UNCHANGED_NOTE = (
    "Frame Unchanged Note: 画面自上一帧起没有变化（同一会话、同一块截图范围）："
    "你手上那张图和它给出的坐标仍然有效，不必重新测量，也不必再要一张图。"
    "这张图按去重规则没有重复附上；确实需要重出图就传 force_image=true。"
)


def _session_key(ctx: Any) -> str:
    """取当前 MCP 会话的标识，用来把「上一帧」按会话隔开。

    pilot 用的是 sessionId（lib/index.js:820）；Python 这边能拿到的是
    ``Context.session_id``。拿不到时（直接调用、单元测试、宿主不给 session）
    退化成 ``__no_session__``：效果等于"这个进程只有一个会话"，比抛错有用。
    """
    try:
        sid = getattr(ctx, "session_id", None)
    except Exception:  # noqa: BLE001 — 取 session 信息失败不该让截图挂掉
        sid = None
    return sid or "__no_session__"


def _frame_key(
    ctx: Any,
    *,
    display: list[int] | None,
    region: list[int] | None,
    annotate: bool,
    grid_lines: tuple[int, int] | None,
    fast: bool,
    tree_signature: str | None = None,
) -> tuple:
    """「同一帧」的判定范围。见文件上方 _FRAME_HASH_STATE 的注释。"""
    return (
        _session_key(ctx),
        tuple(display or ()),
        tuple(region or ()),
        bool(annotate),
        tuple(grid_lines) if grid_lines else None,
        bool(fast),
        tree_signature,
    )


def _raw_frame_hash(desktop_state: Any) -> str | None:
    """取这一帧**原始像素**的哈希。

    为什么不在这里对``desktop_state.screenshot``调 frame_hash()：那张图可能已经
    画上了标注框（框的颜色每帧随机，静止桌面也会得到不同哈希），也可能已经被缩过
    （缩放比例一变哈希就变）。两种都会让"画面没变"永远判成"变了"。

    真正的原始像素哈希是 ``capture_with_hash()`` 在 capture() 出口算的（用的是同一个
    ``frame_cache.frame_hash()``，64x64 网格采样，实测 3.1ms），这里只是把它读出来用，
    既不多算一遍哈希，也不会被上层的画图/缩图污染判断（见 FrameCache.last_digest）。
    """
    region = getattr(desktop_state, "screenshot_region", None)
    return get_frame_cache().last_digest(region)


# ---------------------------------------------------------------- 坐标契约
def _ratio_text(value: float) -> str:
    """1.25 -> "1.25"，1.3333333 -> "1.333"，2.0 -> "2"。"""
    return ('%.3f' % value).rstrip('0').rstrip('.')


def _coordinate_contract_lines(desktop_state: Any) -> list[str]:
    """把「图上 1 像素 = 屏幕多少像素」明确写出来。

    这是 pilot 的 coordinateContract（lib/index.js:980-1008）的简化版。

    为什么必须写：模型从一张被缩过的图上量出坐标，直接丢给 Input，就会点到偏掉的
    位置；而只说一句"the returned image may be downscaled"等于什么都没说 —— 它
    既不知道倍率，也不知道该不该乘系数，最后只能瞎猜。

    所以这里分三种情况，各有明确结论：
      1. 交付尺寸 == 原始尺寸  -> 就是 1:1，坐标直接用，别乘系数（但客户端可能
         再缩一次，所以同时给出按预览尺寸重算倍率的方法，不假装 1:1 一定成立）；
      2. 交付尺寸 < 原始尺寸   -> 给出倍率，以及"乘完再用"的算法；
      3. 本次没出图            -> 直说没有图像坐标可换算，别让调用方以为有。
    """
    screenshot = getattr(desktop_state, "screenshot", None)
    delivered = getattr(screenshot, "size", None) if screenshot is not None else None
    original = getattr(desktop_state, "screenshot_original_size", None)

    if not original or not delivered:
        return [
            'Coordinate Contract: 本次调用没有出图，没有图像坐标可换算；要看图请开 '
            'use_vision=true 或使用 mode="fast"。'
        ]

    orig_w, orig_h = int(original.width), int(original.height)
    got_w, got_h = int(delivered[0]), int(delivered[1])

    lines: list[str] = []
    region = getattr(desktop_state, "screenshot_region", None)
    if region is not None:
        # 区域 / 单屏截图时图的原点不在屏幕左上角，不写清楚模型会把 (0,0) 当成屏幕原点。
        lines.append(
            f"Coordinate Space: 本帧是区域/单屏截图，图上 (0,0) 对应屏幕 "
            f"({region.left},{region.top})，图上 (x,y) 对应屏幕 "
            f"({region.left}+x,{region.top}+y)；Input 一律填屏幕绝对坐标。"
        )

    if (orig_w, orig_h) == (got_w, got_h):
        lines.append(
            f"Coordinate Contract: 本帧按原始尺寸 {orig_w}x{orig_h} 交付，1:1 —— "
            f"图上 1 像素 = 屏幕 1 像素。控件树里的坐标和图上量到的坐标都是屏幕物理像素，"
            f"直接交给 Input，不需要乘任何系数。若预览里看到的尺寸不是 {orig_w}x{orig_h}"
            f"（客户端可能再缩一次），按 {orig_w}/预览宽、{orig_h}/预览高 重新算倍率。"
        )
        return lines

    ratio_x = orig_w / got_w
    ratio_y = orig_h / got_h
    if abs(ratio_x - ratio_y) < 0.002:
        lines.append(
            f"Coordinate Contract: 本帧原图 {orig_w}x{orig_h}，实际交付 {got_w}x{got_h}，"
            f"图上 1 像素 = 屏幕 {_ratio_text(ratio_x)} 像素。把图上量到的坐标乘 "
            f"{_ratio_text(ratio_x)} 再交给 Input；控件树里的坐标本来就是屏幕物理像素，"
            f"不要乘。若预览比 {got_w}x{got_h} 还小，倍率按 {orig_w}/预览宽 重算。"
        )
    else:
        lines.append(
            f"Coordinate Contract: 本帧原图 {orig_w}x{orig_h}，实际交付 {got_w}x{got_h}，"
            f"图上 1 像素 = 屏幕 {_ratio_text(ratio_x)} 像素（水平）/ "
            f"{_ratio_text(ratio_y)} 像素（垂直）。横纵要分别乘；控件树里的坐标已经是"
            f"屏幕物理像素，不要乘。若预览更小，倍率按原图/预览尺寸重算。"
        )
    return lines


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
            "Frame deduplication: the answer carries frame_hash and unchanged. unchanged=true means this "
            "frame is pixel-identical to the last frame this session was shown for the same capture "
            "scope (display/region/annotation/grid), so the image is NOT attached again and the "
            "coordinates you already have are still valid — do not re-measure, and do not ask for the "
            "picture again. Pass force_image=true to get the image anyway. "
            "Coordinate contract: the answer states, in one line, how image pixels relate to screen "
            "pixels (image pixel = screen pixel at original size, or the exact multiplier when the frame "
            "was downscaled). Coordinates in the UI tree are always screen physical pixels and need no "
            "multiplier; only coordinates you read off the picture itself may need one."
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
        force_image: bool | str = False,
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
        force_image = _as_bool(force_image, 'force_image')

        display_norm = _as_int_list(display, 'display')
        region_norm = _as_region(region)
        grid_lines = None
        if width_reference_line and height_reference_line:
            grid_lines = (int(width_reference_line), int(height_reference_line))

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
                display=display_norm,
                region=region_norm,
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

        desktop_state = capture_result['desktop_state']

        # ---- 帧哈希去重 ----
        # 判定放在**提交附件之前**（pilot 也是这么做的，lib/index.js:814-828）：
        # 既然这一帧和上一帧像素完全一样，就没有必要再把同一张图塞进上下文，
        # 那是一次纯浪费的往返（图是 token 大头）。调用方拿到 unchanged=true 加上
        # 一句中文提示，就能直接复用上一帧的坐标，跳过整轮重新测量。
        #
        # 只有**这次真出了图**才谈得上"这一帧"：full 模式不传 use_vision 时压根没截图，
        # 缓存里那份哈希是上一次开会话留下的，拿它判断会答出一个没有依据的 unchanged。
        frame_digest = _raw_frame_hash(desktop_state) if desktop_state.screenshot is not None else None
        unchanged = False
        if frame_digest:
            key = _frame_key(
                ctx,
                display=display_norm,
                region=region_norm,
                annotate=annotate,
                grid_lines=grid_lines,
                fast=fast,
                # 标注框的位置来自控件树，颜色来自 random。哈希只看得见**原始像素**，
                # 所以「树变了但像素没变」这种情况单靠哈希发现不了 —— 而它恰恰会改变
                # 标注框的位置，也就是会改变图上能读到的东西。既然串早就渲染出来了，
                # 顺手把它折进 key，成本是一次几千字节的字符串哈希。
                tree_signature=(
                    f"{capture_result.get('interactive_elements') or ''}"
                    f"\x00{capture_result.get('windows') or ''}"
                    if annotate
                    else None
                ),
            )
            with _FRAME_HASH_LOCK:
                previous = _FRAME_HASH_STATE.get(key)
                _FRAME_HASH_STATE[key] = frame_digest
            # force_image=true 的语义和 pilot 的 args.forceImage 一致：**只影响是否
            # 跳过这一张图**，不影响哈希记账 —— 否则"强制出图一次"会让下一帧的
            # 比较基准变成空，等于自己把去重关掉一轮。
            unchanged = previous == frame_digest and not force_image

        response = build_snapshot_response(
            capture_result,
            include_ui_details=full_mode,
            saved_path=saved_path,
            # fast 模式不再自己写一句 note：_snapshot_helpers 里的 WINDOWS_NOT_LISTED
            # 已经把两处「Focused Window / Opened Windows」都替换成同一句带 mode 提示的
            # 文案了。之前这里再传一次，输出里会出现两条意思重复的说明。
            ui_detail_note=None,
        )

        # 把契约和去重结论插进第一段文本（坐标信息必须在模型读到图之前就摆明）。
        extra_lines: list[str] = []
        if frame_digest:
            extra_lines.append(f"Frame Hash: {frame_digest}")
        extra_lines.append(f"Frame Unchanged: {'true' if unchanged else 'false'}")
        if unchanged:
            extra_lines.append(UNCHANGED_NOTE)
        extra_lines.extend(_coordinate_contract_lines(desktop_state))
        if isinstance(response[0], str):
            response[0] = response[0] + "\n" + "\n".join(extra_lines) + "\n"

        if unchanged:
            # 丢掉这一帧的图：留着的代价是整张 PNG 的 token，收益为零。
            # 注意 save_path 已经在上面处理过了 —— 要落盘的人仍然拿到了文件。
            response = [item for item in response if isinstance(item, str)]

        return response

    # 保留两个旧名字给向后兼容的测试导入（上游有测试直接 import 这两个符号）。
    state_tool = screen_tool
    screenshot_tool = screen_tool
    display_inventory_tool = screen_tool
