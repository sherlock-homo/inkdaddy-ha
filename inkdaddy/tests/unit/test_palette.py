import hashlib
import unittest

from PIL import Image

from inkdaddy_hub.services.palette import (
    WAVESHARE_7COLOR_6,
    image_to_palette_indices,
    palette_indices_to_panel_values,
    pack_indices,
    raw_frame_from_image,
    raw_photo_frame,
    unpack_indices,
)


class PaletteTests(unittest.TestCase):
    def test_pack_unpack_four_bpp(self):
        indices = [0, 1, 2, 3, 4, 5, 0]
        packed = pack_indices(indices, bits_per_pixel=4)
        self.assertEqual(unpack_indices(packed, len(indices), bits_per_pixel=4), indices)

    def test_image_conversion_and_checksum(self):
        image = Image.new("RGB", (2, 2))
        image.putdata([(0, 0, 0), (255, 255, 255), (255, 0, 0), (255, 255, 0)])
        indices = image_to_palette_indices(image, WAVESHARE_7COLOR_6)
        self.assertEqual(indices, [0, 1, 4, 5])
        self.assertEqual(palette_indices_to_panel_values(indices, WAVESHARE_7COLOR_6), [0, 1, 3, 2])
        raw, digest = raw_frame_from_image(image, WAVESHARE_7COLOR_6)
        self.assertEqual(raw, bytes([0x01, 0x32]))
        self.assertEqual(len(raw), 2)
        self.assertEqual(len(digest), 64)

    def test_photo_dither_packs_the_six_panel_codes_in_pixel_order(self):
        image = Image.new("RGB", (6, 1))
        image.putdata(WAVESHARE_7COLOR_6.colors)
        raw, digest = raw_photo_frame(image)
        self.assertEqual(raw, bytes([0x01, 0x65, 0x32]))
        self.assertEqual(digest, hashlib.sha256(raw).hexdigest())

    def test_photo_dither_preserves_midgray_in_linear_light(self):
        image = Image.new("RGB", (800, 480), (128, 128, 128))
        raw, _digest = raw_photo_frame(image)
        self.assertEqual(len(raw), 192_000)
        pigments = unpack_indices(raw, 800 * 480)
        self.assertEqual(set(pigments), {0, 1})
        # A half-scale sRGB input reflects about 21.6% of the nominal white.
        self.assertAlmostEqual(pigments.count(1) / len(pigments), 0.216, delta=0.015)
