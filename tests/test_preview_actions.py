"""GUI action regressions without a display server or a real output directory."""
import io
import os
from pathlib import Path
import queue
import sys
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from PIL import Image
import app
from unpacker.core import Entry
from unpacker.registry import open_archive_ex


class PreviewActionsTest(unittest.TestCase):
    def setUp(self):
        # Widgets are test doubles; actual selection, worker and queue handlers run.
        w = self.window = app.App.__new__(app.App)
        for name in ("tree", "canvas", "lbl_info", "btn_sel", "lbl_status", "progress",
                     "btn_cancel", "btn_dir", "ask_check", "var_ask", "var_only_img",
                     "var_png", "var_filter", "set_status", "after", "_save_config",
                     "withdraw", "destroy", "log_line", "_display_save_path", "_show_preview"):
            setattr(w, name, Mock())
        w.tree.index.side_effect = int
        w.tree.get_children.return_value = ()
        w.var_filter.get.return_value = ""
        w.busy = w._closing = False
        w.queue = queue.Queue()
        w.stop_event = threading.Event()
        w._preview_token = 0
        w._preview_archive = w._pending_preview = None
        w.entries = w.rows = [Entry(f"{i}.png", 0, 0) for i in range(30)]
        w.archive = Mock(entries=w.entries, path="old.zip")
        w.ctx = app.ExtractContext(w.archive)
        w._refresh_save_path = Mock()

    def pump_until(self, condition):
        end = time.monotonic() + 5
        while not condition():
            if time.monotonic() > end:
                self.fail("GUI worker did not finish")
            self.window._poll()
            time.sleep(.005)

    def select(self, index):
        self.window.tree.selection.return_value = (str(index),)
        self.window.on_select()

    def test_only_last_waiting_selection_runs_after_rapid_clicks(self):
        first, release = threading.Event(), threading.Event()
        calls = []
        def read(archive, entry, ctx):
            calls.append(entry.name)
            if len(calls) == 1:
                first.set()
                if not release.wait(4):
                    raise TimeoutError()
            return None, entry.name, "test"

        with patch.object(app, "read_entry", side_effect=read):
            self.select(0)
            self.assertTrue(first.wait(2))
            try:
                for i in range(1, 30):
                    self.select(i)
                self.assertEqual(calls, ["0.png"])
            finally:
                release.set()
            self.pump_until(lambda: self.window._preview_archive is None)
        self.assertEqual(calls, ["0.png", "29.png"])
        self.window._show_preview.assert_called_once_with(self.window.rows[29], "29.png", "test")

    def test_switch_and_close_keep_active_archive_until_worker_finishes(self):
        w = self.window
        old = w.archive
        new = Mock(entries=[Entry("new.png", 0, 0)], path="new.zip")
        started, release = threading.Event(), threading.Event()
        def read(archive, entry, ctx):
            self.assertIs(archive, old)
            self.assertIs(ctx.archive, old)
            started.set()
            if not release.wait(4):
                raise TimeoutError()
            return None, "old image", "test"

        with patch.object(app, "read_entry", side_effect=read), \
                patch.object(app, "open_archive_ex", return_value=(new, "")):
            self.select(0)
            self.assertTrue(started.wait(2))
            try:
                self.assertTrue(w.open_file("new.zip"))
                self.select(0)
                w._on_close()
                w._poll()
                old.close.assert_not_called()
                w.destroy.assert_not_called()
            finally:
                release.set()
            self.pump_until(lambda: w.destroy.called)
        old.close.assert_called_once_with()
        new.close.assert_called_once_with()
        w._show_preview.assert_not_called()

    def test_right_click_on_second_selected_row(self):
        w = self.window
        w.tree.identify_row.return_value = "1"
        w.tree.selection.return_value = ("0", "1")
        w._ask_target = Mock(return_value="output")
        w._start_extract = Mock()
        with patch.object(app.tk, "Menu") as menu:
            w._context_menu(SimpleNamespace(y=10, x_root=10, y_root=10))
        single, selected = menu.return_value.add_command.call_args_list[:2]
        single.kwargs["command"]()
        w._start_extract.assert_called_once_with([w.rows[1]], "这一张", "output")
        w._start_extract.reset_mock()
        selected.kwargs["command"]()
        w._start_extract.assert_called_once_with(w.rows[:2], "2 个文件", "output")
        w.tree.selection_set.assert_not_called()

    def test_close_during_png_save_finishes_file_before_exiting(self):
        w = self.window
        pix = Image.new("RGB", (4, 4), "red")
        source = io.BytesIO()
        pix.save(source, format="PNG")
        with tempfile.TemporaryDirectory() as folder:
            arc_path = Path(folder) / "src.zip"
            with zipfile.ZipFile(arc_path, "w") as out:
                for leaf in ["one.png", "two.png"]:
                    out.writestr(leaf, source.getvalue())
            w.archive, error = open_archive_ex(str(arc_path))
            self.assertIsNotNone(w.archive, error)
            self.addCleanup(w.archive.close)
            w.out_dir = os.path.join(folder, "output")
            w.var_only_img.get.return_value = w.var_png.get.return_value = True
            # Progressbar supports dict-style reads in the completion handler.
            w.progress = MagicMock()
            w.progress.__getitem__.return_value = 2
            saving, release = threading.Event(), threading.Event()
            save_png = Image.Image.save
            def slow_save(image, *args, **kwargs):
                saving.set()
                if not release.wait(4):
                    raise TimeoutError()
                return save_png(image, *args, **kwargs)
            with patch.object(Image.Image, "save", slow_save), patch.object(app.messagebox, "askyesno") as dialog:
                w._start_extract(w.archive.entries, "测试", w.out_dir)
                try:
                    self.assertTrue(saving.wait(3))
                    w._on_close()
                    w._poll()
                    w.destroy.assert_not_called()
                    self.assertTrue(w.stop_event.is_set())
                finally:
                    release.set()
                self.pump_until(lambda: w.destroy.called)
                dialog.assert_not_called()
            with Image.open(Path(w.out_dir, "one.png")) as saved:
                self.assertEqual(saved.getpixel((0, 0)), (255, 0, 0))
            self.assertFalse(Path(w.out_dir, "two.png").exists())


if __name__ == "__main__":
    unittest.main()
