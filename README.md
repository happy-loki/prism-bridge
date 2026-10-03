# Prism Bridge

[![LINUX DO](https://img.shields.io/badge/LINUX-DO-FFB003.svg?logo=data:image/svg%2bxml;base64,DQo8c3ZnIHhtbG5zPSJodHRwOi8vd3d3LnczLm9yZy8yMDAwL3N2ZyIgd2lkdGg9IjEwMCIgaGVpZ2h0PSIxMDAiPjxwYXRoIGQ9Ik00Ni44Mi0uMDU1aDYuMjVxMjMuOTY5IDIuMDYyIDM4IDIxLjQyNmM1LjI1OCA3LjY3NiA4LjIxNSAxNi4xNTYgOC44NzUgMjUuNDV2Ni4yNXEtMi4wNjQgMjMuOTY4LTIxLjQzIDM4LTExLjUxMiA3Ljg4NS0yNS40NDUgOC44NzRoLTYuMjVxLTIzLjk3LTIuMDY0LTM4LjAwNC0yMS40M1EuOTcxIDY3LjA1Ni0uMDU0IDUzLjE4di02LjQ3M0MxLjM2MiAzMC43ODEgOC41MDMgMTguMTQ4IDIxLjM3IDguODE3IDI5LjA0NyAzLjU2MiAzNy41MjcuNjA0IDQ2LjgyMS0uMDU2IiBzdHlsZT0ic3Ryb2tlOm5vbmU7ZmlsbC1ydWxlOmV2ZW5vZGQ7ZmlsbDojZWNlY2VjO2ZpbGwtb3BhY2l0eToxIi8+PHBhdGggZD0iTTQ3LjI2NiAyLjk1N3EyMi41My0uNjUgMzcuNzc3IDE1LjczOGE0OS43IDQ5LjcgMCAwIDEgNi44NjcgMTAuMTU3cS00MS45NjQuMjIyLTgzLjkzIDAgOS43NS0xOC42MTYgMzAuMDI0LTI0LjM4N2E2MSA2MSAwIDAgMSA5LjI2Mi0xLjUwOCIgc3R5bGU9InN0cm9rZTpub25lO2ZpbGwtcnVsZTpldmVub2RkO2ZpbGw6IzE5MTkxOTtmaWxsLW9wYWNpdHk6MSIvPjxwYXRoIGQ9Ik03Ljk4IDcwLjkyNmMyNy45NzctLjAzNSA1NS45NTQgMCA4My45My4xMTNRODMuNDI2IDg3LjQ3MyA2Ni4xMyA5NC4wODZxLTE4LjgxIDYuNTQ0LTM2LjgzMi0xLjg5OC0xNC4yMDMtNy4wOS0yMS4zMTctMjEuMjYyIiBzdHlsZT0ic3Ryb2tlOm5vbmU7ZmlsbC1ydWxlOmV2ZW5vZGQ7ZmlsbDojZjlhZjAwO2ZpbGwtb3BhY2l0eToxIi8+PC9zdmc+)](https://linux.do)

把 [Prism](https://prism.openai.com) 变成本机的 OpenAI 兼容接口（Responses / Chat Completions），给 Codex CLI、OMP 以及任何能填 Base URL 的客户端使用。

- 用你自己的 Prism / OpenAI 账号登录。仓库里没有任何账号、Cookie、密钥。
- 非官方工具，与 OpenAI 无关。

```
客户端（Codex CLI / OMP / 任意 OpenAI 兼容客户端）
  → http://127.0.0.1:18765/v1
    → 本机 Playwright 桥（真实 Chromium，由浏览器自己完成 Cloudflare / Sentinel 校验）
      → https://prism.openai.com
```

## 功能

- **一键登录**：弹出浏览器，登录后凭证保存在本机，不用 F12 抓 Cookie。
- **图形界面**：一个窗口里登录、启动/停止服务、看日志；也可以纯命令行使用。
- **两套接口**：`/v1/responses` 和 `/v1/chat/completions`，都支持 SSE 流式。
- **工具调用**：客户端带的工具（`function`、`custom` 自由文本、`namespace` 分组、Codex CLI 的 `additional_tools`）由桥中继给模型，调用结果回到客户端，在**客户端那台机器**上执行，不使用 Prism 自带的云端沙箱。
- **图片输入**：`input_image` / `image_url` 会先上传到 Prism 项目再交给模型。
- **会话延续**：客户端每轮重放全部历史，桥只把新增内容发进同一个 Prism 会话。
- **保活与自愈**：每 5 分钟心跳；页面崩溃自动重载；新账号自动创建工作区。
- **限流等待**：被 Prism 限流时自动等待重发，期间流不断。

## 环境

- Python 3.10+
- 能同时访问 `prism.openai.com` 和 `sentinel.openai.com` 的网络（两个都要通，见[常见问题](#常见问题)）

```bash
pip install -r requirements.txt
playwright install chromium
```

用图形界面时可以跳过第二条：缺少 Chromium 时界面会出现“下载浏览器”按钮。

## 使用

Windows 双击 `prism.cmd`，按数字键选择：

| 菜单 | 等价命令 | 说明 |
| :--- | :--- | :--- |
| `1` 图形界面 | `python gui.py` | 登录、启动/停止服务、日志都在一个窗口 |
| `2` 启动服务 | `python bridge.py serve` | 在控制台窗口里运行服务 |
| `3` 登录 | `python bridge.py login` | 弹出浏览器，登录成功后自动保存凭证并关闭 |
| `4` 查看状态 | `python bridge.py status` | 账号、会话剩余有效期、端口状态 |

Linux / macOS 直接用右边一列的命令。

第一次使用：先登录，再启动服务。启动需要 15–25 秒（唤醒 Chromium 和 Prism 工作区）。登录凭证大约十天过期，过期后重新登录一次。

图形界面的几点行为：

- 关闭窗口会停止服务。
- 服务运行中点“重新登录”，会先停服务，登录结束后自动重新启动（两者共用同一份浏览器数据）。
- 图形界面和控制台方式不能同时运行服务。

## 接入客户端

- Base URL：`http://127.0.0.1:18765/v1`
- API Key：任意非空值（没设 `PRISM_BRIDGE_API_KEY` 时桥不校验）

### Codex CLI

`config.toml`（codex-cli 0.159.2 实测）：

```toml
model = "gpt-6.1-sol"
model_provider = "prism"

[model_providers.prism]
name = "Prism bridge"
base_url = "http://127.0.0.1:18765/v1"
wire_api = "responses"
env_key = "PRISM_BRIDGE_KEY"
```

环境变量 `PRISM_BRIDGE_KEY` 填任意非空值，然后 `codex exec "Run whoami and tell me the output."`。

### OMP

`models.yml`：

```yaml
providers:
  prism:
    baseUrl: http://127.0.0.1:18765/v1
    api: openai-responses
    apiKey: any-non-empty-value
    models:
      - gpt-6.1-sol
      - gpt-5.6-sol
      - gpt-5.6-terra
      - gpt-6-luna
      - auto
```

各模型需要声明 `input: [text, image]`，否则 OMP 会把图片换成占位符。OMP 在流里长时间等不到事件时会切到 fallback 模型；不想被静默换模型，就把这个 provider 的 fallback 链设为空。

## 模型

| Prism 显示名 | 请求用的 ID |
| :--- | :--- |
| 6.1 Sol | `gpt-6.1-sol` |
| 5.6 Sol | `gpt-5.6-sol` |
| 5.6 Terra | `gpt-5.6-terra` |
| 6 Luna | `gpt-6-luna` |
| 兼容别名 | `gpt-6-sol`（转发为 `gpt-6.1-sol`）、`gpt-6-astra` |
| 交给 Prism 决定 | `auto` |

以上是 2026-10 的目录。Prism 网页的模型下拉框由官方远程配置下发，配置没加载出来时网页只显示 5.6 Sol，这不代表其它模型下线。

Prism 拒绝某个模型 ID 时桥直接报错，不会悄悄换成别的模型。设 `PRISM_ALLOW_MODEL_FALLBACK=1` 才会回退到 Prism 默认模型，此时响应里的 `model` 是 `auto`。

## 接口

- `GET /health`：服务状态、用户 ID、凭证过期时间
- `GET /v1/models`：模型列表
- `POST /v1/responses`：OpenAI Responses（SSE、函数调用、`custom` 工具、`namespace`、`additional_tools`）
- `POST /v1/chat/completions`：Chat Completions（SSE、`tool_calls`）

## 工具调用是怎么中继的

Prism 后端只把 `system` 条目和最后一条 `user` 消息交给模型，请求里的 `instructions` 和原生 `tools` 都会被丢弃（2026-10-03 实测）。所以桥把一段短协议和客户端的工具目录放进用户消息开头的 `<relay_instructions>` 里。模型需要工具时输出：

```text
<client_tool_call>
{"name":"bash","arguments":{"command":"whoami"}}
</client_tool_call>
```

桥把它还原成 Responses 的 `function_call`（自由文本工具还原成 `custom_tool_call`），由客户端执行后把结果带回下一轮。能直接回答的问题模型输出纯文本，不走工具。

桥只做传输，不复制客户端的系统提示：超过 2000 字的 `instructions` 默认不转发（`PRISM_FORWARD_CLIENT_INSTRUCTIONS=1` 或单个请求里 `metadata.forward_instructions=1` 可强制转发）。

接入新客户端不工作时，设 `PRISM_DUMP_DIR=<目录>`，桥会把每个请求体、发往上游的内容和解析结果各存一份 JSON（不含 `Authorization`），先看客户端把工具放在了请求的什么位置。

## 已知限制

- **限流**：短时间内连发多轮（实测约 25 秒 5 轮）后，Prism 会返回 `Error while processing conversation (403 Forbidden)`，持续几十秒到十几分钟，密集重试会让它更久。桥按 20 / 40 / 60 秒……等待重发，单个请求最多等 `PRISM_THROTTLE_WAIT` 秒。每次工具调用都是一轮，长的 agent 循环会因此变慢。
- **单轮大小**：Prism 单轮输入约 10 万字符。超出的请求会拆成几轮发进同一个会话；仍然装不下时返回 `context_length_exceeded`，Codex / OMP 会据此压缩历史再重试。
- **聊天列表**：桥创建的会话会出现在你 Prism 项目的聊天列表里，桥不会删除它们。
- **整段返回**：Prism 生成完才给正文，流式输出里先是心跳，最后一次性给出内容。
- **上游会变**：这是对网页接口的适配，Prism 改版后可能失效。
- **没测过的客户端**：Codex 桌面版、Codex 的 `apply_patch`、经 Codex 传图片、Claude Code 等。

## 环境变量

都是可选的。

| 变量 | 默认 | 作用 |
| :--- | :--- | :--- |
| `PRISM_HOST` | `127.0.0.1` | 监听地址 |
| `PRISM_PORT` | `18765` | 监听端口 |
| `PRISM_BRIDGE_API_KEY` | 空 | 不为空时，`/v1/*` 必须带 `Authorization: Bearer <这个值>` |
| `PRISM_CALLER_OWNED_TOOLS` | `false` | `true`：提示词里说明工具在 HTTP 调用方那边执行，而不是运行桥的这台机器（给网关用） |
| `PRISM_PROFILE_DIR` | `~/.prism-playwright-profile` | 浏览器数据目录 |
| `PRISM_AUTH_FILE` | `<PROFILE_DIR>/auth.json` | 登录凭证文件 |
| `PRISM_BROWSER_CHANNEL` | Linux：`chromium`；其它：空 | Playwright 浏览器通道。空 = 默认的 headless shell |
| `PRISM_TURN_TIMEOUT` | `600` | 单轮最长等待秒数 |
| `PRISM_THROTTLE_WAIT` | `240` | 单个请求最多为限流等待的秒数 |
| `PRISM_ALLOW_MODEL_FALLBACK` | 关 | 模型被拒时回退到 Prism 默认模型 |
| `PRISM_CONTINUE` | `1` | `0` 关闭会话延续，每轮整段重放进新会话 |
| `PRISM_MAX_TURN_BYTES` | `86000` | 单轮文本上限（JSON 转义后的字节数） |
| `PRISM_MAX_TURN_PARTS` | `8` | 一个请求最多拆成几轮 |
| `PRISM_PART_GAP` | `8` | 拆分的各轮之间等待的秒数 |
| `PRISM_CATALOG_REFRESH_CHARS` | `200000` | 续接的会话每增长这么多字符重发一次工具目录，`0` 为不重发 |
| `PRISM_CLIENT_INSTRUCTIONS_MAX` | `2000` | 超过这个长度的 `instructions` 不转发 |
| `PRISM_FORWARD_CLIENT_INSTRUCTIONS` | 关 | 转发任意长度的 `instructions` |
| `PRISM_TOOL_DESC_MAX` | `1200` | 函数型工具描述的截断长度 |
| `PRISM_CUSTOM_TOOL_DESC_MAX` | `40000` | 自由文本工具描述的截断长度 |
| `PRISM_ALLOWED_ORIGINS` | 空 | 允许调用桥的浏览器来源，逗号分隔，`*` 为全部 |
| `PRISM_DUMP_DIR` | 空 | 设为目录后保存每个请求的调试 JSON |

## 接到自建网关（sub2api / new-api 一类）

桥可以当成一个 OpenAI 兼容的上游，挂到网关后面：

```bash
export PRISM_HOST=0.0.0.0
export PRISM_CALLER_OWNED_TOOLS=true
export PRISM_BRIDGE_API_KEY='自己设一个'
python bridge.py serve
```

在网关里新增一条 OpenAI 兼容渠道：Base URL 填 `http://<运行桥的机器>:18765/v1`，类型选 Responses，Key 填上面的 `PRISM_BRIDGE_API_KEY`。

- **一定要设 `PRISM_BRIDGE_API_KEY`**，或者保证端口只有网关能访问。否则任何连得上这个端口的人都在用你的 Prism 账号。
- **服务器没有桌面时**：在有桌面的机器上 `python bridge.py login`，把生成的 `auth.json` 拷到服务器，用 `PRISM_AUTH_FILE` 指过去。凭证过期后重复一次。
- **Linux**：桥默认用完整 Chromium 的新 headless 模式。Playwright 默认的 headless shell 配持久化浏览器目录，在 Linux 容器里启动约 30 秒后会丢光 Cookie。
- **会话延续要靠用户标识**：网关模式下，请求带 `X-User-Id` / `X-OpenAI-User` 请求头或 `user` 字段时才续接会话；都没有时每个请求都当成新会话整段重放，避免不同用户串到同一个会话里。
- 一个 Prism 账号的限流是共用的，多人同时用会更容易触发。

## 常见问题

**启动报 `net::ERR_CONNECTION_CLOSED`**
到不了 `prism.openai.com`。

**页面能开，但所有请求都是 `403 {"error":"Request verification failed"}`**
到不了 `sentinel.openai.com`，校验令牌拿不到。两个域名必须走同一条能用的线路。

**`Error while processing conversation (403 Forbidden)`**
限流，见[已知限制](#已知限制)。停一会儿再用，不要连续重试。

**`This request is too large to send`**
来自 Prism 的单轮大小限制，和模型上下文窗口无关。新版桥已经按会话延续和拆分处理；还遇到的话检查 `PRISM_CONTINUE` 是否被关掉。

**模型说“看不到本机文件”或自己编工具名**
通常是客户端把工具放在了桥不认识的位置。用 `PRISM_DUMP_DIR` 看请求体。

**端口被占用**
桥已经在另一个窗口运行，或者换一个端口：`python bridge.py serve --port 18766`。

## 免责声明

本项目通过浏览器自动化访问你自己账号下的 Prism，可能不符合 OpenAI 的服务条款，账号风险自负。仅供学习和个人使用，请勿用于转售或大规模滥用。

## 社区

本项目认可并参与 [LINUX DO](https://linux.do) 社区。

## 作者

QQ 交流群：`724686992`，群主即为作者。传播、转载、二次分发请标明原作者。
