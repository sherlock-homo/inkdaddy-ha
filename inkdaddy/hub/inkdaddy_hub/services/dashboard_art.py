"""Full-screen dashboard artwork for the 800 × 480 six-ink frame."""

from __future__ import annotations

from calendar import day_abbr, day_name, month_abbr, month_name
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any

from PIL import Image, ImageDraw, ImageFont

from .weather_art import (
    BLACK,
    BLUE,
    GREEN,
    RED,
    WEATHER_SKIES,
    WHITE,
    YELLOW,
    foreground,
    weather_condition,
    weather_landscape,
    weather_symbol,
)

if TYPE_CHECKING:
    from .dashboard_content import WidgetText


_INKS = {"black": BLACK, "white": WHITE, "green": GREEN, "blue": BLUE, "red": RED, "yellow": YELLOW}
_TYPEFACE = Path(__file__).resolve().parent.parent / "fonts" / "BricolageGrotesque.ttf"


@lru_cache(maxsize=192)
def _font(size: int, weight: int) -> ImageFont.FreeTypeFont:
    face = ImageFont.truetype(str(_TYPEFACE), size, layout_engine=ImageFont.Layout.BASIC)
    face.set_variation_by_axes([min(max(size, 12), 96), weight, 100])
    return face


def _text(
    draw: ImageDraw.ImageDraw,
    message: str,
    x: int,
    y: int,
    width: int,
    size: int,
    color: tuple[int, int, int],
    *,
    weight: int = 600,
    anchor: str = "lt",
) -> None:
    text = " ".join(message.split())[:256]
    if not text or width <= 0:
        return
    font = _font(size, weight)
    while size > 12 and font.getlength(text) > width:
        size -= 1
        font = _font(size, weight)
    if font.getlength(text) > width:
        while text and font.getlength(text + "…") > width:
            text = text[:-1]
        text += "…"
    draw.text((x, y), text, font=font, fill=color, anchor=anchor)


def _drawing(image: Image.Image) -> ImageDraw.ImageDraw:
    draw = ImageDraw.Draw(image)
    # Hard glyph edges preserve small strokes when packed to the physical six inks.
    draw.fontmode = "1"
    return draw


def draw_photo_caption(image: Image.Image, x: int, width: int, taken: str, age: str, location: str) -> None:
    """Draw metadata directly in panel inks so thin lettering survives photo dithering."""
    draw = _drawing(image)
    title_font = _font(22, 700)
    place_font = _font(18, 650)
    padding = 12
    max_line = width - 2 * padding
    gap = 16
    # Keep the date and elapsed time together, and wrap a long venue or address below.
    first_width = round(title_font.getlength(taken) + title_font.getlength(age)) + (gap if taken and age else 0)
    places = [location] if location else []
    if location and place_font.getlength(location) > max_line:
        words = location.split()
        for index in range(1, len(words)):
            if place_font.getlength(" ".join(words[:index + 1])) > max_line:
                places = [" ".join(words[:index]), " ".join(words[index:])]
                break
    text_width = min(max_line, max([first_width] + [round(place_font.getlength(line)) for line in places]))
    row_heights = (28 if taken or age else 0) + 22 * len(places)
    y = 462 - row_heights - 13
    draw.rounded_rectangle((x, y, x + text_width + 2 * padding, 462), radius=8, fill=BLACK, outline=WHITE, width=1)
    left, top = x + padding, y + 7
    if taken or age:
        draw.text((left, top), taken, fill=YELLOW, font=title_font, anchor="lt")
        age_x = left + round(title_font.getlength(taken)) + (gap if taken and age else 0)
        draw.text((age_x, top), age, fill=RED, font=title_font, anchor="lt")
        top += 28
    for line in places:
        _text(draw, line, left, top, text_width, 18, GREEN, weight=650)
        top += 22


def _paste_symbol(tile: Image.Image, condition: str, left: int, top: int, size: int) -> None:
    symbol = weather_symbol(condition).resize((size, size), Image.Resampling.NEAREST)
    tile.paste(symbol, (left, top), symbol)


def _date_tile(
    size: tuple[int, int],
    widget: WidgetText,
    primary: tuple[int, int, int],
    accent: tuple[int, int, int],
) -> Image.Image:
    w, h = size
    tile = Image.new("RGB", size, primary)
    d = _drawing(tile)
    fg = foreground(primary)
    pad = 16 if w < 250 else 21
    # Day names and month headings come from the local-date value resolved for this slot.
    parts = widget.value.replace(",", "").split()
    if len(parts) != 3 or parts[0] not in day_abbr or parts[1] not in month_abbr:
        _text(d, widget.label, pad, 16, w - 2 * pad, 22, fg)
        _text(d, widget.value, pad, h // 2, w - 2 * pad, 48, fg)
        return tile
    weekday = day_name[list(day_abbr).index(parts[0])]
    month = month_name[list(month_abbr).index(parts[1])]
    number = parts[2].lstrip("0") or "0"
    _text(
        d,
        widget.label if widget.label and widget.label != "Date" else weekday,
        pad,
        17,
        w - 2 * pad - 24,
        24 if w >= 250 else 19,
        fg,
    )
    # Small rising bars echo the ink stripes in the page masthead.
    mark = accent if accent != primary else fg
    for i in range(3):
        x = w - pad - 2 - i * 7
        d.rectangle((x - 3, 17 + i * 4, x, 35), fill=mark)
    if w < 250:
        _text(d, number, pad, h // 2 - 41, w - 2 * pad, min(86, h // 2), fg, weight=800)
        _text(d, month, pad, h - 34, w - 2 * pad, 23, fg, weight=600)
    else:
        number_size = min(142, h - 58, w // 3)
        number_width = round(_font(number_size, 800).getlength(number))
        baseline = (h + number_size) // 2 + 16
        _text(d, number, pad - 3, baseline, w - 2 * pad, number_size, fg, weight=800, anchor="ls")
        month_x = pad + number_width + 12
        _text(d, month, month_x, baseline - 4, w - month_x - pad, 39, fg, weight=600, anchor="ls")
    return tile


def _weather_tile(size: tuple[int, int], widget: WidgetText) -> Image.Image:
    w, h = size
    condition = weather_condition(widget.value)
    sky = WEATHER_SKIES.get(condition, WHITE)
    fg = foreground(sky)
    tile = Image.new("RGB", size, sky)
    d = _drawing(tile)
    pad = 16 if w < 250 else 21
    # Type and artwork occupy separate rectangles, even on a 1-column tile.
    icon_size = min(int(w * 0.45), h - 22, 192)
    left = w - icon_size - 4
    narrow_text_area = 64 if "Reported " in widget.detail else 45
    landscape_bottom = h - narrow_text_area if w < 250 else h
    weather_landscape(d, condition, (left, h // 2, w, landscape_bottom))
    _paste_symbol(tile, condition, left, max(7, (h - icon_size) // 2 - 19), icon_size)
    text_width = left - pad - 3
    parts = widget.detail.split(" · ")
    temperature = next((part for part in parts if not part.startswith("Reported ")), "")
    as_of = next(
        (part.replace("Reported ", "As of ", 1) for part in parts if part.startswith("Reported ")),
        "",
    )
    temperature = temperature.replace(" °", "°")
    custom = widget.label and widget.label != "Weather"
    if custom:
        _text(d, widget.label, pad, 15, text_width, 18, fg, weight=600)
    if temperature:
        _text(
            d, temperature, pad, max(45, h // 2 - 36), text_width, min(70, h // 2), fg, weight=750
        )
    value = widget.value or "Unavailable"
    # Keep long conditions such as 'Lightning rainy' clear of the illustration.
    if w < 250:
        _text(d, value, pad, h - (58 if as_of else 39), w - 2 * pad, 21, fg)
    else:
        _text(d, value, pad, h - (65 if as_of else 49), left - pad, 31, fg)
    if as_of:
        _text(d, as_of, pad, h - 23, w - 2 * pad if w < 250 else left - pad, 14, fg, weight=500)
    return tile


def _battery_tile(size: tuple[int, int], widget: WidgetText, *, vertical: bool = False) -> Image.Image:
    w, h = size
    tile = Image.new("RGB", size, BLACK)
    d = _drawing(tile)
    pad = 16 if w < 250 else 21
    title = "Battery" if widget.label in ("Display battery", "Battery") else widget.label
    _text(d, title, pad, 16, w - 2 * pad, 23, WHITE)
    try:
        level = float(widget.value.removesuffix("%"))
        if not (0 <= level <= 100):
            raise ValueError
    except ValueError:
        _text(d, widget.value, pad, h // 2, w - 2 * pad, 36, WHITE)
        return tile
    if vertical:
        body_h = min(h - 74, 160)
        body_w = min(w - 2 * pad, round(body_h * 0.67))
        left = (w - body_w) // 2
        top = (h + 46 - body_h) // 2 + 3
        cap = body_w // 4
        d.rounded_rectangle((left + cap, top - 7, left + body_w - cap, top + 1), radius=3, fill=WHITE)
        d.rounded_rectangle((left, top, left + body_w, top + body_h), radius=11, fill=WHITE)
        inner_w, inner_h = body_w - 14, body_h - 14
        track = Image.new("RGB", (inner_w, inner_h), WHITE)
        track_draw = _drawing(track)
        amount = round(inner_h * level / 100)
        if amount:
            track_draw.rectangle((0, inner_h - amount, inner_w - 1, inner_h - 1),
                                 fill=RED if level <= 20 else YELLOW if level <= 50 else GREEN)
        d.rounded_rectangle((left + 4, top + 4, left + body_w - 4, top + body_h - 4), radius=7, fill=BLACK)
        _text(track_draw, widget.value, inner_w // 2, inner_h // 2, inner_w - 6, 39, BLACK,
              weight=750, anchor="mm")
        tile.paste(track, (left + 7, top + 7))
        return tile
    outer_w = min(w - 2 * pad - 12, 370)
    outer_h = min(87, h - 77, outer_w // 2)
    left = (w - outer_w - 10) // 2
    top = (h - outer_h) // 2 + 18
    right, bottom = left + outer_w, top + outer_h
    d.rounded_rectangle(
        (right + 1, top + outer_h // 3, right + 9, bottom - outer_h // 3), radius=3, fill=WHITE
    )
    d.rounded_rectangle((left, top, right, bottom), radius=12, outline=WHITE, width=4)
    # A fixed white track makes the unfilled portion visible at every charge level.
    inner_w, inner_h = outer_w - 16, outer_h - 16
    track = Image.new("RGB", (inner_w, inner_h), WHITE)
    track_draw = _drawing(track)
    charge = RED if level <= 20 else YELLOW if level <= 50 else GREEN
    amount = round(inner_w * level / 100)
    if amount:
        track_draw.rectangle((0, 0, amount - 1, inner_h - 1), fill=charge)
    _text(
        track_draw,
        widget.value,
        inner_w // 2,
        inner_h // 2,
        inner_w - 8,
        min(48, inner_h - 4),
        BLACK,
        weight=750,
        anchor="mm",
    )
    tile.paste(track, (left + 8, top + 8))
    return tile


def render_forecast_tile(
    size: tuple[int, int], forecast: WidgetText, accent: str = "yellow", *, legacy: bool = False
) -> Image.Image:
    w, h = size
    bg = _INKS.get(accent, YELLOW)
    fg = foreground(bg)
    tile = Image.new("RGB", size, bg)
    d = _drawing(tile)
    days = getattr(forecast, "days", ())
    pad = 9 if w < 240 else 16
    title = forecast.label or "7-day forecast"
    unit = getattr(forecast, "unit", "")
    place = forecast.value
    location = " · ".join(part for part in (place, "°" + unit if unit else "") if part)
    short = h < 146
    compact = w < 250
    header_size = 16 if short or compact else 23
    if compact:
        _text(d, title, pad, 8, w - 2 * pad, header_size, fg, weight=700)
        _text(d, location, pad, 28, w - 2 * pad, 14, fg)
        band_top = 50
    else:
        _text(d, title, pad, 10 if short else 15, w // 2 - 12, header_size, fg, weight=700)
        _text(
            d,
            location,
            w - pad,
            11 if short else 19,
            w // 2 - 14,
            15 if short else 17,
            fg,
            anchor="rt",
        )
        band_top = 33 if short else 53
    if not days:
        _text(d, forecast.detail or "Forecast unavailable", pad, h // 2 + 4, w - 2 * pad, 21, fg)
        return tile
    bottom = h - (16 if legacy else 6)
    if legacy:
        _text(d, "open-meteo.com", w - pad, bottom, w - 2 * pad, 12, fg, weight=500, anchor="rt")
    band_h = bottom - band_top - 3
    wide = not legacy and w >= 600 and h >= 175
    col = (w - 2 * pad) / 7
    if not compact:
        d.line((pad, band_top - 5, w - pad, band_top - 5), fill=fg, width=1)
    for i, day in enumerate(days[:7]):
        x = round(pad + (i + 0.5) * col)
        label = date.fromisoformat(day.date).strftime("%a")
        _text(
            d,
            date.fromisoformat(day.date).strftime("%A") if wide else label[:1] if compact else label,
            x,
            band_top,
            int(col) - 2,
            21 if wide else 13 if short else 16,
            fg,
            anchor="mt",
        )
        high_y, low_y = band_top + round(band_h * 0.61), band_top + round(band_h * 0.82)
        # Tiny photo overlays use the compact text rows; dashboard tiles have room for sky symbols.
        if band_h >= 81:
            icon_size = min(int(col) - 5, 68 if wide else 42, max(16, band_h // 3))
            icon_y = band_top + (29 if wide else 18 if compact else 22)
            condition = day.condition
            symbol = Image.new("RGB", (128, 128), WEATHER_SKIES.get(condition, WHITE))
            _paste_symbol(symbol, condition, 0, 0, 128)
            swatch = symbol.resize((icon_size, icon_size), Image.Resampling.NEAREST)
            mask = Image.new("L", (icon_size, icon_size))
            ImageDraw.Draw(mask).ellipse((0, 0, icon_size - 1, icon_size - 1), fill=255)
            tile.paste(swatch, (x - icon_size // 2, icon_y), mask)
        else:
            high_y, low_y = band_top + round(band_h * 0.34), band_top + round(band_h * 0.68)
        _text(
            d,
            f"{day.high}°",
            x,
            high_y,
            int(col) - 1,
            33 if wide else 16 if short or compact else 25,
            fg,
            weight=800,
            anchor="mt",
        )
        _text(
            d,
            f"{day.low}°",
            x,
            low_y,
            int(col) - 1,
            21 if wide else 13 if short or compact else 17,
            fg,
            weight=500,
            anchor="mt",
        )
    return tile


def _generic_tile(
    size: tuple[int, int], widget: WidgetText, kind: str, accent: tuple[int, int, int]
) -> Image.Image:
    w, h = size
    tile = Image.new("RGB", size, WHITE)
    d = _drawing(tile)
    d.rectangle((0, 0, 6, h), fill=accent if accent != WHITE else BLACK)
    pad = 17
    if widget.label:
        _text(d, widget.label, pad, 15, w - 2 * pad, 21, BLACK)
    if kind == "calendar":
        lines = [widget.value, *widget.detail.splitlines()]
        available = h - 62
        for i, line in enumerate(lines):
            _text(d, line, pad, 50 + i * min(33, available // len(lines)), w - 2 * pad, 23, BLACK)
    else:
        _text(d, widget.value, pad, h // 2 - 9, w - 2 * pad, 41, BLACK, weight=700)
        if widget.detail and kind not in ("location", "device_battery"):
            _text(d, widget.detail, pad, h - 39, w - 2 * pad, 19, BLACK)
    return tile


def render_modern_dashboard(
    config: dict[str, Any], widgets: list[WidgetText], *, legacy: bool = False
) -> Image.Image:
    theme = config.get("theme") or {}
    primary = _INKS.get(theme.get("primary"), BLUE)
    accent_name = theme.get("accent", "yellow")
    accent = _INKS.get(accent_name, YELLOW)
    image = Image.new("RGB", (800, 480), WHITE)
    if legacy:
        # Offers from dev.26–28 included a masthead in their checksummed artwork.
        d = _drawing(image)
        _text(d, str(config.get("name") or "Home Overview"), 17, 13, 622, 33, BLACK, weight=750)
        swatches = [primary, accent, GREEN, RED, BLUE, BLACK]
        for i, ink in enumerate(swatches):
            x = 652 + i * 22
            d.polygon(((x + 7, 17), (x + 23, 17), (x + 15, 36), (x, 36)), fill=ink)
    for tile, widget in zip(config["widgets"], widgets, strict=True):
        if legacy:
            x, y = 14 + tile["x"] * 195, 60 + tile["y"] * 205
            size = (tile["w"] * 195 - 7, tile["h"] * 205 - 7)
        else:
            # Gutters separate cells inside the grid. Tiles reach every exterior edge.
            right_cell = tile["x"] + tile["w"]
            bottom_cell = tile["y"] + tile["h"]
            x = tile["x"] * 200 + (3 if tile["x"] else 0)
            y = tile["y"] * 240 + (3 if tile["y"] else 0)
            right = right_cell * 200 - (3 if right_cell < 4 else 0)
            bottom = bottom_cell * 240 - (3 if bottom_cell < 2 else 0)
            size = (right - x, bottom - y)
        kind = tile.get("source", {}).get("type")
        if kind == "date":
            art = _date_tile(size, widget, primary, accent)
        elif kind == "device_battery":
            art = _battery_tile(size, widget, vertical=not legacy and size[0] < 250)
        elif kind == "forecast":
            art = render_forecast_tile(size, widget, accent_name, legacy=legacy)
        elif tile["type"] == "weather_summary" or kind == "weather":
            art = _weather_tile(size, widget)
        else:
            art = _generic_tile(size, widget, kind, accent)
        mask = None
        if legacy:
            mask = Image.new("L", size)
            ImageDraw.Draw(mask).rounded_rectangle((0, 0, size[0] - 1, size[1] - 1), radius=13, fill=255)
        image.paste(art, (x, y), mask)
    return image
