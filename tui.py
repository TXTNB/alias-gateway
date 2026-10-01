"""alias-gateway 终端管理界面（TUI）。

设计说明
--------

这个文件是网关管理接口的客户端，不是服务端的一部分：

    tui.py  --HTTP-->  /panel/*  -->  Gateway（server/handlers.py）

这样做的好处是复用同一套鉴权、打码与配置合并逻辑，不用把"哪些字段
要保持原值"的规则再实现一遍；而且它既能管本机网关，也能管远端网关
（--url 指向哪里就管哪里）。它不直接读写 config.json。

覆盖的能力（与原来的 Web 面板一一对应）：
    首次注册 / 总览 / 配置查看 / 配置编辑（新增与修改同一套表单）/ 配置导入导出
    模型清单 / 上游连通性测试 / 日志查看与清空 / 修改管理密码

界面是全屏重绘的：每个菜单动作都先清屏，结果独占一个结果页，
结果底部显示「按回车返回菜单」；回车后再清屏回到菜单，不会一直往下滚。
报错也使用同一个结果页暂停，不会被旧菜单覆盖。
配置查看也是逐字段摊开成人读格式，不打印 JSON，要 JSON 走菜单 [8] 导出。

用法：
    python3 tui.py                              # 本机（端口取自 config.json）
    python3 tui.py --port 8790                  # 换端口
    python3 tui.py --url http://host:8789       # 管远端网关
    python3 tui.py -p 密码                       # 直接带密码登录（首次进入则改走注册）

只依赖标准库。
"""

import argparse
import getpass
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_PORT = 8789
TOKEN_HEADER = "X-Panel-Token"
HTTP_TIMEOUT = 60.0
DEFAULT_LOG_LIMIT = 60
MENU_WIDTH = 24

# 只在真终端下着色/清屏；输出被重定向时保持纯文本，方便脚本抓取
_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None

# 刚出过报错：下一次不清屏，让错误留在屏幕上（正常输出才覆盖重绘）
_ERROR_SEEN = False


# --------------------------------------------------------------------------
# 输出小工具
# --------------------------------------------------------------------------

def _paint(text, code):
    if not _COLOR:
        return text
    return "\033[%sm%s\033[0m" % (code, text)


def title(text):
    return _paint(text, "1;36")


def good(text):
    return _paint(text, "32")


def warn(text):
    return _paint(text, "33")


def bad(text):
    """红色文本，同时记一笔「刚出过报错」。

    报错不会被下一次清屏覆盖——这正是 _take_error() 的用途。
    """
    global _ERROR_SEEN
    _ERROR_SEEN = True
    return _paint(text, "31")


def dim(text):
    return _paint(text, "2")


# ANSI 颜色码不占显示宽度：算宽度前先剥掉，否则带色的行居中会偏
_ANSI_RE = re.compile(r"\033\[[0-9;]*m")


def _width(text):
    """终端显示宽度（中日韩字符按 2 格算，ANSI 颜色码不计）。"""
    plain = _ANSI_RE.sub("", text)
    return sum(2 if ord(char) > 0x2E80 else 1 for char in plain)


def _pad(text, width):
    """按终端显示宽度右补空格。"""
    return text + " " * max(0, width - _width(text))


def _center(text, width):
    """在给定宽度里居中：左侧补空格（文本更宽就贴左）。"""
    return " " * max(0, (width - _width(text)) // 2) + text


def _row(indent, label, value):
    """一行「标签 值」，标签按显示宽度对齐。"""
    return "%s%s %s" % (" " * indent, _pad(str(label), 12), value)


def _take_error():
    """读出并清掉「刚出过报错」的标记（只生效一次）。"""
    global _ERROR_SEEN
    seen = _ERROR_SEEN
    _ERROR_SEEN = False
    return seen


def _clear():
    """清屏，让下一次输出覆盖上一次的内容。

    只在真终端下做：输出被重定向（管道、文件）时清屏没有意义，
    还会往日志里塞 ANSI 转义。
    """
    if not _COLOR:
        return
    sys.stdout.write("\033[2J\033[H")
    sys.stdout.flush()


def _pause(prompt="  按回车返回菜单 > "):
    """等用户看完当前页面，再返回菜单。

    正常菜单结果和错误结果都使用这个暂停；非终端（管道/脚本）下直接跳过。
    """
    if not _COLOR:
        return
    ask(prompt)


# --------------------------------------------------------------------------
# 异常
# --------------------------------------------------------------------------

class PanelError(Exception):
    """管理接口返回的错误。"""

    def __init__(self, status, message, data=None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.data = data


class AuthExpired(PanelError):
    """401：没登录或 token 已失效。"""


class OfflineError(Exception):
    """连不上网关。"""


# --------------------------------------------------------------------------
# 接口客户端
# --------------------------------------------------------------------------

class PanelClient:
    """/panel/* 接口的瘦客户端。

    只负责发请求与解析错误，业务规则（打码、合并、校验）全在服务端，
    避免两边各实现一套导致行为不一致。
    """

    def __init__(self, base_url):
        self.base_url = base_url.rstrip("/")
        self.token = ""

    # ---- HTTP --------------------------------------------------------
    def _request(self, method, path, payload=None):
        url = self.base_url + path
        data = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=utf-8"
        if self.token:
            headers[TOKEN_HEADER] = self.token

        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            # 4xx/5xx 也当成正常返回：调用方要看响应体里的错误说明
            return exc.code, exc.read()
        except urllib.error.URLError as exc:
            raise OfflineError("无法连接 %s（%s）" % (self.base_url, exc.reason))
        except OSError as exc:
            raise OfflineError("请求失败：%s" % exc)

    def _json(self, method, path, payload=None):
        status, body = self._request(method, path, payload)
        data = _load_json(body)
        if status == 401:
            self.token = ""
            raise AuthExpired(401, _error_text(data) or "登录已失效，请重新登录")
        if status >= 400:
            raise PanelError(status, _error_text(data) or ("HTTP %d" % status), data)
        return data

    # ---- 接口 --------------------------------------------------------
    def health(self):
        return self._json("GET", "/health")

    def login(self, password):
        status, body = self._request("POST", "/panel/login", {"password": password})
        data = _load_json(body) or {}
        if status != 200:
            raise PanelError(status, _error_text(data) or "登录失败", data)
        self.token = data.get("token") or ""
        return data

    def register(self, password):
        """首次进入：设置管理密码，成功后直接拿到会话。"""
        status, body = self._request("POST", "/panel/register", {"password": password})
        data = _load_json(body) or {}
        if status != 200:
            raise PanelError(status, _error_text(data) or "注册失败", data)
        self.token = data.get("token") or ""
        return data

    def logout(self):
        try:
            if self.token:
                self._json("POST", "/panel/logout")
        finally:
            self.token = ""

    def status(self):
        return self._json("GET", "/panel/status")

    def overview(self):
        return self._json("GET", "/panel/overview")

    def config_masked(self):
        return self._json("GET", "/panel/config")

    def config_save(self, payload):
        return self._json("POST", "/panel/config", payload)

    def config_export(self):
        """导出明文配置（含明文 Key），返回原始字节。"""
        status, body = self._request("GET", "/panel/config/export")
        if status == 401:
            self.token = ""
            raise AuthExpired(401, "登录已失效，请重新登录")
        if status >= 400:
            raise PanelError(status, _error_text(_load_json(body)) or "导出失败")
        return body

    def models(self, force=False):
        return self._json("GET", "/panel/models?force=1" if force else "/panel/models")

    def test_upstreams(self):
        return self._json("POST", "/panel/upstreams/test")

    def logs(self, limit=DEFAULT_LOG_LIMIT, level="", module="", keyword=""):
        query = {"limit": str(limit)}
        if level:
            query["level"] = level
        if module:
            query["module"] = module
        if keyword:
            query["q"] = keyword
        return self._json("GET", "/panel/logs?" + urllib.parse.urlencode(query))

    def logs_clear(self):
        return self._json("POST", "/panel/logs/clear")

    def change_password(self, current, new):
        return self._json("POST", "/panel/password", {"current": current, "new": new})


# --------------------------------------------------------------------------
# 解析小工具
# --------------------------------------------------------------------------

def _load_json(body):
    if not body:
        return None
    try:
        return json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None


def _error_text(data):
    """从各种可能的错误体结构里抽出一句人话。"""
    if not isinstance(data, dict):
        return ""
    error = data.get("error")
    if isinstance(error, dict) and error.get("message"):
        return str(error["message"])
    if isinstance(error, str) and error:
        return error
    errors = data.get("errors")
    if isinstance(errors, list) and errors:
        return "; ".join(str(item) for item in errors)
    if data.get("message"):
        return str(data["message"])
    return ""


def _fmt_uptime(seconds):
    try:
        total = int(seconds or 0)
    except (TypeError, ValueError):
        return "-"
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return "%dh %dm %ds" % (hours, minutes, secs)
    if minutes:
        return "%dm %ds" % (minutes, secs)
    return "%ds" % secs


# --------------------------------------------------------------------------
# 交互小工具
# --------------------------------------------------------------------------

def ask(prompt, default=""):
    try:
        raw = input(prompt).strip()
    except EOFError:
        raise SystemExit(0)
    except KeyboardInterrupt:
        print()
        raise SystemExit(0)
    return raw or default


def ask_secret(prompt):
    """读密码：终端不支持 getpass 时回退到普通输入。"""
    try:
        return getpass.getpass(prompt)
    except (EOFError, KeyboardInterrupt):
        print()
        raise SystemExit(0)
    except Exception:
        return ask(prompt)


def confirm(prompt):
    answer = ask("  %s [y/N] > " % prompt).lower()
    return answer in ("y", "yes", "是", "Y")


# --------------------------------------------------------------------------
# 各菜单动作
# --------------------------------------------------------------------------

def action_overview(client):
    """总览：版本、运行时长、监听地址、各类计数与路径。"""
    data = client.overview()
    print()
    print(title("运行总览"))
    rows = [
        ("版本", data.get("version")),
        ("已运行", _fmt_uptime(data.get("uptime"))),
        ("监听", "%s:%s" % (data.get("listen_host"), data.get("listen_port"))),
        ("路由数", data.get("routes")),
        ("上游数", data.get("upstreams")),
        ("别名数", data.get("aliases")),
        ("prefer", data.get("prefers")),
        ("在线会话", data.get("sessions")),
        ("配置文件", data.get("config_path")),
        ("日志文件", data.get("log_path") or dim("(未写文件)")),
    ]
    for name, value in rows:
        print("  %s %s" % (_pad(name, 10), value))


def _fmt_map(value):
    """把 aliases / whitelist 这类映射或列表摊成一行。

    映射用 ``键 = 值``（两侧留空格）—— 等号挤在一起时，长模型名很难一眼看清对应关系。
    """
    if isinstance(value, dict):
        return "、".join("%s = %s" % (k, v) for k, v in value.items()) or dim("（空）")
    if isinstance(value, (list, tuple)):
        return "、".join(str(v) for v in value) or dim("（空）")
    return str(value)


def action_config_view(client):
    """配置查看：逐字段摊开成人读格式，不打印 JSON。

    要看 JSON 原文就走菜单 [8] 导出配置。

    每个上游下面还会列出它实际提供的模型，配置里只有地址，模型清单得问上游。
    清单拉不到（网关没起、上游全挂）时退化成纯配置展示，不影响查看。

    查看之后可以直接改归属：有冲突先走冲突修复，没冲突则列可调整的模型，
    选完就地写进 ``prefer`` 并重新渲染，于是改完立刻能看到"这个模型用了哪个上游的"。
    """
    while True:
        _clear()
        cfg = client.config_masked()
        routes = cfg.get("routes") or []
        listing = _model_listing(client)
        _render_config(cfg, listing)

        # 冲突与上游模型都在清单视图里（不是配置里），所以编辑要走清单那侧的 route 结构
        listing_routes = [r for r in (listing.get(x.get("client_key")) for x in routes) if r]

        conflicts = _collect_conflicts(listing_routes)
        if conflicts:
            print()
            if not confirm("检测到 %d 个同名冲突，现在修？" % len(conflicts)):
                return
            if not _fix_conflicts(client, listing_routes):
                # 全部跳过或写入失败：再渲染一遍还是一样，不如就此打住
                return
            continue

        if not _edit_prefer(client, listing_routes):
            return


def _collect_conflicts(routes):
    """把各条路由的冲突摊平成一维列表（入参是清单视图里的 route）。"""
    return [item for route in routes or [] for item in (route.get("conflicts") or [])]


def _editable_models(routes):
    """找出"被多个上游同时提供"的模型，并带上当前归属。

    ``prefer`` 的键是上游真实模型名，所以这里按原始名归组；只有一个来源的
    模型没什么可选，不列出来。
    """
    items = []
    for route in routes or []:
        prefer = route.get("prefer") or {}
        buckets = {}
        order = []
        for up in route.get("upstreams") or []:
            name = up.get("name")
            for raw in up.get("models") or []:
                if raw not in buckets:
                    buckets[raw] = []
                    order.append(raw)
                buckets[raw].append({"name": name, "base_url": up.get("base_url") or ""})

        for raw in order:
            owners = buckets[raw]
            if len({o["name"] for o in owners}) < 2:
                continue
            items.append({
                "route": route.get("client_key"),
                "model": raw,
                "owners": owners,
                "owner": prefer.get(raw) or "",
            })
    return items


def _edit_prefer(client, routes):
    """改「某个模型采用哪个上游的模型」。

    一次只改一个：选完写进 ``prefer`` 就返回，让调用方重新渲染 —— 这样屏幕始终
    只有一份最新配置，不用在菜单里来回对照。返回是否有改动落盘。
    """
    editable = _editable_models(routes)
    if not editable:
        return False

    print()
    print(title("模型归属"))
    print(dim("  以下模型被多个上游提供，可指定采用哪一个。"))
    for index, item in enumerate(editable, 1):
        if item["owner"]:
            state = "当前采用 %s" % item["owner"]
        else:
            state = bad("未指定（请求会返回 500）")
        print("  [%d] %s %s" % (index, _pad(item["model"], 28), state))
    print(dim("  输入编号调整 / 回车返回"))

    answer = ask("  > ").strip()
    if not answer:
        return False
    if not answer.isdigit() or not (1 <= int(answer) <= len(editable)):
        print(bad("  没有这个编号：%s" % answer))
        return False

    item = editable[int(answer) - 1]
    print()
    print("  %s 用哪个上游的模型？" % item["model"])
    for index, owner in enumerate(item["owners"], 1):
        suffix = dim("（当前）") if owner["name"] == item["owner"] else ""
        print("    [%d] %s %s %s" % (index, owner["name"],
                                     dim(owner["base_url"]), suffix))

    answer = ask("  用哪个 > ").strip()
    names = [o["name"] for o in item["owners"]]
    chosen = ""
    if answer.isdigit() and 1 <= int(answer) <= len(item["owners"]):
        chosen = item["owners"][int(answer) - 1]["name"]
    elif answer in names:
        chosen = answer

    if not chosen:
        print(warn("  已取消。"))
        return False

    saved = _apply_prefer(client, {item["route"]: {item["model"]: chosen}})
    if saved:
        print(good("  已把 %s 改为采用 %s 的模型。" % (item["model"], chosen)))
    return saved


def _render_config(cfg, listing):
    """把配置逐字段打印出来；模型清单由 ``listing`` 提供。"""
    routes = cfg.get("routes") or []
    print()
    print(title("当前配置"))
    print(_row(2, "监听地址", "%s:%s" % (cfg.get("listen_host"), cfg.get("listen_port"))))
    print(_row(2, "管理密码", good("已设置") if cfg.get("panel_registered") else warn("未注册")))
    print(_row(2, "路由数", len(routes)))
    print(dim("  上游 Key 打码显示；客户端 Key 是明文，复制给客户端用。"))
    if not routes:
        print()
        print(dim("  还没有任何路由。走菜单 [3] 配置编辑 → n 新建一条。"))
        return

    for index, route in enumerate(routes, 1):
        print()
        print("  路由 %d：%s" % (index, route.get("client_key") or dim("(待生成)")))
        upstreams = route.get("upstreams") or []
        if not upstreams:
            print("    上游：%s" % dim("无"))

        info = listing.get(route.get("client_key")) or {}
        per_upstream = {u.get("name"): u for u in info.get("upstreams") or []}
        errors = info.get("errors") or {}

        for up in upstreams:
            name = up.get("name")
            auth = up.get("auth") or {}
            print("    上游 %s %s" % (_pad(name or "?", 14), up.get("base_url") or ""))
            detail = "认证 %s" % (auth.get("type") or "none")
            if auth.get("type") == "header" and auth.get("header"):
                detail += "（头 %s）" % auth["header"]
            if auth.get("key"):
                detail += "  Key %s" % auth["key"]
            print("         %s" % dim(detail))
            _print_upstream_models(per_upstream.get(name), errors.get(name))

        for label, field in (("别名", "aliases"), ("首选", "prefer"),
                             ("白名单", "whitelist"), ("黑名单", "blacklist")):
            value = route.get(field)
            if value:
                print("        %s：%s" % (label, _fmt_map(value)))
            if field == "prefer":
                _print_prefer_notes(route, info)

        for item in info.get("conflicts") or []:
            owners = "、".join(o.get("name") for o in item.get("found_in") or [])
            print(warn("        ! 冲突：%s 同时被 %s 提供，未进列表（可用 prefer 指定）"
                       % (item.get("model"), owners)))


def _print_prefer_notes(route, info):
    """说明 ``prefer`` 把每个模型定给了哪个上游。

    修完冲突再看配置，得能一眼看出"这个模型用的是谁家的"，
    否则 prefer 里只有一串名字，还要自己回头对照上面的上游列表。
    """
    prefer = route.get("prefer") or {}
    if not prefer:
        return

    url_by_name = {u.get("name"): u.get("base_url") for u in route.get("upstreams") or []}
    selected = {}
    for model in info.get("models") or []:
        selected[model.get("upstream_model") or model.get("id")] = model

    for raw, owner in prefer.items():
        hit = selected.get(raw)
        client_name = (hit or {}).get("id") or raw
        url = url_by_name.get(owner) or ""
        text = "        %s 采用 %s 的模型" % (client_name, owner)
        if url:
            text += "（%s）" % url
        # 不在可见清单里，说明那个上游此刻没拉到，模型实际用不了
        print(good(text) if hit else warn(text + "，但它当前不在清单里"))


def _model_listing(client):
    """拉一次模型清单，返回 ``{客户端 Key: route 视图}``。

    配置查看靠它给每个上游列出模型。拉不到就返回空字典 —— 查看配置本身
    不该因为上游不可达而失败。
    """
    try:
        data = client.models(force=True)
    except (PanelError, OfflineError):
        return {}
    return {r.get("client_key"): r for r in data.get("routes") or []}


def _print_upstream_models(entry, error):
    """在一个上游下面列出它提供的模型；没有或拉不到时说明原因。"""
    if error:
        print("         %s" % bad("模型拉取失败：%s" % error))
        return
    if entry is None:
        print("         %s" % dim("模型：未在清单中"))
        return
    models = entry.get("models") or []
    if not models:
        print("         %s" % dim("模型：无"))
        return
    print("         %s" % dim("模型"))
    for model in models:
        print("           %s" % model)


# 表单里表示"用户放弃"的哨兵值，和合法的空字符串区分开
_ABORT = object()


def _fmt_pairs(value):
    """`{客户端名: 上游名}` → `客户端名=上游名,...`。"""
    if not isinstance(value, dict):
        return ""
    return ",".join("%s=%s" % (k, v) for k, v in value.items())


def _parse_pairs(text):
    """`客户端名=上游名,...` → dict（认不出的片段直接丢掉）。"""
    pairs = {}
    for chunk in str(text or "").split(","):
        chunk = chunk.strip()
        if "=" not in chunk:
            continue
        key, _, value = chunk.partition("=")
        key, value = key.strip(), value.strip()
        if key and value:
            pairs[key] = value
    return pairs


def _parse_list(text):
    """`a,b,c` → `["a", "b", "c"]`。"""
    return [chunk.strip() for chunk in str(text or "").split(",") if chunk.strip()]


def _field(prompt, current=""):
    """问一个字段：回车保留原值，`-` 清空，`q` 放弃整个表单。"""
    hint = " [%s]" % current if current else ""
    value = ask("  %s%s > " % (prompt, hint), current)
    if value == "q":
        return _ABORT
    if value == "-":
        return ""
    return value


def _field_secret(prompt, current):
    """问一个密钥字段：已有值打码回显，回车保留，`-` 清空，`q` 放弃。"""
    if current:
        return _field(prompt, current)
    value = ask_secret("  %s > " % prompt)
    if value == "q":
        return _ABORT
    return value


def _upstream_form(up=None):
    """单个上游的表单。返回填好的 dict；用户按 `q` 放弃则返回 None。"""
    up = dict(up or {})
    auth = dict(up.get("auth") or {})

    name = _field("上游标识（字母/数字，如 primary）", up.get("name") or "")
    if name is _ABORT or not name:
        return None
    base_url = _field("上游地址（必须以 /v1 结尾）", up.get("base_url") or "")
    if base_url is _ABORT or not base_url:
        return None
    auth_type = _field("认证方式 bearer/header/none", auth.get("type") or "bearer")
    if auth_type is _ABORT:
        return None
    auth_type = (auth_type or "bearer").strip().lower()
    if auth_type not in ("bearer", "header", "none"):
        print(bad("  认证方式只能是 bearer / header / none。"))
        return None

    new_auth = {"type": auth_type, "key": ""}
    if auth_type in ("bearer", "header"):
        key = _field_secret("上游 Key", auth.get("key") or "")
        if key is _ABORT or not key:
            return None
        new_auth["key"] = key
    if auth_type == "header":
        header = _field("自定义头名", auth.get("header") or "Authorization")
        if header is _ABORT:
            return None
        new_auth["header"] = header or "Authorization"

    up.update({
        "name": name,
        "base_url": base_url,
        "auth": new_auth,
        "extra_headers": up.get("extra_headers") or {},
        "timeout": up.get("timeout") or 300,
    })
    return up


def _upstreams_loop(upstreams):
    """管理一条路由下的多个上游。

    一条路由可以挂多个上游：请求时按 ``prefer`` 优先、配置顺序兜底依次尝试，
    某个上游挂了就自动切下一个。返回更新后的列表；用户按 `q` 放弃则返回 None。

    回车只结束这一段（上游列表），后面还有路由级字段要填，提示里写清楚，
    避免用户以为按了回车整个表单就完了。
    """
    upstreams = [dict(u) for u in upstreams or []]

    while True:
        print()
        print(title("① 上游列表"))
        if not upstreams:
            print(dim("  还没有上游。输入 a 添加一个。"))
        for index, up in enumerate(upstreams, 1):
            auth = up.get("auth") or {}
            print("  [%d] %s %s %s" % (
                index,
                _pad(up.get("name") or "?", 12),
                _pad(up.get("base_url") or "", 34),
                dim("(%s)" % (auth.get("type") or "none")),
            ))
        print()
        print(dim("  输入编号修改 / a 添加 / d 删除 / 回车 = 本段完成，继续填路由信息"))

        choice = ask("  > ").strip().lower()
        if not choice:
            print(dim("  上游列表已确认：%d 个" % len(upstreams)))
            return upstreams
        if choice == "q":
            return None

        if choice == "a":
            up = _upstream_form(None)
            if up is None:
                print(dim("  已取消。"))
                continue
            upstreams.append(up)
            continue

        if choice == "d":
            if not upstreams:
                print(bad("  没有可删除的上游。"))
                continue
            raw = ask("  删除哪个（编号）> ").strip()
            if not raw.isdigit() or not (1 <= int(raw) <= len(upstreams)):
                print(bad("  编号不对。"))
                continue
            target = upstreams[int(raw) - 1]
            if not confirm("删除上游 %s？" % (target.get("name") or "?")):
                print(dim("  已取消。"))
                continue
            del upstreams[int(raw) - 1]
            continue

        if not choice.isdigit() or not (1 <= int(choice) <= len(upstreams)):
            print(bad("  没有这个选项：%s" % choice))
            continue

        index = int(choice) - 1
        up = _upstream_form(upstreams[index])
        if up is None:
            print(dim("  已取消。"))
            continue
        upstreams[index] = up


def _route_form(route=None):
    """一条路由的交互式表单，新增与修改共用同一套。

    `route=None` 表示新增；否则在已有路由的副本上修改，回车即保留原值。
    返回填好的 route；用户中途按 `q` 放弃则返回 None。
    """
    is_new = route is None
    route = dict(route or {})
    upstreams = [dict(u) for u in route.get("upstreams") or []]

    print()
    print(title("新建路由" if is_new else "修改路由"))
    print(dim("  分两步：先管上游列表，再填路由信息。"))
    print(dim("  客户端 Key 由服务端自动生成，不用填；回车保留原值，- 清空，q 放弃。"))

    # 先管上游（可以多个），再管别名这类路由级字段
    upstreams = _upstreams_loop(upstreams)
    if upstreams is None or not upstreams:
        return None

    print()
    print(title("② 路由信息"))

    aliases = _field("别名（客户端名=上游名，逗号分隔，可空）", _fmt_pairs(route.get("aliases")))
    if aliases is _ABORT:
        return None
    prefer = _field("首选（上游模型名=上游标识，逗号分隔，可空）",
                    _fmt_pairs(route.get("prefer")))
    if prefer is _ABORT:
        return None
    whitelist = _field("白名单（客户端模型名，逗号分隔，可空）",
                       ",".join(route.get("whitelist") or []))
    if whitelist is _ABORT:
        return None
    blacklist = _field("黑名单（客户端模型名，逗号分隔，可空）",
                       ",".join(route.get("blacklist") or []))
    if blacklist is _ABORT:
        return None

    return {
        "client_key": route.get("client_key") or "",   # 留空 = 服务端生成
        "upstreams": upstreams,
        "aliases": _parse_pairs(aliases),
        "prefer": _parse_pairs(prefer),
        "whitelist": _parse_list(whitelist),
        "blacklist": _parse_list(blacklist),
    }


def _conflict_hint(client, routes):
    """保存后查一次模型清单，把同名冲突与不可达上游提示出来。

    只有"某条路由挂了多个上游"时才值得查 —— 一个上游不可能跟自己撞名，
    单上游的路由也就不用为这次提示多等一轮网络请求。

    多上游场景下"同一个模型名被两个上游提供"很常见，保存时就得告诉用户，
    不要等到客户端拉清单才发现少了个模型。
    """
    if not any(len(r.get("upstreams") or []) > 1 for r in routes or []):
        return ""
    try:
        data = client.models(force=True)
    except (PanelError, OfflineError):
        return ""

    lines = []
    for route in data.get("routes") or []:
        for name, message in (route.get("errors") or {}).items():
            lines.append(bad("  上游 %s 不可达：%s" % (name, message)))
        for item in route.get("conflicts") or []:
            owners = "、".join(o.get("name") for o in item.get("found_in") or [])
            lines.append(warn(
                "  冲突：%s 同时被 %s 提供，未进模型清单（用 prefer 指定归属）"
                % (item.get("model"), owners)
            ))
    return "\n".join(lines)


def _save_config(client, cfg):
    """提交配置。返回 `(ok, 要显示的文本)`，文本已着色。"""
    try:
        result = client.config_save(cfg)
    except PanelError as exc:
        lines = [bad("  保存失败：%s" % exc.message)]
        for item in (exc.data or {}).get("errors") or []:
            lines.append(bad("    - %s" % item))
        return False, "\n".join(lines)
    lines = [good("  已保存。")]
    for item in result.get("warnings") or []:
        lines.append(warn("  警告：%s" % item))
    hint = _conflict_hint(client, cfg.get("routes"))
    if hint:
        lines.append(hint)
    return True, "\n".join(lines)


def action_config_edit(client):
    """配置编辑：新增与修改是同一套表单。

    列出当前路由 → 输入编号改那条 / `n` 新建 / `d` 删除 / 回车返回。
    每次动作前清屏重绘，屏幕始终只有「菜单式列表 + 最近一次结果」。
    报错例外：错误留在屏幕上并等一次回车。
    """
    status = ""
    while True:
        _clear()
        cfg = client.config_masked()
        routes = cfg.get("routes") or []

        print()
        print(title("配置编辑"))
        print(_row(2, "监听地址", "%s:%s" % (cfg.get("listen_host"), cfg.get("listen_port"))))
        print(_row(2, "管理密码",
                   good("已设置") if cfg.get("panel_registered") else warn("未注册")))
        print()
        if not routes:
            print(dim("  当前没有任何路由。"))
        for index, route in enumerate(routes, 1):
            ups = route.get("upstreams") or []
            names = "、".join(u.get("name") or "?" for u in ups) or dim("无上游")
            print("  [%d] %s" % (index, route.get("client_key") or dim("(待生成)")))
            print(dim("      上游 %s" % names))
        if status:
            print()
            print(status)
        print()
        print(dim("  输入编号修改 / n 新建 / d 删除 / 回车返回"))

        choice = ask("  > ").strip().lower()
        status = ""
        if not choice:
            return

        if choice == "n":
            route = _route_form(None)
            if route is None:
                status = dim("  已取消。")
                continue
            routes.append(route)
            cfg["routes"] = routes
            ok, text = _save_config(client, cfg)
            _take_error()
            if ok:
                status = text
            else:
                print(text)
                _pause()
            continue

        if choice == "d":
            if not routes:
                print(bad("  没有可删除的路由。"))
                _pause()
                continue
            raw = ask("  删除哪条（编号）> ").strip()
            if not raw.isdigit() or not (1 <= int(raw) <= len(routes)):
                print(bad("  编号不对。"))
                _pause()
                continue
            target = routes[int(raw) - 1]
            if not confirm("删除路由 %s？" % (target.get("client_key") or "(待生成)")):
                status = dim("  已取消。")
                continue
            del routes[int(raw) - 1]
            cfg["routes"] = routes
            ok, text = _save_config(client, cfg)
            _take_error()
            if ok:
                status = text
            else:
                print(text)
                _pause()
            continue

        if not choice.isdigit() or not (1 <= int(choice) <= len(routes)):
            print(bad("  没有这个选项：%s" % choice))
            _pause()
            continue

        index = int(choice) - 1
        route = _route_form(routes[index])
        if route is None:
            status = dim("  已取消。")
            continue
        routes[index] = route
        cfg["routes"] = routes
        ok, text = _save_config(client, cfg)
        _take_error()
        if ok:
            status = text
        else:
            print(text)
            _pause()


def action_config_import(client):
    """从 JSON 文件整体覆盖配置（用于迁移/恢复备份）。"""
    raw = ask("  配置文件路径（JSON）> ")
    if not raw:
        return
    path = os.path.expanduser(raw)
    if not os.path.isfile(path):
        print(bad("  文件不存在：%s" % path))
        return
    try:
        with open(path, "r", encoding="utf-8") as fp:
            payload = json.load(fp)
    except ValueError as exc:
        print(bad("  不是合法 JSON：%s" % exc))
        return
    except OSError as exc:
        print(bad("  读取失败：%s" % exc))
        return
    if not confirm("用 %s 覆盖当前配置？" % path):
        print(dim("  已取消。"))
        return
    result = client.config_save(payload)
    print(good("  导入成功。"))
    for item in result.get("warnings") or []:
        print(warn("  警告：%s" % item))


def action_config_export(client):
    """导出明文配置（含真实 Key），用于备份迁移。"""
    default = os.path.join(os.getcwd(), "config.export.json")
    raw = ask("  保存到 [%s] > " % default, default)
    path = os.path.expanduser(raw)
    data = client.config_export()
    try:
        folder = os.path.dirname(os.path.abspath(path))
        if folder:
            os.makedirs(folder, exist_ok=True)
        with open(path, "wb") as fp:
            fp.write(data)
    except OSError as exc:
        print(bad("  写入失败：%s" % exc))
        return
    print(good("  已导出到 %s（%d 字节）" % (path, len(data))))
    print(warn("  ! 这是明文配置，含上游 Key 与客户端 Key，请妥善保管。"))


def action_models(client):
    """模型清单：每条路由的可见模型、来源上游与原名溯源。

    每次都强制刷新，不走 30 秒缓存 —— 看清单就是为了确认"此刻上游到底有什么"，
    吃缓存会看到过期结果。

    发现同名冲突（同一个模型被多个上游提供）时就地引导用 prefer 指定归属；
    不指定的话，请求该模型返回 500，启动预热会直接退出。
    冲突处理完还能继续调整归属，每次改完都重新拉清单重绘，改动当场可见。
    """
    while True:
        _clear()
        print(dim("  正在拉取所有上游的模型清单……"))
        data = client.models(force=True)
        routes = data.get("routes") or []
        _render_models(routes)
        if _fix_conflicts(client, routes):
            continue
        if not _edit_prefer(client, routes):
            return


def _render_models(routes):
    """打印模型清单本体。"""
    print()
    print(title("模型清单"))
    if not routes:
        print(dim("  没有配置任何路由。"))
        return
    for route in routes:
        print()
        print("  路由 %s" % route.get("client_key"))
        upstreams = route.get("upstreams") or []
        if upstreams:
            print("    上游：%s" % "、".join(
                "%s(%d)" % (u.get("name"), u.get("count")) for u in upstreams))
        else:
            print("    上游：%s" % dim("无"))
        print("    合并：%s 个原始 → %s 个可见" % (route.get("total_raw"),
                                                  route.get("total_merged")))
        for model in route.get("models") or []:
            raw_name = model.get("upstream_model")
            owner = dim("[%s]" % model.get("upstream"))
            if raw_name:
                print("      %s %s ← %s" % (_pad(model.get("id"), 30), owner, raw_name))
            else:
                print("      %s %s" % (_pad(model.get("id"), 30), owner))
        for name, message in (route.get("errors") or {}).items():
            print(bad("      ! 上游 %s 不可达：%s" % (name, message)))
        for item in route.get("conflicts") or []:
            owners = "、".join(o.get("name") for o in item.get("found_in") or [])
            print(warn("      ! 冲突：%s 同时被 %s 提供，未进列表（可用 prefer 指定）"
                       % (item.get("model"), owners)))


def _conflict_consequence(count):
    """冲突没修完时的后果说明。

    ``plan_merge`` 找不到 ``prefer`` 归属就不会把这个模型放进清单，
    于是请求它命中不了候选、预热也会判定为致命错误。
    """
    return [
        bad("  仍有 %d 个模型冲突未解决：" % count),
        bad("    - 客户端请求这些模型会返回 500（model_conflict_error）"),
        bad("    - 重启网关时启动预热会直接退出（退出码 3）"),
        dim("    修法：在 prefer 里指定归属，或删掉重复的那个上游。"),
    ]


def _fix_conflicts(client, routes):
    """就地把同名冲突修掉：逐个挑一个上游作为归属，写进 ``prefer``。

    冲突的代价是实打实的（模型不可用 + 预热拒绝启动），所以这里不止提示，
    而是给一条能当场走完的修复路径。用户跳过的不写入，最后统一报告后果。

    返回是否有改动成功落盘，调用方靠它决定要不要重新拉一遍清单重绘。
    """
    pending = [
        (route.get("client_key"), item)
        for route in routes or []
        for item in route.get("conflicts") or []
    ]
    if not pending:
        return False

    print()
    print(title("同名冲突修复"))
    print(dim("  同一个模型被多个上游提供，必须指定用哪一个，否则该模型不可用。"))
    print(dim("  输入编号或上游标识指定归属；直接回车跳过（不修）。"))

    edits = {}
    unfixed = 0
    for key, item in pending:
        found = item.get("found_in") or []
        names = [o.get("name") for o in found]
        model = item.get("model")

        print()
        print("  模型 %s（路由 %s）" % (model, key))
        for index, owner in enumerate(found, 1):
            print("    [%d] %s %s" % (index, owner.get("name"),
                                      dim(owner.get("base_url") or "")))

        answer = ask("  用哪个 > ").strip()
        chosen = ""
        if answer.isdigit() and 1 <= int(answer) <= len(found):
            chosen = found[int(answer) - 1].get("name")
        elif answer in names:
            chosen = answer

        if chosen:
            edits.setdefault(key, {})[model] = chosen
            print(good("    已选 %s" % chosen))
        else:
            unfixed += 1
            print(warn("    跳过：%s 暂时不可用。" % model))

    saved = False
    if edits:
        saved = _apply_prefer(client, edits)
        if not saved:
            # 写入失败时所有冲突都还在，按"全部未修"报告
            unfixed = len(pending)

    if unfixed:
        print()
        for line in _conflict_consequence(unfixed):
            print(line)
    return saved


def _apply_prefer(client, edits):
    """把 ``{路由 Key: {模型原名: 上游标识}}`` 合并进配置的 ``prefer`` 并保存。

    重新拉一次打码配置再改，避免用 ``/panel/models`` 的派生视图去覆盖配置 ——
    那份视图里没有 ``prefer`` 之类的原始字段。
    """
    try:
        cfg = client.config_masked()
    except (PanelError, OfflineError) as exc:
        print(bad("  读取配置失败，prefer 未写入：%s" % exc))
        return False

    hit = 0
    for route in cfg.get("routes") or []:
        want = edits.get(route.get("client_key"))
        if not want:
            continue
        prefer = dict(route.get("prefer") or {})
        prefer.update(want)
        route["prefer"] = prefer
        hit += 1

    if not hit:
        print(bad("  没找到匹配的路由，prefer 未写入。"))
        return False

    ok, text = _save_config(client, cfg)
    _take_error()
    print(text)
    return ok


def action_upstream_test(client):
    """并发探测所有上游，显示状态码与延迟。"""
    print(dim("  正在并发探测所有上游……"))
    data = client.test_upstreams()
    results = data.get("results") or []
    print()
    print(title("上游连通性"))
    if not results:
        print(dim("  没有配置任何上游。"))
        return
    for item in results:
        mark = good("OK  ") if item.get("ok") else bad("FAIL")
        print("  %s %s %s %s" % (mark, _pad(item.get("name"), 14),
                                 _pad(item.get("base_url"), 34), item.get("auth_type")))
        if item.get("ok"):
            print(dim("       HTTP %s / %sms" % (item.get("status"), item.get("latency_ms"))))
        else:
            print(bad("       %s" % item.get("message")))


def action_logs(client):
    """日志：按级别 / 模块 / 关键词过滤，可选清空缓冲。"""
    raw_limit = ask("  条数 [%d] > " % DEFAULT_LOG_LIMIT, str(DEFAULT_LOG_LIMIT))
    try:
        limit = max(1, min(int(raw_limit), 2000))
    except ValueError:
        limit = DEFAULT_LOG_LIMIT
    level = ask("  级别过滤（DEBUG/INFO/WARNING/ERROR，逗号分隔，可空）> ")
    module = ask("  模块过滤（chat/models/upstream/auth/config/system/http，可空）> ")
    keyword = ask("  关键词（可空）> ")

    data = client.logs(limit=limit, level=level, module=module, keyword=keyword)
    entries = data.get("logs") or []
    print()
    print(title("日志（命中 %s 条，显示最近 %s 条）" % (data.get("total"), len(entries))))
    if not entries:
        print(dim("  （空）"))
        return
    for entry in entries:
        level_name = entry.get("level") or ""
        line = "%s | %s | %s | %s" % (
            entry.get("ts"), _pad(level_name, 7),
            _pad("[%s]" % entry.get("module"), 10), entry.get("message"))
        if level_name in ("ERROR", "CRITICAL"):
            print(bad("  " + line))
        elif level_name == "WARNING":
            print(warn("  " + line))
        else:
            print("  " + line)

    if confirm("清空日志缓冲？"):
        client.logs_clear()
        print(good("  已清空。"))


def action_password(client):
    """改管理密码；成功后所有会话失效，需要重新登录。"""
    current = ask_secret("  当前密码 > ")
    new = ask_secret("  新密码（至少 4 位）> ")
    again = ask_secret("  再输一次 > ")
    if not new or len(new) < 4:
        print(bad("  新密码至少 4 位。"))
        return
    if new != again:
        print(bad("  两次输入不一致。"))
        return
    result = client.change_password(current, new)
    print(good("  %s" % (result.get("message") or "已更新")))
    print(warn("  所有已登录会话已被踢下线，需要重新登录。"))
    return "relogin"


MENU = [
    ("1", "总览", action_overview),
    ("2", "配置查看", action_config_view),
    ("3", "配置编辑", action_config_edit),
    ("4", "模型清单", action_models),
    ("5", "上游测试", action_upstream_test),
    ("6", "日志", action_logs),
    ("7", "改密码", action_password),
    ("8", "导出配置", action_config_export),
    ("9", "导入配置", action_config_import),
]

ACTIONS = {key: handler for key, _label, handler in MENU}


def _menu_width():
    """菜单块的显示宽度：缩进 2 + 左列 MENU_WIDTH + 间隔 1 + 右列最宽项。

    标题按这个宽度居中 —— 直接按终端宽度居中的话，终端比菜单宽很多时
    标题会飘到屏幕中间，跟下面的菜单对不上。
    """
    labels = ["[%s] %s" % (key, label) for key, label, _handler in MENU]
    labels += ["[0] 退出", "[r] 重新登录"]
    return 2 + MENU_WIDTH + 1 + max(_width(item) for item in labels)


def _print_menu(client):
    """两列排版的菜单；标题与地址在菜单块上方居中。"""
    width = _menu_width()
    print()
    print(_center(title("=== alias-gateway 终端管理 ==="), width))
    print(_center(dim(client.base_url), width))
    items = [(key, label) for key, label, _handler in MENU]
    for index in range(0, len(items), 2):
        left = "[%s] %s" % items[index]
        right = ""
        if index + 1 < len(items):
            right = "[%s] %s" % items[index + 1]
        print("  %s %s" % (_pad(left, MENU_WIDTH), right))
    print("  %s %s" % (_pad("[0] 退出", MENU_WIDTH), "[r] 重新登录"))


def _register(client):
    """首次进入：设置管理密码（成功后自动登录）。"""
    print()
    print(title("  首次使用，请设置管理密码"))
    print(dim("  这个密码用来登录终端管理界面，之后每次进来都要输。"))
    while True:
        password = ask_secret("  新密码（至少 4 位）> ")
        if not password or len(password) < 4:
            print(bad("  密码至少 4 位，请重新输入。"))
            continue
        again = ask_secret("  再输一次 > ")
        if password != again:
            print(bad("  两次输入不一致，请重新输入。"))
            continue
        break
    try:
        data = client.register(password)
    except PanelError as exc:
        print(bad("  注册失败：%s" % exc.message))
        return False
    except OfflineError as exc:
        print(bad("  %s" % exc))
        return False
    print(good("  注册成功，已自动登录（会话 %s 秒有效）" % data.get("expires_in")))
    return True


def _login(client, password=None):
    if password is None:
        password = ask_secret("面板密码 > ")
    try:
        data = client.login(password)
    except PanelError as exc:
        print(bad("  登录失败：%s" % exc.message))
        return False
    except OfflineError as exc:
        print(bad("  %s" % exc))
        return False
    print(good("  登录成功（会话 %s 秒有效）" % data.get("expires_in")))
    return True


def _authenticate(client, password=None):
    """首次进入走注册，之后走登录。"""
    try:
        data = client.status()
    except AuthExpired:
        data = {}
    except PanelError as exc:
        print(bad("  网关响应异常：%s" % exc.message))
        return False
    except OfflineError as exc:
        print(bad("  %s" % exc))
        return False

    if not data.get("panel_registered"):
        return _register(client)
    return _login(client, password)


def run(client):
    """菜单页与结果页分离的主循环。

    进入后台先清屏显示菜单；选择任意菜单后再次清屏，只显示该菜单的结果。
    结果页底部统一等待回车，回车后清屏并返回菜单页。这样不会把旧菜单留在
    结果上方，也不会把结果接到旧输出下面。
    """
    _clear()
    _print_menu(client)
    while True:
        choice = ask("请选择 > ")

        if choice in ("0", "q", "quit", "exit"):
            _clear()
            print(dim("  再见。"))
            return
        if choice in ("r", "R", "relogin"):
            _clear()
            client.token = ""
            if not _authenticate(client):
                return
            _clear()
            _print_menu(client)
            continue

        handler = ACTIONS.get(choice)
        _clear()
        if not handler:
            print(bad("  没有这个选项：%s" % choice))
            _pause()
            _clear()
            _print_menu(client)
            continue

        try:
            if handler(client) == "relogin":
                client.token = ""
                if not _authenticate(client):
                    return
        except AuthExpired as exc:
            print(bad("  %s" % exc.message))
            client.token = ""
            if not _authenticate(client):
                return
        except PanelError as exc:
            print(bad("  失败：%s" % exc.message))
            for item in (exc.data or {}).get("errors") or []:
                print(bad("    - %s" % item))
        except OfflineError as exc:
            print(bad("  %s" % exc))
            if not confirm("重试？"):
                return
        except KeyboardInterrupt:
            print()
            print(dim("  已中断。"))
            return

        # 正常结果和报错都先停在结果页；报错内容也不会被旧菜单覆盖。
        _pause()
        _clear()
        _print_menu(client)


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------

def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog="tui.py",
        description="alias-gateway 终端管理界面（/panel/* 接口的客户端）",
    )
    parser.add_argument("--url", default=None,
                        help="网关地址，例如 http://127.0.0.1:8789")
    parser.add_argument("--port", type=int, default=None,
                        help="网关端口（默认读本机 config.json 的 listen_port）")
    parser.add_argument("-p", "--password", default=None,
                        help="面板密码（不给会交互输入）")
    return parser.parse_args(argv)


def _port_from_config():
    """尝试从本机 config.json 读监听端口，读不到返回 None。"""
    base = os.path.dirname(os.path.abspath(__file__))
    try:
        with open(os.path.join(base, "config.json"), "r", encoding="utf-8") as fp:
            data = json.load(fp)
        return int(data.get("listen_port") or 0) or None
    except (OSError, ValueError, TypeError):
        return None


def resolve_url(args):
    """决定要连哪个网关：显式 --url 优先，其次 --port，再其次配置文件。"""
    if args.url:
        return args.url
    port = args.port or _port_from_config() or DEFAULT_PORT
    return "http://127.0.0.1:%d" % port


def main(argv=None):
    args = parse_args(argv)
    client = PanelClient(resolve_url(args))

    print(title("alias-gateway 终端管理"))
    print(dim("  目标 %s" % client.base_url))

    try:
        client.health()
    except OfflineError as exc:
        print(bad("  %s" % exc))
        print(dim("  请先启动网关：python3 app.py"))
        return 1
    except PanelError as exc:
        print(bad("  网关响应异常：%s" % exc.message))
        return 1

    if not _authenticate(client, args.password):
        return 1

    # 登录完成后直接清屏进入菜单，不再额外等待一次回车。
    _clear()

    try:
        run(client)
    finally:
        try:
            client.logout()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())