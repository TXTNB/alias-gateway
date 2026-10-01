# alias-gateway

一个 **OpenAI 兼容的模型别名映射网关**：用一个客户端 Key 聚合多个上游，
把上游的真实模型名映射成你自己起的名字，对外暴露统一的 `/v1/models`
与 `/v1/chat/completions` / `/v1/responses`。

- 后端：**纯 Python 3.9+ 标准库**（`http.server` / `urllib.request` / `ssl` / `hashlib` / `hmac` / `secrets` / `threading` / `logging`），无第三方依赖。
- 管理界面：**纯终端**（`tui.py`），无 Web 前端、无框架、无构建步骤。
- 支持 **SSE 流式透传**、多上游故障切换、白/黑名单、启动预热冲突检测。

---

## 目录结构

```
alias-gateway/
├── app.py                  # 入口：参数解析、配置加载、预热、起服务、优雅退出
├── config.example.json     # 配置示例（空骨架，字段说明见下）
├── README.md
├── core/                   # 纯逻辑，不碰网络 IO 之外的东西
│   ├── __init__.py
│   ├── alias.py            # 别名双向替换（客户端名 <-> 上游真名）
│   ├── auth.py             # 管理密码哈希 + 内存会话表
│   ├── config.py           # 配置加载/校验/归一化/首次生成/打码/合并
│   ├── errors.py           # 错误类型 + OpenAI 风格错误体 + 冲突报告
│   ├── logs.py             # 日志格式化（文件 + 内存环形缓冲）
│   ├── models.py           # /v1/models 的拉取、改名、合并、去重、过滤
│   ├── router.py           # Key -> route -> 候选上游 的选择逻辑
│   └── upstream.py         # 真正发 HTTP 请求（urllib）
├── server/                 # HTTP 层
│   ├── __init__.py
│   ├── handlers.py         # Gateway：所有业务处理函数，返回 Response
│   ├── http.py             # ThreadingHTTPServer + 路由分发 + chunked 流式
│   └── sse.py              # SSE 解析与转发辅助
├── tests/                  # 标准库 unittest，不联网
│   ├── __init__.py
│   ├── test_alias.py
│   ├── test_auth.py
│   ├── test_config.py
│   ├── test_models.py
│   ├── test_router.py
│   └── test_server.py      # server 层冒烟 + SSE 改名 + 面板逻辑
├── .e2e/                   # 端到端验收 / 本地联调（仅本地用）
│   ├── fake_upstream.py    # 假上游：模拟一个 OpenAI 兼容服务
│   ├── acceptance.py       # 端到端验收（起假上游 + 真网关，逐条核对清单）
│   ├── make_test_config.py # 生成 config.test.json（缺失时 dev.sh 会自动调用）
│   ├── config.test.json    # 配套的联调配置（可由上面那个脚本重新生成）
│   └── dev.sh              # 一键起「假上游 + 网关」并冒烟
└── tui.py                  # 终端管理界面（/panel/* 接口的客户端）
```

运行时生成（不在仓库里）：

```
config.json     # 首次启动自动创建
gateway.log     # 日志文件
```

---

## 快速开始

```bash
cd alias-gateway
python3 app.py
```

首次启动会自动生成一份**空骨架** `config.json`，并在终端打印初始化信息：

```
[init] config.json created (empty skeleton, no upstream)
[init] panel not registered yet, run: python3 tui.py (it will ask you to set a password)
[init] no route configured yet, add one in the TUI (menu [3] config edit)
```

空骨架长这样——除了监听地址和一个空的管理密码哈希，**没有任何预填数据**：

```json
{
  "listen_port": 8789,
  "listen_host": "127.0.0.1",
  "panel_password_hash": "",
  "routes": []
}
```

`routes` 为空是**合法状态**：服务照常起来，只是还没有可用的客户端 Key，
等你加了一条路由才有。

随后启动终端管理界面：

```
python3 tui.py
```

> `tui.py` 默认连本机 `config.json` 里的 `listen_port`。要管远端网关用
> `--url http://host:8789`，要换端口用 `--port 8790`。
> 用 `app.py --port 8790` 换端口时新端口会**写回** `config.json`，
> 所以之后 `tui.py` 不带参数也能找到网关。

**首次进入是注册流程**：没有内置默认密码，`tui.py` 会先让你设置管理密码
（至少 4 位，输两遍），设置成功后自动登录；之后再进来就是正常的登录。
注册接口在已注册后会被拒绝，重复注册不会覆盖原密码。

登录后第一件事是加一条自己的路由：菜单 `[3] 配置编辑` → 输入 `n`，
表单分**两段**：**① 上游列表**（可以挂**多个**上游，`a` 添加 / 编号修改 / `d` 删除 /
回车结束本段）→ **② 路由信息**（别名、首选、黑白名单）。
客户端 Key 留空、由服务端自动生成。
修改已有路由就在同一界面输入它的编号，**新增与修改用的是同一套表单**
（回车保留原值，输入 `-` 清空，输入 `q` 放弃）。

> 一条路由挂多个上游时，请求会按 `prefer` 优先、配置顺序兜底依次尝试，
> 某个上游挂了自动切下一个。若两个上游提供**同名模型**，保存后会立刻提示
> 冲突（用 `prefer` 指定归属即可消解）。

> 界面是全屏重绘的：**每个菜单动作都先清屏**，结果独占一个结果页，
> 结果底部显示「按回车返回菜单」；回车后再清屏回到菜单，不会一直往下滚。
> 报错也使用同一个结果页暂停，不会被旧菜单覆盖。
> 配置查看也是逐字段摊开的人读格式，**不打印 JSON**；要看 JSON 原文走菜单 `[8]` 导出。
> 每个上游下面还会列出它**实际提供**的模型（问上游要的清单，不是配置里的字段），
> 上游不可达时显示原因，同名冲突就地提示。
> 有冲突先走冲突修复；没冲突则列出"被多个上游提供"的模型，可直接改它采用哪一个，
> 改完重新渲染并标出「某模型采用某上游的模型（地址）」。
> 映射类字段（别名 / 首选）统一用 `键 = 值` 的写法，长模型名不会挤在一起。

### 命令行参数

| 参数 | 说明 |
| --- | --- |
| `--config PATH` | 配置文件路径（默认：项目根目录 `config.json`） |
| `--host HOST` | 覆盖监听地址（会写回配置文件） |
| `--port PORT` | 覆盖监听端口（会写回配置文件） |
| `--log PATH` | 日志文件路径（默认：项目根目录 `gateway.log`） |
| `--log-level LEVEL` | `DEBUG` / `INFO` / `WARNING` / `ERROR`，默认 `INFO` |
| `--no-preheat` | 跳过启动预热（不做上游模型冲突检测） |

### 退出码

| 码 | 含义 |
| --- | --- |
| `0` | 正常退出 |
| `1` | 运行时异常 |
| `2` | 配置非法 / 端口无法绑定 |
| `3` | 启动预热发现模型冲突 |

---

## 配置说明

`config.json` 的完整结构：

```json
{
  "listen_port": 8789,
  "listen_host": "127.0.0.1",
  "panel_password_hash": "",

  "routes": [
    {
      "client_key": "sk-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",

      "upstreams": [
        {
          "name": "upstream-1",
          "base_url": "https://你的上游地址/v1",
          "auth": { "type": "bearer", "key": "上游的Key" },
          "extra_headers": {},
          "timeout": 300
        }
      ],

      "aliases": {
        "客户端模型名": "上游真实模型名"
      },

      "prefer": {
        "上游真实模型名": "优先用哪个上游的name"
      },

      "whitelist": [],
      "blacklist": []
    }
  ]
}
```

### 字段说明

| 字段 | 说明 |
| --- | --- |
| `listen_host` / `listen_port` | 监听地址与端口，默认 `127.0.0.1:8789` |
| `panel_password_hash` | 管理密码哈希，格式 `pbkdf2_sha256$迭代数$盐$摘要`。**空字符串表示尚未注册**，首次进入 `tui.py` 时设置；之后改密码用菜单 `[7]`，不要手写 |
| `routes[].client_key` | 客户端 Key，客户端用 `Authorization: Bearer <client_key>` 调用。默认自动生成 `sk-` + 64 位十六进制（共 67 字符）。**它是明文，管理界面里也不打码**（要复制给客户端用） |
| `upstreams[].name` | 上游标识，同一 route 内唯一，`prefer` 里用它指代上游 |
| `upstreams[].base_url` | 上游 API 根地址，**必须以 `/v1` 结尾** |
| `upstreams[].auth.type` | `bearer` / `header` / `none`。`bearer` 与 `header` 必须填 `key` |
| `upstreams[].auth.key` | 上游 Key |
| `upstreams[].auth.header` | `type=header` 时的自定义头名，默认 `Authorization` |
| `upstreams[].extra_headers` | 额外请求头，逐条透传给上游 |
| `upstreams[].timeout` | 单次请求超时秒数，正整数，默认 `300` |
| `aliases` | `{客户端可见名: 上游真实名}`，双向生效 |
| `prefer` | `{上游真实模型名: 上游 name}`，用来消解同名冲突 |
| `whitelist` | 非空时**只保留**列出的客户端可见模型名 |
| `blacklist` | 非空时**排除**列出的客户端可见模型名 |

> 手写配置时可参考 `config.example.json`（空骨架，字段说明见上表）；
> 但注意首次启动**不**从它拷贝，而是用内置默认配置生成同样空骨架的 `config.json`。

---

## 别名与冲突

### 双向替换

`aliases` 是 `{客户端名: 上游真名}`：

- **请求方向**：客户端发 `deepseek-flash` → 转发给上游时改写成 `deepseek-v4.1-flash`。
- **响应方向**：上游返回 `deepseek-v4.1-flash` → 回给客户端时改写成 `deepseek-flash`。

不在表里的名字原样透传。若两个客户端名指向同一个上游真名（多对一），
响应方向取表里第一个出现的键，结果稳定。

### 同名冲突

当**同一个客户端可见名被两个及以上不同上游提供**时，就是一个冲突：

- 启动预热会拉一遍所有上游的模型清单，发现冲突就打印一份 FATAL 报告
  （列出每个冲突模型、出现在哪些上游、以及修法），然后以退出码 `3` 退出。
- 请求 `/v1/models` 时同样会检测，冲突模型**不会**出现在返回列表里。
- 请求转发时若命中冲突模型，返回 `500 model_conflict_error`。
- **管理界面保存配置后会立刻提示**：只要这条路由挂了多个上游，就会拉一次
  清单并把冲突与不可达上游打出来，不用等客户端拉清单才发现少了模型。
- **模型清单里可就地修**：`[4] 模型清单` 检测到冲突后会逐个列出涉及的上游，
  输入编号（或上游标识）选定归属，当场写进 `prefer` 并保存；跳过的冲突会
  明确报告后果（请求返回 500、重启预热退出码 3）。

修法：在 `prefer` 里指定用哪个上游：

```json
"prefer": { "deepseek-v4.1-flash": "upstream-1" }
```

`prefer` 的**键是上游真实模型名**，**值是上游的 `name`**。
指定后该模型不再算冲突，且转发时优先走这个上游。

---

## 客户端调用

```bash
# 列出模型
curl http://127.0.0.1:8789/v1/models \
  -H "Authorization: Bearer sk-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"

# 对话（流式）
curl -N http://127.0.0.1:8789/v1/chat/completions \
  -H "Authorization: Bearer sk-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx" \
  -H "Content-Type: application/json" \
  -d '{
        "model": "deepseek-flash",
        "stream": true,
        "messages": [{"role": "user", "content": "你好"}]
      }'
```

### 状态码约定

| 码 | 场景 |
| --- | --- |
| `200` | 正常（流式时为 chunked） |
| `400` | 模型不在暴露列表里 / 请求体非法 |
| `401` | 客户端 Key 缺失或错误 |
| `500` | 模型冲突未用 `prefer` 解决 |
| `502` | 全部候选上游都连不上 |
| 其他 | 上游非 2xx 时**原样透传**状态码与响应体 |

---

## 终端管理界面

管理界面是纯终端的 `tui.py`，它**不直接读写 config.json**，而是作为 `/panel/*`
接口的客户端工作：

```
tui.py  --HTTP-->  /panel/*  -->  Gateway
```

这样做的好处是复用同一套鉴权、打码与配置合并逻辑（"打码的 Key 提交回来要
保留原值"这类规则只实现一次），而且它既能管本机网关，也能管远端网关。

```bash
python3 tui.py                          # 本机（端口读 config.json）；首次进入会要求注册
python3 tui.py --port 8790              # 换端口
python3 tui.py --url http://host:8789   # 管远端网关
python3 tui.py -p 你的密码               # 直接带密码登录（未注册则改走注册）
```

菜单：

| 菜单 | 能力 |
| --- | --- |
| `[1]` 总览 | 版本、运行时长、监听地址、路由/上游/别名数量、配置文件与日志路径 |
| `[2]` 配置查看 | 逐字段摊开的人读格式（**不是 JSON**）：上游 Key 已打码，客户端 Key 是明文；每个上游下列出它实际提供的模型，冲突可修、模型归属（采用哪个上游）可直接改 |
| `[3]` 配置编辑 | 新增与修改同一套表单：输入编号改那条 / `n` 新建 / `d` 删除 / 回车返回。表单分两段：**① 上游列表**（可挂多个上游：`a` 添加 / 编号修改 / `d` 删除 / 回车结束本段）→ **② 路由信息**（别名 / 首选 / 黑白名单） |
| `[4]` 模型清单 | 每条路由的模型、来源上游、原名溯源、冲突与不可达提示（每次都强制刷新，不吃缓存）；发现同名冲突时就地选归属写入 `prefer` |
| `[5]` 上游测试 | 并发探测所有上游，显示状态码与延迟 |
| `[6]` 日志 | 按级别 / 模块 / 关键词过滤，可选清空缓冲 |
| `[7]` 改密码 | 改完踢掉所有会话，需重新登录 |
| `[8]` 导出配置 | 导出**明文**配置（含真实 Key），用于备份迁移 |
| `[9]` 导入配置 | 从 JSON 文件整体覆盖配置 |

- 登录后 token 只存在进程内存里，请求头 `X-Panel-Token`，**8 小时**过期，服务重启即失效。
- 配置编辑提交的是**配置副本**：上游 Key 没动过会被服务端按打码值自动回填原值，
  不会把 `••••••••` 写进配置；客户端 Key 留空则回填原值（它是明文，改就直接改）。
- 界面输出在真终端下会自动清屏重绘；输出被重定向（管道、文件）时不清屏，
  也不会插入 ANSI 转义，方便脚本抓取。
- **每个菜单结果都等回车**：进入菜单后先清屏，结果单独显示；底部按一次回车，
  再清屏返回菜单。报错同样停留在结果页，避免旧菜单覆盖错误信息。
- `/panel/register` 只在**未注册**时可用，成功后直接返回会话；已注册后返回 `400`。

> `/panel/*` 默认只监听 `127.0.0.1`。若要跨机管理，请通过 Nginx 加 HTTPS
> 与访问控制，不要直接把管理端口暴露到公网。

---

## 日志

日志同时写入文件与内存环形缓冲（保留最近 2000 条，供管理界面读取）。

格式：

```
2026-10-01 10:30:00 | INFO  | [chat]     | key=sk-abc | deepseek-flash → deepseek-v4.1-flash | upstream=upstream-1 | 200 | 331ms
```

模块标签：`chat` / `models` / `upstream` / `auth` / `config` / `system` / `http`。

---

## 部署

### systemd

`/etc/systemd/system/alias-gateway.service`：

```ini
[Unit]
Description=alias-gateway (OpenAI compatible model alias gateway)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=wbgateway
Group=wbgateway
WorkingDirectory=/opt/alias-gateway
ExecStart=/usr/bin/python3 /opt/alias-gateway/app.py \
    --config /opt/alias-gateway/config.json \
    --log /var/log/alias-gateway/gateway.log \
    --log-level INFO
Restart=on-failure
RestartSec=3

# 只监听本机，由 Nginx 对外
# 如需直连可改成 0.0.0.0 并自行加防火墙
Environment=PYTHONUNBUFFERED=1

# 最小权限
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/opt/alias-gateway /var/log/alias-gateway

[Install]
WantedBy=multi-user.target
```

启用：

```bash
sudo mkdir -p /var/log/alias-gateway
sudo chown wbgateway:wbgateway /var/log/alias-gateway
sudo systemctl daemon-reload
sudo systemctl enable --now alias-gateway
sudo systemctl status alias-gateway
```

> 退出码 `3`（模型冲突）也会触发 `Restart=on-failure` 重启循环。
> 若希望冲突时**停住不动**、等人工处理，把 `Restart` 改成 `on-abnormal`。

### Nginx 反向代理

对外暴露时，**必须**关掉缓冲，否则 SSE 流式会被攒成一次性返回：

```nginx
server {
    listen 443 ssl http2;
    server_name gateway.example.com;

    ssl_certificate     /etc/letsencrypt/live/gateway.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/gateway.example.com/privkey.pem;

    # 客户端请求体可能很大（长上下文）
    client_max_body_size 64m;

    location / {
        proxy_pass http://127.0.0.1:8789;
        proxy_http_version 1.1;

        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # SSE / 流式必须关缓冲
        proxy_buffering off;
        proxy_cache off;
        proxy_request_buffering off;

        # 长连接与长响应
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
        chunked_transfer_encoding on;
    }

    # 如果只想把 API 暴露到公网、管理接口留在内网：
    # location /panel/ { allow 10.0.0.0/8; deny all; proxy_pass http://127.0.0.1:8789; }
}
```

HTTP 跳 HTTPS（可选）：

```nginx
server {
    listen 80;
    server_name gateway.example.com;
    return 301 https://$host$request_uri;
}
```

#### 配置示例（多上游 + 别名 + prefer）

```json
{
  "listen_port": 8789,
  "listen_host": "127.0.0.1",
  "panel_password_hash": "",
  "routes": [
    {
      "client_key": "sk-pleaseReplaceMe1",
      "upstreams": [
        {
          "name": "primary",
          "base_url": "https://api-a.example.com/v1",
          "auth": { "type": "bearer", "key": "sk-a-xxxx" },
          "extra_headers": { "X-Trace": "gateway" },
          "timeout": 300
        },
        {
          "name": "backup",
          "base_url": "https://api-b.example.com/v1",
          "auth": { "type": "bearer", "key": "sk-b-yyyy" },
          "extra_headers": {},
          "timeout": 300
        }
      ],
      "aliases": {
        "deepseek-flash": "deepseek-v4.1-flash",
        "deepseek-pro": "deepseek-v4.1-pro"
      },
      "prefer": {
        "deepseek-v4.1-flash": "primary"
      },
      "whitelist": [],
      "blacklist": ["gpt-6-astra"]
    }
  ]
}
```

这条路由挂了 `primary` 与 `backup` 两个上游：`deepseek-v4.1-flash` 由
`prefer` 指定优先走 `primary`，`primary` 连不上时自动切到 `backup`；
其余模型按配置顺序（`primary` → `backup`）依次尝试。
两个上游若同时提供某个未在 `prefer` 里指定的同名模型，就算冲突。

---

## 测试

全部是标准库 `unittest`，**不联网**（网络调用以打桩方式注入）：

```bash
cd alias-gateway
python3 -m unittest discover -s tests -v
```

或单独跑：

```bash
python3 -m unittest tests.test_alias -v
python3 -m unittest tests.test_auth -v
python3 -m unittest tests.test_config -v
python3 -m unittest tests.test_models -v
python3 -m unittest tests.test_router -v
python3 -m unittest tests.test_server -v
```

### 本地联调（不起真上游也能跑）

想先看看网关长什么样、又不想接真上游，用假上游 + 联调脚本：

```bash
cd alias-gateway
bash .e2e/dev.sh          # 起假上游 + 起网关，前台运行，Ctrl+C 退出
bash .e2e/dev.sh smoke    # 起 -> 打一遍冒烟测试 -> 自动关掉
```

`dev.sh` 会打印管理界面入口、客户端 Base URL 与 API Key：

| 项 | 值 |
| --- | --- |
| 管理界面 | 终端里运行 `python3 tui.py --port 8790`（测试配置里密码已是 `admin`） |
| 客户端 Base URL | `http://127.0.0.1:8790/v1` |
| 客户端 API Key | `sk-local-test-0001` |
| 假上游端口 | `9911`（`fake`）与 `9912`（`backup`），`UP_PORT` / `BACKUP_PORT` 可覆盖 |
| 网关端口 | `8790`（`GW_PORT` 可覆盖） |

联调配置是一条路由挂**两个**上游，并**故意留了一个未解决的同名冲突**
（`shared-model` 两边都提供、`prefer` 为空），用来验证冲突提示与 `prefer` 修复流程；
因此 `dev.sh` 起网关时带 `--no-preheat`，否则预热会以退出码 3 拒绝启动。

也可以自己分两个终端手动起：

```bash
# 终端 1：两个假上游
python3 .e2e/fake_upstream.py --port 9911 --label fake
python3 .e2e/fake_upstream.py --port 9912 --label backup --models "shared-model,backup-only"

# 终端 2：网关（用配套的 .e2e/config.test.json，它已指向 9911/9912）
python3 app.py --config .e2e/config.test.json --no-preheat
```

然后按上面表格里的地址调 `/v1/models`、`/v1/chat/completions`，或运行 `python3 tui.py --port 8790` 打开终端管理界面。
假上游自带的模型是 `deepseek-v4.1-flash`、`shared-model`、`secret-model`，
其中 `deepseek-v4.1-flash` 在示例配置里被改名成了 `deepseek-flash`；
`shared-model` 被两个上游同时提供，正是那个故意留的冲突。

> 假上游只认 `127.0.0.1`，仅用于本机验证，**不要**拿它对外提供服务。

### 端到端验收

`.e2e/acceptance.py` 会真起两个假上游 + 真起网关，逐条核对下面的验收清单：

```bash
cd alias-gateway
python3 .e2e/acceptance.py
```

它只使用本机回环地址与临时目录，跑完自动清理，不依赖外网。

---

## 验收自检清单

1. 首次启动自动生成**空骨架** `config.json`（`routes: []`，无预填上游、无占位域名），并打印三行 `[init]` 信息；`panel_password_hash` 为空表示未注册。
2. 管理密码以哈希存储（不是明文）；首次进入 `tui.py` 走注册流程设置密码，注册后 `POST /panel/register` 返回 `400`。
3. `GET /v1/models` 会合并所有上游的模型并按 `aliases` 改名。
4. 请求 / 响应两个方向的别名替换都生效，未配置的原样透传。
5. 首选上游失败时自动切换下一个候选上游。
6. 同名冲突未配 `prefer` 时：预热退出码 `3`、`/v1/models` 不含该模型、请求返回 `500`；
   在 `[4] 模型清单` 里选归属可当场写进 `prefer` 并消解。
7. 配了 `prefer` 后冲突消失，转发走指定上游。
8. `whitelist` 非空时只暴露列出的模型。
9. 客户端 Key 形如 `sk-` + 64 位十六进制，管理界面里明文可见；上游 Key 始终打码。
10. `blacklist` 排除列出的模型。
11. `stream: true` 时 SSE 逐块透传，不缓冲。
12. 错误的客户端 Key 返回 `401`。
13. 请求被排除的模型返回 `400`。
14. 管理界面为纯终端 `tui.py`，覆盖首次注册、总览、配置查看、配置编辑（新增/修改/删除同一套表单）、导入导出、模型清单、上游测试、日志、改密码；界面清屏重绘（**报错时不清屏**），配置查看不打印 JSON 且在每个上游下列出实际模型。
15. 代码按模块拆分，无单文件巨石。
