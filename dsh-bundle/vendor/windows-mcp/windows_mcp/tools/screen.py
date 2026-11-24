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
# 「这块屏幕区域上一帧的哈希」——对应 pilot 的 lastFrameHashes（lib/index.js:1480）。
#
# **key 里为什么不再带 MCP 会话**（原来第一维就是 ctx.session_id）：
#
# 屏幕是全局唯一的物理资源，不随 MCP 会话变化。"上一帧"对使用者来说就是
# 「同一块截图范围上一次拍出来是什么样」，跟"哪条连接问的"没有关系。
# 而 ctx.session_id 在 DSH 那条路上**每次请求都是新 UUID**：DSH 的 MCP 客户端
# 协商的是 2026-07-28 协议，这一代协议下每个请求自成一条连接，FastMCP 于是
# 每次现造会话标识（fastmcp/server/context.py 的 session_id 文档里写得很明白：
# "every request is its own connection and gets a new identifier, so it can't be
# used to share data between tool calls"）。实测日志里连拍两次是两个不同的 UUID，
# 于是 previous 永远取不到值、unchanged 永远 false —— 去重等于没开。
#
# 直连 stdio 之所以"看起来正常"，恰恰是因为那边 ctx.session_id 抛 RuntimeError
# 被吞成 __no_session__，所有调用退化成同一个 key —— 那正好就是全局去重。
# 换句话说：直连能工作是因为去重**退化成全局**，不是因为"按会话分开"是对的。
#
# 顺带修掉一个内存泄漏：按会话分 key 时，DSH 每调一次就往 _FRAME_HASH_STATE 里
# 塞一个永不回收的新条目。现在 key 只由截图范围决定，天然有界；再加一道
# _FRAME_HASH_MAX 上限兜底，防 region 满天飞时无界增长。
#
# key 里仍然要带 display / region / 标注框 / grid / 模式：这几个参数组合出来的
# 是**不同的图**。全桌面没变不代表某个 region 没变，标了框的图和没标框的图也不
# 是同一张（框的颜色是 random 生成的，标框模式下连"像素一样"都很难成立）。
_FRAME_HASH_STATE: dict[tuple, str] = {}
_FRAME_HASH_LOCK = threading.Lock()

# 去重状态的条目上限。正常用法只有几十条（region/display 的有限组合），这个上限
# 只为兜住"有人拿随机 region 刷"的极端情况：超了就从最久没被写入的那条开始扔。
# 扔掉的代价仅仅是那块区域下一次判成"变了"（多一张图），不会答错。
_FRAME_HASH_MAX = 256

# 去重命中时的说明文案。
#
# 为什么写得这么啰嗦、还要反问调用方一句"你手上有没有图"：去重状态是进程级
# **全局**的（理由见上面 _FRAME_HASH_STATE 的注释），它不区分调用方。两个智能体
# 同时用这个工具时完全可能这样：A 拍了区域 X，B 紧接着拍区域 X 且画面没变，
# 于是 B 也拿到 unchanged=true。对 B 来说"画面没变"是真的，但**B 手上并没有那张
# 图** —— 如果文案直接说"你手上那张图仍然有效"，B 就会拿着一份不存在的图像去
# 量坐标，这是静默错误。
#
# 唯一诚实且可自证的做法是把两件事拆开说：
#   1. 画面没变（unchanged: true）—— 服务端能确定的事实，说满；
#   2. 这次附图了没有（Image Attached: false）—— 也是服务端能确定的事实；
#   3. 要不要复用上一帧 —— 这是**调用方自己**才知道的事（它手上有没有图），
#      所以文案把判断依据和补救动作明确交回给调用方：没有图就 force_image=true。
# 这样做的代价最多是一次多余往返，而不是一次基于幻觉坐标的误点击。
UNCHANGED_NOTE = (
    "Frame Unchanged Note: 这一帧和本进程上一次拍同一块截图范围的结果像素完全一致，"
    "所以按去重规则没有重复附图（Image Attached: false）。"
    "这个「上一帧」是**进程级全局**的，不区分是哪个调用方拍的 —— 如果同时有别的"
    "智能体在拍同一块区域，那帧可能是它拍的。请先核对一件事：你手上到底有没有"
    "这块区域的上一帧图像？"
    "有 -> 那张图和它给出的坐标仍然有效，直接用，不必重新测量，也不必再要图；"
    "没有（例如你这次才第一次拍这块区域）-> 立刻带 force_image=true 再调一次，"
    "这一帧就会附图。force_image 只影响本次是否附图，不会把去重关掉。"
)


def _frame_key(
    *,
    display: list[int] | None,
    region: list[int] | None,
    annotate: bool,
    grid_lines: tuple[int, int] | None,
    fast: bool,
    tree_signature: str | None = None,
) -> tuple:
    """「同一帧」的判定范围：只由截图范围本身决定，**不含会话维度**。

    见文件上方 _FRAME_HASH_STATE 的注释：会话维度是错的（屏幕是全局资源），
    而且它在 DSH 那条路上每次都变，等于把去重整个关掉。
    """
    return (
        tuple(display or ()),
        tuple(region or ()),
        bool(annotate),
        tuple(grid_lines) if grid_lines else None,
        bool(fast),
        tree_signature,
    )


def _remember_frame(key: tuple, digest: str) -> str | None:
    """记下这一帧的哈希，返回同一 key 上一次的哈希（没记录过就是 None）。

    读+写在**同一把锁**里：并发调用时不会出现两次都读到同一个 previous 的竞态
    （那会让两个调用同时判成"变了"，只是少省一次图，但既然锁是现成的就没必要留）。
    写入时把 key 挪到字典末尾（先 pop 再赋值），于是淘汰顺序是"最久没被写入"而不是
    "最早建出来"，频繁用的区域不会被冷区域挤掉。
    """
    with _FRAME_HASH_LOCK:
        previous = _FRAME_HASH_STATE.pop(key, None)
        _FRAME_HASH_STATE[key] = digest
        while len(_FRAME_HASH_STATE) > _FRAME_HASH_MAX:
            oldest = next(iter(_FRAME_HASH_STATE))
            # 刚写进去的就是最新的一条，理论上不会成为 oldest；留着这层判断是为了
            # 万一 _FRAME_HASH_MAX 被调到 0 时不至于把刚写的数据删掉。
            if oldest == key:
                break
            del _FRAME_HASH_STATE[oldest]
        return previous


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


def _unchanged_coordinate_lines(desktop_state: Any) -> list[str]:
    """去重命中时的坐标说明：只说「按你手上那一帧的契约来」。

    为什么不能复用 _coordinate_contract_lines()：那个函数是按"本次交付了多大一张图"
    算倍率的，而去重命中时图会被剥掉。对着没有给出去的图讲"本帧按 200x100 交付、
    图上 1 像素 = 屏幕 1 像素"，等于暗示调用方它拿到了图，和实际正好相反。

    这里唯一能确定、也唯一有用的话是：画面没变所以倍率也没变，用你手上那一帧的契约；
    没有那一帧就别猜。区域原点仍然要给 —— 它只取决于这次 request 的 region 参数，
    和图有没有附无关，写出来不会误导。
    """
    region = getattr(desktop_state, "screenshot_region", None)
    lines: list[str] = []
    if region is not None:
        lines.append(
            f"Coordinate Space: 本帧是区域/单屏截图，图上 (0,0) 对应屏幕 "
            f"({region.left},{region.top})，图上 (x,y) 对应屏幕 "
            f"({region.left}+x,{region.top}+y)；Input 一律填屏幕绝对坐标。"
        )
    lines.append(
        "Coordinate Contract: 本次未附图（去重命中），倍率和你手上那一帧完全相同，"
        "按那一帧给出的契约换算即可。手上没有那一帧就不要推算倍率 —— "
        "带 force_image=true 重新调用，会附图并重新给出契约。"
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
            "Frame deduplication: the answer carries frame_hash, unchanged and image_attached. "
            "unchanged=true means this frame is pixel-identical to the last frame captured for the "
            "same capture scope (display/region/annotation/grid) — that comparison is process-wide "
            "and does NOT distinguish callers, because the screen is one physical resource, so "
            "another agent may have captured that frame. When unchanged=true the image is NOT "
            "attached again; check Image Attached: if you already hold the previous frame for this "
            "scope its coordinates are still valid — do not re-measure and do not ask for the "
            "picture again; if you do not hold it, call again with force_image=true to get the "
            "image. Pass force_image=true to get the image anyway; it only affects whether this "
            "call attaches the image, never the dedupe bookkeeping. "
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
        # 缓存里那份哈希是上一次留下的，拿它判断会答出一个没有依据的 unchanged。
        frame_digest = _raw_frame_hash(desktop_state) if desktop_state.screenshot is not None else None
        unchanged = False
        if frame_digest:
            key = _frame_key(
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
            # 记账（更新 _FRAME_HASH_STATE）和"这次附不附图"是两件事，不能混：
            #   - 记账必须无条件做，否则 force_image 一次就会把比较基准清空，
            #     等于自己把去重关掉一轮（pilot 的 args.forceImage 也是这样）；
            #   - 附图则看 force_image：它只影响这一张图附不附。
            previous = _remember_frame(key, frame_digest)
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
        # "附没附图"要单独说一句，不能只靠 unchanged 反推，也不能靠"算出来的图"反推。
        #
        # 去重状态是进程级全局的（见 _FRAME_HASH_STATE 的注释），所以 unchanged=true
        # 只证明「画面没变」，不证明「你手上已经有这张图」：别的智能体可能刚拍过同一块
        # 区域。而这一帧到底有没有作为附件返回，是服务端此刻能百分之百确定的事实 ——
        # 把它显式写出来，调用方就有据可查地判断自己该不该复用上一帧的坐标。
        #
        # 判据是"最终会不会附图"（unchanged 为真时图会在下面被剥掉），不是
        # "capture 有没有生成图"，两者在不命中去重时才会同时为真。
        attach_image = not unchanged and any(not isinstance(item, str) for item in response)
        extra_lines.append(f"Image Attached: {'true' if attach_image else 'false'}")
        if unchanged:
            extra_lines.append(UNCHANGED_NOTE)
        # 坐标契约也要跟着"这次到底有没有图"走：unchanged 时图被剥掉了，再说一遍
        # "本帧按原始尺寸 NxM 交付、图上 1 像素 = 屏幕 1 像素"就是指着一张没给出去的图
        # 讲换算，反而会把调用方推向"我好像有图"。这种时候要说的只有一句：按你手上
        # 那一帧的契约来，没有图就 force_image。
        if unchanged:
            extra_lines.extend(_unchanged_coordinate_lines(desktop_state))
        else:
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
