"""归档条目隔离、输出边界、并发命名及写入失败回归测试。"""
import io
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from zipfile import ZipFile

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from unpacker.extract import ExtractOptions, extract_file, read_entry


def link_directory(target, link):
    if os.name == "nt":
        import _winapi
        _winapi.CreateJunction(str(target), str(link))
    else:
        link.symlink_to(target, target_is_directory=True)


class OutputPaths(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.out = self.folder / "输出"
        self.out.mkdir()

    def zipfile(self, name, items):
        path = self.folder / name
        with ZipFile(path, "w") as archive:
            for key, data in items:
                archive.writestr(key, data)
        return path

    def export(self, path, **options):
        opts = dict(out_dir=str(self.out), only_images=False, convert_png=False)
        opts.update(options)
        return extract_file(str(path), ExtractOptions(**opts))

    def test_directory_conflict_affects_only_one_entry(self):
        (self.out / "art").write_bytes(b"original")
        src = self.zipfile("cg.zip", [("art/1.png", b"skipped"), ("ok.png", b"good")])
        stats = self.export(src)
        self.assertEqual((stats.failed, stats.raw), (1, 1))
        self.assertIn("art/1.png", stats.errors[0])
        self.assertEqual((self.out / "ok.png").read_bytes(), b"good")
        self.assertEqual((self.out / "art").read_bytes(), b"original")

    def test_junction_cannot_escape_or_make_outside_subdirectories(self):
        outside = self.folder / "outside"
        outside.mkdir()
        link_directory(outside, self.out / "art")
        src = self.zipfile("cg.zip", [("art/deeper/1.png", b"bad"), ("ok.png", b"good")])
        stats = self.export(src)
        self.assertEqual((stats.failed, stats.raw), (1, 1))
        self.assertEqual(list(outside.iterdir()), [])
        with self.assertRaises(ValueError):
            self.export(src, base_dir=str(self.out), out_dir=str(self.out / "art" / "batch"))
        self.assertEqual(list(outside.iterdir()), [])

    def test_leaf_junction_is_rejected_before_overwrite(self):
        outside = self.folder / "elsewhere"
        outside.mkdir()
        link_directory(outside, self.out / "art.png")
        src = self.zipfile("cg.zip", [("art.png", b"bad"), ("ok.png", b"good")])
        stats = self.export(src, overwrite=True)
        self.assertEqual((stats.failed, stats.raw), (1, 1))
        self.assertIn("路径超出保存目录", stats.errors[0])
        self.assertEqual(list(outside.iterdir()), [])

    def test_rechecks_parent_after_read(self):
        outside = self.folder / "elsewhere"
        outside.mkdir()
        art = self.out / "art"
        src = self.zipfile("cg.zip", [("art/sprite.png", b"bad"), ("ok.png", b"good")])

        def swap_then_read(archive, entry, ctx, **options):
            if entry.name == "art/sprite.png":
                art.rename(self.out / "before")
                link_directory(outside, art)
            return read_entry(archive, entry, ctx, **options)

        with patch("unpacker.extract.read_entry", side_effect=swap_then_read):
            stats = self.export(src)
        self.assertEqual((stats.failed, stats.raw), (1, 1))
        self.assertIn("路径超出保存目录", stats.errors[0])
        self.assertEqual(list(outside.iterdir()), [])

    def test_overwrite_never_follows_file_link(self):
        protected = self.folder / "untouched"
        protected.write_bytes(b"safe")
        link = self.out / "art.png"
        try:
            link.symlink_to(protected)
        except OSError as exc:
            self.skipTest(f"file symlink unavailable: {exc}")
        src = self.zipfile("cg.zip", [("art.png", b"bad"), ("ok.png", b"good")])
        stats = self.export(src, overwrite=True)
        self.assertEqual((stats.failed, stats.raw), (1, 1))
        self.assertTrue(link.is_symlink())
        self.assertEqual(protected.read_bytes(), b"safe")

    def test_overwrite_does_not_modify_another_hard_link(self):
        protected = self.folder / "untouched"
        protected.write_bytes(b"safe")
        os.link(protected, self.out / "art.png")
        src = self.zipfile("cg.zip", [("art.png", b"new")])
        self.assertEqual(self.export(src, overwrite=True).raw, 1)
        self.assertEqual(protected.read_bytes(), b"safe")
        self.assertEqual((self.out / "art.png").read_bytes(), b"new")

    def test_flatten_overwrite_and_devices(self):
        (self.out / "x.txt").write_bytes(b"original")
        src = self.zipfile("cg.zip", [("a/x.txt", b"first"), ("b/x.txt", b"second"),
                                     ("dir/Nul.png", b"image")])
        stats = self.export(src, flatten=True, overwrite=True)
        self.assertEqual((stats.raw, stats.failed), (3, 0))
        self.assertEqual((self.out / "x.txt").read_bytes(), b"first")
        self.assertEqual((self.out / "x (1).txt").read_bytes(), b"second")
        self.assertEqual((self.out / "_Nul.png").read_bytes(), b"image")

    def test_concurrent_publish_never_overwrites_other_files(self):
        expected = {bytes([i]) for i in range(12)}
        sources = [self.zipfile(f"source{i}.zip", [("art.png", value)])
                   for i, value in enumerate(expected)]
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(self.export, sources))
        self.assertTrue(all(s.raw == 1 and s.failed == 0 for s in results))
        self.assertEqual({p.read_bytes() for p in self.out.iterdir()}, expected)

    def test_failed_png_leaves_existing_file_intact(self):
        png = io.BytesIO()
        Image.new("RGB", (2, 2)).save(png, "PNG")
        (self.out / "a.png").write_bytes(b"original")
        src = self.zipfile("cg.zip", [("a.png", png.getvalue()), ("ok.txt", b"good")])

        def fail_after_write(image, stream, format):
            stream.write(b"incomplete")
            raise OSError("simulated write failure")

        with patch.object(Image.Image, "save", fail_after_write):
            stats = self.export(src, convert_png=True, overwrite=True)
        self.assertEqual((stats.failed, stats.raw), (1, 1))
        self.assertEqual((self.out / "a.png").read_bytes(), b"original")
        self.assertEqual({p.name for p in self.out.iterdir()}, {"a.png", "ok.txt"})


if __name__ == "__main__":
    unittest.main()
