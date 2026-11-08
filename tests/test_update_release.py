"""版本选择、限流回退、校验与中断下载。"""
import hashlib
from http.client import IncompleteRead
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import updater


class Response(io.BytesIO):
    def __init__(self, data=b"", url="", headers=None):
        super().__init__(data)
        self.url = url
        self.headers = headers or {}

    def geturl(self):
        return self.url


def release_info(tag, data):
    version = tag.removeprefix("v")
    return {
        "tag_name": tag, "body": " 支持多帧图片。 ", "prerelease": False, "draft": False,
        "assets": [
            {"name": "source-code.zip"},
            {"name": updater._filename(version), "state": "uploaded", "size": len(data),
             "digest": "sha256:" + hashlib.sha256(data).hexdigest(),
             "browser_download_url": updater._asset_url(tag, version)},
        ]
    }


class UpdateReleaseTests(unittest.TestCase):
    def check(self, payload, current):
        with patch.object(updater, "_open", return_value=Response(json.dumps(payload).encode())):
            return updater.check_update(current)

    def test_numeric_order_older_equal_and_current(self):
        self.assertIsNone(self.check(release_info("v1.2", b"a"), "1.4"))
        self.assertIsNone(self.check(release_info("v1.4.0", b"a"), "1.4"))
        result = self.check(release_info("v1.10.1", b"a"), "1.9")
        self.assertEqual(result.version, "1.10.1")
        self.assertEqual(result.notes, "支持多帧图片。")
        self.assertEqual(result.page_url, updater._TAG_PREFIX + "v1.10.1")

    def test_invalid_metadata_is_not_trusted(self):
        for field, bad in [("digest", None), ("size", -1), ("state", "new"),
                           ("name", "other.zip"), ("browser_download_url", "https://evil.test/")]:
            value = release_info("v2", b"x")
            value["assets"][1][field] = bad
            with self.subTest(field=field), self.assertRaises(updater.UpdateError):
                self.check(value, "1.0")
        for flag in ["prerelease", "draft"]:
            value = release_info("v2", b"x")
            value[flag] = True
            with self.assertRaises(updater.UpdateError):
                self.check(value, "1.0")

    def test_limited_api_falls_back_to_matching_published_checksum(self):
        blob = b"download"
        digest = hashlib.sha256(blob).hexdigest()
        checksum = f"{digest}  CG-Unpacker-v1.5-Windows-x64.zip\r\n".encode()
        for value, valid in [(checksum, True), (checksum.replace(b"1.5", b"1.6"), False)]:
            limited = HTTPError(updater._API, 403, "rate limit", {}, None)
            op = unittest.mock.Mock()
            op.open.side_effect = [limited, Response(url=updater._TAG_PREFIX + "v1.5"),
                                   Response(value)]
            with patch.object(updater, "build_opener", return_value=op):
                if valid:
                    release = updater.check_update("1.4")
                    self.assertEqual(release.sha256, digest)
                    self.assertEqual(release.size, 0)
                    self.assertEqual(op.open.call_args.args[0].full_url, release.url + ".sha256")
                else:
                    with self.assertRaisesRegex(updater.UpdateError, "校验文件"):
                        updater.check_update("1.4")

    def test_limited_api_older_release_without_checksum(self):
        with patch.object(updater, "_open", side_effect=[
            updater._ApiLimited(), Response(url=updater._TAG_PREFIX + "v1.2")
        ]):
            self.assertIsNone(updater.check_update("1.4"))

    def test_redirects_keep_release_scope_and_https(self):
        download = updater._asset_url("v1.2", "1.2")
        for source, target in [(download, "https://github.com/x/y/file.zip"),
                               (download, "http://release-assets.githubusercontent.com/b"),
                               (download, "https://objects.githubusercontent.com@evil.test/"),
                               (updater.RELEASES_URL, "https://github.com/other/repo/releases/tag/v1.2")]:
            with self.assertRaises(updater.UpdateError):
                updater._ReleaseRedirect(source).redirect_request(Request(source), None, 302, "", {}, target)
        cdn = "https://release-assets.githubusercontent.com/signed?s=token"
        result = updater._ReleaseRedirect(download).redirect_request(Request(download), None, 302, "", {}, cdn)
        self.assertEqual(result.full_url, cdn)

    def test_download_verifies_and_replaces_only_on_success(self):
        payload = b"zip data"
        release = self.check(release_info("v1.5", payload), "1.4")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "release.zip"
            for data, length in [(b"bad data", "8"), (b"trunc", "8"), (payload + b"x", None),
                                 (payload, "123")]:
                path.write_bytes(b"old")
                response = Response(data, headers={"Content-Length": length} if length else {})
                with self.subTest(data=data, length=length), patch.object(updater, "_open", return_value=response):
                    with self.assertRaises(updater.UpdateError):
                        updater.download_update(release, path)
                self.assertEqual(path.read_bytes(), b"old")
                self.assertEqual(list(Path(tmp).iterdir()), [path])
            progress = []
            with patch.object(updater, "_open", return_value=Response(payload)):
                updater.download_update(release, path, lambda done, total: progress.append((done, total)))
            self.assertEqual(path.read_bytes(), payload)
            self.assertEqual(progress[-1], (len(payload), len(payload)))

    def test_interrupted_read_cleans_up(self):
        class Cut(Response):
            def read1(self, n):
                raise IncompleteRead(b"ab")

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "release.zip"
            release = self.check(release_info("v1.5", b"abc"), "1.4")
            with patch.object(updater, "_open", return_value=Cut()):
                with self.assertRaisesRegex(updater.UpdateError, "中断"):
                    updater.download_update(release, path)
            self.assertFalse(any(path.parent.iterdir()))


if __name__ == "__main__":
    unittest.main()
