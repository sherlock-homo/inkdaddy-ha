from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Iterable

from PIL import Image, ImageChops


@dataclass(frozen=True)
class Palette:
    name: str
    colors: tuple[tuple[int, int, int], ...]
    packed_values: tuple[int, ...]
    bits_per_pixel: int = 4
    packing: str = "high_nibble_first"


WAVESHARE_7COLOR_6 = Palette(
    name="waveshare_7color_6",
    colors=(
        (0, 0, 0),
        (255, 255, 255),
        (0, 180, 0),
        (0, 0, 255),
        (255, 0, 0),
        (255, 255, 0),
    ),
    # Visual palette order differs from the Spectra/Waveshare 7.3E panel nibble codes.
    packed_values=(0x0, 0x1, 0x6, 0x5, 0x3, 0x2),
)

PALETTES = {WAVESHARE_7COLOR_6.name: WAVESHARE_7COLOR_6}

PHOTO_DITHER_VERSION = "linear-srgb-floyd-steinberg-v1"


def _linear_srgb(value: int) -> int:
    encoded = value / 255
    linear = encoded / 12.92 if encoded <= 0.04045 else ((encoded + 0.055) / 1.055) ** 2.4
    return round(255 * linear)


_LINEAR_SRGB = tuple(_linear_srgb(value) for value in range(256))
_MONOCHROME_TO_PANEL = (
    bytes([WAVESHARE_7COLOR_6.packed_values[0]]) * 255
    + bytes([WAVESHARE_7COLOR_6.packed_values[1]])
)


def get_palette(name: str) -> Palette:
    try:
        return PALETTES[name]
    except KeyError as exc:
        raise ValueError(f"Unknown palette {name!r}.") from exc


def nearest_palette_index(rgb: tuple[int, int, int], palette: Palette = WAVESHARE_7COLOR_6) -> int:
    r, g, b = rgb
    best_index = 0
    best_distance = float("inf")
    for index, (pr, pg, pb) in enumerate(palette.colors):
        distance = (r - pr) ** 2 + (g - pg) ** 2 + (b - pb) ** 2
        if distance < best_distance:
            best_distance = distance
            best_index = index
    return best_index


def image_to_palette_indices(image: Image.Image, palette: Palette = WAVESHARE_7COLOR_6) -> list[int]:
    rgb_image = image.convert("RGB")
    raw = rgb_image.tobytes()
    return [
        nearest_palette_index((raw[index], raw[index + 1], raw[index + 2]), palette)
        for index in range(0, len(raw), 3)
    ]


def indices_to_image(
    indices: Iterable[int],
    *,
    width: int,
    height: int,
    palette: Palette = WAVESHARE_7COLOR_6,
) -> Image.Image:
    values = list(indices)
    if len(values) != width * height:
        raise ValueError(f"Expected {width * height} pixels, got {len(values)}.")
    image = Image.new("RGB", (width, height))
    image.putdata([palette.colors[index] for index in values])
    return image


def pack_indices(indices: Iterable[int], bits_per_pixel: int = 4) -> bytes:
    values = list(indices)
    if bits_per_pixel not in (1, 2, 4):
        raise ValueError("Only 1, 2, and 4 bpp packing is supported.")
    max_value = (1 << bits_per_pixel) - 1
    packed = bytearray()
    accumulator = 0
    bits_used = 0
    for value in values:
        if value < 0 or value > max_value:
            raise ValueError(f"Palette index {value} does not fit in {bits_per_pixel} bpp.")
        shift = 8 - bits_per_pixel - bits_used
        accumulator |= value << shift
        bits_used += bits_per_pixel
        if bits_used == 8:
            packed.append(accumulator)
            accumulator = 0
            bits_used = 0
    if bits_used:
        packed.append(accumulator)
    return bytes(packed)


def palette_indices_to_panel_values(indices: Iterable[int], palette: Palette = WAVESHARE_7COLOR_6) -> list[int]:
    values: list[int] = []
    for index in indices:
        if index < 0 or index >= len(palette.packed_values):
            raise ValueError(f"Palette index {index} is not valid for {palette.name}.")
        values.append(palette.packed_values[index])
    return values


def unpack_indices(data: bytes, pixel_count: int, bits_per_pixel: int = 4) -> list[int]:
    if bits_per_pixel not in (1, 2, 4):
        raise ValueError("Only 1, 2, and 4 bpp packing is supported.")
    mask = (1 << bits_per_pixel) - 1
    values: list[int] = []
    for byte in data:
        for shift in range(8 - bits_per_pixel, -1, -bits_per_pixel):
            values.append((byte >> shift) & mask)
            if len(values) == pixel_count:
                return values
    if len(values) != pixel_count:
        raise ValueError(f"Packed data ended after {len(values)} pixels; expected {pixel_count}.")
    return values


def raw_frame_from_image(
    image: Image.Image, palette: Palette = WAVESHARE_7COLOR_6, *, dither: bool = False
) -> tuple[bytes, str]:
    values: list[int] | bytes
    if dither:
        values = _dither_to_panel_values(image.convert("RGB"), palette.colors, palette.packed_values)
    else:
        values = palette_indices_to_panel_values(image_to_palette_indices(image, palette), palette)
    return _pack_frame(values, palette.bits_per_pixel)


def raw_photo_frame(image: Image.Image) -> tuple[bytes, str]:
    # Adjacent panel pixels average in light. Diffuse errors in linear sRGB so
    # mixes preserve the brightness of midtones in the nominal panel palette.
    palette = WAVESHARE_7COLOR_6
    rgb_image = image.convert("RGB")
    red, green, blue = rgb_image.split()
    if not (ImageChops.difference(red, green).getbbox() or ImageChops.difference(green, blue).getbbox()):
        # Use only black and white when the input is entirely monochrome.
        grayscale = red.point(_LINEAR_SRGB).convert("1", dither=Image.Dither.FLOYDSTEINBERG)
        values = grayscale.convert("L").tobytes().translate(_MONOCHROME_TO_PANEL)
        return _pack_frame(values, palette.bits_per_pixel)

    linear_image = rgb_image.point(_LINEAR_SRGB * 3)
    linear_palette = tuple(
        (_LINEAR_SRGB[red], _LINEAR_SRGB[green], _LINEAR_SRGB[blue])
        for red, green, blue in palette.colors
    )
    values = _dither_to_panel_values(linear_image, linear_palette, palette.packed_values)
    return _pack_frame(values, palette.bits_per_pixel)


def _dither_to_panel_values(
    rgb_image: Image.Image,
    colors: tuple[tuple[int, int, int], ...],
    panel_values: tuple[int, ...],
) -> bytes:
    palette_image = Image.new("P", (1, 1))
    padding = 256 - len(colors)
    swatches = colors + (colors[-1],) * padding
    palette_image.putpalette([channel for color in swatches for channel in color])
    indices = rgb_image.quantize(palette=palette_image, dither=Image.Dither.FLOYDSTEINBERG)
    translation = bytes(panel_values) + bytes([panel_values[-1]]) * padding
    return indices.tobytes().translate(translation)


def _pack_frame(values: list[int] | bytes, bits_per_pixel: int) -> tuple[bytes, str]:
    if bits_per_pixel == 4:
        low = values[1::2]
        if len(values) % 2:
            low = [*low, 0]
        raw = bytes((first << 4) | second for first, second in zip(values[::2], low))
    else:
        raw = pack_indices(values, bits_per_pixel)
    return raw, hashlib.sha256(raw).hexdigest()
