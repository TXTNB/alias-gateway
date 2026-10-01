"""配置的加载、校验、归一化、首次生成、打码与管理端合并。

对外主要接口：

    store = ConfigStore.load(path)      读取（不存在则生成空骨架）
    store.validate()                    校验，返回错误列表
    store.apply_overrides(**fields)     命令行覆盖（校验 + 落盘）
    store.apply_panel_update(payload)   管理端整体提交
    store.masked()                      给管理端读的打码副本
    store.register_password(new)        首次注册（设置管理密码）

首次生成的 ``config.json`` 是一份空骨架：只有监听地址和一个空密码哈希，
``routes`` 为空数组。这里不预填任何上游。以前会塞一个
``https://example.invalid/v1`` 的占位条目，看起来像"已经配好的 API 数据"，
实际是连不上的假地址，容易让人以为网关里凭空多出东西。

``routes`` 为空是合法状态：服务照常起来，只是还没有可用的客户端 Key，
等用户在管理界面里加。
"""

import copy
import json
import os
import re
import secrets
import tempfile
import threading

from . import alias as alias_mod
from . import auth
from .errors import ConfigError, format_conflict_report

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8789
DEFAULT_UPSTREAM_TIMEOUT = 300

CLIENT_KEY_RE = re.compile(r"^[A-Za-z0-9_\-\.]{4,128}$")
UPSTREAM_NAME_RE = re.compile(r"^[A-Za-z0-9_\-\.]{1,64}$")
AUTH_TYPES = ("bearer", "header", "none")

MASK = "••••••••"
MASK_KEEP = 4


def generate_client_key():
    """生成客户端 Key：``sk-`` + 64 位小写十六进制，共 67 字符。"""
    return "sk-" + secrets.token_hex(32)


def mask_secret(value, keep=MASK_KEEP):
    """给上游 Key 打码：保留前 ``keep`` 位，后面统一用 ``MASK`` 代替。

    太短的值（不长于 ``keep``）直接整个打码，避免露出大部分内容。
    """
    if value is None:
        return ""
    text = str(value)
    if not text:
        return ""
    if len(text) <= keep:
        return MASK
    return text[:keep] + MASK


def default_config():
    """首次生成用的空骨架。

    这里不预填任何上游，上游全部由用户自己在管理界面里加。
    """
    return {
        "listen_port": DEFAULT_PORT,
        "listen_host": DEFAULT_HOST,
        "panel_password_hash": "",
        "routes": [],
    }


def normalize_upstream(raw, index=0):
    """把一条上游配置补全成标准结构，缺字段用默认值兜住。

    这里只做类型与默认值整理，合法性交给 :func:`validate_config` ——
    归一化尽量不抛异常，否则用户改错一个字段就整个配置读不进来。
    """
    if not isinstance(raw, dict):
        raise ConfigError("upstream #%d 不是对象" % (index + 1))
    name = str(raw.get("name") or "upstream-%d" % (index + 1)).strip()
    base_url = str(raw.get("base_url") or "").strip().rstrip("/")

    auth_raw = raw.get("auth") or {}
    if not isinstance(auth_raw, dict):
        auth_raw = {}
    auth_type = str(auth_raw.get("type") or "bearer").strip().lower()
    auth_obj = {"type": auth_type, "key": str(auth_raw.get("key") or "")}
    if auth_type == "header":
        auth_obj["header"] = str(auth_raw.get("header") or "Authorization").strip()

    extra = raw.get("extra_headers") or {}
    if not isinstance(extra, dict):
        extra = {}
    extra_headers = {str(k): str(v) for k, v in extra.items()}

    try:
        timeout = int(raw.get("timeout") or DEFAULT_UPSTREAM_TIMEOUT)
    except (TypeError, ValueError):
        timeout = DEFAULT_UPSTREAM_TIMEOUT
    if timeout <= 0:
        timeout = DEFAULT_UPSTREAM_TIMEOUT

    return {
        "name": name,
        "base_url": base_url,
        "auth": auth_obj,
        "extra_headers": extra_headers,
        "timeout": timeout,
    }


def normalize_route(raw, index=0):
    """归一化一条路由，包含它的上游、别名、首选与黑白名单。"""
    if not isinstance(raw, dict):
        raise ConfigError("route #%d 不是对象" % (index + 1))

    upstreams_raw = raw.get("upstreams")
    if upstreams_raw is None:
        upstreams_raw = []
    if not isinstance(upstreams_raw, list):
        raise ConfigError("route #%d 的 upstreams 必须是数组" % (index + 1))
    upstreams = [normalize_upstream(u, i) for i, u in enumerate(upstreams_raw)]

    aliases_raw = raw.get("aliases") or {}
    if not isinstance(aliases_raw, dict):
        raise ConfigError("route #%d 的 aliases 必须是对象" % (index + 1))
    aliases = {str(k): str(v) for k, v in aliases_raw.items() if str(k) and str(v)}

    prefer_raw = raw.get("prefer") or {}
    if not isinstance(prefer_raw, dict):
        raise ConfigError("route #%d 的 prefer 必须是对象" % (index + 1))
    prefer = {str(k): str(v) for k, v in prefer_raw.items() if str(k) and str(v)}

    def _str_list(value, field):
        if value is None:
            return []
        if not isinstance(value, list):
            raise ConfigError("route #%d 的 %s 必须是数组" % (index + 1, field))
        return [str(v) for v in value if str(v)]

    return {
        "client_key": str(raw.get("client_key") or "").strip(),
        "upstreams": upstreams,
        "aliases": aliases,
        "prefer": prefer,
        "whitelist": _str_list(raw.get("whitelist"), "whitelist"),
        "blacklist": _str_list(raw.get("blacklist"), "blacklist"),
    }


def normalize_config(raw):
    """归一化整份配置：端口转 int、host 去空白、routes 逐条处理。"""
    if not isinstance(raw, dict):
        raise ConfigError("配置根节点必须是对象")

    try:
        port = int(raw.get("listen_port") or DEFAULT_PORT)
    except (TypeError, ValueError):
        port = DEFAULT_PORT

    routes_raw = raw.get("routes")
    if routes_raw is None:
        routes_raw = []
    if not isinstance(routes_raw, list):
        raise ConfigError("routes 必须是数组")

    return {
        "listen_port": port,
        "listen_host": str(raw.get("listen_host") or DEFAULT_HOST).strip(),
        "panel_password_hash": str(raw.get("panel_password_hash") or ""),
        "routes": [normalize_route(r, i) for i, r in enumerate(routes_raw)],
    }


def validate_config(cfg):
    """校验配置，返回错误信息列表（空列表表示通过）。

    这里只做"能不能安全跑起来"的判断，不做自动修复 ——
    自动修复会让用户搞不清自己到底提交了什么。
    """
    errors = []

    if not (1 <= int(cfg.get("listen_port", 0)) <= 65535):
        errors.append("listen_port 必须在 1..65535 之间")
    if not cfg.get("listen_host"):
        errors.append("listen_host 不能为空")

    stored_hash = cfg.get("panel_password_hash")
    if stored_hash and not auth.verify_format(stored_hash):
        errors.append("panel_password_hash 格式非法，应为 pbkdf2_sha256$iters$salt$hash")

    routes = cfg.get("routes") or []

    # routes 为空是合法状态：刚生成的空骨架还没配置任何上游。
    # 服务照常起来，只是没有可用 Key，等用户在管理界面里加。

    seen_keys = {}
    for idx, route in enumerate(routes, 1):
        key = route.get("client_key") or ""
        if not CLIENT_KEY_RE.match(key):
            errors.append("route #%d 的 client_key 非法（4-128 位字母数字与 _-.'）" % idx)
        if key in seen_keys:
            errors.append("client_key 重复：'%s'（route #%d 与 #%d）" % (key, seen_keys[key], idx))
        else:
            seen_keys[key] = idx

        upstreams = route.get("upstreams") or []
        if not upstreams:
            errors.append("route '%s' 至少要有一个 upstream" % (key or "#%d" % idx))

        names = {}
        for uidx, up in enumerate(upstreams, 1):
            name = up.get("name") or ""
            if not UPSTREAM_NAME_RE.match(name):
                errors.append("route '%s' 的 upstream #%d name 非法" % (key, uidx))
            if name in names:
                errors.append("route '%s' 的 upstream name 重复：'%s'" % (key, name))
            else:
                names[name] = uidx

            base_url = up.get("base_url") or ""
            if not (base_url.startswith("http://") or base_url.startswith("https://")):
                errors.append("route '%s' 的 upstream '%s' base_url 必须以 http:// 或 https:// 开头" % (key, name))
            elif not base_url.rstrip("/").endswith("/v1"):
                errors.append("route '%s' 的 upstream '%s' base_url 必须以 /v1 结尾" % (key, name))

            auth_type = (up.get("auth") or {}).get("type")
            if auth_type not in AUTH_TYPES:
                errors.append(
                    "route '%s' 的 upstream '%s' auth.type 必须是 %s" % (key, name, "/".join(AUTH_TYPES))
                )
            elif auth_type in ("bearer", "header") and not (up.get("auth") or {}).get("key"):
                errors.append("route '%s' 的 upstream '%s' auth.type=%s 但没填 key" % (key, name, auth_type))
            if auth_type == "header" and not (up.get("auth") or {}).get("header"):
                errors.append("route '%s' 的 upstream '%s' auth.type=header 但没填 header 名" % (key, name))

            try:
                if int(up.get("timeout") or 0) <= 0:
                    errors.append("route '%s' 的 upstream '%s' timeout 必须为正整数" % (key, name))
            except (TypeError, ValueError):
                errors.append("route '%s' 的 upstream '%s' timeout 必须是整数" % (key, name))

        prefer = route.get("prefer") or {}
        for model, owner in prefer.items():
            if owner not in names:
                errors.append(
                    "route '%s' 的 prefer['%s'] 指向不存在的 upstream '%s'" % (key, model, owner)
                )

    return errors


def detect_conflicts(cfg, model_map, route_keys=None):
    """按已拉到的模型清单做冲突检测，返回冲突列表。

    具体判定复用 :func:`core.models.plan_merge`，这里只负责遍历 route。
    """
    from . import models as models_mod

    conflicts = []
    for route in cfg.get("routes") or []:
        key = route.get("client_key")
        if route_keys is not None and key not in route_keys:
            continue

        per_upstream = model_map.get(key) or {}
        if not per_upstream:
            continue

        _selected, route_conflicts = models_mod.plan_merge(route, per_upstream)
        conflicts.extend(route_conflicts)
    return conflicts


def conflict_report(conflicts):
    """冲突的多行报告（启动预热失败时打到 stderr）。"""
    return format_conflict_report(conflicts)


class ConfigStore:
    """配置文件的内存视图 + 原子落盘。

    所有读写都在同一把可重入锁里，管理端保存与请求转发会并发访问，
    读接口一律返回深拷贝或新列表，避免调用方改到内部状态。
    """

    def __init__(self, path):
        self.path = os.path.abspath(path)
        self._lock = threading.RLock()
        self._config = None
        self.created = False

    @classmethod
    def load(cls, path):
        """读取配置；文件不存在时生成空骨架并落盘。"""
        store = cls(path)
        store.reload()
        return store

    def reload(self):
        """重新从磁盘读取；文件不存在则生成空骨架。

        ``created`` 标记本次是否属于"首次生成"，调用方据此决定要不要打印
        ``[init]`` 提示。
        """
        with self._lock:
            if os.path.exists(self.path):
                self._config = self._read_file()
                self.created = False
            else:
                self._config = default_config()
                self.created = True
                self.save()
            return self._config

    def _read_file(self):
        """读盘 + 归一化 + 校验，任何问题都包成 :class:`ConfigError`。"""
        try:
            with open(self.path, "r", encoding="utf-8") as fp:
                raw = json.load(fp)
        except json.JSONDecodeError as exc:
            raise ConfigError("配置文件不是合法 JSON：%s" % exc)
        except OSError as exc:
            raise ConfigError("配置文件读取失败：%s" % exc)

        cfg = normalize_config(raw)
        errors = validate_config(cfg)
        if errors:
            raise ConfigError("配置文件校验失败：\n  - " + "\n  - ".join(errors))
        return cfg

    def save(self):
        """原子落盘：先写临时文件并 fsync，再 ``os.replace`` 顶替原文件。

        这样即使写到一半断电，也不会留下半截配置文件。
        """
        with self._lock:
            cfg = self._config
            if cfg is None:
                raise ConfigError("没有可保存的配置")
            folder = os.path.dirname(self.path) or "."
            os.makedirs(folder, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".config-", suffix=".json", dir=folder)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fp:
                    json.dump(cfg, fp, ensure_ascii=False, indent=2, sort_keys=False)
                    fp.write("\n")
                    fp.flush()
                    os.fsync(fp.fileno())
                os.replace(tmp, self.path)
            except Exception:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise

    @property
    def config(self):
        with self._lock:
            return self._config

    def snapshot(self):
        """返回深拷贝，调用方随便改都不影响内部状态。"""
        with self._lock:
            return copy.deepcopy(self._config)

    def validate(self):
        with self._lock:
            return validate_config(self._config)

    def routes(self):
        with self._lock:
            return list(self._config.get("routes") or [])

    def get_route(self, client_key):
        with self._lock:
            for route in self._config.get("routes") or []:
                if route.get("client_key") == client_key:
                    return route
            return None

    def upstreams(self):
        """把所有 route 下的上游拉平成一个列表，并标上所属 route。

        管理界面的"上游测试"要一次性看到全部上游，就用这个。
        """
        out = []
        with self._lock:
            for route in self._config.get("routes") or []:
                for up in route.get("upstreams") or []:
                    item = dict(up)
                    item["route_key"] = route.get("client_key")
                    out.append(item)
        return out

    def password_hash(self):
        with self._lock:
            return self._config.get("panel_password_hash")

    def apply_overrides(self, **fields):
        """命令行覆盖（``--host`` / ``--port``），校验通过后写回配置文件。

        以前 ``--port`` 只改内存里的监听地址，配置文件里还是旧值，于是
        ``tui.py`` 不带参数时按 config.json 去连旧端口，自然连不上。
        现在覆盖值会落盘，管理界面不用带参数也能找到网关。

        返回 ``(changed, errors)``；校验失败时回滚成原值，不落盘。
        """
        clean = {k: v for k, v in fields.items() if v is not None}
        if not clean:
            return False, []

        with self._lock:
            backup = {k: self._config.get(k) for k in clean}
            self._config.update(clean)

            errors = validate_config(self._config)
            if errors:
                self._config.update(backup)
                return False, errors

            changed = any(backup[k] != clean[k] for k in clean)
            if changed:
                self.save()
            return changed, []

    def masked(self):
        """给管理端读的副本。

        客户端 Key 不打码（它本来就是要复制到客户端里用的）；只有上游的
        ``auth.key`` 属于第三方凭据，才打码。密码哈希永远不出现在副本里。
        """
        with self._lock:
            cfg = copy.deepcopy(self._config)
        cfg["panel_password_hash"] = ""
        cfg["panel_registered"] = auth.is_registered(self._config.get("panel_password_hash"))
        for route in cfg.get("routes") or []:
            for up in route.get("upstreams") or []:
                auth_obj = up.get("auth") or {}
                auth_obj["key_masked"] = mask_secret(auth_obj.get("key"))
                auth_obj["key"] = mask_secret(auth_obj.get("key"))
        return cfg

    def apply_panel_update(self, payload):
        """管理端提交的完整配置。空/打码的 Key 保留原值，其余整体替换。

        返回 ``(ok, errors, warnings)``：warnings 是不阻断保存但值得提醒的问题。
        """
        with self._lock:
            current = copy.deepcopy(self._config)

        if not isinstance(payload, dict):
            return False, ["提交内容不是 JSON 对象"], []

        try:
            merged = self._merge_payload(current, payload)
            cfg = normalize_config(merged)
        except ConfigError as exc:
            return False, [str(exc)], []

        errors = validate_config(cfg)
        if errors:
            return False, errors, []

        warnings = self._warnings(cfg)

        with self._lock:
            self._config = cfg
            self.save()
        return True, [], warnings

    def _merge_payload(self, current, payload):
        """把管理端提交合并到当前配置上。

        规则：
          * 顶层 listen_host / listen_port 直接覆盖；
          * panel_password_hash 管理端不提交，永远保留当前值；
          * route 按 client_key 匹配，匹配不上的当新 route（客户端 Key 是明文，直接采用）；
          * upstream 里打码或空白的 key 保留原值（按 route+upstream name 对齐）。
        """
        merged = copy.deepcopy(current)
        merged["listen_host"] = payload.get("listen_host", current.get("listen_host"))
        merged["listen_port"] = payload.get("listen_port", current.get("listen_port"))
        merged["panel_password_hash"] = current.get("panel_password_hash")

        routes_in = payload.get("routes")
        if routes_in is None:
            return merged
        if not isinstance(routes_in, list):
            raise ConfigError("routes 必须是数组")

        current_routes = {r.get("client_key"): r for r in current.get("routes") or []}
        merged_routes = []
        for raw_route in routes_in:
            if not isinstance(raw_route, dict):
                raise ConfigError("route 必须是对象")
            route = copy.deepcopy(raw_route)
            route.pop("client_key_masked", None)

            # 客户端 Key 留空时，按顺序沿用原来的（或生成一个新的）
            if not str(route.get("client_key") or "").strip():
                route["client_key"] = self._fallback_client_key(raw_route, current_routes, merged_routes)

            old_route = current_routes.get(route.get("client_key"))
            old_ups = {}
            if old_route:
                old_ups = {u.get("name"): u for u in old_route.get("upstreams") or []}

            new_ups = []
            for raw_up in route.get("upstreams") or []:
                if not isinstance(raw_up, dict):
                    raise ConfigError("upstream 必须是对象")
                up = copy.deepcopy(raw_up)
                up.pop("key_masked", None)
                auth_obj = up.get("auth")
                if isinstance(auth_obj, dict):
                    auth_obj.pop("key_masked", None)
                    if _looks_masked(auth_obj.get("key")):
                        old = old_ups.get(up.get("name")) or {}
                        auth_obj["key"] = ((old.get("auth") or {}).get("key")) or ""
                new_ups.append(up)
            route["upstreams"] = new_ups
            merged_routes.append(route)

        merged["routes"] = merged_routes
        return merged

    @staticmethod
    def _fallback_client_key(raw_route, current_routes, merged_routes):
        """管理端没填 client_key 时，回填原始 Key。

        优先按 route 下标对齐，其次生成一个新的。
        """
        index = len(merged_routes)
        keys = list(current_routes.keys())
        if index < len(keys):
            return keys[index]
        return generate_client_key()

    def _warnings(self, cfg):
        """不阻断保存、但值得提醒的问题。"""
        warnings = []
        for route in cfg.get("routes") or []:
            ambiguous = alias_mod.ambiguous_upstreams(route.get("aliases") or {})
            for up_name, clients in ambiguous.items():
                warnings.append(
                    "route '%s'：%s 同时被 %s 映射到，响应方向只会用 '%s'"
                    % (route.get("client_key"), up_name, "/".join(clients), clients[0])
                )
        return warnings

    def register_password(self, new_password):
        """首次注册：设置管理密码。已注册则拒绝。返回 ``(ok, message)``。"""
        if not new_password or len(str(new_password)) < 4:
            return False, "密码至少 4 位"
        with self._lock:
            stored = self._config.get("panel_password_hash")
            if auth.is_registered(stored):
                return False, "已经注册过了，请直接登录"
            self._config["panel_password_hash"] = auth.hash_password(new_password)
            self.save()
        return True, "注册成功"

    def change_password(self, current_password, new_password):
        """校验旧密码并写入新哈希。返回 ``(ok, message)``。"""
        if not new_password or len(str(new_password)) < 4:
            return False, "新密码至少 4 位"
        with self._lock:
            stored = self._config.get("panel_password_hash")
            if not auth.verify_password(current_password, stored):
                return False, "当前密码不正确"
            self._config["panel_password_hash"] = auth.hash_password(new_password)
            self.save()
        return True, "管理密码已更新"


def _looks_masked(value):
    """判断一个值是不是打码后的占位（管理端没改过它）。"""
    if value is None:
        return True
    text = str(value)
    if not text:
        return True
    return MASK in text


def init_messages(store):
    """首次生成 config.json 时终端要打印的几行。"""
    cfg = store.config or {}
    routes = cfg.get("routes") or []
    if routes:
        last = "[init] default route client_key: %s" % routes[0].get("client_key")
    else:
        last = "[init] no route configured yet, add one in the TUI (menu [3] config edit)"
    return [
        "[init] config.json created (empty skeleton, no upstream)",
        "[init] panel not registered yet, run: python3 tui.py (it will ask you to set a password)",
        last,
    ]