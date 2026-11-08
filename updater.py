"""CG Unpacker 的公开 GitHub Releases 版本检查和下载。"""
from dataclasses import dataclass
import hashlib
from http.client import HTTPException
import json
import os
from pathlib import Path
import re
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import build_opener, HTTPRedirectHandler, Request

_REPO = "https://github.com/SatouTO/CG-Unpacker"
RELEASES_URL = _REPO + "/releases/latest"
_API = "https://api.github.com/repos/SatouTO/CG-Unpacker/releases/latest"
_TAG_PREFIX = _REPO + "/releases/tag/"
_VERSION = re.compile(r"v?([0-9]{1,9}(?:\.[0-9]{1,9}){0,2})")
_MAX_ZIP = 512 * 1024 * 1024
_TIMEOUT = 20
_CDN = {"release-assets.githubusercontent.com", "objects.githubusercontent.com",
        "github-production-release-asset-2e65be.s3.amazonaws.com"}


class UpdateError(Exception):
    """可直接呈现的更新失败原因。"""


class _ApiLimited(UpdateError):
    pass


@dataclass(frozen=True)
class Release:
    version: str
    notes: str
    url: str
    size: int
    sha256: str

    @property
    def page_url(self):
        tag = self.url.rsplit("/", 2)[-2]
        return _TAG_PREFIX + tag


def _version(tag):
    match = _VERSION.fullmatch(tag) if isinstance(tag, str) else None
    if not match:
        raise UpdateError("无法识别版本号。请前往 GitHub 发布页查看。")
    return match[1]


def _number(version):
    numbers = tuple(int(part) for part in version.split("."))
    return numbers + (0,) * (3 - len(numbers))


def _filename(version):
    return f"CG-Unpacker-v{version}-Windows-x64.zip"


def _asset_url(tag, version):
    return f"{_REPO}/releases/download/{tag}/{_filename(version)}"


class _ReleaseRedirect(HTTPRedirectHandler):
    max_redirections = 5

    def __init__(self, source):
        self.source = source

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if self.source == RELEASES_URL:
            safe = newurl.startswith(_TAG_PREFIX) and _VERSION.fullmatch(newurl[len(_TAG_PREFIX):])
        elif self.source == _API:
            safe = False
        else:
            target = urlsplit(newurl)
            safe = target.scheme == "https" and target.netloc.lower() in _CDN
        if not safe:
            raise UpdateError("GitHub 返回了意外的重定向地址，已停止更新。")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open(url, accept):
    req = Request(url, headers={"User-Agent": "CG-Unpacker-Updater",
                                "Accept": accept, "Accept-Encoding": "identity"})
    try:
        return build_opener(_ReleaseRedirect(url)).open(req, timeout=_TIMEOUT)
    except HTTPError as error:
        error.close()
        if url == _API and error.code in (403, 429):
            raise _ApiLimited("GitHub API 暂时限制了请求。") from error
        if error.code == 404:
            raise UpdateError("GitHub 上未找到对应的发布版本或更新文件。") from error
        raise UpdateError(f"GitHub 请求失败（HTTP {error.code}），请稍后再试。") from error
    except (HTTPException, OSError) as error:
        raise UpdateError("未能连接 GitHub，请检查网络或代理后重试。") from error


def _small_response(url, accept, limit):
    try:
        with _open(url, accept) as response:
            data = response.read(limit + 1)
    except (HTTPException, OSError) as error:
        raise UpdateError("未能读完 GitHub 的版本信息，请稍后重试。") from error
    if len(data) > limit:
        raise UpdateError("GitHub 返回的版本信息超过大小限制。")
    return data


def _from_api(current):
    raw = _small_response(_API, "application/vnd.github+json", 1024 * 1024)
    try:
        data = json.loads(raw)
    except (UnicodeError, ValueError) as error:
        raise UpdateError("GitHub 返回的版本信息无法解析。") from error
    if not isinstance(data, dict) or data.get("draft") is not False or data.get("prerelease") is not False:
        raise UpdateError("未取得 GitHub 上的正式发布版本。")
    tag = data.get("tag_name")
    version = _version(tag)
    if _number(version) <= current:
        return None
    name = _filename(version)
    url = _asset_url(tag, version)
    assets = data.get("assets")
    if not isinstance(assets, list):
        raise UpdateError("GitHub 返回的发布附件列表无效。")
    for asset in assets:
        if not isinstance(asset, dict) or asset.get("name") != name:
            continue
        if asset.get("state") != "uploaded" or asset.get("browser_download_url") != url:
            raise UpdateError("GitHub 发布包的状态或地址不正确。")
        size = asset.get("size")
        if type(size) is not int or not 0 < size <= _MAX_ZIP:
            raise UpdateError("GitHub 发布包大小无效或超过 512 MiB。")
        digest = asset.get("digest")
        if not isinstance(digest, str) or not re.fullmatch(r"sha256:[a-fA-F0-9]{64}", digest):
            raise UpdateError("GitHub 未提供有效的 SHA-256 校验值，无法自动安装此版本。")
        notes = data.get("body")
        return Release(version, notes.strip() if isinstance(notes, str) else "",
                       url, size, digest[7:].lower())
    raise UpdateError(f"v{version} 尚未提供 Windows x64 更新包。")


def _from_release_page(current):
    # 共用出口可能触发匿名 API 限流。此时仅使用同一仓库正式发布的校验文件。
    with _open(RELEASES_URL, "text/html") as response:
        latest = response.geturl()
    if not latest.startswith(_TAG_PREFIX):
        raise UpdateError("未取得 GitHub 上的最新正式版本。")
    tag = latest[len(_TAG_PREFIX):]
    version = _version(tag)
    if _number(version) <= current:
        return None
    url = _asset_url(tag, version)
    checksum = _small_response(url + ".sha256", "application/octet-stream", 512)
    name = _filename(version).encode("ascii")
    match = re.fullmatch(rb"([a-fA-F0-9]{64}) [ *]" + re.escape(name) + rb"(?:\r?\n)?", checksum)
    if not match:
        raise UpdateError("GitHub 发布的 SHA-256 校验文件无效，无法自动安装此版本。")
    return Release(version, "更新说明请查看此版本的 GitHub 发布页。", url, 0,
                   match[1].decode("ascii").lower())


def check_update(current_version: str) -> Release | None:
    """查找比当前版本更新的正式 Windows x64 发布包。"""
    current = _number(_version(current_version))
    try:
        return _from_api(current)
    except _ApiLimited:
        return _from_release_page(current)


def download_update(release: Release, dest_zip, progress=None):
    """验证完整 ZIP 后才放到 dest_zip；progress(done, total) 在下载线程中调用。"""
    version = _version(release.version)
    if (release.url not in (_asset_url(version, version), _asset_url("v" + version, version))
            or not re.fullmatch(r"[0-9a-f]{64}", release.sha256)
            or not 0 <= release.size <= _MAX_ZIP):
        raise UpdateError("更新包的地址、大小或校验值无效。")
    dest = Path(dest_zip)
    temp = None
    try:
        with _open(release.url, "application/octet-stream") as response:
            length = response.headers.get("Content-Length")
            total = release.size
            if length is not None:
                if not re.fullmatch(r"[0-9]{1,12}", length):
                    raise UpdateError("服务器返回的更新包大小无效。")
                total = int(length)
                if not 0 < total <= _MAX_ZIP or (release.size and total != release.size):
                    raise UpdateError("更新包大小与发布信息不一致。")
            digest = hashlib.sha256()
            count = 0
            reported = 0
            next_progress = 0
            deadline = time.monotonic() + 600
            with tempfile.NamedTemporaryFile(dir=dest.parent, suffix=".part", delete=False) as output:
                temp = Path(output.name)
                while True:
                    if time.monotonic() > deadline:
                        raise UpdateError("下载超过 10 分钟，请重试。")
                    chunk = response.read1(256 * 1024)
                    if not chunk:
                        break
                    count += len(chunk)
                    if count > (total or _MAX_ZIP):
                        raise UpdateError("更新包超出预期大小。")
                    digest.update(chunk)
                    output.write(chunk)
                    now = time.monotonic()
                    if progress and now >= next_progress:
                        progress(count, total)
                        reported = count
                        next_progress = now + 0.15
        if not count or (total and count != total):
            raise UpdateError("更新包下载不完整，请重试。")
        if digest.hexdigest() != release.sha256:
            raise UpdateError("更新包 SHA-256 校验失败，请重新下载。")
        os.replace(temp, dest)
        temp = None
        if progress and (count != reported or not total):
            progress(count, total or count)
    except (HTTPException, URLError, TimeoutError, ConnectionError) as error:
        raise UpdateError("更新包下载中断，请检查网络后重试。") from error
    except OSError as error:
        raise UpdateError(f"无法保存更新包：{error}") from error
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)
