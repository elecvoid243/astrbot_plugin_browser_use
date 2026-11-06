# astrbot_plugin_browser_use

随 AstrBot 插件生命周期启动、停止本地 Browser Use MCP。AstrBot 继续作为主 Agent，调用已有的 vLLM / OpenAI 兼容模型；Browser Use 只提供浏览器工具。

## 多账户 Chat 消费兼容

多账户宿主要求所有 Dashboard 账户（包括管理员）使用实际带 `browser-owned-chat-v1` 守卫标记的处理器。
请同步部署匹配的 AstrBot 后端、Dashboard 和本插件；旧版插件不能仅凭相同 URL 获得消费权限。
如果浏览器工具可以正常打开网页，但 ChatUI 的 Agent 浏览器显示“无访问权限”，请先检查安装目录中的本插件是否包含此适配；仅更新 AstrBot 核心不会更新已安装的插件。替换插件文件后需重载本插件或重启 AstrBot，刷新页面本身不会更新内存中的处理器。
现有观察处理器仍核对真实会话所有者、当前浏览器归属及 generation/session/lease，
管理员日常 Chat 也不能通过状态、截图、订阅或停止入口接管另一账户的浏览器。
这个标记没有增加工具权限或独立执行沙箱；非 Dashboard 的工具生命周期保持原有语义。

## 用户安装：Windows x64 离线完整包

1. 在有网的 Windows x64 机器按下方步骤构建，生成 `dist/astrbot_plugin_browser_use-0.4.0-win-x64.zip` 和 SHA256 文件，同时携带匹配的 AstrBot 后端与 Dashboard 发行构建。
2. 将 ZIP 复制到无外网机器，解压到任意目录，停止 AstrBot。
3. 双击解压目录中的 `install.cmd`，选择实际的 AstrBot **data 目录**（其中已有 `plugins` 文件夹）。也可运行 `install.cmd -AstrBotDataDir "D:/AstrBot/data"`。
4. 启动 AstrBot，插件将自动加载并连接 MCP。插件配置中的 Python、浏览器路径留空即可使用随包环境。
5. 后续通过插件页面的开关即可启停：禁用插件会先关闭浏览器会话，再断开 MCP 并移除对应工具；重新启用会重建连接，无需重启整个 AstrBot。重载插件也会执行相同的停止、启动流程。

完整包超过当前 AstrBot 的 128 MB 上传上限，因此通过本地安装入口复制，不能上传完整包。安装器只支持首次安装，拒绝覆盖现有目录；更新时先停止 AstrBot，再将旧插件目录移出 `plugins` 后安装。登录状态等持久数据保存在单独的 `plugin_data` 目录中。

无需安装系统 Python、Node.js、uv、Playwright 或通过插件管理器在线下载依赖。源码目录本身不包含二进制依赖；必须使用构建后的完整 ZIP 才能直接离线运行。

运行环境为 Windows x64，建议 Windows 10/11。需要 AstrBot `>=4.28,<5`，且提供 `get_llm_tool_manager()`、`mcp_server_runtime_view`、`enable_mcp_server()` 和 `disable_mcp_server()` 接口；已按当前 AstrBot_for_spzx 工作区实现。

**Agent 浏览器还要求 AstrBot_for_spzx 包含 #86 的宿主和 Dashboard 变更。版本号 4.28.1 本身不足以证明兼容。** 核对完整包 `runtime/manifest.json` 中的 `astrbot_revision` 与 `dashboard_sha256`；前端 ZIP 的 `dist/assets/browser-observer-build.json` 记录匹配的后端提交。插件 ZIP 只安装插件，不升级后端或 Dashboard。停止 AstrBot 后另行部署匹配后端，并将 Dashboard ZIP 解压，使用 `python main.py --webui-dir "D:/matching-dashboard/dist"` 启动。不要将前端 ZIP 当作插件上传，也不要覆盖生产数据。先在临时数据目录验收。

前端打包命令还会输出 `*-backend-source.zip` 和校验文件，提供匹配后端源码；该源码包需要已有的兼容 AstrBot Python 环境，不携带宿主全部依赖。将旧 data 目录独立保留，在新代码位置通过 `ASTRBOT_ROOT` 指向其父目录，再选择配套前端。离线插件安装不执行 pip 或浏览器下载。

## 私有运行时清单

0.4.0 完整包包含固定的 aiortc 1.14.0、PyAV 16.0.1 和软件 VP8 编码依赖。`runtime/requirements.lock` 锁定所有 Windows x64 wheel 及 SHA256；`scripts/runtime-win-x64.json` 固定 Python、Chromium 和 pip 归档。构建校验原始归档后从 wheelhouse 以 `--no-index --require-hashes` 安装，不借用现有运行目录的包。

`runtime/runtime-check.json` 记录私有依赖、FFmpeg 版本/构建配置/许可证、1920×1080 编码检查及 DLL/PYD 哈希。Python、Chromium 和依赖 wheel 原有许可证与 notices 随包保留；各组件适用自己的上游许可证。用户机器不运行 pip，不把媒体依赖安装到 AstrBot。

## 有网打包

构建脚本需要 Windows x64 和 Python 3.11+（附带 pip）。它会：

- 获取固定版本的官方 Windows embeddable Python 和 Chrome for Testing。
- 使用版本库中的 `scripts/requirements-win-x64.lock` 获取精确版本和 SHA256 匹配的 Windows x64 wheel，不在无网机器编译。升级依赖需显式更新锁文件，并重新完整验收。
- 以 `--no-index --require-hashes` 安装到随包 Python。
- 将依赖预装在 `runtime/python/Lib/site-packages`；这是专用嵌入式发行包，不是复制普通 `.venv`。
- 执行 `pip check`，并使用本机回环网页验证 MCP 握手、工具发现、中文填表、点击、HTML 读取、截图和关闭。
- 构建完整 ZIP，再解压最终 ZIP 验证 MCP、中文表单、连续帧、滚动、新标签和停止/关闭。成功日志出现后才可分发；自检失败的残留 ZIP 不可分发。运行时不执行 pip 或任何依赖下载。

默认固定版本：Browser Use `0.13.11`、Python `3.13.17`、Chrome for Testing `156.0.8078.47`、pip `26.2.2`。Browser Use 选择已发布到 PyPI 的版本；构建时不追踪 `main` 或 `latest`。

```powershell
# First build/package the matching Dashboard in the AstrBot repository.
# node node_modules/vue-tsc/bin/vue-tsc.js --noEmit
# node node_modules/vite/bin/vite.js build
# python scripts/package_browser_observer_dashboard.py --plugin-revision <plugin-commit> --output D:/delivery/dashboard.zip
# Use that repository's git rev-parse HEAD and Dashboard ZIP SHA256 below.
python scripts/build_offline.py --astrbot-revision <40-character-commit> --dashboard-sha256 <64-character-sha256>
# Build only the small plugin ZIP when a private runtime is already available.
python scripts/build_offline.py --source-only
```

下载缓存在 `.build/downloads`，构建临时环境使用后自动清理。ZIP 中保留 `runtime/requirements.lock`、`runtime/manifest.json`、Python/浏览器及依赖包携带的许可文件。分发前应检查这些组件的再分发许可。

`manifest.json` 包含插件、Python、pip、Browser Use、Chromium 版本、源码哈希、构建输入及来源和匹配前端校验值。校验外层 ZIP：`Get-FileHash .\dist\astrbot_plugin_browser_use-0.4.0-win-x64.zip -Algorithm SHA256`，与 `.zip.sha256` 对照。移动插件安装目录后，留空默认路径会在下次启用时重新定位；安装器仍拒绝覆盖已有插件，`plugin_data` 中的数据保留，但不同浏览器会话使用新 profile。

## 完整宿主验收与故障排查

在具有 AstrBot 开发依赖、`aiohttp playwright psutil` 的测试解释器运行匹配后端仓库中的脚本。它从最终 ZIP 安装到新建的中文/空格目录，启动真实 AstrBot、SQLite、登录、插件管理、完整 Chat.vue 和 Agent 工具执行；仅以本地 OpenAI 兼容 fixture 代替模型服务。本机复用随包 Chromium，不下载测试浏览器。`--evidence-dir` 必须尚不存在。

```powershell
python scripts/browser_observer_acceptance.py --plugin-zip D:/delivery/astrbot_plugin_browser_use-0.4.0-win-x64.zip --dashboard-dir D:/delivery/dist --evidence-dir D:/temporary/new-acceptance
```

该命令在 **AstrBot 仓库** 执行，生成截图、宿主日志、模型请求和逐项结果。测试为宿主进程注入临时的外部 DNS/套接字拦截，并设置失效的外网 HTTP 代理，网页/模型均使用回环；这是模拟无外网，不等于系统防火墙或断开网络的干净目标机。被拒绝的更新查询可能出现在日志中。实际无网 Windows 目标机和真实 vLLM 工具调用仍需分别验收。不要把模型 fixture 成功当作特定 vLLM 模型兼容证明。

- 入口缺失：核对 Dashboard 构建校验值和后端提交；清除旧前端缓存并重新登录。
- `runtime_unavailable`：检查完整 ZIP、独立解释器、Chromium 和插件加载日志；源码小包没有二进制环境。
- `busy`：在占用聊天关闭浏览器；切聊天不会转交登录态。
- `disabled`：启用插件后重新发起任务；停止后的会话须先关闭浏览器。
- 401/403：重新登录并使用当前用户拥有的聊天，不手工替换 `chat_id`。
- 离线启动失败：勿执行 pip 修补宿主环境；核对 SHA256、安装路径及安全设置是否放行私有 Python，保留日志后在有网构建机重建完整包。

## 源码开发或复用现成环境

配套的新版宿主和 Dashboard 可协商本地 WebRTC 视频；私有 Python 需具备已验证的
aiortc `1.14.0`、PyAV `16.0.1` 与 libvpx。缺少依赖或接收端帧元数据能力时保留 JPEG，
不要把这些库安装进 AstrBot 的 Python。源码的视频支持不代表现有离线 ZIP 已包含这些依赖；
应以最终包的锁文件、清单和安装验收为准。

视频与人工输入分离，使用原有接管租约和有序输入 WebSocket。媒体票据仅由宿主为当前账号
拥有的聊天签发，双方不配置公网 STUN/TURN。视频故障保留人工所有权，可明确重试或切换
JPEG。新画面必须带实际呈现帧对应的签发记录与完整坐标 token；缺字段不会降级保护。
同源、跨源/OOPIF、open/closed shadow 的坐标不明确时暂停新输入，已有松开和重置仍走原
输入会话；这不承诺任意 DOM/动画语义的原子点击。实际帧率和交互延迟取决于页面、分辨率
与机器，不能将上限或目标值当作保证。

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

MCP 启动入口为上游 `BrowserUseServer` 增加会话存储隔离、受鉴权的观测/控制桥和退出清理：stdio 断开后关闭活跃浏览器会话。插件停止时还会主动调用 `browser_close_all`，避免浏览器进程随 MCP 连接遗留。

停止时还会在宿主客户端的重连锁内撤销旧连接和启动配置，防止已开始的 Agent 请求持有旧 MCP 工具并在插件禁用后自动拉起进程。此处依赖当前 AstrBot MCPClient 的重连锁与连接配置字段。

浏览器配置、下载文件及工作区保存在 AstrBot 管理的 `data/plugin_data/astrbot_plugin_browser_use/`；升级、禁用插件时保留。每次新建浏览器会话使用独立的 profile、下载和工作目录，关闭后重新打开不会继承上一会话的登录态。已有 `profile/` 数据不会被删除或自动分配给新聊天。每次启用会根据当前安装路径重新生成 `browser-use.json`，不要手动编辑该生成文件。

明确指定浏览器路径，关闭默认扩展自动下载、遥测、云同步、版本检查和费用数据获取；不继承模型 API 密钥。不启用 Browser Use 的 `retry_with_browser_use_agent` 和 `browser_extract_content` 工具，其余基础浏览器工具由 AstrBot 主 Agent 使用。

已有 vLLM 的模型仍需支持正常的工具调用；截图理解需要模型支持图像输入。纯文本模型可以使用页面状态和 HTML。浏览器由首次调用工具的聊天独占，同一聊天内的操作串行执行；其他聊天会收到 busy，直到所有者调用 `browser_close_all` 或禁用插件。所有者来自 AstrBot 工具执行上下文，模型参数不能指定所有者。配置开关不等同于网络防火墙；严格离线验收应在禁止外网的机器上进行。

## Agent 浏览器实时观测与接口契约

未加载项目时从输入框上方 **文件与浏览器 → Web 预览 → Agent 浏览器** 进入；加载项目后从 **工作区 → Web 预览 → Agent 浏览器** 打开，先显示当前快照，再持续更新 Agent 当前 Chromium 的真实地址、标题、登录态和表单值。标签页会随 Agent 的切换与关闭自动跟随；工具栏显示实时观测状态、当前标签、尺寸和序号。“刷新快照”会重新查询身份并连接观测。原 iframe 入口对应 **Web 预览 → 网页预览**，书签保持原行为。项目卸载不会关闭正在观看的 Agent 浏览器；Git、文件、终端和 iframe 的原入口及书签行为保持。动作列表显示正在执行/等待、成功、失败、超时和取消；输入摘要隐藏内容，实际点击/输入位置以短暂圆环标记。画面默认只读，服务端确认人工接管后可点击；观测图片不会发送给模型。

隐藏侧栏、切换工作区标签或将 Dashboard 浏览器页放到后台会断开画面订阅；最后一个观看者离开后停止采集；只读时浏览器操作继续，人工控制时断开输入并保持暂停。重新打开会读取新快照并恢复连续观测。意外断流会自动重连（重试间隔逐步增加，最多五秒），也可点击“刷新快照”。空闲状态每三秒查询是否已有浏览器，查询与恢复均不启动浏览器。切换聊天立即取消旧请求、读流和重试并移除旧画面；刷新页面后重新打开侧栏恢复。状态区分空闲、其他聊天占用、插件禁用、运行环境不可用、断开和重连中。

Dashboard 扩展接口（沿用 AstrBot 登录鉴权）：

- `GET /api/v1/plugins/extensions/browser-use/status?chat_id=<ChatUI session_id>`
- `GET /api/v1/plugins/extensions/browser-use/snapshot?chat_id=...&generation=...&browser_session_id=...`
- `GET /api/v1/plugins/extensions/browser-use/stream?chat_id=...&generation=...&browser_session_id=...`

所有接口均检查登录用户是该 WebChat 会话的 `creator`，并检查 `(username, chat_id)` 与工具事件 `webchat!username!chat_id` 的所有者一致。非 WebChat 平台可使用浏览器工具，但当前观测接口只支持 WebChat。未认证为 401，缺少聊天或快照/流身份为 400，无聊天访问权为 403，忙碌或旧代次/会话为 409，禁用或运行环境不可用为 503。无占用时返回 `available: false`，查询不会启动浏览器。禁用、占用和运行环境不可用的错误 envelope 新增 `data.reason`，分别为 `disabled`、`busy` 和 `runtime_unavailable`；HTTP 状态不变。状态与快照响应禁止缓存。流在所有者、浏览器会话或本次启用凭据失效时结束。

状态/快照成功返回 `{status: "ok", data: ...}`。`data` 包括 `generation`（每次 MCP 进程启动的新代次）、`browser_session_id`（实际 BrowserSession ID）、`tab_id`（当前 CDP target）、`sequence`、`timestamp`（UTC ISO 8601）、`available`、`url`、`title`、`width` 和 `height`。快照另外包含 `image`（base64 JPEG）、`mime_type`。状态查询的尺寸为 0，当前标签不存在时 `tab_id` 为 null。

流返回 `text/event-stream`，每块为 `id: <sequence>\ndata: <JSON>\n\n`。JSON 直接携带上述完整身份/页面字段及 `type: "state" | "frame" | "action"`，没有状态响应 envelope。`frame` 另带 `image`、`mime_type: "image/jpeg"` 和 `capture_timestamp`（截图开始采集的 Unix 秒数）；尺寸是编码图像的实际尺寸。没有待发事件时发送心跳 `: hb\n\n`，静止页面仍保留约一秒一次的一致性补帧。`sequence` 在同一代次内统一计数快照和流事件，状态查询只读取最近序号；丢帧、快照和多个订阅者都可能造成序号跳跃，不保证连续。客户端只接收匹配代次/浏览器会话且序号更新的事件；每帧自带标签身份，因此丢失中间状态也可正确跟随。更换标签或 URL 时立即作废旧图像的输入坐标，配套的新 Dashboard 保留上一帧显示至新截图到达。旧 CDP session 的帧仍尝试确认，但不触发新截图。

动作和状态/画面使用同一代次内的 `sequence`。`type: "action"` 表示动作快照变化；所有状态、快照、帧和动作事件均包含 `current_action`（执行中或 null）、`recent_actions`（当前浏览器最多 32 个终态动作）、`action_cursor`（最近动作事件序号）和 `highlight`（有效操作标记或 null）。动作记录包含 `action_id`、可信 `invocation_id`、`generation`、`browser_session_id`、开始时的 `tab_id`、`kind`、`status`、`started_at` 和终态的 `finished_at`（Unix 秒）。`kind` 为 `navigate|click|type|scroll|tab|close|inspect`，`status` 为 `running|succeeded|failed|timed_out|cancelled`。显示摘要由这些枚举翻译产生，不复制工具参数、输入或结果。输入工具本身也使用敏感输入标记，避免上游日志和工具返回重复明文。

连接流时可传 `action_cursor=<最后应用的动作序号>`。运行环境保留最近 128 个动作事件：有效游标先补发当前浏览器的较新动作，再发当前状态；游标为 0 或超出恢复窗口时发 `action_reset: true` 的权威状态快照。超前游标返回 409，格式错误返回 400；不使用 Last-Event-ID。每个观看者在桥接和宿主各保留最多 128 个动作事件、一个最新状态槽和一个独立的最新画面槽；画面不会覆盖动作。动作积压溢出时使用同样的 `action_reset` 快照收敛到有界的最近记录。每次新订阅重启共享 screencast 取得新帧；图像不持久化、不重放历史。Dashboard 每次恢复先查询状态和一致快照，再用快照的 `action_cursor` 订阅增量；不推断连续帧序号、不重放旧图片。重复或较旧序号被丢弃，较旧动作游标不能覆盖当前动作。`action_reset` 或运行代次/浏览器会话改变时显示重新同步提示。连接与延迟回调受当前聊天和请求取消范围约束。

页面额外包含 `document_id`（当前文档的 performance.timeOrigin）、`viewport_width/viewport_height` 和 `scroll_x/scroll_y`。点击/输入完成后，从该次实际执行的上游事件结果提取坐标，不读取或刷新 DOM 索引；若执行没有可靠坐标，只显示摘要。`highlight` 包含这些页面身份、`action_id`、`x/y` 和 `expires_at`，最长显示两秒，按图片对应的视口比例绘制。导航（包括相同 URL 重载）、切页、滚动、视口变化、下一动作和到期都会清除旧标记。取消表示停止等待及后续执行，不撤销已发生的网页操作。

动作状态先报告新的滚动位置或视口时，侧栏作废旧画面的输入坐标，等待坐标空间匹配的新帧后再显示标记。私有采集将 CDP 通知自带的 `scrollOffsetX/Y`、`deviceWidth/Height` 与页面采样核对，仅相符且 `pageScaleFactor=1`、`offsetTop=0` 的通知触发即时截图；缺少、无效或不匹配的元数据仍确认，但由一致性补帧获取当前页面。最终图片统一由 `Page.captureScreenshot` 产生，并在截图后核对文档、视口和滚动位置，不用较新的页面采样覆盖旧像素的坐标空间。事件结构、统一序号、图像上限与恢复游标保持原契约。

私有桥只绑定 `127.0.0.1` 的随机端口，使用本次启用的随机 Bearer 凭据和运行代次校验；发现文件仅存放端口和代次。公共响应不包含 CDP 地址或桥接凭据。快照通过 `Page.captureScreenshot` 读取当前实际视口，最多 2 倍采样、JPEG quality 90、最大 1920×1080，不调用 DOM 状态刷新或修改 Agent 焦点。标签变化时丢弃该次结果并要求刷新。插件退出时关闭桥并移除其发现文件。

只读观测通过同一 BrowserSession 的 `Page.startScreencast` 检测画面变化，不直接发布其原始图片；变化通知、静止补帧和人工操作反馈统一使用上述高清截图路径，避免不同采样比例交替。重复的相同像素和坐标通知不触发额外截图，静止页面约一秒补帧一次；停止 Agent 执行后仍保留观测。压缩图片最多 2 MiB、单事件最多 3 MiB；元数据拒绝超过 8192 字符的 URL 或 1024 字符的标题。只读采集间隔至少 250 ms（最多约 4 Hz）。采集不调用 `browser_get_state` 或其他 MCP 工具，不刷新 DOM 状态或改变焦点。每个代次最多 4 个订阅，每个订阅在私有桥和宿主代理各保留一个待发画面，并为动作保留独立有界缓冲，慢客户端覆盖旧画面；CDP 确认独立执行并有界。私有桥写入超过 2 秒就关闭该订阅。人工接管使用下述专用鉴权通道，人工期间以 100 ms 采集间隔（目标上限约 10 Hz）复用一致性截图反馈。

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

此验收以生产 Vue 工作台入口、侧栏和观测组件、AstrBot 的实际插件请求绑定、隔离的登录身份适配器、插件处理器、私有 MCP 和真实 Chromium 连通测试，覆盖无项目打开、项目卸载、聊天切换、页面刷新、断流、断网与动作缓冲过期，并输出中英文截图；spcode 项目元数据、宿主生命周期、会话数据库和登录身份由测试适配器提供。它不等于完整 AstrBot 登录部署、实际无网目标机或真实 vLLM 联调。

## 停止、关闭与重新启用

侧栏“停止执行”会请求停止同一聊天的 AstrBot Agent，取消私有运行环境的动作子任务，等待其退出后确认终态；已发生的网页操作不会撤销。排队和后续调用被拒绝，浏览器仍可观看。点击“关闭浏览器”释放占用后再发起新任务，使用全新隔离 profile；原有数据不被覆盖或交给下一所有者。隐藏面板只停止观看。

公共 POST `/browser-use/stop` 和 `/browser-use/close` 使用查询参数 `chat_id`、`generation`、`browser_session_id`，沿用 Dashboard 登录及聊天所有权校验。响应为确认后的状态，新增 `execution_state`（`active`、`stopped`、`closed`、`disabled`）。停止/关闭递增宿主租期，旧队列不能混入重新打开的同一聊天；旧插件工具绑定激活凭据，禁用后不能跨重新启用调用。内部 `/control` 仅接受本次激活 Bearer 凭据和运行代次。

禁用先撤销准入，再结束动作、会话、观测、回环桥和自有 MCP。清理失败保留所有权以便重试；不接管或关闭其他实例服务。重新启用建立新代次。残留 HTTP 路由禁用后返回 `503 disabled`，旧会话控制返回 `409`。


## Agent 浏览器人工接管 / Agent browser manual control

配套 AstrBot 宿主与 Dashboard 支持 H01/H02 控制协议时，在 Agent 运行中或完成后点击
**接管浏览器**。后端确认前保持只读；确认后可单击原有 Chromium 页面，再点击
**交还控制权**。浏览器、标签页和登录态保留。运行中的本地 Agent 在接管时暂停，
在途动作及页面事件确认静止前不开放人工输入。交还后废弃旧计划，依据最新页面重新决策；
人工等待不占工具超时，不持续调用模型。取消或结束的轮次不会被交还重新启动。
断连、隐藏面板或观测失效后保持暂停，需要主动重新接管并交还；停止、关闭、禁用仍
保留各自语义。旧客户端继续观测，旧插件在新界面保持只读并显示升级提示。

With the matching host and Dashboard, **Take control** admits one authenticated
human while the local Agent runs or after it finishes. In-flight browser events
must settle before human input is acknowledged. The current turn pauses outside
tool timeouts, discards stale plans and replans from a fresh page observation only
after explicit handback. Cancelled or completed turns remain terminal. Click the existing Chromium page, then **Hand back**
to refresh page/element knowledge and restore Agent admission. Browser and login
state survive. Disconnect pauses input and never starts an Agent automatically.

The host issues a single-use, 15-second ticket at
`POST /api/v1/plugins/browser-control/session`. Submit it in the first WebSocket
message to `/api/v1/plugins/browser-control/ws`; credentials never belong in a URL.
The host rechecks the account, owned chat and enabled plugin on each command.
The private runtime additionally validates owner, connection, generation, browser,
control epoch, input ordering and actual target identity. It accepts only typed
click, heartbeat and release operations. Agent tools and new model turns reject
while human control or paused admission is active. No arbitrary CDP/script proxy
is exposed. Human control uses validated viewport JPEGs at no more than the
ten-capture-per-second target while human control is active (read-only remains four Hz), over the existing SSE observation stream.
Control metadata cannot overwrite the only pending changed frame of a static page.

The exact protocol is documented in the matching AstrBot source at
`docs/en/dev/browser-manual-control.md` and `docs/zh/dev/browser-manual-control.md`.
Keyboard/composition/paste (H03) and scrolling/double-click feedback (H04) are implemented. Full lifecycle recovery remains H05; final offline-package acceptance belongs to H06.


H04 adds `pointer_input_version: 1`, typed bounded wheel input, native double-click counts,
and nested-scroll `position_revision` validation. Human snapshots retain the existing
size/queue limits and target up to 10 captures/second. `applied_input_id` distinguishes
input acknowledgements from a subsequently loaded image; human timeline entries contain
categories only. Legacy single-click clients and read-only observation remain compatible.

Stale wheel samples receive `status: stale`, consume their ordered ID without
CDP dispatch, and keep human ownership while the next image synchronizes. The
client displays the rejection and does not replay that gesture. Other uncertain
or invalid commands retain the existing fail-closed behavior.

## 0.4.0 人工登录交付

0.4.0 包含人工接管、键盘/中文提交与粘贴、滚动/双击、显式交还和断连恢复。不能仅比较宿主版本号：Dashboard 的 `browser-observer-build.json` 与插件 `runtime/manifest.json` 必须具有相同 `astrbot_revision` 和 `plugin_revision`；同时核对插件记录的 Dashboard ZIP SHA256。两仓库先提交最终源码，再打包；打包脚本拒绝未提交的受跟踪变更。宿主源码 ZIP 仍要求已有兼容的 AstrBot Python 环境，不是宿主依赖离线安装包。Windows x64 私有 Python/Chromium 随完整插件包提供，其他平台不能使用该二进制包。

升级时停止宿主，保存配套旧后端、Dashboard、插件包及校验值，将旧插件目录移出 `data/plugins`，再使用原安装器。不要将旧插件与新插件同时加载；保留 `data/plugin_data`。当前浏览器会话不能跨宿主重启保存；同一任务内人工接管则保留原浏览器和登录态。回退也应停止宿主并整体恢复配套三个组件，保留独立数据备份。

登录流程：Agent 打开登录页后点击“接管浏览器”，等待确认并聚焦远端输入框；输入中英文账号和测试密码，提交网页表单；确认登录成功后点击“交还控制权”。失联、隐藏或刷新只会暂停，必须重新接管并明确交还。动作摘要隐藏输入，交还后的可见页面内容仍可能进入 Agent 的重新观察。

最终 ZIP 的组合验收在 AstrBot 仓库执行：

```powershell
python scripts/browser_manual_control_acceptance.py --plugin-zip D:/delivery/astrbot_plugin_browser_use-0.4.0-win-x64.zip --dashboard-dir D:/delivery/dist --evidence-dir D:/temporary/new-h06 --login-handoff --pointer-input --text-input --lost-key-target --recovery
# For the separate one-step-budget regression, use a new directory and
# --handoff-boundaries, omitting --login-handoff.
```

脚本使用原 PowerShell 安装器，搬迁含中文/空格的完整 data 树，路径配置留空以验证随包环境；校对源码哈希、配套提交，执行真实宿主登录、普通账户/管理员和私有 Chromium 登录表单。只在临时解释器注入 Python socket/DNS 审计与安装器拦截；私有 Chromium 使用不可达 SOCKS 代理、仅回环绕过、禁用 QUIC 与外部解析，并采样进程连接与资源。审计先实际尝试并拒绝测试外部地址，记录加载它的宿主/私有解释器及浏览器约束，不记录输入内容。这不是内核防火墙或物理断网，也不能证明所有原生网络路径。真实 vLLM、实体 Windows 输入法候选窗口、其他操作系统仍需独立验证。截图、模型请求、网络审计、延迟/资源和清理结果保存在新建证据目录。


### 自适应与高清观测

配套的新 Dashboard 会同步侧栏／全屏的可用尺寸。只读调整经宿主校验聊天归属；Agent 动作执行期间延后，人工控制期间仅控制窗口通过原输入通道调整。新能力标记为 `viewport_resize_version: 1`，旧前端仍可观看。

快照按实际视口最多 2 倍采样，JPEG 质量 90，上限 1920×1080。图片最多 2 MiB、事件最多 3 MiB，复杂图片按需降质，待发帧缓冲仍只保留最新图片。需要更新宿主后端、Dashboard 与插件源码；已有 dist 离线包不会随源码自动更新。

过期且未执行的点击／键盘输入返回 `stale`，保留人工控制、丢弃未发送输入并等待新画面，不重放操作。心跳续租不再访问网页执行上下文；真实断连、撤权、未知执行结果仍暂停控制。
