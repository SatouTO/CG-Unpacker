import os
from pathlib import Path
import queue
import sys
from tempfile import TemporaryDirectory
import threading
import time
import unittest
from unittest.mock import Mock, patch
from zipfile import ZipFile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app
from unpacker.extract import read_entry


class BatchFolders(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.out = self.root / "导出"
        self.window = app.App.__new__(app.App)
        w = self.window
        for name in ("progress", "var_only_img", "var_png", "after", "btn_cancel",
                     "btn_dir", "ask_check", "_display_save_path", "log_line", "set_status"):
            setattr(w, name, Mock())
        w.var_only_img.get.return_value = w.var_png.get.return_value = False
        w.stop_event = threading.Event()
        w.busy = w._closing = False
        w.queue = queue.Queue()

    def make_zip(self, relative, items):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        with ZipFile(path, "w") as archive:
            for name, data in items:
                archive.writestr(name, data)
        return os.fspath(path)

    def run_batch(self, files):
        with patch.object(app.messagebox, "showinfo") as dialog:
            self.window._start_batch(files, str(self.out))
            deadline = time.monotonic() + 5
            while self.window.busy and time.monotonic() < deadline:
                self.window._poll()
                time.sleep(.005)
            self.assertFalse(self.window.busy, "批量解包线程没有完成")
            dialog.assert_called_once()
        return dialog.call_args.args[1]

    def test_same_names_in_subfolders_keep_separate_contents_and_real_suffixes(self):
        files = [self.make_zip(rel, [("image.bmp", rel.encode())]) for rel in
                 ("A/data.zip", "B/DATA.dat", "C/data (2).zip")]
        summary = self.run_batch(list(reversed(files)))
        expected = {"data": b"A/data.zip", "DATA (3)": b"B/DATA.dat",
                    "data (2)": b"C/data (2).zip"}
        self.assertEqual({p.parent.name: p.read_bytes() for p in self.out.rglob("image.bmp")},
                         expected)
        self.assertIn("成功 3 个，失败 0 个", summary)
        self.assertIn("原样导出 3 个文件", summary)

    def test_partial_entry_failure_and_unrecognized_package_count_as_failed(self):
        partial = self.make_zip("partial.zip", [("art/image.png", b"blocked"), ("ok.txt", b"ok")])
        invalid = self.root / "broken.zip"
        invalid.write_bytes(b"invalid index")
        directory = self.out / "partial"
        directory.mkdir(parents=True)
        (directory / "art").write_bytes(b"file blocks directory")
        summary = self.run_batch([partial, str(invalid)])
        self.assertIn("成功 0 个，失败 2 个", summary)
        self.assertIn("原样导出 1 个文件", summary)
        self.assertEqual((directory / "ok.txt").read_bytes(), b"ok")

    def test_cancel_in_second_package_keeps_completed_files_and_partial_progress(self):
        files = [self.make_zip("A.zip", [("done.txt", b"a")]),
                 self.make_zip("B.zip", [("first.txt", b"b"), ("never.txt", b"c")]),
                 self.make_zip("C.zip", [("never.txt", b"d")])]

        def cancel_during_read(archive, entry, ctx, **kwargs):
            if entry.name == "first.txt":
                self.window.stop_event.set()
            return read_entry(archive, entry, ctx, **kwargs)

        with patch("unpacker.extract.read_entry", side_effect=cancel_during_read):
            summary = self.run_batch(files)
        self.assertIn("已取消", summary)
        self.assertIn("成功 1 个，失败 0 个，未完成 2 个", summary)
        self.assertIn("原样导出 2 个文件", summary)
        self.window.progress.configure.assert_called_with(value=1)
        self.assertFalse((self.out / "B" / "never.txt").exists())
        self.assertFalse((self.out / "C").exists())


if __name__ == "__main__":
    unittest.main()
