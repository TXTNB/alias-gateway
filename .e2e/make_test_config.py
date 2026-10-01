"""生成 .e2e/config.test.json —— 本地联调用的测试配置。

只用于 `.e2e/dev.sh` 的冒烟：一条路由挂**两个**假上游（:9911 与 :9912），
覆盖"多上游合并"与"同名模型冲突提示"这两条路径。
客户端 Key 固定 `sk-local-test-0001`，管理密码固定 `admin`（已哈希）。

两个上游都会提供 `shared-model` 且 **prefer 为空** —— 这是故意留的冲突，
用来验证配置查看 / 模型清单里会打出冲突提示、该模型不会出现在可见列表里。
因此 dev.sh 必须带 `--no-preheat` 启动，否则预热会以退出码 3 拒绝启动。

端口 8789 会被 dev.sh 用 sed 换成实际端口，所以这里必须原样写
`"listen_port": 8789`。

用法：
    python3 .e2e/make_test_config.py [输出路径]
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import auth

CLIENT_KEY = "sk-local-test-0001"
PANEL_PASSWORD = "admin"
UPSTREAM_PORT = 9911
BACKUP_PORT = 9912

# 两个假上游都会提供它，用来**制造一个真实冲突**：不在 prefer 里指定归属，
# 于是它会从可见清单里消失，配置查看与模型清单里都会打出冲突提示。
# （dev.sh 因此带 --no-preheat 启动，否则预热会直接退出码 3）
CONFLICT_MODEL = "shared-model"

# 只有 fake 提供，用来验证多上游是"合并"而不是"覆盖"
FAKE_ONLY_MODEL = "deepseek-v4.1-flash"


def _upstream(name, port):
    """一条指向本机假上游的上游配置。"""
    return {
        "name": name,
        "base_url": "http://127.0.0.1:%d/v1" % port,
        "auth": {"type": "none", "key": ""},
        "extra_headers": {},
        "timeout": 30,
    }


def build_config():
    """拼出模板配置；密码每次重新哈希，所以输出不会逐字节稳定。"""
    return {
        "listen_port": 8789,
        "listen_host": "127.0.0.1",
        "panel_password_hash": auth.hash_password(PANEL_PASSWORD),
        "routes": [{
            "client_key": CLIENT_KEY,
            "upstreams": [
                _upstream("fake", UPSTREAM_PORT),
                _upstream("backup", BACKUP_PORT),
            ],
            "aliases": {"deepseek-flash": FAKE_ONLY_MODEL},
            "prefer": {},
            "whitelist": [],
            "blacklist": [],
        }],
    }


def main(argv):
    """写配置到目标路径（默认写回本文件旁边）。"""
    target = argv[1] if len(argv) > 1 else os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "config.test.json")
    with open(target, "w", encoding="utf-8") as fp:
        json.dump(build_config(), fp, ensure_ascii=False, indent=2)
        fp.write("\n")
    sys.stderr.write("wrote %s\n" % target)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))