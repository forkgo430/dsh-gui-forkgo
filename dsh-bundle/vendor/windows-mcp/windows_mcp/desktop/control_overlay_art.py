"""Pure bitmap artwork shared by the active AI-control indicator."""

import math
import os
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

from windows_mcp.desktop.overlay_bitmap import premultiplied_bgra

_BORDER = 56
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
_TITLE_FACES = ("msyhbd.ttc", "Dengb.ttf", "simhei.ttf", "seguisb.ttf")
_HINT_FACES = ("msyh.ttc", "Deng.ttf", "simsun.ttc", "segoeui.ttf")


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


def _glow_alpha(depth: int, extent: int = _BORDER) -> int:
    """Peak at the screen edge and fade continuously to transparency inward."""
    if depth >= extent - 1:
        return 0
    return round(240 * (1 - depth / (extent - 1)) ** 1.4)


def _edge_bitmap(width: int, height: int, side: str, color: tuple[int, int, int]) -> bytes:
    """Pre-render feathered edges, joining corners at one glow strength."""
    image = Image.new("RGBA", (width, height))
    draw = ImageDraw.Draw(image)
    extent = min(_BORDER, height if side in ("top", "bottom") else width)
    corner_width = min(extent, width // 2)
    for depth in range(extent):
        # Let the gradient itself reach the edge; do not draw a separate outline.
        alpha = _glow_alpha(depth, extent)
        if side in ("top", "bottom"):
            y = depth if side == "top" else height - 1 - depth
            draw.line((0, y, width - 1, y), fill=(*color, alpha))
            # The side strips start below/above these corner squares. Use the
            # nearest screen edge as the depth, matching the straight strips
            # without blending two windows or drawing a diagonal bright seam.
            for side_depth in range(corner_width):
                corner_alpha = _glow_alpha(min(depth, side_depth), extent)
                draw.point((side_depth, y), fill=(*color, corner_alpha))
                draw.point((width - 1 - side_depth, y), fill=(*color, corner_alpha))
        elif side == "left":
            draw.line((depth, 0, depth, height - 1), fill=(*color, alpha))
        else:
            x = width - 1 - depth
            draw.line((x, 0, x, height - 1), fill=(*color, alpha))
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
    # Prefer presentation-sized text, then shrink only when a monitor is narrow.
    # The ceiling is 16 so the title lands at 24px: readable across the room,
    # small enough that the panel stays a strip rather than a dialog.
    for hint_size in range(16, 7, -1):
        title_font = _load_font(_TITLE_FACES, hint_size + 8)
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
    # Fade every edge from transparent to 80%; the rounded mask softens the corners too.
    image.putalpha(_notice_panel_mask(width, height).point(lambda alpha: round(alpha * 0.8)))
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
    """Anchor the aura at the solid start of the panel's outer fade."""
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
