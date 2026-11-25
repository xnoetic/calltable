"""hub HTTP 客户端（标准库 urllib）。CLI 与执行器共用。"""

from __future__ import annotations

import http.client
import json
import os
import shutil
import time
import uuid
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


class HubError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, payload=None):
        super().__init__(message)
        self.status = status
        self.payload = payload


class HubClient:
    def __init__(self, base_url: str, token: str, timeout: float = 60,
                 proxy: str | None = None):
        self.base = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        #: 只给 hub 流量用的 HTTP 代理（Cloud Run 里是 Tailscale 用户态
        #: tailscaled 的 outbound proxy）。不设全局环境变量，任务自己的外网
        #: 流量不受影响。
        self.proxy = (proxy or "").strip() or None
        if self.proxy:
            self._opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": self.proxy,
                                             "https": self.proxy}))
        else:
            self._opener = None

    def _open(self, req, timeout):
        if self._opener is not None:
            return self._opener.open(req, timeout=timeout)
        return urllib.request.urlopen(req, timeout=timeout)

    # ------------------------------------------------------------ 基础
    def _url(self, path: str, params: dict | None = None) -> str:
        url = self.base + path
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        return url

    def request(self, method: str, path: str, *, data: bytes | None = None,
                json_body=None, headers: dict | None = None,
                params: dict | None = None, timeout: float | None = None,
                raw: bool = False):
        body = data
        hdrs = {"Authorization": f"Bearer {self.token}"}
        if json_body is not None:
            body = json.dumps(json_body).encode("utf-8")
            hdrs["Content-Type"] = "application/json"
        if headers:
            hdrs.update(headers)
        req = urllib.request.Request(self._url(path, params), data=body,
                                     method=method, headers=hdrs)
        try:
            with self._open(req, timeout or self.timeout) as resp:
                payload = resp.read()
                if raw:
                    return payload, dict(resp.headers)
                if not payload:
                    return None, dict(resp.headers)
                if resp.headers.get("Content-Type", "").startswith("application/json"):
                    return json.loads(payload.decode("utf-8")), dict(resp.headers)
                return payload, dict(resp.headers)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:500]
            payload = None
            try:
                payload = json.loads(detail)
            except ValueError:
                pass
            raise HubError(f"{method} {path} -> HTTP {exc.code}: {detail}",
                           status=exc.code, payload=payload) from exc
        except urllib.error.URLError as exc:
            raise HubError(f"{method} {path} -> {exc.reason}") from exc

    def get_json(self, path: str, params: dict | None = None, **kw):
        return self.request("GET", path, params=params, **kw)[0]

    def post_json(self, path: str, body, **kw):
        return self.request("POST", path, json_body=body, **kw)[0]

    # ------------------------------------------------------------ 文件
    def upload_file(self, path: str, file_path: str | Path,
                    headers: dict | None = None, block: int = 1024 * 1024,
                    on_progress=None):
        """流式上传（I7）：用 http.client 的 send(fileobj) 分块发送，不进内存。"""
        size = os.path.getsize(file_path)
        parsed = urllib.parse.urlparse(self.base + path)
        timeout = max(self.timeout, 600)
        target = parsed.path or "/"
        if self.proxy:
            # 与 _open 一样走 hub 专用代理（Cloud Run 里只有它能到 hub）：
            # http 发绝对 URI 给代理转发；https 先 CONNECT 建隧道。
            proxy = urllib.parse.urlparse(
                self.proxy if "://" in self.proxy else "http://" + self.proxy)
            if parsed.scheme == "https":
                conn = http.client.HTTPSConnection(proxy.hostname,
                                                   proxy.port or 80,
                                                   timeout=timeout)
                conn.set_tunnel(parsed.hostname, parsed.port or 443)
            else:
                conn = http.client.HTTPConnection(proxy.hostname,
                                                  proxy.port or 80,
                                                  timeout=timeout)
                target = urllib.parse.urlunparse(
                    (parsed.scheme or "http", parsed.netloc, target, "", "", ""))
        else:
            conn = http.client.HTTPConnection(parsed.hostname,
                                              parsed.port or 80,
                                              timeout=timeout)
        try:
            conn.putrequest("POST", target)
            conn.putheader("Authorization", f"Bearer {self.token}")
            conn.putheader("Content-Type", "application/octet-stream")
            conn.putheader("Content-Length", str(size))
            for key, value in (headers or {}).items():
                conn.putheader(key, str(value))
            conn.endheaders()
            sent = 0
            started = time.time()
            with open(file_path, "rb") as handle:
                while True:
                    chunk = handle.read(block)
                    if not chunk:
                        break
                    conn.send(chunk)
                    sent += len(chunk)
                    if on_progress is not None:
                        on_progress(sent, size, time.time() - started)
            response = conn.getresponse()
            payload = response.read()
            if response.status >= 400:
                raise HubError(f"POST {path} -> HTTP {response.status}: "
                               f"{payload[:500]!r}", status=response.status)
            return json.loads(payload) if payload else None
        finally:
            conn.close()

    def upload_bundle(self, project: str, file_path: str | Path,
                      block: int = 4 * 1024 * 1024, on_progress=None) -> dict:
        """分块上传 bundle；断线后从服务端已有的字节数续传。"""
        size = os.path.getsize(file_path)
        upload_id = uuid.uuid4().hex
        sent = 0
        started = time.time()
        with open(file_path, "rb") as handle:
            while sent < size:
                handle.seek(sent)
                chunk = handle.read(block)
                if not chunk:
                    break
                for attempt in range(6):
                    try:
                        payload, _ = self.request(
                            "POST", f"/api/projects/{project}/bundle/part",
                            data=chunk,
                            params={"upload_id": upload_id, "offset": sent},
                            headers={"Content-Type": "application/octet-stream"},
                            timeout=max(self.timeout, 300))
                        sent = int((payload or {}).get("received", sent + len(chunk)))
                        break
                    except HubError as exc:
                        if exc.status == 409 and exc.payload:
                            sent = int(exc.payload.get("received", sent))
                            break
                        if attempt == 5:
                            raise
                        time.sleep(min(2 ** attempt, 15))
                if on_progress is not None:
                    on_progress(sent, size, time.time() - started)
        result = self.post_json(
            f"/api/projects/{project}/bundle/finalize",
            {"upload_id": upload_id})
        return result

    #: 下载失败（连接被重置 / early EOF / 超时）时的最大尝试次数。
    DOWNLOAD_ATTEMPTS = 5

    def download(self, path: str, dest: str | Path,
                 params: dict | None = None, *, attempts: int | None = None,
                 resume: bool = True) -> Path:
        """流式下载，支持断点续传与重试。

        本地文件已存在时用 ``Range: bytes=<size>-`` 续传：服务端返回 206 就追加，
        返回 200（服务端不支持 Range）就截断重来。连接中断（early EOF、
        ConnectionReset、超时）会退避重试，最多 ``DOWNLOAD_ATTEMPTS`` 次。
        """
        url = self._url(path, params)
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        attempts = max(1, int(attempts or self.DOWNLOAD_ATTEMPTS))
        last_error: Exception | None = None
        for attempt in range(attempts):
            offset = dest.stat().st_size if (resume and dest.exists()) else 0
            headers = {"Authorization": f"Bearer {self.token}"}
            if offset:
                headers["Range"] = f"bytes={offset}-"
            req = urllib.request.Request(url, headers=headers)
            try:
                with self._open(req, max(self.timeout, 600)) as resp:
                    status = getattr(resp, "status", 200)
                    if offset and status == 200:
                        offset = 0  # 服务端忽略了 Range：从头写
                    expected = int(resp.headers.get("Content-Length") or 0)
                    with dest.open("ab" if offset else "wb") as handle:
                        shutil.copyfileobj(resp, handle, 1024 * 1024)
                written = dest.stat().st_size if dest.exists() else 0
                if expected and written < offset + expected:
                    raise HubError(
                        f"GET {path} -> short read ({written} < "
                        f"{offset + expected}); retrying")
                return dest
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", "replace")[:500]
                error = HubError(f"GET {path} -> HTTP {exc.code}: {detail}",
                                 status=exc.code)
                if exc.code == 416:
                    # 本地残留比服务端文件还长：丢掉，从头重下。
                    try:
                        dest.unlink()
                    except OSError:
                        pass
                    last_error = error
                elif exc.code in (403, 404, 410):
                    raise error from exc
                else:
                    last_error = error
            except (urllib.error.URLError, OSError, HubError) as exc:
                last_error = exc if isinstance(exc, HubError) else HubError(
                    f"GET {path} -> {getattr(exc, 'reason', exc)}")
            if attempt + 1 < attempts:
                time.sleep(min(2 ** attempt, 15))
        raise last_error or HubError(f"GET {path} -> download failed")

