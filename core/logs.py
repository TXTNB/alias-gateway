"""日志：文件输出 + 内存环形缓冲。

管理界面要能读到最近的日志，所以除了写文件，还在内存里留一份结构化副本
（:class:`RingBufferHandler`）。日志行格式固定：

    时间 | 级别 | [模块] | 消息

模块标签由 :func:`get_logger` 传入，渲染成 ``[chat]``、``[upstream]`` 这类前缀，
管理界面就靠它做过滤。
"""

import collections
import logging
import os
import threading
import time

LOGGER_NAME = "gateway"
RING_CAPACITY = 2000
MODULE_WIDTH = 10
LEVEL_WIDTH = 5

_active_path = ""


class GatewayFormatter(logging.Formatter):
    """把日志渲染成 ``时间 | 级别 | [模块] | 消息``。"""

    def format(self, record):
        ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(record.created))
        level = record.levelname.ljust(LEVEL_WIDTH)
        module = str(getattr(record, "module_tag", record.name))
        try:
            message = record.getMessage()
        except Exception:
            message = str(record.msg)
        text = "%s | %s | [%s] | %s" % (ts, level, module.ljust(MODULE_WIDTH), message)
        if record.exc_info:
            text += "\n" + self.formatException(record.exc_info)
        return text


class RingBufferHandler(logging.Handler):
    """在内存里保留最近 ``capacity`` 条结构化日志，供管理端读取。"""

    def __init__(self, capacity=RING_CAPACITY):
        super().__init__()
        self.capacity = int(capacity)
        self._buffer = collections.deque(maxlen=self.capacity)
        self._lock = threading.Lock()

    def emit(self, record):
        # 日志本身不能把进程搞崩：格式化失败就丢弃这一条。
        try:
            entry = {
                "ts": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(record.created)),
                "level": record.levelname,
                "module": str(getattr(record, "module_tag", record.name)),
                "message": record.getMessage(),
            }
        except Exception:
            return
        with self._lock:
            self._buffer.append(entry)

    def snapshot(self):
        """取一份缓冲副本，避免调用方拿着内部 deque。"""
        with self._lock:
            return list(self._buffer)

    def clear(self):
        with self._lock:
            self._buffer.clear()


class TaggedLogger(logging.LoggerAdapter):
    """给每条日志挂上模块标签，Formatter 靠它渲染 ``[chat]``。"""

    def process(self, msg, kwargs):
        extra = dict(kwargs.get("extra") or {})
        extra["module_tag"] = self.extra.get("tag", LOGGER_NAME)
        kwargs["extra"] = extra
        return msg, kwargs


def setup(log_path=None, level=logging.INFO, capacity=RING_CAPACITY, console=True):
    """初始化根 logger，返回环形缓冲句柄。

    重复调用会先摘掉旧 handler，方便测试里反复初始化而不重复输出。
    写不了日志文件也不影响服务运行，内存缓冲照常工作。
    """
    global _active_path
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(level)
    logger.propagate = False
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        try:
            handler.close()
        except Exception:
            pass

    ring = RingBufferHandler(capacity)
    ring.setFormatter(GatewayFormatter())
    logger.addHandler(ring)

    _active_path = os.path.abspath(log_path) if log_path else ""

    if log_path:
        try:
            folder = os.path.dirname(os.path.abspath(log_path))
            if folder:
                os.makedirs(folder, exist_ok=True)
            file_handler = logging.FileHandler(log_path, encoding="utf-8")
            file_handler.setFormatter(GatewayFormatter())
            logger.addHandler(file_handler)
        except OSError:
            pass

    if console:
        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(GatewayFormatter())
        logger.addHandler(stream_handler)

    return ring


def get_logger(tag):
    """取一个带模块标签的 logger，例如 ``get_logger("chat")``。"""
    return TaggedLogger(logging.getLogger(LOGGER_NAME), {"tag": tag})