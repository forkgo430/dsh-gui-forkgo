"""Pure bitmap artwork shared by the active AI-control indicator."""

import math
import os
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

from windows_mcp.desktop.overlay_bitmap import premultiplied_bgra

_BORDER = 56
# Peak alpha of the frame glow, at the very edge of the screen.
_GLOW_PEAK_ALPHA = 236
# Radius the frame glow rounds its corners by. Zero gives the old crisp
# 90-degree crease; anything comparable to _BORDER wraps the light around the
# corner the way an actual bezel-lit display looks.
_GLOW_CORNER_RADIUS = 28
_CURSOR_SIZE = 88
_CURSOR_LABEL = "AI"
_RIPPLE_SIZE = 96
_RIPPLE_FRAMES = 9
_FONT_DIR = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
_NOTICE_GLOW_PAD = 32
_BREATH_PERIOD_SECONDS = 3.0
_BREATH_MIN_ALPHA = 140
# 中文文案。这里是本仓库与上游唯一的差别所在：
# 上游的标题是 "AI is controlling this computer"，键盘前的用户看不懂就只能猜。
# 这台机器和用它的人都是中文环境，所以标题、提示全部换成中文。
#
# 字号也一并压小了：上游 `range(24, 7, -1)` 在 1920 宽的屏上会取到 24 → 标题 32px，
# 面板高 160px 上下，压着一整条顶边，看着像弹窗。现在上限 16 → 标题 24px，
# 面板降到 120px 上下，仍然隔着一个房间看得见，但不再抢视线。
_NOTICE_TITLE = "AI 正在控制这台电脑"
_NOTICE_SHORTCUT = "Ctrl + Alt + Shift + Backspace"
_NOTICE_MOUSE_HINT = "大幅移动鼠标"
_NOTICE_HINT = f"或按 {_NOTICE_SHORTCUT} 接管"
# Key-cap geometry for the shortcut shown inside the hint line.
_BADGE_PAD_X = 8
_BADGE_GAP = 8

# CJK-capable faces, best first, per weight. A missing face falls through to the
# next one and finally to Latin faces: the notice must render on any Windows
# install, and a missing glyph would come out as tofu rather than as English.
#
# 字体选择：本机所有中文字体逐套渲染对比过。结论是 **Noto Sans SC（思源黑体）**
# 最好看 —— 笔画粗细均匀、字重适中，中英文混排时基线和字面高度都协调。
#
# 为什么换掉原来的等线（Deng）：
#   等线 Bold 的横画偏粗、竖画偏细，28px 下笔画边缘发硬，中文的"口"部框架
#   （正在、这台）显得局促；它的英文字母又偏窄，"AI" 和后面的中文之间会
#   留出一块不自然的空隙。
#   微软雅黑 Bold 反过来——太粗，在深色背景上笔画粘连，小字号（提示行 19px）
#   尤其发糊。Noto Sans SC 在 19px 上依然清晰，那是因为它的字面比雅黑略小、
#   留白更多，同样字号下反而更透气。
#
# 字体名里的空格和括号是 Windows 的注册名，必须原样写。
# OTF 是 CFF 轮廓，Pillow 的 truetype 能直接读，和 TTF 没区别。
# 回退链保留等线 → 雅黑 → 黑体，保证没装思源的机器也能出字。
_TITLE_FACES = (
    "Noto Sans SC Medium (TrueType).otf",
    "Noto Sans SC Bold (TrueType).otf",
    "Dengb.ttf",
    "msyhbd.ttc",
    "simhei.ttf",
    "seguisb.ttf",
)
_HINT_FACES = (
    "Noto Sans SC (TrueType).otf",
    "Noto Sans SC Medium (TrueType).otf",
    "Deng.ttf",
    "msyh.ttc",
    "simsun.ttc",
    "segoeui.ttf",
)

# 字号阶梯：标题 30、提示 19。
#
# 标题从 28 提到 30：Noto Sans SC 的字面比等线小一圈，同样 28 看着比原来矮，
# 30 才回到原来的视觉高度。提示行维持 19 —— 它是次要信息，跟着放大只会抢视线。
# 两者相差 11px，层级关系比原来的 9px 更清楚。
_TITLE_SIZE = 30
_HINT_SIZE = 19
# 面板不透明度：必须完全不透明。
#
# 原来 0.88，本意是"保住层次感"。代价是背后任何文字都会透上来 —— 实测在 DSH 的
# 插件页上，「已安装」「发现」两个标签压在面板底下清清楚楚，和面板自己的标题叠成
# 两层文字，一眼看过去像渲染重影。
#
# 这类浮层不存在"半透明更好看"：它盖住的是任意内容的桌面，不让内容透上来才能保证
# 只读到自己那两行字。1.0 之后边缘仍有 8px 羽化，不会切出生硬的直角。
_NOTICE_ALPHA = 1.0


def _load_font(faces: tuple[str, ...], size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """First available face at ``size``, else Pillow's bitmap default."""
    for face in faces:
        try:
            return ImageFont.truetype(str(_FONT_DIR / face), size)
        except OSError:
            continue
    return ImageFont.load_default()


def _breath_opacity(elapsed: float) -> int:
    """Start bright and cycle smoothly while keeping the control cue visible."""
    strength = (1 + math.cos(2 * math.pi * elapsed / _BREATH_PERIOD_SECONDS)) / 2
    return round(_BREATH_MIN_ALPHA + (255 - _BREATH_MIN_ALPHA) * strength)


def _glow_alpha(depth: float, extent: int = _BORDER) -> int:
    """Edge glow strength at ``depth`` pixels in from the screen edge.

    One continuous ramp, no plateau.

    上一版用的是 ``((cos(t*pi)+1)/2)**1.15``：这条曲线在前 18px 只掉 26%
    （236→174），后面的 38px 才掉到 0。肉眼看就是「一条实心蓝边 + 一层渐变」，
    也就是屏幕上像有两层蓝光。余弦在 t=0 处导数为零，越靠近边缘越平，所以那段
    天然发死。

    现在用 ``(1-t)**1.45``：边缘处斜率最大，从第一像素就开始变暗，整条带子只有
    一段渐变。指数 1.45 是折中——上游的更早版本用 1.4，配上硬切到 0 的结尾会显得
    像描边；现在的 1.45 全程平滑，且不像余弦那样前半段发死。
    """
    if depth >= extent:
        return 0
    t = depth / extent
    return round(_GLOW_PEAK_ALPHA * (1 - t) ** 1.45)


def _frame_layer_bitmap(
    width: int,
    height: int,
    color: tuple[int, int, int],
    *,
    origin: tuple[int, int],
    screen: tuple[int, int],
) -> bytes:
    """Render one piece of the frame glow from a single distance field.

    Why a distance field rather than a per-side gradient: the frame is drawn by
    four separate layered windows, and alpha compositing *adds* wherever two of
    them overlap. Per-side fades meeting at a corner therefore leave a bright
    square there. One shared field means every window agrees about any pixel.

    Why the screen geometry instead of the strip's own rect: a corner only
    exists in the field, not in any single strip. The top strip spans the full
    width but is only ``_BORDER`` tall, so it never sees a "below the screen"
    edge to round against. Passing the whole screen rect plus this strip's
    offset lets each window evaluate the same function of absolute position.

    ``origin`` is this bitmap's top-left in screen coordinates; ``screen`` is
    the full screen size.
    """
    image = Image.new("RGBA", (width, height))
    if width <= 0 or height <= 0:
        return premultiplied_bgra(image, 1.0)

    ox, oy = origin
    sw, sh = screen
    right, bottom = sw - 1, sh - 1

    pixels = image.load()
    for y in range(height):
        sy = oy + y
        for x in range(width):
            sx = ox + x

            # Depth into the glow band = distance to the nearest screen edge.
            # The band is the same width on all four sides, so a mid-edge pixel
            # and a corner pixel at the same depth get the same alpha, and the
            # four strips agree where they meet.
            depth = min(sx, right - sx, sy, bottom - sy)

            a = _glow_alpha(depth, _BORDER)
            if a:
                pixels[x, y] = (*color, a)
    return premultiplied_bgra(image, 1.0)


def _edge_bitmap(
    width: int,
    height: int,
    side: str,
    color: tuple[int, int, int],
    *,
    lead: int = 0,
    tail: int = 0,
) -> bytes:
    """One side of the frame glow, feathered in two dimensions.

    Kept for callers that only want a single strip (the preview renderer, and
    any test that asserts strip geometry). The live indicator uses
    :func:`_frame_layer_bitmap` instead, which computes one distance field for
    the whole frame so overlapping layers cannot brighten the corners.

    ``lead`` / ``tail`` mark how far the strip's span runs under the two
    perpendicular strips; the sideways fade starts there.
    """
    image = Image.new("RGBA", (width, height))
    extent = min(_BORDER, height if side in ("top", "bottom") else width)
    if extent <= 0:
        return premultiplied_bgra(image, 1.0)

    pixels = image.load()
    horizontal = side in ("top", "bottom")
    span = width if horizontal else height

    for y in range(height):
        for x in range(width):
            if horizontal:
                depth = y if side == "top" else height - 1 - y
                along = x
            else:
                depth = x if side == "left" else width - 1 - x
                along = y

            a = _glow_alpha(depth, extent)
            if lead > 0 and along < lead:
                a = max(a, _glow_alpha(lead - along, extent))
            if tail > 0 and span - 1 - along < tail:
                a = max(a, _glow_alpha(tail - (span - 1 - along), extent))

            if a:
                pixels[x, y] = (*color, a)

    return premultiplied_bgra(image, 1.0)


def _cursor_bitmap(color: tuple[int, int, int]) -> bytes:
    """Build a broad cursor aura with no drawn ellipse or sharp contour."""
    half = (_CURSOR_SIZE - 1) / 2
    pixels = []
    for y in range(_CURSOR_SIZE):
        for x in range(_CURSOR_SIZE):
            radius = math.hypot(x - half, y - half)
            clear_center = 1 - math.exp(-0.5 * (max(0.0, radius - 1) / 5) ** 2)
            outer_glow = math.exp(-0.5 * (radius / 20) ** 2)
            edge_fade = min(1.0, max(0.0, (half - radius) / 8))
            alpha = round(230 * clear_center * outer_glow * edge_fade)
            pixels.append((*color, alpha))
    image = Image.new("RGBA", (_CURSOR_SIZE, _CURSOR_SIZE))
    image.putdata(pixels)
    _draw_cursor_label(image, color)
    return premultiplied_bgra(image, 1.0)


def _draw_cursor_label(image: Image.Image, color: tuple[int, int, int]) -> None:
    """Tag the glow above and right of the hotspot, clear of the arrow body."""
    font = _load_font(_TITLE_FACES, 13)
    draw = ImageDraw.Draw(image)
    box = draw.textbbox((0, 0), _CURSOR_LABEL, font=font)
    width, height = box[2] - box[0] + 12, box[3] - box[1] + 8
    left, bottom = _CURSOR_SIZE // 2 + 8, _CURSOR_SIZE // 2 - 8
    draw.rounded_rectangle(
        (left, bottom - height, left + width, bottom), radius=height // 2, fill=(*color, 255)
    )
    draw.text(
        (left + 6 - box[0], bottom - height + 4 - box[1]),
        _CURSOR_LABEL,
        font=font,
        fill=(255, 255, 255, 255),
    )


def _ripple_frames(color: tuple[int, int, int]) -> list[bytes]:
    """Render an expanding, fading ring that marks where AI clicked."""
    scale = 4  # Supersample so the thin ring stays smooth after downsizing.
    size = _RIPPLE_SIZE * scale
    frames = []
    for index in range(_RIPPLE_FRAMES):
        progress = (index + 1) / _RIPPLE_FRAMES
        radius = (6 + progress * (_RIPPLE_SIZE / 2 - 9)) * scale
        image = Image.new("RGBA", (size, size), (*color, 0))
        ImageDraw.Draw(image).ellipse(
            (size / 2 - radius, size / 2 - radius, size / 2 + radius, size / 2 + radius),
            outline=(*color, round(240 * (1 - progress) ** 0.7)),
            width=3 * scale,
        )
        image = image.resize((_RIPPLE_SIZE, _RIPPLE_SIZE), Image.Resampling.LANCZOS)
        frames.append(premultiplied_bgra(image, 1.0))
    return frames


def _notice_shape_mask(width: int, height: int, radius: int = 22) -> Image.Image:
    """Downsample a rounded mask so all four corners have smooth pixel coverage."""
    scale = 8
    mask = Image.new("L", (width * scale, height * scale))
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, width * scale - 1, height * scale - 1), radius=radius * scale, fill=255
    )
    return mask.resize((width, height), Image.Resampling.LANCZOS).filter(
        ImageFilter.GaussianBlur(3)
    )


def _notice_panel_mask(width: int, height: int) -> Image.Image:
    """Fade the rounded panel through its outer eight pixels."""
    edge_fade = [round(255 * (step / 8) ** 2 * (3 - 2 * step / 8)) for step in range(9)]
    horizontal = [edge_fade[min(8, x, width - 1 - x)] for x in range(width)]
    vertical = [edge_fade[min(8, y, height - 1 - y)] for y in range(height)]
    fade_mask = Image.new("L", (width, height))
    fade_mask.putdata(
        [min(horizontal[x], vertical[y]) for y in range(height) for x in range(width)]
    )
    return ImageChops.multiply(_notice_shape_mask(width, height), fade_mask)


def _notice_bitmap(screen_width: int) -> tuple[int, int, bytes] | None:
    """Render both takeover methods below the ownership title."""
    available_width = screen_width - 32
    if available_width < 200:
        return None
    horizontal_padding = 32 if screen_width >= 640 else 20
    # 先按设计字号排，只有窄屏才逐档缩小。28/19 在 1920 上宽度 424px，
    # 远小于可用宽度；这里保留回退只是为了让极小屏也能出图。
    for shrink in range(0, 10):
        title_size = _TITLE_SIZE - shrink
        hint_size = _HINT_SIZE - shrink
        title_font = _load_font(_TITLE_FACES, title_size)
        hint_font = _load_font(_HINT_FACES, hint_size)
        measure = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
        title_box = measure.textbbox((0, 0), _NOTICE_TITLE, font=title_font)
        hint_box = measure.textbbox((0, 0), _NOTICE_HINT, font=hint_font)
        mouse_box = measure.textbbox((0, 0), _NOTICE_MOUSE_HINT, font=hint_font)
        mouse_width, mouse_height = mouse_box[2] - mouse_box[0], mouse_box[3] - mouse_box[1]
        hint_prefix, _, hint_suffix = _NOTICE_HINT.partition(_NOTICE_SHORTCUT)
        prefix_width = math.ceil(measure.textlength(hint_prefix, font=hint_font))
        shortcut_width = math.ceil(measure.textlength(_NOTICE_SHORTCUT, font=hint_font))
        suffix_width = math.ceil(measure.textlength(hint_suffix, font=hint_font))
        title_width, title_height = title_box[2] - title_box[0], title_box[3] - title_box[1]
        # The shortcut sits on a black key cap. Its width plus the gaps on both
        # sides is the whole block; the sentence is measured around it so a
        # longer key name (or a longer translation) cannot collide with the cap.
        badge_width = shortcut_width + 2 * _BADGE_PAD_X
        hint_height = hint_box[3] - hint_box[1]
        hint_width = (
            prefix_width + _BADGE_GAP + badge_width + _BADGE_GAP + suffix_width
        )
        width = max(title_width, hint_width, mouse_width) + 2 * horizontal_padding
        if width <= available_width:
            break
    else:
        return None

    height = title_height + mouse_height + hint_height + 54
    image = Image.new("RGBA", (width, height), (8, 42, 88, 0))
    # Fade every edge from transparent to _NOTICE_ALPHA; the rounded mask softens
    # the corners too. 0.80 washed the glyph edges out against bright wallpapers.
    image.putalpha(
        _notice_panel_mask(width, height).point(
            lambda alpha: round(alpha * _NOTICE_ALPHA)))
    draw = ImageDraw.Draw(image)
    title_y = 18 - title_box[1]
    hint_y = 36 + title_height + mouse_height - hint_box[1]
    draw.text(
        ((width - mouse_width) // 2 - mouse_box[0], 28 + title_height - mouse_box[1]),
        _NOTICE_MOUSE_HINT,
        font=hint_font,
        fill=(225, 240, 255, 255),
    )
    draw.text(
        ((width - title_width) // 2 - title_box[0], title_y),
        _NOTICE_TITLE,
        font=title_font,
        fill=(248, 251, 255, 255),
    )
    hint_x = (width - hint_width) // 2
    badge_left = hint_x + prefix_width + _BADGE_GAP
    # A solid black key label separates the takeover shortcut from its sentence.
    draw.rounded_rectangle(
        (
            badge_left,
            32 + title_height + mouse_height,
            badge_left + badge_width,
            39 + title_height + mouse_height + hint_height,
        ),
        radius=6,
        fill=(0, 0, 0, 255),
    )
    draw.text((hint_x, hint_y), hint_prefix, font=hint_font, fill=(225, 240, 255, 255))
    draw.text(
        (badge_left + _BADGE_PAD_X, hint_y),
        _NOTICE_SHORTCUT,
        font=hint_font,
        fill=(255, 255, 255, 255),
    )
    draw.text(
        (badge_left + badge_width + _BADGE_GAP, hint_y),
        hint_suffix,
        font=hint_font,
        fill=(225, 240, 255, 255),
    )
    return width, height, premultiplied_bgra(image, 1.0)


def _notice_glow_bitmap(
    width: int, height: int, color: tuple[int, int, int], pad: int = _NOTICE_GLOW_PAD
) -> bytes:
    """Panel aura. Retained only for the preview renderer and geometry tests.

    实时指示器已经不再用它：面板外面再套一圈光晕，和四周的框光是同一套蓝色，
    屏幕上就是两层蓝。渲染面板本体就够，这里留着是因为预览图和几何测试还需要
    一个「带光晕的面板」作为对照。
    """
    size = (width + 2 * pad, height + 2 * pad)
    shape = Image.new("L", size)
    # The inset contour meets the panel where its eight-pixel fade begins.
    shape.paste(_notice_shape_mask(width - 16, height - 16, 14), (pad + 8, pad + 8))
    outside = ImageChops.subtract(shape.filter(ImageFilter.GaussianBlur(min(15, pad / 2))), shape)
    # Feather the inner cutoff as well, so the halo cannot end as a bright line.
    outside = outside.filter(ImageFilter.GaussianBlur(3))
    aura = Image.new("RGBA", size, (*color, 0))
    peak = outside.getextrema()[1]
    aura.putalpha(outside.point(lambda alpha: round(alpha * 211 / peak)))
    return premultiplied_bgra(aura, 1.0)
