from __future__ import annotations

from dataclasses import dataclass
from math import isfinite

from PIL import Image, ImageChops, ImageFilter, ImageOps

PHOTO_FRAMING_MODES = ("fit", "fill", "intelligent")
_FOCUS_SAMPLE_SIZE = 128


@dataclass(frozen=True)
class PhotoFraming:
    mode: str
    x: float | None = None
    y: float | None = None
    zoom: float = 1.0

    def __post_init__(self) -> None:
        if self.mode not in PHOTO_FRAMING_MODES:
            raise ValueError("Choose Fit, Fill, or Intelligent Crop.")
        if (self.x is None) != (self.y is None):
            raise ValueError("Set both focus coordinates.")
        for coordinate in (self.x, self.y):
            if coordinate is not None and not _in_range(coordinate, 0, 1):
                raise ValueError("Focus coordinates must be between 0 and 1.")
        if not _in_range(self.zoom, 1, 3):
            raise ValueError("Zoom must be between 1 and 3.")
        if self.mode == "fit" and (self.x is not None or self.zoom != 1):
            raise ValueError("Choose Fill or Intelligent Crop to adjust focus and zoom.")


def _in_range(value: object, minimum: float, maximum: float) -> bool:
    return type(value) in (int, float) and isfinite(value) and minimum <= value <= maximum


def frame_photo(
    image: Image.Image, target: tuple[int, int], framing: PhotoFraming
) -> tuple[Image.Image, tuple[float, float]]:
    """Resize the content, returning its effective crop focus before any fit padding."""
    if framing.mode == "fit":
        return ImageOps.contain(image, target, method=Image.Resampling.LANCZOS), (0.5, 0.5)
    width, height = image.size
    target_ratio = target[0] / target[1]
    crop_width = min(width, height * target_ratio) / framing.zoom
    crop_height = min(height, width / target_ratio) / framing.zoom
    if framing.x is not None and framing.y is not None:
        x, y = framing.x, framing.y
    elif framing.mode == "intelligent":
        x, y = _local_focus(image, crop_width / width, crop_height / height)
    else:
        x, y = 0.5, 0.5
    left = max(0.0, min(width - crop_width, x * width - crop_width / 2))
    top = max(0.0, min(height - crop_height, y * height - crop_height / 2))
    box = (left, top, left + crop_width, top + crop_height)
    focus = ((left + crop_width / 2) / width, (top + crop_height / 2) / height)
    return image.resize(target, Image.Resampling.LANCZOS, box=box), focus


def _local_focus(image: Image.Image, width_fraction: float, height_fraction: float) -> tuple[float, float]:
    if width_fraction >= 1 and height_fraction >= 1:
        return 0.5, 0.5
    sample = ImageOps.contain(
        image, (_FOCUS_SAMPLE_SIZE, _FOCUS_SAMPLE_SIZE), method=Image.Resampling.BOX
    )
    width, height = sample.size
    if min(width, height) < 2:
        return 0.5, 0.5
    # Local RGB contrast can find detail in both luminance and color. Smoothing
    # favors a region of the scene over isolated pixels such as sensor noise.
    detail = ImageChops.difference(sample, sample.filter(ImageFilter.GaussianBlur(radius=2)))
    red, green, blue = (channel.tobytes() for channel in detail.split())
    stride = width + 1
    summed = [0] * (stride * (height + 1))
    for y in range(height):
        row_energy = 0
        for x in range(width):
            index = y * width + x
            row_energy += red[index] + green[index] + blue[index]
            summed[(y + 1) * stride + x + 1] = summed[y * stride + x + 1] + row_energy
    if not summed[-1]:
        return 0.5, 0.5

    window_w = max(1, round(width * width_fraction))
    window_h = max(1, round(height * height_fraction))
    travel_x, travel_y = width - window_w, height - window_h
    best_score = -1.0
    best = (0.5, 0.5)
    for top in range(travel_y + 1):
        y = (top + window_h / 2) / height
        for left in range(travel_x + 1):
            x = (left + window_w / 2) / width
            upper = top * stride
            lower = (top + window_h) * stride
            right = left + window_w
            energy = summed[lower + right] - summed[lower + left] - summed[upper + right] + summed[upper + left]
            center_cost = 0.06 * summed[-1] * (abs(x - 0.5) + abs(y - 0.5))
            score = energy - center_cost
            if score > best_score:
                best, best_score = (x, y), score
    return best
