# astrbot_plugin_browser_use

随 AstrBot 插件生命周期启动、停止本地 Browser Use MCP。AstrBot 继续作为主 Agent，调用已有的 vLLM / OpenAI 兼容模型；Browser Use 只提供浏览器工具。

## 用户安装：Windows x64 离线完整包

1. 在有网的 Windows x64 机器运行 `python scripts/build_offline.py`，生成 `dist/astrbot_plugin_browser_use-0.1.0-win-x64.zip` 和 SHA256 文件。
2. 将 ZIP 复制到无外网机器，解压到任意目录，停止 AstrBot。
3. 双击解压目录中的 `install.cmd`，选择实际的 AstrBot **data 目录**（其中已有 `plugins` 文件夹）。也可运行 `install.cmd -AstrBotDataDir "D:/AstrBot/data"`。
4. 启动 AstrBot，插件将自动加载并连接 MCP。插件配置中的 Python、浏览器路径留空即可使用随包环境。
5. 后续通过插件页面的开关即可启停：禁用插件会先关闭浏览器会话，再断开 MCP 并移除对应工具；重新启用会重建连接，无需重启整个 AstrBot。重载插件也会执行相同的停止、启动流程。

完整包超过当前 AstrBot 的 128 MB 上传上限，因此通过本地安装入口复制，不能上传完整包。安装器只支持首次安装，拒绝覆盖现有目录；更新时先停止 AstrBot，再将旧插件目录移出 `plugins` 后安装。登录状态等持久数据保存在单独的 `plugin_data` 目录中。

无需安装系统 Python、Node.js、uv、Playwright 或通过插件管理器在线下载依赖。源码目录本身不包含二进制依赖；必须使用构建后的完整 ZIP 才能直接离线运行。

运行环境为 Windows x64，建议 Windows 10/11。需要 AstrBot `>=4.28,<5`，且提供 `get_llm_tool_manager()`、`mcp_server_runtime_view`、`enable_mcp_server()` 和 `disable_mcp_server()` 接口；已按当前 AstrBot_for_spzx 工作区实现。

## 有网打包

构建脚本需要 Windows x64 和 Python 3.11+（附带 pip）。它会：

- 获取固定版本的官方 Windows embeddable Python 和 Chrome for Testing。
- 用随包 Python 解析适用于自身版本、Windows x64 的依赖，只接受 wheel，不在无网机器编译。
- 生成带 SHA256 的全量依赖锁文件，再以 `--no-index --require-hashes` 安装到随包 Python。
- 将依赖预装在 `runtime/python/Lib/site-packages`；这是专用嵌入式发行包，不是复制普通 `.venv`。
- 执行 `pip check`，并使用本机回环网页验证 MCP 握手、工具发现、中文填表、点击、HTML 读取、截图和关闭。
- 仅在验证成功后输出包含本地安装入口的完整 ZIP。运行时不执行 pip 或任何依赖下载。

默认固定版本：Browser Use `0.13.11`、Python `3.13.17`、Chrome for Testing `156.0.8078.47`、pip `26.2.2`。Browser Use 选择已发布到 PyPI 的版本；构建时不追踪 `main` 或 `latest`。

```powershell
python scripts/build_offline.py
# Replace pins explicitly when upgrading; every build runs the same smoke test.
python scripts/build_offline.py --browser-use-version 0.13.11
# Build only the small plugin ZIP when a private runtime is already available.
python scripts/build_offline.py --source-only
```

下载缓存在 `.build/downloads`，构建临时环境使用后自动清理。ZIP 中保留 `runtime/requirements.lock`、`runtime/manifest.json`、Python/浏览器及依赖包携带的许可文件。分发前应检查这些组件的再分发许可。

## 源码开发或复用现成环境

插件的 `requirements.txt` 没有宿主依赖。不要将 `browser-use` 安装进 AstrBot 的 Python；当前双方对 MCP、psutil、reportlab 的版本要求有冲突。

将插件放入 AstrBot 的插件目录，或上传构建出的 `*-source.zip`；然后在插件设置中填写（环境未配置时会提示加载失败，配置后重载即可）：

- `python_path`：独立安装了 Browser Use 的 `python.exe`。
- `browser_path`：完整 Chrome/Chromium 发行目录中的 `chrome.exe`。
- `headless`：默认开启；关闭后可在桌面查看浏览器。
- `allowed_domains`：默认空列表，不限制导航；可限制到具体内网地址。
- `startup_timeout`：默认 180 秒。

相对路径均以插件目录为基准。修改设置后重载插件。Python 路径不能是 AstrBot 自身的解释器。

## MCP 生命周期和离线行为

参考 `astrbot_plugin_spcode_toolkit` 的 Vivado MCP 集成，通过 AstrBot 的动态 MCP 管理器启动独立进程，服务名固定为 `astrbot_plugin_browser_use`。不写入全局 `mcp_server.json`，不修改 stdio 白名单。若用户限制了 Python 启动，需要自行在 AstrBot 的安全设置中放行。

启动期间同步等待 MCP 就绪；失败会回滚本插件创建的服务并显示插件加载错误。插件不会接管同名的外部 MCP，也不会在停止时关闭其他所有服务。

MCP 启动入口为上游 `BrowserUseServer` 增加会话存储隔离、只读观测桥和退出清理：stdio 断开后关闭活跃浏览器会话。插件停止时还会主动调用 `browser_close_all`，避免浏览器进程随 MCP 连接遗留。

停止时还会在宿主客户端的重连锁内撤销旧连接和启动配置，防止已开始的 Agent 请求持有旧 MCP 工具并在插件禁用后自动拉起进程。此处依赖当前 AstrBot MCPClient 的重连锁与连接配置字段。

浏览器配置、下载文件及工作区保存在 AstrBot 管理的 `data/plugin_data/astrbot_plugin_browser_use/`；升级、禁用插件时保留。每次新建浏览器会话使用独立的 profile、下载和工作目录，关闭后重新打开不会继承上一会话的登录态。已有 `profile/` 数据不会被删除或自动分配给新聊天。每次启用会根据当前安装路径重新生成 `browser-use.json`，不要手动编辑该生成文件。

明确指定浏览器路径，关闭默认扩展自动下载、遥测、云同步、版本检查和费用数据获取；不继承模型 API 密钥。不启用 Browser Use 的 `retry_with_browser_use_agent` 和 `browser_extract_content` 工具，其余基础浏览器工具由 AstrBot 主 Agent 使用。

已有 vLLM 的模型仍需支持正常的工具调用；截图理解需要模型支持图像输入。纯文本模型可以使用页面状态和 HTML。浏览器由首次调用工具的聊天独占，同一聊天内的操作串行执行；其他聊天会收到 busy，直到所有者调用 `browser_close_all` 或禁用插件。所有者来自 AstrBot 工具执行上下文，模型参数不能指定所有者。配置开关不等同于网络防火墙；严格离线验收应在禁止外网的机器上进行。

## Agent 浏览器实时观测与接口契约

在当前聊天的工作区侧栏打开 **Web 预览 → Agent 浏览器**，先显示当前快照，再持续更新 Agent 当前 Chromium 的真实地址、标题、登录态和表单值。标签页会随 Agent 的切换与关闭自动跟随；工具栏显示实时观测状态、当前标签、尺寸和序号。“刷新快照”会重新查询身份并连接观测。原 iframe 入口对应 **Web 预览 → 网页预览**，书签保持原行为。当前切片需要已加载项目才能打开工作区；无项目入口和动作时间线由后续切片提供。画面只读，观测图片不会发送给模型。

隐藏侧栏、切换工作区标签或将 Dashboard 浏览器页放到后台会断开画面订阅；最后一个观看者离开后停止 screencast，浏览器操作继续。重新打开会读取新快照并恢复连续观测。意外断流显示重连提示，可点击“刷新快照”；自动恢复与动作事件回放留给后续切片。

Dashboard 扩展接口（沿用 AstrBot 登录鉴权）：

- `GET /api/v1/plugins/extensions/browser-use/status?chat_id=<ChatUI session_id>`
- `GET /api/v1/plugins/extensions/browser-use/snapshot?chat_id=...&generation=...&browser_session_id=...`
- `GET /api/v1/plugins/extensions/browser-use/stream?chat_id=...&generation=...&browser_session_id=...`

所有接口均检查登录用户是该 WebChat 会话的 `creator`，并检查 `(username, chat_id)` 与工具事件 `webchat!username!chat_id` 的所有者一致。非 WebChat 平台可使用浏览器工具，但当前观测接口只支持 WebChat。未认证为 401，缺少聊天或快照/流身份为 400，无聊天访问权为 403，忙碌或旧代次/会话为 409，禁用或运行环境不可用为 503。无占用时返回 `available: false`，查询不会启动浏览器。流在所有者、浏览器会话或本次启用凭据失效时结束。

状态/快照成功返回 `{status: "ok", data: ...}`。`data` 包括 `generation`（每次 MCP 进程启动的新代次）、`browser_session_id`（实际 BrowserSession ID）、`tab_id`（当前 CDP target）、`sequence`、`timestamp`（UTC ISO 8601）、`available`、`url`、`title`、`width` 和 `height`。快照另外包含 `image`（base64 JPEG）、`mime_type`。状态查询的尺寸为 0，当前标签不存在时 `tab_id` 为 null。

流返回 `text/event-stream`，每块为 `id: <sequence>\ndata: <JSON>\n\n`。JSON 直接携带上述完整身份/页面字段及 `type: "state" | "frame"`，没有状态响应 envelope。`frame` 另带 `image`、`mime_type: "image/jpeg"` 和 `capture_timestamp`（CDP 帧采集的 Unix 秒数）；尺寸是编码图像的实际尺寸。无变化时只发心跳 `: hb\n\n`。`sequence` 在同一代次内统一计数快照和流事件，状态查询只读取最近序号；丢帧、快照和多个订阅者都可能造成序号跳跃，不保证连续。客户端只接收匹配代次/浏览器会话且序号更新的事件；每帧自带标签身份，因此丢失中间状态也可正确跟随。更换标签或 URL 的状态先清空旧图像，旧 CDP session 的帧仍尝试确认，但不发布。

后续 B03 可在同一顺序分配点 `BrowserObserver.publish()` 增加动作事件类型及关联字段；B02 尚未定义动作 payload，也不支持 Last-Event-ID 历史回放。每次新订阅先发当前状态，再重启共享 screencast 取得新帧（静止页面也适用），图像不持久化、不重放历史。后续动作缓冲不能直接复用会丢旧事件的单槽图像队列。

私有桥只绑定 `127.0.0.1` 的随机端口，使用本次启用的随机 Bearer 凭据和运行代次校验；发现文件仅存放端口和代次。公共响应不包含 CDP 地址或桥接凭据。快照通过 `Page.captureScreenshot` 读取当前视口（1280×720、JPEG quality 65），不调用 DOM 状态刷新或修改 Agent 焦点。标签变化时丢弃该次结果并要求刷新。插件退出时关闭桥并移除其发现文件。

实时观测通过同一 BrowserSession 的 `Page.startScreencast` 取帧，JPEG quality 65、最大 1280×720、压缩图片最多 512 KiB、单事件最多 768 KiB；元数据拒绝超过 8192 字符的 URL 或 1024 字符的标题。发布间隔至少 250 ms（最多约 4 Hz，静止页面不强制产生新帧）。采集不调用 `browser_get_state` 或其他 MCP 工具，不刷新 DOM 状态或改变焦点。每个代次最多 4 个订阅，每个订阅在私有桥和宿主代理各保留一个待发事件，慢客户端覆盖旧画面；CDP 确认独立执行并有界。私有桥写入超过 2 秒就关闭该订阅。没有人工接管接口。

## 验证

```powershell
python -m pytest
ruff format .
ruff check .
# Run against an extracted offline ZIP, without an LLM or AstrBot process.
python scripts/smoke_test.py --plugin-dir D:/offline/astrbot_plugin_browser_use
```

单元测试覆盖启停、重载、启动失败回滚、同名服务保护、关闭失败、并发启停、路径重定位和离线配置。真实浏览器自检使用临时用户目录和 `127.0.0.1` 测试页，不调用 vLLM 或任何云模型。

`tests/test_observation.py` 在本机有完整 Windows runtime 时额外验证真实 Chromium、中文输入、快照、连续变化/滚动、标签切换/关闭、慢消费者、凭据、错误所有者和过期身份；缺少 runtime 时跳过。输出 `OBSERVER_METRICS` 记录采样帧率与最大交付延迟。`tests/test_stream.py` 覆盖单槽缓冲和断流/关闭清理。宿主测试需要 `pytest pytest-asyncio aiohttp fastapi pillow`。可选端到端组件验收还需要宿主 `playwright`（复用随包 Chromium，无需下载浏览器）、Node 和已安装 Dashboard 依赖：

```powershell
$env:BROWSER_OBSERVER_DASHBOARD_ROOT = 'G:/github/AstrBot_for_spzx/dashboard'
$env:BROWSER_OBSERVER_EVIDENCE_DIR = 'D:/temporary/observer-evidence'
python -m pytest tests/test_observation.py -q
```

2026-10-31 的本机 Windows x64 验证使用随包 Python 3.13.17、Browser Use 0.13.11、Chrome 156.0.8078.47 和宿主 Python 3.12：动态回环页面连续采样 12 帧 / 3.110 秒，约 3.859 帧/秒，CDP 采集时间到宿主 SSE 消费的最大采样延迟为 92 ms。该短样本不包含前端绘制延迟，不代表稳定帧率承诺。

此验收以生产 Vue 观测组件、AstrBot 的实际插件请求绑定、隔离的登录身份适配器、插件处理器、私有 MCP 和真实 Chromium 连通测试，输出中英文截图；宿主生命周期、会话数据库和登录身份由测试适配器提供。它不等于完整 AstrBot 登录部署、实际无网目标机或真实 vLLM 联调。
