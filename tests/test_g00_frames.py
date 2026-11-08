"""Regression cases for RealLive frame placement, raw export and allocation limits."""
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from unpacker import ExtractOptions, decode_image, extract, open_archive
from unpacker.core import FileView
from unpacker.formats import reallive
from unpacker.images_g00 import g00_to_image, parse_meta


def literals(raw, bytes_per_symbol=1):
    packed = bytearray()
    for start in range(0, len(raw), 8 * bytes_per_symbol):
        chunk = raw[start:start + 8 * bytes_per_symbol]
        packed.append((1 << (len(chunk) // bytes_per_symbol)) - 1)
        packed.extend(chunk)
    return struct.pack("<II", 8 + len(packed), len(raw)) + packed


def solid_tile(w, h, bgra):
    tile = struct.pack("<5H", 0, 0, 0, w, h) + bytes(0x52) + bytes(bgra) * (w * h)
    return struct.pack("<HH", 1, 1) + bytes(0x70) + tile


def container(w, h, frames):
    header = struct.pack("<BHHi", 2, w, h, len(frames))
    coords = bytearray()
    table = bytearray()
    payload = bytearray()
    for x, y, frame in frames:
        coords.extend(struct.pack("<ii", x, y) + bytes(16))
        table.extend(struct.pack("<II", 4 + 8 * len(frames) + len(payload), len(frame)))
        payload.extend(frame)
    return header + coords + literals(struct.pack("<i", len(frames)) + table + payload)


class G00Frames(unittest.TestCase):
    def test_archive_preview_and_both_export_modes(self):
        frames = [(0, 0, solid_tile(4, 4, (30, 20, 10, 255))),
                  (2, 1, solid_tile(2, 2, (60, 50, 40, 128)))]
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / "frames.g00"
            path.write_bytes(container(4, 4, frames))
            with open_archive(str(path)) as arc:
                self.assertEqual([e.name for e in arc.entries], ["0001.g00", "0002.g00"])
                with patch.object(arc, "read", side_effect=AssertionError("re-encoded frame during conversion")):
                    stats = extract(arc, arc.entries, ExtractOptions(str(root / "png")))
                self.assertEqual((stats.converted, stats.raw, stats.failed), (2, 0, 0))
                raw = extract(arc, arc.entries, ExtractOptions(str(root / "raw"), convert_png=False))
                self.assertEqual((raw.raw, raw.failed), (2, 0))
                for i, entry in enumerate(arc.entries):
                    preview = arc.decode_frame(entry)
                    independent, desc = decode_image((root / "raw" / entry.name).read_bytes(), None)
                    self.assertEqual(desc, "G00 (RealLive)")
                    with Image.open(root / "png" / f"{i + 1:04}.png") as png:
                        for image in (preview, independent, png):
                            self.assertEqual(image.size, (4, 4))
                            self.assertEqual(image.mode, "RGBA")
                            self.assertEqual(image.getpixel((0, 0)), [(10, 20, 30, 255), (0, 0, 0, 0)][i])
                            self.assertEqual(image.getpixel((2, 1)), [(10, 20, 30, 255), (40, 50, 60, 128)][i])

    def test_single_frame_and_clipping(self):
        frame = container(2, 2, [(-1, 1, solid_tile(2, 2, (3, 2, 1, 170)))])
        image = g00_to_image(frame)
        self.assertEqual(image.getpixel((0, 1)), (1, 2, 3, 170))
        self.assertEqual(image.getpixel((1, 1)), (0, 0, 0, 0))
        v0 = struct.pack("<BHH", 0, 1, 1) + literals(bytes((2, 4, 6)), 3)
        self.assertEqual(g00_to_image(v0).getpixel((0, 0)), (6, 4, 2))

    def test_oversize_rejected_before_decompression_or_canvas(self):
        huge = container(32768, 32768, [(0, 0, b"x"), (0, 0, b"y")])
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "oversize.g00"
            path.write_bytes(huge)
            view = FileView(str(path))
            try:
                with patch.object(reallive, "lz_decompress", side_effect=AssertionError("decompressed oversized canvas")):
                    self.assertIsNone(reallive._open_container(view))
            finally:
                view.close()
        with patch("unpacker.images_g00.lz_decompress", side_effect=AssertionError("oversized canvas")):
            self.assertIsNone(g00_to_image(huge))
            self.assertIsNone(reallive._g00_at(huge, 0))
            for kind in (0, 1):
                data = struct.pack("<BHHII", kind, 10000, 10000, 8, 100000002)
                self.assertIsNone(parse_meta(data))
                self.assertIsNone(reallive._g00_at(data, 0))

    def test_bad_frame_does_not_hide_next_frame(self):
        cut = solid_tile(1, 1, (30, 20, 10, 255))[:-1]
        good = solid_tile(1, 1, (60, 50, 40, 255))
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / "damaged.g00"
            path.write_bytes(container(2, 2, [(0, 0, cut), (1, 1, good)]))
            with open_archive(str(path)) as arc:
                with self.assertRaises(ValueError):
                    arc.decode_frame(arc.entries[0])
                stats = extract(arc, arc.entries, ExtractOptions(str(root / "png")))
                self.assertEqual((stats.converted, stats.failed, stats.raw), (1, 1, 0))
                self.assertEqual([p.name for p in (root / "png").iterdir()], ["0002.png"])


if __name__ == "__main__":
    unittest.main()
