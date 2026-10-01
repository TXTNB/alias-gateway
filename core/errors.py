"""网关的错误类型，以及冲突报告的两套渲染方式。

每种错误自带 HTTP 状态码与 OpenAI 风格的 ``type`` 字段，
:meth:`GatewayError.payload` 直接产出可以塞进响应体的结构。

冲突报告有两条路径，格式不一样：

* 启动预热用 :func:`format_conflict_report` —— 多行报告打到 stderr，进程退出；
* 请求过程中用 :func:`format_conflict_message` —— 压成一行塞进 ``error.message``。
"""


class GatewayError(Exception):
    """所有网关自有异常的基类，携带 HTTP 状态码与错误类型。"""

    status = 500
    err_type = "server_error"

    def __init__(self, message, status=None, err_type=None):
        super().__init__(message)
        self.message = str(message)
        if status is not None:
            self.status = int(status)
        if err_type is not None:
            self.err_type = err_type

    def payload(self):
        """OpenAI 风格的错误体。"""
        return {"error": {"message": self.message, "type": self.err_type}}


class AuthError(GatewayError):
    """客户端 Key 缺失或不对。"""

    def __init__(self, message="Invalid client key."):
        super().__init__(message, 401, "invalid_request_error")


class PanelAuthError(GatewayError):
    """管理端未登录或会话过期。"""

    def __init__(self, message="管理端未登录或会话已过期"):
        super().__init__(message, 401, "panel_auth_error")


class BadRequestError(GatewayError):
    """请求体非法，或请求的模型不可用。"""

    def __init__(self, message):
        super().__init__(message, 400, "invalid_request_error")


class NotFoundError(GatewayError):
    """路径不存在。"""

    def __init__(self, message="Not found."):
        super().__init__(message, 404, "not_found_error")


class ModelConflictError(GatewayError):
    """请求时发现同名模型冲突，且没有配 prefer。"""

    def __init__(self, message):
        super().__init__(message, 500, "model_conflict_error")


class UpstreamError(GatewayError):
    """上游连不上，或所有候选上游都失败。"""

    def __init__(self, message):
        super().__init__(message, 502, "upstream_error")


class ConfigError(GatewayError):
    """配置文件非法。"""

    def __init__(self, message):
        super().__init__(message, 500, "config_error")


def format_conflict_report(conflicts):
    """把冲突渲染成多行报告（启动预热用）。

    按 route 分组，每条冲突列出模型名、涉及的上游以及可直接照抄的 ``prefer`` 修法。
    """
    routes = []
    for c in conflicts:
        key = c.get("route")
        if key not in routes:
            routes.append(key)

    lines = ["[FATAL] Model conflicts detected during startup preheat.", ""]
    index = 0
    for route_key in routes:
        group = [c for c in conflicts if c.get("route") == route_key]
        lines.append("  Route : %s" % route_key)
        lines.append("")
        for c in group:
            index += 1
            found = c.get("found_in") or []
            lines.append("  [%d] Model : %s" % (index, c.get("model")))
            if c.get("client_name"):
                lines.append("      Client : %s" % c["client_name"])
            lines.append("      Found in:")
            for f in found:
                label = "'%s'" % f.get("name")
                lines.append("        - upstream %s %s" % (label.ljust(11), f.get("base_url")))
            fix_owner = found[0].get("name") if found else "<upstream-name>"
            lines.append('      Fix: "prefer": {"%s": "%s"}' % (c.get("model"), fix_owner))
            lines.append("")

    total = len(conflicts)
    lines.append(
        "  Total: %d conflict%s in %d route%s."
        % (total, "" if total == 1 else "s", len(routes), "" if len(routes) == 1 else "s")
    )
    lines.append("")
    lines.append("  Exit.")
    return "\n".join(lines)


def format_conflict_message(conflicts, route_key):
    """把冲突压成一行（请求时用），直接作为 ``error.message`` 返回。"""
    parts = []
    for c in conflicts:
        names = ", ".join([f.get("name") or "?" for f in (c.get("found_in") or [])])
        fix_owner = (c.get("found_in") or [{}])[0].get("name") or "<upstream-name>"
        text = (
            "model '%s' is provided by upstreams [%s]; "
            'fix: "prefer": {"%s": "%s"}'
            % (c.get("model"), names, c.get("model"), fix_owner)
        )
        if c.get("client_name"):
            text += " (client name: %s)" % c["client_name"]
        parts.append(text)
    return "Model conflict in route '%s': %s" % (route_key, " | ".join(parts))