"""业务处理：三条对外 API + 一组管理接口。

对外 API 只有三条：

    GET  /v1/models               合并所有上游的模型清单并改名
    POST /v1/chat/completions     转发到上游，请求/响应双向改名
    POST /v1/responses            同上（新接口，路径不同）

管理接口都在 ``/panel/*`` 下，靠 ``X-Panel-Token`` 鉴权。

两条重要的设计取舍：

* **模型清单带 30 秒缓存** —— 每个请求都去拉一遍上游清单太贵，
  缓存过期或配置变更时失效（见 :class:`ModelCache`）。
* **清单拉不到时降级转发** —— 上游全挂时不做成员校验，按配置顺序直接试，
  免得清单拿不到就把整条链路拖死。
"""

import json
import threading
import time

from core import alias as alias_mod
from core import auth
from core import config as config_mod
from core import logs
from core import models as models_mod
from core import router as router_mod
from core import upstream as upstream_mod
from core.errors import (
    BadRequestError,
    GatewayError,
    NotFoundError,
    PanelAuthError,
    UpstreamError,
)
from . import sse

CHAT_PATHS = {
    "/v1/chat/completions": "/chat/completions",
    "/v1/responses": "/responses",
}

MODEL_CACHE_TTL = 30.0
MAX_BODY_BYTES = 32 * 1024 * 1024


class Response:
    """处理函数的返回值。

    ``stream`` 非空时走 chunked 流式写出，否则按 ``body`` 整体返回。
    """

    def __init__(self, status=200, body=b"", content_type="application/json; charset=utf-8",
                 headers=None, stream=None):
        self.status = int(status)
        self.body = body
        self.content_type = content_type
        self.headers = dict(headers or {})
        self.stream = stream

    @classmethod
    def json(cls, payload, status=200, headers=None):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        return cls(status, data, "application/json; charset=utf-8", headers)

    @classmethod
    def text(cls, text, status=200, content_type="text/plain; charset=utf-8"):
        return cls(status, str(text).encode("utf-8"), content_type)


class ModelCache:
    """按客户端 Key 缓存上游模型清单，默认 30 秒。

    缓存里同时存下拉取失败信息，这样"上游不可达"的提示也能复用，
    不会每个请求都重新探测一遍。
    """

    def __init__(self, ttl=MODEL_CACHE_TTL):
        self.ttl = float(ttl)
        self._data = {}
        self._lock = threading.Lock()

    def get(self, client_key):
        with self._lock:
            entry = self._data.get(client_key)
        if not entry:
            return None
        if time.time() - entry["at"] > self.ttl:
            return None
        return entry

    def put(self, client_key, per_upstream, errors):
        with self._lock:
            self._data[client_key] = {
                "at": time.time(),
                "per_upstream": per_upstream,
                "errors": errors,
            }

    def invalidate(self, client_key=None):
        """配置变更后整表失效；传 key 则只失效一条。"""
        with self._lock:
            if client_key is None:
                self._data.clear()
            else:
                self._data.pop(client_key, None)


class Gateway:
    """所有处理函数的宿主：持有配置、会话表、模型缓存与日志缓冲。"""

    def __init__(self, store, log_path=None, log_capacity=logs.RING_CAPACITY,
                 console=True):
        self.store = store
        self.sessions = auth.SessionStore()
        self.cache = ModelCache()
        self.started_at = time.time()
        self.ring = logs.setup(log_path=log_path, capacity=log_capacity, console=console)
        self.log = logs.get_logger("system")

    def _model_map(self, route, force=False):
        """取这条 route 的模型清单，优先用缓存。"""
        key = route.get("client_key")
        if not force:
            cached = self.cache.get(key)
            if cached:
                return cached["per_upstream"], cached["errors"]

        per_upstream, errors = models_mod.collect_models(route)
        self.cache.put(key, per_upstream, errors)
        return per_upstream, errors

    def _require_panel(self, token):
        if not self.sessions.validate(token):
            raise PanelAuthError()

    def handle_models(self, client_key):
        """``GET /v1/models``：合并所有上游的模型并按别名改名。

        始终强制刷新，因为客户端通常只在启动时拉一次，拿到旧清单的代价更大。
        """
        started = time.time()
        route = router_mod.match_route(self.store.routes(), client_key)
        key = route.get("client_key")

        per_upstream, errors = self._model_map(route, force=True)
        catalog = models_mod.build_catalog(route, per_upstream, errors, fetch=False)

        for name, message in (errors or {}).items():
            logs.get_logger("upstream").warning("%s unreachable: %s", name, message)

        elapsed = int((time.time() - started) * 1000)
        summary = models_mod.summarize(route, per_upstream)
        logs.get_logger("models").info(
            "key=%s | %s | %d | %dms", key, summary, 200, elapsed
        )

        payload = {
            "object": "list",
            "data": catalog.models,
        }
        if catalog.conflicts:
            payload["conflicts"] = catalog.conflicts
        return Response.json(payload)

    def handle_chat(self, client_key, path, raw_body):
        """``POST /v1/chat/completions`` 与 ``/v1/responses``。

        流程：鉴权 → 解析请求体 → 校验模型可用性 → 逐个候选上游尝试转发。
        上游返回非 2xx 时**原样透传**（含状态码），只有全部上游都连不上才回 502。
        """
        started = time.time()
        upstream_path = CHAT_PATHS.get(path)
        if upstream_path is None:
            raise NotFoundError()

        route = router_mod.match_route(self.store.routes(), client_key)
        key = route.get("client_key")

        body = self._parse_json(raw_body)
        client_model = body.get("model")
        if not client_model or not isinstance(client_model, str):
            raise BadRequestError("Request body must contain a non-empty string 'model'.")

        aliases = route.get("aliases") or {}
        raw_model = alias_mod.to_upstream(client_model, aliases)

        per_upstream, errors = self._model_map(route)
        have_map = bool(per_upstream)

        if have_map:
            router_mod.detect_request_conflict(route, client_model, per_upstream)
            catalog = models_mod.build_catalog(route, per_upstream, errors, fetch=False)
            if client_model not in set(catalog.ids):
                raise BadRequestError(
                    "The model '%s' does not exist or is not available on route '%s'."
                    % (client_model, key)
                )
            candidates = router_mod.candidate_upstreams(route, client_model, per_upstream)
        else:
            # 清单拉不到：不做成员校验，按配置顺序直接试，避免整条链路不可用
            candidates = router_mod.candidate_upstreams(
                route, client_model, None, require_membership=False
            )

        forward_body = dict(body)
        forward_body["model"] = raw_model

        stream_requested = bool(body.get("stream"))
        last_error = None

        for index, upstream in enumerate(candidates):
            up_name = upstream.get("name")
            try:
                resp = upstream_mod.request(
                    upstream, "POST", upstream_path, body=forward_body
                )
            except upstream_mod.UpstreamUnreachable as exc:
                last_error = "%s: %s" % (up_name, exc)
                logs.get_logger("upstream").error("%s unreachable: %s", up_name, exc)
                continue
            except Exception as exc:
                last_error = "%s: %s: %s" % (up_name, type(exc).__name__, exc)
                logs.get_logger("upstream").error("%s failed: %s", up_name, exc)
                continue

            if not (200 <= resp.status < 300):
                if resp.is_event_stream:
                    return self._stream_response(resp, aliases, key, client_model,
                                                 raw_model, up_name, started, stream_requested)
                return self._passthrough_response(resp, aliases, key, client_model,
                                                  raw_model, up_name, started)

            if resp.is_event_stream:
                return self._stream_response(resp, aliases, key, client_model,
                                             raw_model, up_name, started, stream_requested)

            return self._passthrough_response(resp, aliases, key, client_model,
                                              raw_model, up_name, started)

        elapsed = int((time.time() - started) * 1000)
        message = "All upstreams failed for model '%s' on route '%s'." % (client_model, key)
        if last_error:
            message += " Last error: %s" % last_error
        logs.get_logger("chat").error(
            "key=%s | %s → %s | all upstreams failed | 502 | %dms | %s",
            key, client_model, raw_model, elapsed, last_error or "-",
        )
        raise UpstreamError(message)

    def _stream_response(self, resp, aliases, key, client_model, raw_model,
                         up_name, started, stream_requested):
        """SSE 流式转发：边读边改边发。

        日志写在生成器的 finally 里，因为这时候才知道客户端是正常读完还是中途断开。
        """
        status = resp.status

        def generator():
            logger = logs.get_logger("chat")
            try:
                for chunk in sse.iter_stream(resp, aliases):
                    yield chunk
            except (BrokenPipeError, ConnectionResetError):
                logger.info("key=%s | client disconnected during stream", key)
            finally:
                resp.close()
                elapsed = int((time.time() - started) * 1000)
                logger.info(
                    "key=%s | %s → %s | upstream=%s | %d | %dms",
                    key, client_model, raw_model, up_name, status, elapsed,
                )

        headers = {
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        }
        return Response(
            status=status,
            content_type=resp.content_type or "text/event-stream; charset=utf-8",
            headers=headers,
            stream=generator(),
        )

    def _passthrough_response(self, resp, aliases, key, client_model, raw_model,
                              up_name, started):
        """非流式透传：整体读完，JSON 就改模型名，其余原样返回。"""
        status = resp.status
        content_type = resp.content_type or "application/json; charset=utf-8"
        data = resp.read_all()
        resp.close()

        elapsed = int((time.time() - started) * 1000)
        logs.get_logger("chat").info(
            "key=%s | %s → %s | upstream=%s | %d | %dms",
            key, client_model, raw_model, up_name, status, elapsed,
        )

        if "application/json" in content_type.lower() and data:
            try:
                payload = json.loads(data.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                payload = None
            if payload is not None:
                sse.rewrite_json_body(payload, aliases)
                data = json.dumps(payload, ensure_ascii=False).encode("utf-8")

        return Response(status, data, content_type)

    @staticmethod
    def _parse_json(raw_body):
        """解析并校验请求体，任何问题都抛 400。"""
        if not raw_body:
            raise BadRequestError("Request body is empty; expected a JSON object.")
        if len(raw_body) > MAX_BODY_BYTES:
            raise BadRequestError("Request body too large.")
        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise BadRequestError("Invalid JSON in request body: %s" % exc)
        if not isinstance(payload, dict):
            raise BadRequestError("Request body must be a JSON object.")
        return payload

    def panel_login(self, raw_body):
        """管理端登录：校验密码后签发会话 token。"""
        payload = self._parse_json(raw_body)
        password = payload.get("password") or ""
        stored = self.store.password_hash()
        if not auth.is_registered(stored):
            raise PanelAuthError("尚未注册，请先设置管理密码")
        if not auth.verify_password(password, stored):
            logs.get_logger("auth").warning("panel login failed")
            raise PanelAuthError("密码不正确")
        token = self.sessions.create()
        logs.get_logger("auth").info("panel login ok")
        return Response.json({
            "ok": True,
            "token": token,
            "expires_in": self.sessions.ttl,
            "panel_registered": True,
        })

    def panel_register(self, raw_body):
        """首次注册：设置管理密码并直接登录。已注册后返回 400。"""
        payload = self._parse_json(raw_body)
        password = payload.get("password") or ""
        ok, message = self.store.register_password(password)
        if not ok:
            logs.get_logger("auth").warning("panel register rejected: %s", message)
            return Response.json({"ok": False, "message": message}, status=400)
        token = self.sessions.create()
        logs.get_logger("auth").info("panel registered")
        return Response.json({
            "ok": True,
            "token": token,
            "expires_in": self.sessions.ttl,
            "panel_registered": True,
            "message": message,
        })

    def panel_logout(self, token):
        self.sessions.revoke(token)
        return Response.json({"ok": True})

    def panel_status(self, token):
        """不需要登录就能查的状态：是否已注册、是否已登录、运行时长。"""
        ok = self.sessions.validate(token)
        return Response.json({
            "authenticated": ok,
            "panel_registered": auth.is_registered(self.store.password_hash()),
            "uptime": int(time.time() - self.started_at),
            "version": _version(),
        })

    def panel_config_get(self, token):
        """读打码后的配置副本（上游 Key 打码，客户端 Key 明文）。"""
        self._require_panel(token)
        return Response.json(self.store.masked())

    def panel_config_save(self, token, raw_body):
        """保存配置。打码或空白的上游 Key 会被回填成原值。"""
        self._require_panel(token)
        payload = self._parse_json(raw_body)
        ok, errors, warnings = self.store.apply_panel_update(payload)
        if not ok:
            return Response.json({"ok": False, "errors": errors}, status=400)
        self.cache.invalidate()
        logs.get_logger("config").info("panel saved config (%d routes)", len(self.store.routes()))
        for warning in warnings:
            logs.get_logger("config").warning(warning)
        return Response.json({"ok": True, "errors": [], "warnings": warnings})

    def panel_config_export(self, token):
        """导出明文配置（含真实 Key），用于备份迁移。"""
        self._require_panel(token)
        cfg = self.store.snapshot()
        data = json.dumps(cfg, ensure_ascii=False, indent=2).encode("utf-8")
        return Response(
            200, data, "application/json; charset=utf-8",
            {"Content-Disposition": 'attachment; filename="config.json"'},
        )

    def panel_models(self, token, force=False):
        """管理端的模型清单视图：逐条 route 给出模型、来源、冲突与不可达信息。

        没有任何 route 时返回空列表而不是报错 —— 空骨架是合法状态。
        """
        self._require_panel(token)
        out = []
        for route in self.store.routes():
            key = route.get("client_key")
            per_upstream, errors = self._model_map(route, force=force)
            catalog = models_mod.build_catalog(route, per_upstream, errors, fetch=False)
            out.append({
                "client_key": key,
                "upstreams": [
                    {"name": name, "count": len(ids or []), "models": list(ids or [])}
                    for name, ids in per_upstream.items()
                ],
                "errors": errors,
                "total_raw": sum(len(v or []) for v in per_upstream.values()),
                "total_merged": len(catalog.models),
                "models": catalog.models,
                "conflicts": catalog.conflicts,
                # 带上 prefer：管理界面要据此显示/修改"这个模型采用哪个上游的"
                "prefer": dict(route.get("prefer") or {}),
            })
        return Response.json({"ok": True, "routes": out})

    def panel_password(self, token, raw_body):
        """改管理密码；成功后踢掉所有会话，强制重新登录。"""
        self._require_panel(token)
        payload = self._parse_json(raw_body)
        current = payload.get("current") or ""
        new = payload.get("new") or ""
        ok, message = self.store.change_password(current, new)
        if not ok:
            return Response.json({"ok": False, "message": message}, status=400)
        self.sessions.revoke_all()
        logs.get_logger("auth").info("panel password changed")
        return Response.json({"ok": True, "message": message})

    def panel_logs(self, token, query):
        """读日志缓冲，支持按级别 / 模块 / 关键词过滤。"""
        self._require_panel(token)
        entries = self.ring.snapshot()

        level = (query.get("level") or [""])[0]
        module = (query.get("module") or [""])[0]
        keyword = (query.get("q") or [""])[0]
        try:
            limit = int((query.get("limit") or ["500"])[0])
        except ValueError:
            limit = 500
        limit = max(1, min(limit, 2000))

        if level:
            wanted = {v.strip().upper() for v in level.split(",") if v.strip()}
            entries = [e for e in entries if e.get("level") in wanted]
        if module:
            wanted = {v.strip().lower() for v in module.split(",") if v.strip()}
            entries = [e for e in entries if str(e.get("module", "")).lower() in wanted]
        if keyword:
            needle = keyword.lower()
            entries = [
                e for e in entries
                if needle in str(e.get("message", "")).lower()
                or needle in str(e.get("module", "")).lower()
            ]

        total = len(entries)
        entries = entries[-limit:]
        return Response.json({"ok": True, "total": total, "logs": entries})

    def panel_logs_clear(self, token):
        self._require_panel(token)
        self.ring.clear()
        return Response.json({"ok": True})

    def panel_upstreams_test(self, token, raw_body=None):
        """并发探测所有上游，每个上游一个线程。

        结果按原顺序回填，所以并发也不会打乱管理界面的显示顺序。
        """
        self._require_panel(token)
        upstreams = self.store.upstreams()

        results = [None] * len(upstreams)
        threads = []

        def worker(index, up):
            ok, status, latency, message = upstream_mod.probe(up)
            results[index] = {
                "route_key": up.get("route_key"),
                "name": up.get("name"),
                "base_url": up.get("base_url"),
                "auth_type": (up.get("auth") or {}).get("type"),
                "ok": ok,
                "status": status,
                "latency_ms": latency,
                "message": message,
                "checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }

        for index, up in enumerate(upstreams):
            thread = threading.Thread(target=worker, args=(index, up), daemon=True)
            thread.start()
            threads.append(thread)
        for thread in threads:
            thread.join(timeout=40)

        final = [r for r in results if r is not None]
        for item in final:
            if not item["ok"]:
                logs.get_logger("upstream").warning(
                    "%s unreachable: %s", item["name"], item["message"]
                )
        return Response.json({"ok": True, "results": final})

    def panel_overview(self, token):
        """总览页的数据源：计数、路径、监听地址等。"""
        self._require_panel(token)
        routes = self.store.routes()
        upstream_total = sum(len(r.get("upstreams") or []) for r in routes)
        alias_total = sum(len(r.get("aliases") or {}) for r in routes)
        prefer_total = sum(len(r.get("prefer") or {}) for r in routes)
        return Response.json({
            "ok": True,
            "uptime": int(time.time() - self.started_at),
            "version": _version(),
            "routes": len(routes),
            "upstreams": upstream_total,
            "aliases": alias_total,
            "prefers": prefer_total,
            "sessions": len(self.sessions),
            "config_path": self.store.path,
            "log_path": logs_path(),
            "panel_registered": auth.is_registered(self.store.password_hash()),
            "listen_host": self.store.config.get("listen_host"),
            "listen_port": self.store.config.get("listen_port"),
        })


def _version():
    try:
        from server import VERSION
        return VERSION
    except Exception:
        return "unknown"


def logs_path():
    """当前日志文件路径（没写文件时返回空串）。"""
    return getattr(logs, "_active_path", "") or ""


def error_response(exc):
    """把异常转成响应：网关自有异常用它的状态码，其余统一 500。"""
    if isinstance(exc, GatewayError):
        return Response.json(exc.payload(), status=exc.status)
    return Response.json(
        {"error": {"message": str(exc) or type(exc).__name__, "type": "server_error"}},
        status=500,
    )