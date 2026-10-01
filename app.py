#!/usr/bin/env python3
"""alias-gateway 入口。

启动顺序：

    1. 解析命令行参数；
    2. 加载配置（不存在则生成空骨架）；
    3. 命令行覆盖 --host / --port，校验后写回配置文件；
    4. 预热：拉一遍所有上游的模型清单做冲突检测，有冲突就打印报告并退出；
    5. 起 HTTP 服务；
    6. 接 SIGTERM / SIGINT 优雅退出。

退出码：

    0  正常退出
    1  运行时错误
    2  配置错误
    3  启动预热发现模型冲突
"""

import argparse
import logging
import os
import signal
import sys
import threading

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from core import config as config_mod  # noqa: E402
from core import logs  # noqa: E402
from core import models as models_mod  # noqa: E402
from core.errors import ConfigError  # noqa: E402
from server import http as http_mod  # noqa: E402
from server.handlers import Gateway  # noqa: E402

DEFAULT_CONFIG_NAME = "config.json"
DEFAULT_LOG_NAME = "gateway.log"

EXIT_OK = 0
EXIT_RUNTIME = 1
EXIT_CONFIG = 2
EXIT_CONFLICT = 3


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog="alias-gateway",
        description="OpenAI 兼容的模型别名映射网关",
    )
    parser.add_argument(
        "--config",
        default=os.path.join(BASE_DIR, DEFAULT_CONFIG_NAME),
        help="配置文件路径（默认：项目根目录的 config.json）",
    )
    parser.add_argument("--host", default=None, help="覆盖监听地址（会写回配置文件）")
    parser.add_argument("--port", type=int, default=None, help="覆盖监听端口（会写回配置文件）")
    parser.add_argument(
        "--log",
        default=os.path.join(BASE_DIR, DEFAULT_LOG_NAME),
        help="日志文件路径（默认：项目根目录的 gateway.log）",
    )
    parser.add_argument(
        "--no-preheat",
        action="store_true",
        help="跳过启动预热（不做上游模型冲突检测）",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="日志级别",
    )
    return parser.parse_args(argv)


def preheat(store, logger):
    """启动预热：拉所有上游的模型清单并做冲突检测。

    返回 ``(ok, model_map)``。有冲突时 ok=False，报告已经打印到 stderr。

    上游全挂不算失败 —— 这时候没有清单可比，直接放行，让服务起来，
    总比因为上游临时不可用就拒绝启动要好。
    """
    routes = store.routes()
    if not routes:
        logger.warning("no routes configured, skipping preheat")
        return True, {}

    model_map = {}
    reachable = 0
    for route in routes:
        key = route.get("client_key")
        per_upstream, errors = models_mod.collect_models(route)
        model_map[key] = per_upstream
        reachable += len(per_upstream)
        for name, message in (errors or {}).items():
            logger.warning("preheat: upstream '%s' unreachable: %s", name, message)

    if reachable == 0:
        logger.warning("preheat: no upstream reachable, conflict check skipped")
        return True, model_map

    conflicts = config_mod.detect_conflicts(store.snapshot(), model_map)
    if conflicts:
        report = config_mod.conflict_report(conflicts)
        sys.stderr.write(report + "\n")
        sys.stderr.flush()
        logger.error("startup aborted: %d model conflict(s)", len(conflicts))
        return False, model_map

    logger.info("preheat ok: %d route(s) checked, no conflict", len(routes))
    return True, model_map


def main(argv=None):
    args = parse_args(argv)
    config_path = os.path.abspath(args.config)
    log_path = os.path.abspath(args.log) if args.log else None
    level = getattr(logging, args.log_level, logging.INFO)

    logs.setup(log_path=log_path, level=level)
    logger = logs.get_logger("system")

    # ---- 配置 ----
    try:
        store = config_mod.ConfigStore.load(config_path)
    except ConfigError as exc:
        sys.stderr.write("[FATAL] %s\n" % exc)
        return EXIT_CONFIG

    if store.created:
        for line in config_mod.init_messages(store):
            sys.stdout.write(line + "\n")
        sys.stdout.flush()
        logger.info("config.json created at %s", config_path)

    errors = store.validate()
    if errors:
        sys.stderr.write("[FATAL] config invalid:\n  - " + "\n  - ".join(errors) + "\n")
        return EXIT_CONFIG

    # 命令行覆盖要落盘，否则 tui.py 不带参数时仍按 config.json 里的旧端口去连，
    # 换了端口就连不上了。
    changed, override_errors = store.apply_overrides(
        listen_host=args.host,
        listen_port=int(args.port) if args.port else None,
    )
    if override_errors:
        sys.stderr.write("[FATAL] config invalid:\n  - " + "\n  - ".join(override_errors) + "\n")
        return EXIT_CONFIG
    if changed:
        logger.info("command line override persisted to %s", config_path)

    host = store.config.get("listen_host") or config_mod.DEFAULT_HOST
    port = int(store.config.get("listen_port") or config_mod.DEFAULT_PORT)

    if not args.no_preheat:
        ok, _ = preheat(store, logger)
        if not ok:
            return EXIT_CONFLICT

    gateway = Gateway(store, log_path=log_path)
    gateway.log.info("alias-gateway starting (config=%s)", config_path)

    try:
        server = http_mod.build_server(gateway, host, port)
    except OSError as exc:
        sys.stderr.write("[FATAL] cannot bind %s:%d - %s\n" % (host, port, exc))
        return EXIT_CONFIG

    # ---- 优雅退出 ----
    shutdown_event = threading.Event()

    def _handle_signal(signum, _frame):
        logger.info("received signal %s, shutting down gracefully", signum)
        shutdown_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _handle_signal)
        except (ValueError, OSError):
            pass

    gateway.log.info("api: http://%s:%d/v1 | panel: python3 tui.py", host, port)

    try:
        http_mod.serve_forever(server, shutdown_event)
    except Exception as exc:
        logger.error("server crashed: %s", exc)
        return EXIT_RUNTIME

    logger.info("bye")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())