"""目录拖放、扫描完成和从结果列表打开或解包的交互回归。"""
from pathlib import Path
import queue
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app
from scan_dialog import ScanDialog


class FolderWorkflowTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name, "游戏 目录")
        self.root.mkdir()
        self.window = w = app.App.__new__(app.App)
        w.busy = w._closing = False
        w.queue = queue.Queue()
        w.stop_event = threading.Event()
        for key in ("tk", "after", "btn_results", "btn_cancel", "btn_dir", "ask_check", "progress",
                    "set_status", "log_line", "_start_batch", "open_file", "_ask_target"):
            setattr(w, key, Mock())

    def finish_scan(self):
        end = time.monotonic() + 5
        while self.window.busy:
            self.assertLess(time.monotonic(), end)
            self.window._poll()
            time.sleep(.005)

    def test_directory_and_overlapping_file_drop_show_results_before_export(self):
        folder = self.root / "资源"
        folder.mkdir()
        path = folder / "包.unknown"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("sprite.png", b"test")
        (folder / "坏包.dat").write_bytes(b"junk")
        w = self.window
        w.tk.splitlist.return_value = (str(self.root), str(path))
        with patch.object(app, "ScanDialog") as dialog:
            self.assertEqual(w.on_drop(SimpleNamespace(data="Tcl path list")), "copy")
            w.after.call_args.args[1]()
            self.finish_scan()
            _, results, root = dialog.call_args.args
            self.assertEqual(root, str(self.root))
            self.assertEqual([(a.path, a.format, a.entry_count) for a in results],
                             [(str(path), "ZIP", 1)])
            w._ask_target.assert_not_called()
            w.open_file.assert_not_called()
            w._start_batch.assert_not_called()
            w.btn_results.set_enabled.assert_called_with(True)

    def test_empty_folder_from_picker_reports_zero(self):
        w = self.window
        with patch.object(app.filedialog, "askdirectory", return_value=str(self.root)), \
                patch.object(app.messagebox, "showinfo") as info:
            w.choose_folder()
            self.finish_scan()
        self.assertIn("0 个", info.call_args.args[1])
        w.btn_results.set_enabled.assert_called_with(False)
        w._ask_target.assert_not_called()

    def test_cancel_scan_never_opens_a_dialog_or_starts_export(self):
        w = self.window

        def scanning(_paths, *, should_stop, progress):
            self.assertTrue(w.stop_event.wait(2))
            return SimpleNamespace(archives=[], scanned=1, unreadable=0, cancelled=should_stop())

        with patch.object(app, "scan_archives", side_effect=scanning), \
                patch.object(app, "ScanDialog") as dialog, patch.object(app.messagebox, "showinfo") as info:
            w._handle_dropped([str(self.root)])
            w.cancel()
            self.finish_scan()
            dialog.assert_not_called()
            info.assert_not_called()
        self.assertIn("已取消", w.set_status.call_args.args[0])
        w._start_batch.assert_not_called()

    def test_result_actions_use_selected_paths_and_respect_cancelled_destination(self):
        dialog = ScanDialog.__new__(ScanDialog)
        dialog.app = w = self.window
        dialog.archives = [SimpleNamespace(path=str(self.root / n)) for n in ("a.zip", "b.zip", "c.zip")]
        dialog.tree = Mock()
        dialog.close = Mock()
        dialog.tree.selection.return_value = ("1",)
        dialog.open_selected()
        w.open_file.assert_called_once_with(str(self.root / "b.zip"), parent=dialog)
        dialog.close.assert_called_once()
        dialog.close.reset_mock()
        dialog.tree.selection.return_value = ("0", "2")
        w._ask_target.return_value = None
        dialog.extract_selected()
        dialog.close.assert_not_called()
        w._start_batch.assert_not_called()
        w._ask_target.return_value = "output"
        dialog.extract_selected()
        all_paths = [a.path for a in dialog.archives]
        w._start_batch.assert_called_once_with([all_paths[0], all_paths[2]], "output", all_scan_paths=all_paths)
        dialog.extract_all()
        w._start_batch.assert_called_with(all_paths, "output", all_scan_paths=all_paths)


if __name__ == "__main__":
    unittest.main()
