import os
from pathlib import Path
import struct
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from unpacker.core import FileView
from unpacker.discovery import scan_archives
from unpacker.formats import reallive
from test_g00_frames import literals
from test_xp3 import build_xp3


def single_g00():
    return struct.pack("<BHH", 0, 1, 1) + literals(b"ABC", 3)


class FolderScanTests(unittest.TestCase):
    def test_recursive_content_probe_overlapping_roots_and_closed_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            nested = root / "素材 子目录"
            nested.mkdir()
            archive = nested / "改名的包"
            with zipfile.ZipFile(archive, "w") as bundle:
                bundle.writestr("first.txt", b"1")
                bundle.writestr("second.txt", b"2")
            exe = root / "Game.exe"
            exe.write_bytes(b"MZ" + bytes(256) + build_xp3(b"image", "a.png"))
            (root / "junk.xp3").write_bytes(b"broken")
            (root / "settings.ini").touch()
            result = scan_archives([nested, root, archive, root / "missing"])
            self.assertEqual((result.scanned, result.unreadable, result.cancelled), (4, 1, False))
            self.assertEqual([(info.path, info.format, info.entry_count) for info in result.archives],
                             [(str(exe), "XP3", 1), (str(archive), "ZIP", 2)])
            archive.unlink()
            exe.unlink()

    def test_dat_signatures_and_chunk_boundary(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            g00 = single_g00()
            (root / "frames.dat").write_bytes(bytes(reallive._SCAN_CHUNK - 1) + g00 + g00)
            with zipfile.ZipFile(root / "packed.dat", "w") as archive:
                archive.writestr("frame.g00", g00)
            # 画布、长度都合理，但正文一开始就回引不存在的像素。
            (root / "junk.dat").write_bytes(struct.pack("<BHHII", 0, 1, 1, 11, 3) + bytes(3))
            result = scan_archives([root])
            self.assertEqual([(Path(a.path).name, a.format, a.entry_count) for a in result.archives],
                             [("frames.dat", "G00", 2), ("packed.dat", "ZIP", 1)])

    def test_cancel_inside_large_dat(self):
        stop = threading.Event()
        read_bytes = FileView.bytes
        reads = []

        def cancel_after_chunk(view, offset, size):
            reads.append(size)
            if size >= reallive._SCAN_CHUNK:
                stop.set()
            return read_bytes(view, offset, size)

        with tempfile.TemporaryDirectory() as tmp:
            dat = Path(tmp, "many.dat")
            dat.write_bytes(bytes(4 * reallive._SCAN_CHUNK) + single_g00())
            with patch.object(FileView, "bytes", cancel_after_chunk):
                result = scan_archives([dat], should_stop=stop.is_set)
            self.assertTrue(result.cancelled)
            self.assertEqual(result.archives, [])
            self.assertLessEqual(max(reads), reallive._SCAN_CHUNK + 4)
            dat.unlink()

    def test_does_not_follow_directory_link(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp, "game")
            outside = Path(tmp, "other")
            root.mkdir()
            outside.mkdir()
            (outside / "g00.dat").write_bytes(single_g00())
            try:
                if os.name == "nt":
                    import _winapi
                    _winapi.CreateJunction(str(outside), str(root / "linked"))
                else:
                    os.symlink(outside, root / "linked", target_is_directory=True)
            except OSError as error:
                self.skipTest(str(error))
            self.assertEqual(scan_archives([root]).scanned, 0)


if __name__ == "__main__":
    unittest.main()
