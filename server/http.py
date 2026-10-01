"""HTTP 层：路由分发、请求体读取、响应写出。

基于标准库 ``ThreadingHTTPServer``，每个连接一个线程。分三块：

* :class:`RequestHandler` —— 把路径分发到 :class:`~server.handlers.Gateway` 的
  对应方法，并负责普通响应与 chunked 流式响应两种写出方式；
* :class:`GatewayServer` —— 持有一个 Gateway 实例，供 handler 取用；
* :func:`serve_forever` / :func:`shutdown` —— 起停控制，配合
  ``app.py`` 的信号处理做优雅退出。

管理接口的路径表集中在 :data:`PANEL_GET_ROUTES` / :data:`PANEL_POST_ROUTES`，
加接口时改表即可，不用改分发逻辑。
"""

import json
import socket
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from core import logs
from core.errors import GatewayError, NotFoundError, PanelAuthError
from server.handlers import Gateway, Response, error_response

TOKEN_HEADER = "X-Panel-Token"
MAX_BODY_BYTES = 32 * 1024 * 1024
SERVER_TAG = "alias-gateway"

PANEL_GET_ROUTES = {
    "/panel/status": "panel_status",
    "/panel/config": "panel_config_get",
    "/panel/config/export": "panel_config_export",
    "/panel/logs": "panel_logs",
    "/panel/overview": "panel_overview",
    "/panel/models": "panel_models",
}

PANEL_POST_ROUTES = {
    "/panel/login": "panel_login",
    "/panel/register": "panel_register",
    "/panel/logout": "panel_logout",
    "/panel/config": "panel_config_save",
    "/panel/password": "panel_password",
    "/panel/logs/clear": "panel_logs_clear",
    "/panel/upstreams/test": "panel_upstreams_test",
}

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Authorization, Content-Type, %s" % TOKEN_HEADER,
    "Access-Control-Max-Age": "86400",
}


class RequestHandler(BaseHTTPRequestHandler):
    """单个请求的处理者。

    走 HTTP/1.1，响应必须自己给 ``Content-Length`` 或 ``Transfer-Encoding``，
    否则客户端会一直等下去 —— 这是 :meth:`_write_response` 与
    :meth:`_write_stream` 分开写的原因。
    """

    protocol_version = "HTTP/1.1"
    server_version = SERVER_TAG
    sys_version = ""

    def log_message(self, fmt, *args):
        """默认实现往 stderr 打日志，这里改走统一日志。"""
        logs.get_logger("http").debug("%s - %s", self.address_string(), fmt % args)

    def log_error(self, fmt, *args):
        logs.get_logger("http").warning("%s - %s", self.address_string(), fmt % args)

    @property
    def gateway(self):
        return self.server.gateway

    def _token(self):
        return self.headers.get(TOKEN_HEADER) or ""

    def _query(self):
        return parse_qs(urlparse(self.path).query)

    def _read_body(self):
        """按 Content-Length 读请求体。

        超限时返回空 bytes，让后续的 JSON 解析给出明确的 400，
        而不是把内存读爆。
        """
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return b""
        if length > MAX_BODY_BYTES:
            return b""
        remaining = length
        chunks = []
        while remaining > 0:
            chunk = self.rfile.read(min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def _write_response(self, resp):
        """写一个整体响应（含 Content-Length）。"""
        if resp.stream is not None:
            self._write_stream(resp)
            return

        body = resp.body or b""
        self.send_response(resp.status)
        self.send_header("Content-Type", resp.content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for name, value in resp.headers.items():
            self.send_header(name, value)
        for name, value in CORS_HEADERS.items():
            self.send_header(name, value)
        self.end_headers()
        if body and self.command != "HEAD":
            self.wfile.write(body)

    def _write_stream(self, resp):
        """写一个 chunked 流式响应，边读边发不缓冲。

        客户端中途断开属于正常情况（用户点了停止），只记一行 info 就收工。
        """
        self.send_response(resp.status)
        self.send_header("Content-Type", resp.content_type)
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("Cache-Control", "no-store")
        for name, value in resp.headers.items():
            self.send_header(name, value)
        for name, value in CORS_HEADERS.items():
            self.send_header(name, value)
        self.end_headers()

        try:
            for chunk in resp.stream:
                if not chunk:
                    continue
                if isinstance(chunk, str):
                    chunk = chunk.encode("utf-8")
                self.wfile.write(b"%x\r\n" % len(chunk) + chunk + b"\r\n")
                self.wfile.flush()
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            logs.get_logger("http").info("client closed connection during stream")
            self.close_connection = True
        except OSError as exc:
            logs.get_logger("http").warning("stream write failed: %s", exc)
            self.close_connection = True

    def _fail(self, exc):
        """把异常渲染成错误响应；连响应都写不出去就算了。"""
        resp = error_response(exc)
        try:
            self._write_response(resp)
        except (BrokenPipeError, ConnectionResetError, OSError):
            self.close_connection = True

    def _dispatch(self, method):
        """统一入口：按 method + path 找到处理函数。

        未预期的异常一律记 traceback 后转 500，不让线程带着异常退出。
        """
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"

        try:
            if method == "OPTIONS":
                self._write_response(Response(204, b"", "text/plain"))
                return

            raw_body = self._read_body() if method in ("POST", "PUT", "PATCH") else b""

            if path in ("/v1/models", "/models") and method == "GET":
                resp = self.gateway.handle_models(_client_key_from(self.headers))
                self._write_response(resp)
                return

            if path in ("/v1/chat/completions", "/v1/responses") and method == "POST":
                resp = self.gateway.handle_chat(
                    _client_key_from(self.headers), path, raw_body
                )
                self._write_response(resp)
                return

            if path == "/" and method in ("GET", "HEAD"):
                self._write_response(Response.json({
                    "service": SERVER_TAG,
                    "api": "/v1/models, /v1/chat/completions, /v1/responses",
                    "admin": "/panel/*（管理界面请用终端：python3 tui.py）",
                    "health": "/health",
                }))
                return

            if path in PANEL_GET_ROUTES and method == "GET":
                handler = getattr(self.gateway, PANEL_GET_ROUTES[path])
                if path == "/panel/logs":
                    self._write_response(handler(self._token(), self._query()))
                elif path == "/panel/models":
                    self._write_response(handler(self._token(), _truthy(self._query(), "force")))
                else:
                    self._write_response(handler(self._token()))
                return

            if path in PANEL_POST_ROUTES and method == "POST":
                handler = getattr(self.gateway, PANEL_POST_ROUTES[path])
                name = PANEL_POST_ROUTES[path]
                if name in ("panel_login", "panel_register"):
                    self._write_response(handler(raw_body))
                elif name == "panel_logout":
                    self._write_response(handler(self._token()))
                elif name == "panel_upstreams_test":
                    self._write_response(handler(self._token(), raw_body))
                else:
                    self._write_response(handler(self._token(), raw_body))
                return

            if path == "/health":
                self._write_response(Response.json({"ok": True, "service": SERVER_TAG}))
                return

            raise NotFoundError("No route for %s %s" % (method, path))

        except GatewayError as exc:
            self._fail(exc)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except Exception as exc:
            logs.get_logger("http").error(
                "unhandled error on %s %s: %s\n%s",
                method, path, exc, traceback.format_exc(),
            )
            self._fail(exc)

    def do_GET(self):
        self._dispatch("GET")

    def do_HEAD(self):
        self._dispatch("HEAD")

    def do_POST(self):
        self._dispatch("POST")

    def do_PUT(self):
        self._dispatch("PUT")

    def do_PATCH(self):
        self._dispatch("PATCH")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def do_OPTIONS(self):
        self._dispatch("OPTIONS")


def _truthy(query, name):
    """把 ``?force=1`` 这类查询参数解析成布尔值。"""
    values = query.get(name)
    if not values:
        return False
    value = str(values[0]).strip().lower()
    return value not in ("", "0", "false", "no", "off")


def _client_key_from(headers):
    from core.router import extract_client_key

    return extract_client_key(headers.get("Authorization"))


class GatewayServer(ThreadingHTTPServer):
    """带 Gateway 引用的 HTTP 服务，供 handler 通过 ``self.server`` 取到。"""

    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 64

    def __init__(self, address, gateway):
        self.gateway = gateway
        super().__init__(address, RequestHandler)

    def handle_error(self, request, client_address):
        """客户端断开不算错误，其余记日志。"""
        exc = sys.exc_info()[1]
        if isinstance(exc, (BrokenPipeError, ConnectionResetError)):
            return
        logs.get_logger("http").error(
            "error handling request from %s: %s", client_address, exc
        )


def build_server(gateway, host, port):
    return GatewayServer((host, port), gateway)


def serve_forever(server, shutdown_event=None):
    """在后台线程里跑 serve_forever，主线程轮询退出事件。

    ``app.py`` 收到 SIGTERM/SIGINT 时会 set 这个事件，这里就顺势收工。
    """
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.5})
    thread.daemon = True
    thread.start()

    logger = logs.get_logger("system")
    host, port = server.server_address[:2]
    logger.info("listening on http://%s:%d", host, port)

    try:
        while shutdown_event is None or not shutdown_event.is_set():
            time.sleep(0.4)
    except KeyboardInterrupt:
        logger.info("keyboard interrupt, shutting down")
    finally:
        shutdown(server)
    return thread


def shutdown(server):
    """停服务并释放端口，两个步骤都容忍异常（可能已经关过了）。"""
    logger = logs.get_logger("system")
    try:
        server.shutdown()
    except Exception:
        pass
    try:
        server.server_close()
    except Exception:
        pass
    logger.info("server stopped")