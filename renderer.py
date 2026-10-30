from __future__ import annotations

import math
import random
import secrets
from io import BytesIO
from pathlib import Path

from PIL import (
    Image,
    ImageChops,
    ImageDraw,
    ImageFilter,
    ImageFont,
    ImageOps,
)

WIDTH = 960
HEIGHT = 300
LENGTH = 7

ALPHABET = (
    "ABCDFGHIJKLMNOPQRSTUVWXYZ"
    "abcdefghijkmnpqrstuvwxyz"
    "023456789"
    "#&%@+?=-"
)

BASE_DIR = Path(__file__).resolve().parent
FONT_DIR = BASE_DIR / "fonts"

# Each family is loaded independently. Missing font files are skipped.
# Keep only font files that you are licensed to use and distribute.
FONT_FAMILIES = (
    (
        "Verdana",
        FONT_DIR / "Verdana.ttf",
        FONT_DIR / "Verdana-Bold.ttf",
    ),
    (
        "Roboto",
        FONT_DIR / "Roboto-Regular.ttf",
        FONT_DIR / "Roboto-Bold.ttf",
    ),
    (
        "Georgia",
        FONT_DIR / "Georgia.ttf",
        FONT_DIR / "Georgia-Bold.ttf",
    ),
    (
        "DejaVu Sans",
        FONT_DIR / "DejaVuSans.ttf",
        FONT_DIR / "DejaVuSans-Bold.ttf",
    ),
)

HIDDEN_COLOR = (53, 53, 53)
DECOY_COLOR = (41, 41, 41)

HIDDEN_OPACITY = 0.62
DECOY_OPACITY = 0.50

HIDDEN_BLUR = 6
DECOY_OUTLINE = 5

# Background interference.
BACKGROUND_LINE_WIDTH = (2, 2, 3)
BACKGROUND_CORE_OPACITY = 145
BACKGROUND_HALO_OPACITY = 23
BACKGROUND_HALO_WIDTH = (7, 9, 11)
BACKGROUND_HALO_BLUR = 2.5

# Foreground interference.
FOREGROUND_LINE_WIDTH = 3
FOREGROUND_CORE_OPACITY = 115
FOREGROUND_HALO_OPACITY = 21
FOREGROUND_HALO_WIDTH = 8
FOREGROUND_HALO_BLUR = 2.8

# Character distortion.
GLYPH_WIDTH_FACTOR = 0.78
GLYPH_HEIGHT_SCALE = 1.14

GLYPH_SCALE_X = (0.94, 1.07)
GLYPH_SCALE_Y = (0.95, 1.06)

GLYPH_WARP_X = (1.5, 3.0)
GLYPH_WARP_Y = (1.0, 3.0)

GLYPH_ROTATION = 6.5

FONT_SIZE = 112

BACKGROUND_STOPS = (
    (241, 240, 234),
    (224, 233, 231),
    (238, 226, 226),
)

HIDDEN_GRADIENTS = (
    ((39, 52, 63), (69, 68, 78)),
    ((44, 64, 62), (69, 60, 68)),
    ((56, 52, 68), (77, 66, 61)),
)

DECOY_GRADIENTS = (
    ((89, 111, 111), (143, 93, 101)),
    ((116, 94, 131), (80, 120, 121)),
    ((139, 104, 78), (91, 111, 137)),
)

BACKGROUND_COLORS = [
    (170, 169, 162),
    (170, 161, 157),
    (165, 173, 174),
    (179, 170, 160),
    (146, 155, 153),
    (185, 181, 171),
    (157, 158, 155),
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

def _load_fonts() -> list[
    tuple[ImageFont.FreeTypeFont, ImageFont.FreeTypeFont, bool]
]:
    """Load every available family as (regular, bold, has_bold_face)."""

    loaded_fonts = []

    for _name, regular_path, bold_path in FONT_FAMILIES:
        if not regular_path.is_file():
            continue

        try:
            regular_font = ImageFont.truetype(
                str(regular_path),
                FONT_SIZE
            )

            if bold_path.is_file():
                bold_font = ImageFont.truetype(
                    str(bold_path),
                    FONT_SIZE
                )
                has_distinct_bold_face = True
            else:
                bold_font = regular_font
                has_distinct_bold_face = False

            loaded_fonts.append(
                (
                    regular_font,
                    bold_font,
                    has_distinct_bold_face,
                )
            )

        except OSError:
            # Ignore an invalid or unreadable font and try the next family.
            continue

    if loaded_fonts:
        return loaded_fonts

    # DejaVu files are missing too; use Pillow's bundled fallback.
    fallback = ImageFont.load_default(
        size=FONT_SIZE
    )

    return [(fallback, fallback, False)]


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
# BACKGROUND GRADIENT
# ==========================================

def _gradient_background() -> Image.Image:

    vertical = Image.linear_gradient(
        "L"
    ).resize(
        (WIDTH, HEIGHT),
        Image.Resampling.BICUBIC
    )

    horizontal = vertical.rotate(
        90,
        expand=True
    ).resize(
        (WIDTH, HEIGHT),
        Image.Resampling.BICUBIC
    )

    diagonal = ImageChops.blend(
        horizontal,
        vertical,
        0.32
    )

    background = ImageOps.colorize(
        diagonal,
        black=BACKGROUND_STOPS[0],
        mid=BACKGROUND_STOPS[1],
        white=BACKGROUND_STOPS[2]
    )

    return background.convert("RGBA")


# ==========================================
# BACKGROUND INTERFERENCE
# ==========================================

def _draw_background(
    image: Image.Image,
    rng: random.Random
) -> None:

    # Separate strong lines and soft halos.

    core_layer = Image.new(
        "RGBA",
        (WIDTH, HEIGHT),
        (0, 0, 0, 0)
    )

    halo_layer = Image.new(
        "RGBA",
        (WIDTH, HEIGHT),
        (0, 0, 0, 0)
    )

    core_draw = ImageDraw.Draw(
        core_layer,
        "RGBA"
    )

    halo_draw = ImageDraw.Draw(
        halo_layer,
        "RGBA"
    )

    for _ in range(48):

        x = rng.uniform(-120, WIDTH + 120)
        y = rng.uniform(0, HEIGHT)

        p0 = (
            x - 260,
            y + rng.uniform(-35, 35)
        )

        p1 = (
            x - rng.uniform(70, 160),
            y + rng.uniform(-170, 170)
        )

        p2 = (
            x + rng.uniform(70, 160),
            y + rng.uniform(-170, 170)
        )

        p3 = (
            x + 260,
            y + rng.uniform(-70, 70)
        )

        points = _cubic_bezier(
            p0,
            p1,
            p2,
            p3,
            steps=36
        )

        color = rng.choice(BACKGROUND_COLORS)

        # Faint, wide, blurred halo.
        halo_draw.line(
            points,
            fill=color + (BACKGROUND_HALO_OPACITY,),
            width=rng.choice(BACKGROUND_HALO_WIDTH)
        )

        # Stronger center line.
        core_draw.line(
            points,
            fill=color + (BACKGROUND_CORE_OPACITY,),
            width=rng.choice(BACKGROUND_LINE_WIDTH)
        )

    halo_layer = halo_layer.filter(
        ImageFilter.GaussianBlur(
            BACKGROUND_HALO_BLUR
        )
    )

    image.alpha_composite(halo_layer)
    image.alpha_composite(core_layer)

    # Sharp loops and ovals.

    draw = ImageDraw.Draw(
        image,
        "RGBA"
    )

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
            outline=rng.choice(BACKGROUND_COLORS) + (65,),
            width=rng.choice((1, 2))
        )

    # Interference dots.

    for _ in range(260):

        x = rng.randint(0, WIDTH - 1)
        y = rng.randint(0, HEIGHT - 1)

        radius = rng.choice((1, 1, 2))

        draw.ellipse(
            (
                x - radius,
                y - radius,
                x + radius,
                y + radius
            ),
            fill=rng.choice(BACKGROUND_COLORS) + (65,)
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
# CHARACTER WARPING
# ==========================================

def _warp_mask(
    mask: Image.Image,
    rng: random.Random
) -> Image.Image:

    # Slightly vary each character's proportions.

    scale_x = rng.uniform(*GLYPH_SCALE_X)
    scale_y = rng.uniform(*GLYPH_SCALE_Y)

    new_width = max(
        1,
        round(mask.width * scale_x)
    )

    new_height = max(
        1,
        round(mask.height * scale_y)
    )

    mask = mask.resize(
        (new_width, new_height),
        Image.Resampling.BICUBIC
    )

    # Add transparent padding for the curved displacement.

    padding = 10

    canvas = Image.new(
        "L",
        (
            mask.width + padding * 2,
            mask.height + padding * 2
        ),
        0
    )

    canvas.paste(
        mask,
        (padding, padding)
    )

    source = canvas.load()

    warped = Image.new(
        "L",
        canvas.size,
        0
    )

    destination = warped.load()

    width, height = canvas.size

    amplitude_x = rng.uniform(*GLYPH_WARP_X)
    amplitude_y = rng.uniform(*GLYPH_WARP_Y)

    phase_x = rng.uniform(0, math.tau)
    phase_y = rng.uniform(0, math.tau)

    # Smooth horizontal and vertical displacement.

    x_displacement = [
        amplitude_x * math.sin(
            math.tau * y / max(1, height - 1) + phase_x
        )
        for y in range(height)
    ]

    y_displacement = [
        amplitude_y * math.sin(
            math.tau * x / max(1, width - 1) + phase_y
        )
        for x in range(width)
    ]

    # Bilinear sampling keeps the warped edges smooth.

    for y in range(height):

        for x in range(width):

            source_x = x + x_displacement[y]
            source_y = y + y_displacement[x]

            if (
                source_x < 0
                or source_y < 0
                or source_x >= width - 1
                or source_y >= height - 1
            ):
                continue

            x0 = int(source_x)
            y0 = int(source_y)

            fx = source_x - x0
            fy = source_y - y0

            top_left = source[x0, y0]
            top_right = source[x0 + 1, y0]
            bottom_left = source[x0, y0 + 1]
            bottom_right = source[x0 + 1, y0 + 1]

            top = (
                top_left * (1 - fx)
                + top_right * fx
            )

            bottom = (
                bottom_left * (1 - fx)
                + bottom_right * fx
            )

            value = (
                top * (1 - fy)
                + bottom * fy
            )

            destination[x, y] = round(value)

    return warped


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
    blur: float = 0,
    rotation: float = 0,
    gradient: tuple[
        tuple[int, int, int],
        tuple[int, int, int]
    ] | None = None
) -> None:

    # Independent character rotation.

    if abs(rotation) > 0.01:

        mask = mask.rotate(
            rotation,
            resample=Image.Resampling.BICUBIC,
            expand=True,
            fillcolor=0
        )

    # Optional blur.

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

    # Apply a horizontal color gradient.

    if gradient is not None:

        start, end = gradient

        gradient_mask = Image.linear_gradient(
            "L"
        ).resize(
            mask.size,
            Image.Resampling.BICUBIC
        )

        layer = ImageOps.colorize(
            gradient_mask,
            black=start,
            white=end
        ).convert("RGBA")

    else:

        layer = Image.new(
            "RGBA",
            mask.size,
            color + (255,)
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

    # Separate blurred halos from stronger center lines.

    core_layer = Image.new(
        "RGBA",
        (WIDTH, HEIGHT),
        (0, 0, 0, 0)
    )

    halo_layer = Image.new(
        "RGBA",
        (WIDTH, HEIGHT),
        (0, 0, 0, 0)
    )

    core_draw = ImageDraw.Draw(
        core_layer,
        "RGBA"
    )

    halo_draw = ImageDraw.Draw(
        halo_layer,
        "RGBA"
    )

    colors = (
        (116, 123, 120),
        (164, 156, 150),
        (113, 124, 145),
        (158, 124, 132),
    )

    for _ in range(12):

        x0 = (
            rng.uniform(-80, 80),
            rng.uniform(-40, HEIGHT + 40)
        )

        x3 = (
            rng.uniform(WIDTH - 80, WIDTH + 80),
            rng.uniform(-40, HEIGHT + 40)
        )

        x1 = (
            rng.uniform(120, 390),
            rng.uniform(-100, HEIGHT + 100)
        )

        x2 = (
            rng.uniform(570, 850),
            rng.uniform(-100, HEIGHT + 100)
        )

        points = _cubic_bezier(
            x0,
            x1,
            x2,
            x3,
            steps=40
        )

        color = rng.choice(colors)

        # Very transparent blurred outer line.

        halo_draw.line(
            points,
            fill=color + (FOREGROUND_HALO_OPACITY,),
            width=FOREGROUND_HALO_WIDTH
        )

        # More visible center line.

        core_draw.line(
            points,
            fill=color + (FOREGROUND_CORE_OPACITY,),
            width=FOREGROUND_LINE_WIDTH
        )

    halo_layer = halo_layer.filter(
        ImageFilter.GaussianBlur(
            FOREGROUND_HALO_BLUR
        )
    )

    image.alpha_composite(halo_layer)
    image.alpha_composite(core_layer)


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

    font_families = _load_fonts()

    image = _gradient_background()

    rng = random.Random(
        secrets.randbits(64)
    )

    _draw_background(
        image,
        rng
    )

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

        # Choose a font family independently for this hidden character.

        _, hidden_bold_font, hidden_has_bold_face = rng.choice(
            font_families
        )

        hidden_mask = _glyph_mask(
            hidden_char,
            hidden_bold_font,
            target_width,
            embolden=not hidden_has_bold_face
        )

        hidden_mask = _warp_mask(
            hidden_mask,
            rng
        )

        _composite_mask(
            image,
            hidden_mask,
            center_x + rng.uniform(-3, 3),
            center_y + rng.uniform(-7, 7),
            HIDDEN_COLOR,
            HIDDEN_OPACITY,
            blur=HIDDEN_BLUR,
            rotation=rng.uniform(
                -GLYPH_ROTATION,
                GLYPH_ROTATION
            ),
            gradient=rng.choice(HIDDEN_GRADIENTS)
        )

        # Choose a font family independently for this decoy character.

        decoy_regular_font, _, _ = rng.choice(
            font_families
        )

        decoy_mask = _glyph_mask(
            decoy_char,
            decoy_regular_font,
            target_width
        )

        decoy_mask = _warp_mask(
            decoy_mask,
            rng
        )

        outline = _outline_mask(
            decoy_mask
        )

        _composite_mask(
            image,
            outline,
            center_x + rng.uniform(-3, 3),
            center_y + rng.uniform(-7, 7),
            DECOY_COLOR,
            DECOY_OPACITY,
            rotation=rng.uniform(
                -GLYPH_ROTATION,
                GLYPH_ROTATION
            ),
            gradient=rng.choice(DECOY_GRADIENTS)
        )

    # Foreground interference.

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
