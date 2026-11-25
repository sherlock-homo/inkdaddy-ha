from dataclasses import replace
from datetime import date, timedelta

from inkdaddy_hub.services.dashboard_art import _battery_tile, _weather_tile, render_forecast_tile
from inkdaddy_hub.services.dashboard_content import ForecastText, WidgetText, render_dashboard
from inkdaddy_hub.services.dashboard_forecast import ForecastDay
from inkdaddy_hub.services.home_assistant import _WEATHER_STATES
from inkdaddy_hub.services.palette import WAVESHARE_7COLOR_6, raw_frame_from_image
from inkdaddy_hub.services.weather_art import (
    BLACK,
    GREEN,
    RED,
    WEATHER_SKIES,
    YELLOW,
    foreground,
)
from PIL import ImageChops


def _luminance(rgb):
    linear = [
        v / 255 / 12.92 if v / 255 <= 0.04045 else ((v / 255 + 0.055) / 1.055) ** 2.4 for v in rgb
    ]
    return sum(v * w for v, w in zip(linear, (0.2126, 0.7152, 0.0722)))


def test_every_weather_sky_keeps_legible_text_on_the_packed_panel():
    assert set(WEATHER_SKIES) == _WEATHER_STATES
    art = set()
    for condition, background in WEATHER_SKIES.items():
        value = condition.replace("partlycloudy", "partly cloudy").replace("-", " ").capitalize()
        tile = _weather_tile((188, 198), WidgetText("Weather", value, "52 °F"))
        text_ink = foreground(background)
        lo, hi = sorted([_luminance(background), _luminance(text_ink)])
        assert (hi + 0.05) / (lo + 0.05) >= 4.5
        # Both the temperature and condition must be present on uninterrupted sky.
        assert {c for _, c in tile.crop((14, 61, 95, 127)).getcolors()} == {background, text_ink}
        assert {c for _, c in tile.crop((13, 157, 177, 187)).getcolors()} == {background, text_ink}
        art.add(tile.crop((100, 12, 188, 152)).tobytes())
        raw, _ = raw_frame_from_image(tile, dither=True)
        panel = dict(zip(WAVESHARE_7COLOR_6.colors, WAVESHARE_7COLOR_6.packed_values))
        channels = iter(tile.tobytes())
        colors = zip(channels, channels, channels)
        assert raw == bytes((panel[a] << 4) | panel[b] for a, b in zip(colors, colors))
    assert len(art) == len(_WEATHER_STATES)


def test_battery_fill_tracks_real_charge_and_preserves_missing_state():
    w, h = 383, 198
    # Exact unoccluded row inside the outlined track.
    for level, color in [(0, RED), (12, RED), (50, YELLOW), (83, GREEN), (100, GREEN)]:
        tile = _battery_tile((w, h), WidgetText("Battery", f"{level}%", "Last reported"))
        channels = iter(tile.crop((20, 93, w - 21, 94)).tobytes())
        row = zip(channels, channels, channels)
        filled = sum(c == color for c in row)
        # The inner track spans 313 px at this widget size.
        assert filled == round(313 * level / 100)
    missing = _battery_tile((w, h), WidgetText("Battery", "Unavailable"))
    assert missing.getpixel((34, 93)) == BLACK
    assert ImageChops.difference(
        missing, _battery_tile((w, h), WidgetText("Battery", "0%"))
    ).getbbox()


def test_forecast_dispatch_renders_all_seven_days_on_full_dashboard():
    base = date(2026, 11, 25)
    days = tuple(
        ForecastDay(str(base + timedelta(days=i)), 50 + i, 30 + i, "sunny") for i in range(7)
    )
    forecast = ForecastText("7-day forecast", "Atlanta, GA", unit="F", days=days)
    cfg = {
        "name": "Home Status",
        "theme": {"primary": "red", "accent": "blue"},
        "widgets": [
            {
                "type": "weather_summary",
                "source": {"type": "forecast"},
                "x": 2,
                "y": 1,
                "w": 2,
                "h": 1,
            }
        ],
    }
    dashboard = render_dashboard(cfg, [forecast])
    tile = render_forecast_tile((383, 198), forecast, "blue")
    # Compare inside the rounded tile; the outer corners belong to the white page.
    assert tile.crop((0, 13, 383, 185)).tobytes() == dashboard.crop((404, 278, 787, 450)).tobytes()
    for i in range(7):
        rows = list(days)
        rows[i] = replace(rows[i], high=91)
        changed = render_forecast_tile((383, 198), replace(forecast, days=tuple(rows)), "blue")
        x0, y0, x1, _ = ImageChops.difference(tile, changed).getbbox()
        assert 16 + i * (351 / 7) <= x0 < x1 <= 17 + (i + 1) * (351 / 7)
        assert y0 > 80
    assert {c for _, c in tile.getcolors()} <= set(WAVESHARE_7COLOR_6.colors)
