"""真正发 HTTP 请求的那一层。

只用标准库 ``urllib``，把上游响应用 :class:`UpstreamResponse` 包一层，
统一处理三件事：

* 请求头拼装（认证方式、额外头、UA）；
* 超时与连接异常 —— 连不上统一抛 :class:`UpstreamUnreachable`，
  好让路由层去切换下一个候选上游；
* 流式读取 —— :meth:`UpstreamResponse.iter_chunks` 边读边吐，不整段缓冲。
"""

import json
import socket
import ssl
import urllib.error
import urllib.request

DEFAULT_TIMEOUT = 300
CHUNK_SIZE = 4096
USER_AGENT = "alias-gateway/1.0"


class UpstreamResponse:
    """对 ``urlopen`` 返回对象的一层薄封装，兼容非 2xx 与流式两种形态。"""

    def __init__(self, status, headers, raw, url):
        self.status = int(status)
        self.headers = headers or {}
        self.raw = raw
        self.url = url
        self._closed = False

    @property
    def is_event_stream(self):
        """响应是不是 SSE，决定网关走流式还是整体透传。"""
        ctype = (self.content_type or "").lower()
        return "text/event-stream" in ctype

    @property
    def content_type(self):
        for key, value in self.headers.items():
            if key.lower() == "content-type":
                return value
        return ""

    def header(self, name, default=None):
        """按名字取响应头，大小写不敏感。"""
        for key, value in self.headers.items():
            if key.lower() == name.lower():
                return value
        return default

    def read_all(self, limit=None):
        """读完整段响应体；读超时或连接中断时返回已经拿到的部分。"""
        if self.raw is None:
            return b""
        chunks = []
        total = 0
        while True:
            try:
                chunk = self.raw.read(CHUNK_SIZE)
            except (socket.timeout, ssl.SSLError, OSError):
                break
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if limit is not None and total >= limit:
                break
        return b"".join(chunks)

    def iter_chunks(self, chunk_size=CHUNK_SIZE):
        """逐块吐出响应体，给 SSE 转发用。"""
        if self.raw is None:
            return
        while True:
            try:
                chunk = self.raw.read(chunk_size)
            except (socket.timeout, ssl.SSLError, OSError):
                return
            if not chunk:
                return
            yield chunk

    def json(self):
        """把响应体解析成 JSON；解析不了就返回 None 而不是抛异常。"""
        data = self.read_all()
        if not data:
            return None
        try:
            return json.loads(data.decode("utf-8", "replace"))
        except (ValueError, UnicodeDecodeError):
            return None

    def close(self):
        """幂等关闭，避免 finally 里重复调用出问题。"""
        if self._closed:
            return
        self._closed = True
        if self.raw is not None:
            try:
                self.raw.close()
            except Exception:
                pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False


def build_headers(upstream, extra=None):
    """拼请求头：先铺基础头，再按 auth.type 加认证，最后让额外头覆盖。"""
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "User-Agent": USER_AGENT,
    }

    auth_obj = upstream.get("auth") or {}
    auth_type = (auth_obj.get("type") or "bearer").lower()
    key = auth_obj.get("key") or ""

    if auth_type == "bearer" and key:
        headers["Authorization"] = "Bearer %s" % key
    elif auth_type == "header" and key:
        header_name = auth_obj.get("header") or "Authorization"
        headers[header_name] = key

    for name, value in (upstream.get("extra_headers") or {}).items():
        headers[str(name)] = str(value)

    if extra:
        for name, value in extra.items():
            if value is None:
                continue
            headers[str(name)] = str(value)

    return headers


def join_url(base_url, path):
    """拼接 base_url 与 path，容忍两侧斜杠的写法差异。"""
    base = str(base_url or "").rstrip("/")
    if not path:
        return base
    if not path.startswith("/"):
        path = "/" + path
    return base + path


def request(upstream, method, path, body=None, extra_headers=None, stream=True):
    """向上游发一次请求。

    上游返回 4xx/5xx 不抛异常 —— 原样包成 :class:`UpstreamResponse` 让上层决定
    怎么处理（通常是透传给客户端）。只有连不上才抛 :class:`UpstreamUnreachable`。
    """
    url = join_url(upstream.get("base_url"), path)
    headers = build_headers(upstream, extra_headers)

    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")

    req = urllib.request.Request(url, data=data, headers=headers, method=method.upper())

    timeout = upstream.get("timeout") or DEFAULT_TIMEOUT
    try:
        timeout = float(timeout)
    except (TypeError, ValueError):
        timeout = float(DEFAULT_TIMEOUT)

    try:
        raw = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as exc:
        return UpstreamResponse(exc.code, dict(exc.headers or {}), exc, url)
    except (urllib.error.URLError, socket.timeout, ssl.SSLError, OSError) as exc:
        raise UpstreamUnreachable(str(_describe(exc)), url)

    return UpstreamResponse(raw.status, dict(raw.headers or {}), raw, url)


class UpstreamUnreachable(OSError):
    """上游连不上。路由层收到它就切下一个候选上游。"""

    def __init__(self, message, url=""):
        super().__init__(message)
        self.url = url


def _describe(exc):
    """把底层异常压成一句可读的话。"""
    reason = getattr(exc, "reason", None)
    if reason is not None:
        return "%s: %s" % (type(exc).__name__, reason)
    return str(exc) or type(exc).__name__


def probe(upstream, path="/models"):
    """探测上游连通性，返回 ``(ok, status, latency_ms, message)``。

    管理界面的"上游测试"用它；读一点点就断开，只关心握手和状态码。
    """
    import time

    started = time.time()
    try:
        with request(upstream, "GET", path, body=None, stream=True) as resp:
            try:
                resp.raw.read(256)
            except Exception:
                pass
            latency = int((time.time() - started) * 1000)
            ok = 200 <= resp.status < 300
            message = "OK" if ok else "HTTP %d" % resp.status
            return ok, resp.status, latency, message
    except UpstreamUnreachable as exc:
        latency = int((time.time() - started) * 1000)
        return False, None, latency, str(exc)
    except Exception as exc:
        latency = int((time.time() - started) * 1000)
        return False, None, latency, "%s: %s" % (type(exc).__name__, exc)


def fetch_models(upstream):
    """拉上游的模型清单，返回 ``(ok, ids, message)``。

    兼容两种响应形态：标准 OpenAI 的 ``{"data": [...]}``，
    以及部分服务返回的 ``{"models": [...]}``。单个条目既接受对象也接受纯字符串。
    """
    try:
        with request(upstream, "GET", "/models", body=None, stream=True) as resp:
            if not (200 <= resp.status < 300):
                return False, [], "HTTP %d" % resp.status
            payload = resp.json()
    except UpstreamUnreachable as exc:
        return False, [], str(exc)
    except Exception as exc:
        return False, [], "%s: %s" % (type(exc).__name__, exc)

    if not isinstance(payload, dict):
        return False, [], "响应不是 JSON 对象"

    data = payload.get("data")
    if data is None and isinstance(payload.get("models"), list):
        data = payload["models"]
    if not isinstance(data, list):
        return False, [], "响应里没有 data 数组"

    ids = []
    for item in data:
        if isinstance(item, dict):
            model_id = item.get("id") or item.get("name")
        elif isinstance(item, str):
            model_id = item
        else:
            model_id = None
        if model_id:
            ids.append(str(model_id))
    return True, ids, "OK"