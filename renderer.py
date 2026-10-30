
from __future__ import annotations

import random
import secrets
from io import BytesIO
from pathlib import Path

from PIL import (
    Image,
    ImageChops,
    ImageDraw,
    ImageFilter,
    ImageFont
)

WIDTH = 960
HEIGHT = 300
LENGTH = 7

ALPHABET = (
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "abcdefghijklmnopqrstuvwxyz"
    "0123456789"
    "&%#@+><?~=-"
)

BASE_DIR = Path(__file__).resolve().parent
FONT_DIR = BASE_DIR / "fonts"

REGULAR_FONT_PATH = FONT_DIR / "DejaVuSans.ttf"
BOLD_FONT_PATH = FONT_DIR / "DejaVuSans-Bold.ttf"

BACKGROUND = (232, 232, 229, 255)
HIDDEN_COLOR = (53, 53, 53)
DECOY_COLOR = (41, 41, 41)

HIDDEN_OPACITY = 0.82
DECOY_OPACITY = 0.40

HIDDEN_BLUR = 6
DECOY_OUTLINE = 3

GLYPH_WIDTH_FACTOR = 0.78
GLYPH_HEIGHT_SCALE = 1.14

FONT_SIZE = 112

BACKGROUND_COLORS = [
    (170, 169, 162, 110),
    (170, 161, 157, 110),
    (165, 173, 174, 110),
    (179, 170, 160, 110),
    (146, 155, 153, 110),
    (185, 181, 171, 110),
    (157, 158, 155, 110),
]


# ==========================================
# CODE GENERATION
# ==========================================

def generate_codes() -> tuple[str, str]:
    hidden = "".join(
        secrets.choice(ALPHABET)
        for _ in range(LENGTH)
    )

    decoy = "".join(
        secrets.choice(
            ALPHABET.replace(char, "")
        )
        for char in hidden
    )

    return hidden, decoy


# ==========================================
# FONT LOADING
# ==========================================

def _load_fonts():
    if (
        REGULAR_FONT_PATH.is_file()
        and BOLD_FONT_PATH.is_file()
    ):
        return (
            ImageFont.truetype(
                str(REGULAR_FONT_PATH),
                FONT_SIZE
            ),
            ImageFont.truetype(
                str(BOLD_FONT_PATH),
                FONT_SIZE
            ),
            True
        )

    # Pillow's bundled scalable fallback.
    fallback = ImageFont.load_default(
        size=FONT_SIZE
    )

    return fallback, fallback, False


# ==========================================
# BEZIER CURVES
# ==========================================

def _cubic_bezier(
    p0: tuple[float, float],
    p1: tuple[float, float],
    p2: tuple[float, float],
    p3: tuple[float, float],
    steps: int = 28
) -> list[tuple[float, float]]:

    points = []

    for step in range(steps + 1):
        t = step / steps
        u = 1 - t

        x = (
            u**3 * p0[0]
            + 3 * u**2 * t * p1[0]
            + 3 * u * t**2 * p2[0]
            + t**3 * p3[0]
        )

        y = (
            u**3 * p0[1]
            + 3 * u**2 * t * p1[1]
            + 3 * u * t**2 * p2[1]
            + t**3 * p3[1]
        )

        points.append((x, y))

    return points


# ==========================================
# BACKGROUND
# ==========================================

def _draw_background(
    image: Image.Image,
    rng: random.Random
) -> None:

    draw = ImageDraw.Draw(image, "RGBA")

    # Stringy curved lines.
    for _ in range(42):

        x = rng.uniform(-100, WIDTH + 100)
        y = rng.uniform(0, HEIGHT)

        p0 = (x - 240, y)

        p1 = (
            x - 100,
            y + rng.uniform(-100, 100)
        )

        p2 = (
            x + 100,
            y + rng.uniform(-100, 100)
        )

        p3 = (
            x + 240,
            y + rng.uniform(-50, 50)
        )

        points = _cubic_bezier(
            p0, p1, p2, p3
        )

        draw.line(
            points,
            fill=rng.choice(BACKGROUND_COLORS),
            width=rng.choice((1, 1, 2))
        )

    # Loops and ovals.
    for _ in range(18):

        x = rng.uniform(0, WIDTH)
        y = rng.uniform(0, HEIGHT)

        rx = rng.uniform(12, 57)
        ry = rng.uniform(5, 25)

        draw.ellipse(
            (
                x - rx,
                y - ry,
                x + rx,
                y + ry
            ),
            outline=rng.choice(BACKGROUND_COLORS),
            width=rng.choice((1, 2))
        )

    # Interference dots.
    for _ in range(260):

        x = rng.randint(0, WIDTH - 1)
        y = rng.randint(0, HEIGHT - 1)

        radius = rng.choice((1, 1, 2))
        color = rng.choice(BACKGROUND_COLORS)

        draw.ellipse(
            (
                x - radius,
                y - radius,
                x + radius,
                y + radius
            ),
            fill=color
        )


# ==========================================
# GLYPH MASK GENERATION
# ==========================================

def _glyph_mask(
    character: str,
    font: ImageFont.FreeTypeFont,
    target_width: int,
    *,
    embolden: bool = False
) -> Image.Image:

    bbox = font.getbbox(character)

    left, top, right, bottom = bbox

    stroke_width = 3 if embolden else 0
    pad = 12 + stroke_width

    source = Image.new(
        "L",
        (
            max(1, right - left) + pad * 2,
            max(1, bottom - top) + pad * 2
        ),
        0
    )

    source_draw = ImageDraw.Draw(source)

    source_draw.text(
        (pad - left, pad - top),
        character,
        font=font,
        fill=255,
        stroke_width=stroke_width,
        stroke_fill=255
    )

    ink_bounds = source.getbbox()

    if not ink_bounds:
        raise ValueError(
            f"Could not render glyph: {character!r}"
        )

    source = source.crop(ink_bounds)

    target_height = max(
        1,
        round(source.height * GLYPH_HEIGHT_SCALE)
    )

    return source.resize(
        (
            max(1, target_width),
            target_height
        ),
        Image.Resampling.LANCZOS
    )


# ==========================================
# COMPOSITING
# ==========================================

def _composite_mask(
    image: Image.Image,
    mask: Image.Image,
    center_x: float,
    center_y: float,
    color: tuple[int, int, int],
    opacity: float,
    blur: float = 0
) -> None:

    if blur > 0:

        padding = max(
            4,
            int(blur * 4)
        )

        padded = Image.new(
            "L",
            (
                mask.width + padding * 2,
                mask.height + padding * 2
            ),
            0
        )

        padded.paste(
            mask,
            (padding, padding)
        )

        mask = padded.filter(
            ImageFilter.GaussianBlur(blur)
        )

    alpha = mask.point(
        lambda value: round(value * opacity)
    )

    layer = Image.new(
        "RGBA",
        mask.size,
        color + (0,)
    )

    layer.putalpha(alpha)

    position = (
        round(center_x - mask.width / 2),
        round(center_y - mask.height / 2)
    )

    image.alpha_composite(
        layer,
        position
    )


# ==========================================
# OUTLINE GENERATION
# ==========================================

def _outline_mask(
    mask: Image.Image
) -> Image.Image:

    outer = mask.filter(
        ImageFilter.MaxFilter(DECOY_OUTLINE)
    )

    inner = mask.filter(
        ImageFilter.MinFilter(DECOY_OUTLINE)
    )

    return ImageChops.subtract(
        outer,
        inner
    )


# ==========================================
# FOREGROUND INTERFERENCE
# ==========================================

def _draw_foreground_interference(
    image: Image.Image,
    rng: random.Random
) -> None:

    draw = ImageDraw.Draw(image, "RGBA")

    colors = (
        (116, 123, 120, 48),
        (164, 156, 150, 48)
    )

    for _ in range(12):

        x0 = (0, rng.uniform(0, HEIGHT))
        x3 = (WIDTH, rng.uniform(0, HEIGHT))

        x1 = (240, rng.uniform(0, HEIGHT))
        x2 = (680, rng.uniform(0, HEIGHT))

        draw.line(
            _cubic_bezier(
                x0, x1, x2, x3,
                steps=32
            ),
            fill=rng.choice(colors),
            width=1
        )


# ==========================================
# FINAL WEBP RENDERER
# ==========================================

def render_webp(
    hidden: str,
    decoy: str
) -> bytes:

    if len(hidden) != LENGTH or len(decoy) != LENGTH:
        raise ValueError(
            f"Both strings must be exactly {LENGTH} characters."
        )

    if any(char not in ALPHABET for char in hidden + decoy):
        raise ValueError(
            "A challenge contains an unsupported character."
        )

    if any(a == b for a, b in zip(hidden, decoy)):
        raise ValueError(
            "Corresponding hidden and decoy glyphs must differ."
        )

    regular_font, bold_font, has_distinct_bold_face = (
        _load_fonts()
    )

    image = Image.new(
        "RGBA",
        (WIDTH, HEIGHT),
        BACKGROUND
    )

    rng = random.Random(
        secrets.randbits(64)
    )

    _draw_background(image, rng)

    side_padding = 65

    cell_width = (
        WIDTH - side_padding * 2
    ) / LENGTH

    target_width = max(
        1,
        round(cell_width * GLYPH_WIDTH_FACTOR)
    )

    center_y = HEIGHT / 2

    for index, (hidden_char, decoy_char) in enumerate(
        zip(hidden, decoy)
    ):

        center_x = (
            side_padding
            + cell_width * (index + 0.5)
        )

        # Hidden layer: solid and blurred.
        hidden_mask = _glyph_mask(
            hidden_char,
            bold_font,
            target_width,
            embolden=not has_distinct_bold_face
        )

        _composite_mask(
            image,
            hidden_mask,
            center_x,
            center_y,
            HIDDEN_COLOR,
            HIDDEN_OPACITY,
            blur=HIDDEN_BLUR
        )

        # Decoy layer: outlined and translucent.
        decoy_mask = _glyph_mask(
            decoy_char,
            regular_font,
            target_width
        )

        outline = _outline_mask(
            decoy_mask
        )

        _composite_mask(
            image,
            outline,
            center_x,
            center_y,
            DECOY_COLOR,
            DECOY_OPACITY
        )

    _draw_foreground_interference(
        image,
        rng
    )

    output = BytesIO()

    image.convert("RGB").save(
        output,
        format="WEBP",
        quality=94,
        method=6
    )

    return output.getvalue()
