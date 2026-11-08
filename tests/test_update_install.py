import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import update_install as updates


@unittest.skipUnless(os.name == "nt", "Windows portable installer")
class PortableUpdateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.work = Path(self.tmp.name)
        self.root = self.work / "中文 安装位置"
        self.root.mkdir()
        self.exe = self.root / "图片解包器.exe"
        self.exe.write_bytes(b"MZ-old")
        self.lib = self.root / "_internal"
        self.lib.mkdir()
        (self.lib / "old.dll").write_bytes(b"old")
        self.settings = self.root / "config.json"
        self.settings.write_text("my-config")
        self.output = self.root / "output" / "picked.png"
        self.output.parent.mkdir()
        self.output.write_bytes(b"my-work")
        self.bundle = self.work / "update.zip"
        self.build()

    def build(self, extra=None, exe=b"MZ-new"):
        with zipfile.ZipFile(self.bundle, "w") as z:
            for path, data in [
                ("CG Unpacker.exe", exe), ("使用说明.txt", b"help"),
                ("_internal/base_library.zip", b"lib"), ("_internal/python312.dll", b"py"),
                ("licenses/Pillow.txt", b"notice"), ("config.json", b"unwanted"),
                ("output/picked.png", b"unwanted"),
            ]:
                z.writestr("CG Unpacker/" + path, data)
            if extra:
                z.writestr(extra, b"outside")

    def prepare(self):
        return updates.prepare_update(self.bundle, self.root, self.exe.name)

    def run_helper(self, job, restart):
        # Run the exact Windows filesystem transaction. Replace only the GUI launch.
        driver = self.work / "run-helper.ps1"
        driver.write_text(
            "param([string]$stage)\n. (Join-Path $stage 'install.ps1')\n"
            + "function Start-NewVersion($path,$root) { " + restart + " }\n"
            + "$job = Get-Content -LiteralPath (Join-Path $stage 'job.json') -Raw -Encoding UTF8 | ConvertFrom-Json\n"
            + "Install-Update $stage $job\n",
            encoding="utf-8-sig")
        return subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy",
                               "Bypass", "-File", str(driver), str(job.staging_dir)],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30,
                              creationflags=subprocess.CREATE_NO_WINDOW)

    def test_update_preserves_user_data_and_installed_filename(self):
        job = self.prepare()
        self.bundle.unlink()  # The verified payload no longer depends on the downloaded ZIP.
        result = self.run_helper(job, "if (!(Test-Path -LiteralPath $path)) { throw 'absent' }")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.exe.read_bytes(), b"MZ-new")
        self.assertFalse((self.root / "CG Unpacker.exe").exists())
        self.assertFalse((self.lib / "old.dll").exists())
        self.assertEqual((self.lib / "python312.dll").read_bytes(), b"py")
        self.assertEqual((self.root / "licenses/Pillow.txt").read_bytes(), b"notice")
        self.assertEqual(self.settings.read_text(), "my-config")
        self.assertEqual(self.output.read_bytes(), b"my-work")
        updates.discard_update(job)
        self.assertFalse(job.staging_dir.exists())

    def test_start_failure_rolls_back_every_managed_item(self):
        (self.root / "使用说明.txt").write_bytes(b"old-help")
        job = self.prepare()
        result = self.run_helper(job, "throw 'candidate failed to start'")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"candidate failed to start", result.stderr)
        self.assertEqual(self.exe.read_bytes(), b"MZ-old")
        self.assertEqual((self.lib / "old.dll").read_bytes(), b"old")
        self.assertFalse((self.lib / "python312.dll").exists())
        self.assertFalse((self.root / "licenses").exists())
        self.assertEqual((self.root / "使用说明.txt").read_bytes(), b"old-help")
        self.assertEqual(self.output.read_bytes(), b"my-work")

    def test_locked_file_restores_a_real_executable(self):
        # Windows does not allow a move of this open documentation file. The
        # application's old entry point has already moved when this happens.
        system = Path(os.environ["SystemRoot"], "System32")
        original = (system / "where.exe").read_bytes()
        candidate = (system / "whoami.exe").read_bytes()
        self.exe.write_bytes(original)
        self.build(exe=candidate)
        job = self.prepare()
        note = self.root / "使用说明.txt"
        note.write_bytes(b"old-help")
        with note.open("rb"):
            result = self.run_helper(job, "throw 'unreachable-restart'")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn(b"unreachable-restart", result.stderr)
        self.assertEqual(self.exe.read_bytes(), original)
        self.assertEqual((self.lib / "old.dll").read_bytes(), b"old")
        self.assertEqual(note.read_bytes(), b"old-help")
        self.assertEqual(self.settings.read_text(), "my-config")

    def test_stale_job_does_not_replace_a_changed_program(self):
        job = self.prepare()
        self.exe.write_bytes(b"MZ-changed")
        result = self.run_helper(job, "throw 'unexpected restart'")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.exe.read_bytes(), b"MZ-changed")
        self.assertEqual((self.lib / "old.dll").read_bytes(), b"old")

    def test_restart_checks_the_launched_process_window(self):
        job = self.prepare()
        # Exercise the PowerShell health check with deterministic process states;
        # never activate another user's real window while running the test suite.
        script = self.work / "window-check.ps1"
        script.write_text(r"""
param($stage)
. (Join-Path $stage 'install.ps1')
function Start-Process($FilePath, $WorkingDirectory, [switch]$PassThru) { return $script:probe }
function Check($title, $handle, $exited, $success, $kill) {
    $script:probe = [pscustomobject]@{ MainWindowTitle=$title; MainWindowHandle=[IntPtr]$handle;
        HasExited=$exited; ExitCode=0; Killed=$false; Waited=$false; Disposed=$false }
    $probe | Add-Member ScriptMethod Refresh {}
    $probe | Add-Member ScriptMethod Kill { $this.Killed=$true; $this.HasExited=$true }
    $probe | Add-Member ScriptMethod WaitForExit { param($timeout) $this.Waited=$true; return $true }
    $probe | Add-Member ScriptMethod Dispose { $this.Disposed=$true }
    $ready = $true
    try { Start-NewVersion 'test.exe' $stage 150 } catch { $ready = $false }
    if ($ready -ne $success -or $probe.Killed -ne $kill -or $probe.Waited -ne $kill -or
        -not $probe.Disposed) { throw "Incorrect launch result for $title" }
}
Check 'CG Unpacker v1.4' 33 $false $true $false
Check '游戏图片解包器 v1.4.1' 22 $false $true $false
Check 'CG Unpacker v1.4' 0 $false $false $true
Check 'Unhandled exception in script' 11 $false $false $true
Check 'Unexpected dialog' 11 $false $false $true
Check 'CG Unpacker v1.4' 33 $true $false $false
""", encoding="utf-8-sig")
        result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy",
                                 "Bypass", "-File", str(script), str(job.staging_dir)],
                                capture_output=True, timeout=20, creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_traversal_device_stream_and_case_collisions(self):
        for extra in ("CG Unpacker/../overwritten.txt", "CG Unpacker/_internal/../bad.dll",
                      "CG Unpacker/_internal/CON.txt", "CG Unpacker/_internal/file:stream",
                      "CG Unpacker/_internal/PYTHON312.DLL", "/outside/hit", "D:/hit"):
            with self.subTest(extra=extra):
                self.build(extra)
                with self.assertRaises(ValueError):
                    self.prepare()
                self.assertEqual(list(self.root.glob(".cg-update-*")), [])
        self.assertFalse((self.root / "overwritten.txt").exists())

    def test_archive_link_and_expanded_size_are_rejected(self):
        with patch.object(updates, "_MAX_EXPANDED", 2):
            with self.assertRaises(ValueError):
                self.prepare()
        with zipfile.ZipFile(self.bundle, "a") as z:
            info = zipfile.ZipInfo("CG Unpacker/_internal/linked")
            info.external_attr = 0o120777 << 16
            z.writestr(info, "../../../other")
        with self.assertRaises(ValueError):
            self.prepare()

    def test_existing_junction_stops_before_switching(self):
        outside = self.work / "elsewhere"
        outside.mkdir()
        (outside / "keep").write_bytes(b"keep")
        result = subprocess.run(
            ["powershell.exe","-NoProfile","-NonInteractive","-Command",
             "$ErrorActionPreference='Stop'; New-Item -ItemType Junction -Path $env:CG_TEST_LINK "
             "-Target $env:CG_TEST_DEST | Out-Null"],
            env={**os.environ, "CG_TEST_LINK":str(self.lib / "dir"), "CG_TEST_DEST":str(outside)},
            capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertEqual(result.returncode, 0, result.stderr)
        job = self.prepare()
        result = self.run_helper(job, "throw 'unexpected restart'")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.exe.read_bytes(), b"MZ-old")
        self.assertEqual((outside / "keep").read_bytes(), b"keep")
        self.assertEqual((self.lib / "dir" / "keep").read_bytes(), b"keep")


if __name__ == "__main__":
    unittest.main()
