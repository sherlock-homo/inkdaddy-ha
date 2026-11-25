"""Two-dimensional weather symbols drawn in the panel's six native inks."""

from __future__ import annotations

import math
from functools import lru_cache

from PIL import Image, ImageDraw

from .palette import WAVESHARE_7COLOR_6

BLACK, WHITE, GREEN, BLUE, RED, YELLOW = WAVESHARE_7COLOR_6.colors
WEATHER_SKIES = {
    "sunny": YELLOW,
    "partlycloudy": BLUE,
    "clear-night": BLACK,
    "cloudy": BLUE,
    "fog": WHITE,
    "hail": GREEN,
    "rainy": BLUE,
    "pouring": BLACK,
    "lightning": BLACK,
    "lightning-rainy": BLUE,
    "snowy": BLUE,
    "snowy-rainy": BLUE,
    "windy": GREEN,
    "windy-variant": GREEN,
    "exceptional": RED,
}


def foreground(background: tuple[int, int, int]) -> tuple[int, int, int]:
    # Of the panel inks, only blue and black need white lettering for 4.5:1.
    return WHITE if background in (BLUE, BLACK) else BLACK


def weather_condition(value: str) -> str:
    return value.lower().replace("partly cloudy", "partlycloudy").replace(" ", "-")


def _sun(draw: ImageDraw.ImageDraw, x: int, y: int, radius: int, ink: tuple[int, int, int]) -> None:
    for degree in range(0, 360, 45):
        theta = math.radians(degree)
        a, b = radius + 6, radius + 15
        draw.line(
            (
                x + math.cos(theta) * a,
                y + math.sin(theta) * a,
                x + math.cos(theta) * b,
                y + math.sin(theta) * b,
            ),
            fill=ink,
            width=5,
        )
    draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=ink)


def _cloud(draw: ImageDraw.ImageDraw, x: int, y: int, ink: tuple[int, int, int]) -> None:
    # Three joined circles make one continuous silhouette, including at 20px.
    for left, top, right, bottom in ((2, 22, 32, 51), (17, 1, 65, 49), (48, 15, 82, 50)):
        draw.ellipse((x + left, y + top, x + right, y + bottom), fill=ink)
    draw.rounded_rectangle((x + 4, y + 30, x + 83, y + 52), radius=11, fill=ink)


def _snowflake(draw: ImageDraw.ImageDraw, x: int, y: int, size: int) -> None:
    for dx, dy in ((size, 0), (size // 2, size), (-size // 2, size)):
        draw.line((x - dx, y - dy, x + dx, y + dy), fill=WHITE, width=3)


@lru_cache(maxsize=24)
def weather_symbol(condition: str) -> Image.Image:
    """Return a transparent 128px sky illustration; callers own the solid sky color."""
    sky = WEATHER_SKIES.get(condition, WHITE)
    image = Image.new("RGBA", (128, 128))
    draw = ImageDraw.Draw(image)
    if condition == "sunny":
        _sun(draw, 64, 63, 29, RED)
    elif condition == "clear-night":
        draw.ellipse((29, 26, 96, 95), fill=WHITE)
        draw.ellipse((47, 11, 106, 75), fill=sky)
        for x, y, r in ((98, 25, 6), (109, 66, 5), (18, 79, 4), (77, 109, 4)):
            draw.polygon(
                (
                    (x, y - r),
                    (x + 2, y - 2),
                    (x + r, y),
                    (x + 2, y + 2),
                    (x, y + r),
                    (x - 2, y + 2),
                    (x - r, y),
                    (x - 2, y - 2),
                ),
                fill=YELLOW,
            )
    elif condition == "fog":
        _sun(draw, 75, 43, 20, YELLOW)
        for i, (a, b) in enumerate(((24, 101), (12, 85), (31, 113), (17, 85))):
            y = 60 + i * 11
            draw.rounded_rectangle((a, y, b, y + 4), radius=2, fill=BLACK)
    elif condition in ("windy", "windy-variant"):
        if condition == "windy-variant":
            _cloud(draw, 23, 5, WHITE)
        for x, y, end in ((17, 48, 81), (9, 66, 104), (27, 85, 77)):
            draw.line((x, y, end, y), fill=BLACK, width=5)
            draw.arc((end - 12, y - 23, end + 11, y + 2), start=265, end=90, fill=BLACK, width=5)
    elif condition == "exceptional":
        draw.polygon(((64, 13), (117, 101), (10, 101)), fill=BLACK)
        draw.rounded_rectangle((59, 43, 69, 73), radius=4, fill=WHITE)
        draw.ellipse((59, 81, 69, 91), fill=WHITE)
    elif condition in WEATHER_SKIES:
        if condition == "partlycloudy":
            _sun(draw, 81, 41, 21, YELLOW)
        if condition == "cloudy":
            _cloud(draw, 32, 25, BLACK)
        _cloud(
            draw,
            17 if condition == "partlycloudy" else 22,
            46 if condition in ("partlycloudy", "cloudy") else 22,
            WHITE,
        )
        if condition in ("rainy", "pouring", "lightning-rainy", "snowy-rainy"):
            for x in (40, 63, 86) if condition != "snowy-rainy" else (40, 64):
                draw.line((x, 81, x - 8, 99 if condition == "pouring" else 93), fill=WHITE, width=4)
                if condition == "pouring":
                    draw.line((x + 5, 99, x, 110), fill=BLUE, width=4)
        if condition in ("snowy", "snowy-rainy"):
            for x, y in ((38, 88), (62, 102), (88, 87)) if condition == "snowy" else ((90, 90),):
                _snowflake(draw, x, y, 7)
        if condition == "hail":
            for x, y in ((41, 86), (66, 101), (88, 86)):
                draw.polygon(((x, y - 6), (x + 6, y), (x, y + 6), (x - 6, y)), fill=BLACK)
        if condition in ("lightning", "lightning-rainy"):
            draw.polygon(
                ((66, 62), (83, 62), (70, 83), (83, 83), (53, 114), (60, 91), (49, 91)), fill=YELLOW
            )
    else:
        draw.arc((22, 22, 104, 104), start=35, end=325, fill=BLACK, width=6)
        draw.line((45, 64, 82, 64), fill=BLACK, width=6)
    return image


def weather_landscape(
    draw: ImageDraw.ImageDraw, condition: str, box: tuple[int, int, int, int]
) -> None:
    """Two native-ink silhouettes make the right edge a little weather print."""
    if condition not in WEATHER_SKIES:
        return
    left, top, right, bottom = box
    w, h = right - left, bottom - top

    def ridge(points: tuple[tuple[float, float], ...], color: tuple[int, int, int]) -> None:
        outline = [(left + round(w * x), top + round(h * y)) for x, y in points]
        draw.polygon([*outline, (right, bottom), (left, bottom)], fill=color)

    if condition in ("snowy", "snowy-rainy", "hail"):
        ridge(((0, 1), (0.34, 0.61), (0.50, 0.79), (0.80, 0.39), (1, 0.62)), BLACK)
        draw.polygon(
            (
                (left + round(w * 0.66), top + round(h * 0.57)),
                (left + round(w * 0.80), top + round(h * 0.39)),
                (left + round(w * 0.94), top + round(h * 0.55)),
                (left + round(w * 0.81), top + round(h * 0.50)),
            ),
            fill=WHITE,
        )
        ridge(((0.42, 1), (0.73, 0.78), (1, 0.82)), WHITE)
    elif condition in ("rainy", "pouring", "lightning", "lightning-rainy"):
        ridge(
            ((0, 1), (0.38, 0.78), (0.65, 0.76), (1, 0.56)),
            BLUE if WEATHER_SKIES[condition] == BLACK else BLACK,
        )
        for x, y in ((0.53, 0.88), (0.77, 0.71), (0.83, 0.93)):
            a, b = left + round(w * x), top + round(h * y)
            draw.line((a, b, a + max(7, w // 12), b), fill=WHITE, width=2)
    elif condition == "clear-night":
        ridge(((0, 1), (0.28, 0.77), (0.40, 0.81), (0.77, 0.48), (1, 0.57)), BLUE)
    elif condition == "exceptional":
        for i in range(2):
            x = right - (i + 1) * max(13, w // 5)
            draw.polygon(
                (
                    (x, bottom),
                    (x + w // 8, bottom - h // 3),
                    (x + w // 5, bottom - h // 3),
                    (x + w // 14, bottom),
                ),
                fill=BLACK,
            )
    else:
        # Undulating silhouettes anchor the sky without adding a border behind the text.
        ridge(
            ((0, 1), (0.23, 0.82), (0.48, 0.77), (0.66, 0.63), (0.82, 0.59), (1, 0.61)),
            GREEN if WEATHER_SKIES[condition] != GREEN else WHITE,
        )
        ridge(((0.38, 1), (0.60, 0.91), (0.79, 0.91), (1, 0.78)), BLACK)
