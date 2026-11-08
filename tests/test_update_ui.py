"""Update UI state transitions; no real downloads or installer processes."""
from contextlib import ExitStack
from pathlib import Path
import queue
import sys
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app


class UpdateActionsTest(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.network = Mock()
        self.installer = Mock()
        self.stack.enter_context(patch.dict(sys.modules, {"updater": self.network,
                                                           "update_install": self.installer}))
        self.stack.enter_context(patch.object(sys, "frozen", True, create=True))
        self.stack.enter_context(patch.object(sys, "platform", "win32"))
        self.ask = self.stack.enter_context(patch.object(app.messagebox, "askyesno", return_value=False))
        self.info = self.stack.enter_context(patch.object(app.messagebox, "showinfo"))
        self.stack.enter_context(patch.object(app.tk, "Toplevel"))
        for widget in ("Label", "Frame", "Progressbar", "Button"):
            self.stack.enter_context(patch.object(app.ttk, widget))
        w = self.w = app.App.__new__(app.App)
        w._closing = w.busy = False
        w.sf = 1.0
        w.queue = queue.Queue()
        for attr in ("btn_update", "_on_close", "_open_releases", "log_line", "progress"):
            setattr(w, attr, Mock())
        self.release = SimpleNamespace(version="1.5", notes="修复两处预览问题")

    def dispatch(self):
        self.w._handle_update(self.w.queue.get(timeout=3))

    def test_single_background_check_and_latest(self):
        self.network.check_update.return_value = None
        self.w.check_for_updates()
        self.w.check_for_updates()
        self.dispatch()
        self.network.check_update.assert_called_once_with(app.APP_VER)
        self.assertIn("暂无可安装", self.info.call_args.args[1])
        self.assertIsNone(self.w._update_cancel)

    def test_cancel_the_offer_keeps_current_installation(self):
        self.network.check_update.return_value = self.release
        self.w.check_for_updates()
        self.dispatch()
        prompt = self.ask.call_args.args[1]
        for text in (app.APP_VER, self.release.version, self.release.notes):
            self.assertIn(text, prompt)
        self.network.download_update.assert_not_called()
        self.w._on_close.assert_not_called()

    def test_busy_or_closed_never_installs(self):
        self.w.busy = True
        self.w.check_for_updates()
        self.network.check_update.assert_not_called()
        self.w.busy = False
        self.w._closing = True
        self.w._update_cancel = threading.Event()
        staged = object()
        self.w._handle_update(("update_ready", staged))
        self.installer.discard_update.assert_called_once_with(staged)
        self.installer.launch_update.assert_not_called()

    def test_error_offers_release_page_and_retry(self):
        self.network.check_update.side_effect = OSError("连接超时")
        self.ask.return_value = True
        self.w.check_for_updates()
        self.dispatch()
        self.assertIn("连接超时", self.ask.call_args.args[1])
        self.w._open_releases.assert_called_once()
        self.assertIsNone(self.w._update_cancel)

    def test_download_then_launch_before_graceful_shutdown(self):
        self.network.check_update.return_value = self.release
        self.ask.return_value = True
        staged = object()
        archive_paths = []

        def download(release, path, progress):
            self.assertIs(release, self.release)
            self.assertIsNot(threading.current_thread(), threading.main_thread())
            archive_paths.append(path)
            path.write_bytes(b"example data")
            progress(5, 10)
            progress(10, 10)

        def prepare(path, install_dir, exe_name):
            self.assertEqual(path.read_bytes(), b"example data")
            self.assertEqual(Path(install_dir) / exe_name, Path(sys.executable))
            return staged

        self.network.download_update.side_effect = download
        self.installer.prepare_update.side_effect = prepare
        self.w.check_for_updates()
        self.dispatch()
        for _ in range(4):
            self.dispatch()
        self.assertEqual(len(archive_paths), 1)
        self.assertFalse(archive_paths[0].exists())
        self.w.progress.configure.assert_not_called()
        self.installer.launch_update.assert_called_once_with(staged)
        self.w._on_close.assert_called_once()

    def test_unknown_length_shows_received_bytes_until_download_verification(self):
        self.network.check_update.return_value = self.release
        self.ask.return_value = True
        def download(_release, path, progress):
            path.write_bytes(b"data")
            progress(2 << 20, 0)
        self.network.download_update.side_effect = download
        self.w.check_for_updates()
        self.dispatch()  # offer accepted
        self.dispatch()  # data received, no Content-Length
        self.w._update_progress.start.assert_called_once()
        self.w._update_label.configure.assert_called_with(text="正在下载更新，已接收 2.0 MiB")
        self.dispatch()
        self.w._update_label.configure.assert_called_with(text="正在准备安装…")
        self.dispatch()
        self.w._on_close.assert_called_once()

    def test_releases_opens_complete_history(self):
        self.network.RELEASES_URL = "https://github.com/SatouTO/CG-Unpacker/releases/latest"
        with patch.object(app.os, "startfile", create=True) as browser:
            app.App._open_releases(self.w)
        browser.assert_called_once_with("https://github.com/SatouTO/CG-Unpacker/releases")

    def test_cancel_while_downloading_cleans_up_without_launch(self):
        downloading = threading.Event()
        resume = threading.Event()
        self.network.check_update.return_value = self.release
        self.ask.return_value = True
        paths = []

        def slow_download(release, path, progress):
            paths.append(path)
            path.write_bytes(b"partial")
            downloading.set()
            if not resume.wait(3):
                raise AssertionError("cancel did not arrive")
            progress(1, 10)

        self.network.download_update.side_effect = slow_download
        self.w.check_for_updates()
        self.dispatch()
        try:
            self.assertTrue(downloading.wait(2))
            app.ttk.Button.call_args.kwargs["command"]()
        finally:
            resume.set()
        self.dispatch()
        self.assertFalse(paths[0].exists())
        self.installer.prepare_update.assert_not_called()
        self.installer.launch_update.assert_not_called()
        self.w._on_close.assert_not_called()
        self.assertIsNone(self.w._update_cancel)

    def test_launch_failure_keeps_existing_window_open(self):
        self.w._update_cancel = threading.Event()
        self.installer.launch_update.side_effect = OSError("无法准备重启")
        staged = object()
        self.w._handle_update(("update_ready", staged))
        self.installer.discard_update.assert_called_once_with(staged)
        self.w._on_close.assert_not_called()
        self.assertIn("无法准备重启", self.ask.call_args.args[1])


if __name__ == "__main__":
    unittest.main()
