import io
from pathlib import Path
import struct
import sys
import tempfile
import unittest
import warnings
import zipfile
import zlib

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from unpacker import ExtractOptions, extract, open_archive_ex
from unpacker.core import FileView, NotThisFormat
from unpacker.formats import xp3
from test_xp3 import build_xp3


class ArchiveBoundsTests(unittest.TestCase):
    def test_duplicate_zip_names_read_and_export_both_images(self):
        colors = [(255, 0, 0), (0, 0, 255)]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "sprites.zip")
            with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
                with warnings.catch_warnings():
                    warnings.filterwarnings("ignore", message="Duplicate name:")
                    for color in colors:
                        buf = io.BytesIO()
                        Image.new("RGB", (2, 2), color).save(buf, "PNG")
                        archive.writestr("图/sprite.png", buf.getvalue())
            archive, error = open_archive_ex(str(path))
            self.assertIsNotNone(archive, error)
            with archive:
                for entry, color in zip(archive.entries, colors, strict=True):
                    with Image.open(io.BytesIO(archive.read(entry))) as image:
                        self.assertEqual(image.getpixel((0, 0)), color)
                out = Path(tmp, "out")
                stats = extract(archive, archive.entries, ExtractOptions(str(out)))
                self.assertEqual((stats.converted, stats.failed), (2, 0), stats.errors)
                for filename, color in zip(("sprite.png", "sprite (1).png"), colors):
                    with Image.open(out / "图" / filename) as image:
                        self.assertEqual(image.getpixel((0, 0)), color)

    def check_bad_index(self, index_record):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "broken.xp3")
            path.write_bytes(xp3.MAGIC + struct.pack("<Q", 19) + index_record)
            view = FileView(str(path))
            try:
                with self.assertRaises(NotThisFormat):
                    xp3.detect(view)
            finally:
                view.close()

    def test_missing_segment_records(self):
        section = b"segm" + struct.pack("<Q", 12000 * 28)
        index = b"File" + struct.pack("<Q", len(section)) + section
        self.check_bad_index(b"\x00" + struct.pack("<Q", len(index)) + index)

    def test_truncated_or_expanding_index(self):
        self.check_bad_index(b"\x00" + struct.pack("<Q", 512))
        payload = zlib.compress(b"A" * 16384)
        for claimed in (5, xp3.MAX_INDEX_BYTES + 1):
            with self.subTest(claimed=claimed):
                self.check_bad_index(b"\x01" + struct.pack("<QQ", len(payload), claimed) + payload)

    def test_compressed_index(self):
        data = build_xp3(b"hello", "source.png")
        offset = struct.unpack_from("<Q", data, len(xp3.MAGIC))[0]
        index = data[offset + 9:]
        packed = zlib.compress(index)
        data = data[:offset] + b"\x01" + struct.pack("<QQ", len(packed), len(index)) + packed
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "valid.xp3")
            path.write_bytes(data)
            archive, error = open_archive_ex(str(path))
            self.assertIsNotNone(archive, error)
            with archive:
                self.assertEqual(archive.read(archive.entries[0]), b"hello")


if __name__ == "__main__":
    unittest.main()
