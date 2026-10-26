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

MCP 启动入口只为上游 `BrowserUseServer` 增加退出清理：stdio 断开后关闭活跃浏览器会话。插件停止时还会主动调用 `browser_close_all`，避免浏览器进程随 MCP 连接遗留。

停止时还会在宿主客户端的重连锁内撤销旧连接和启动配置，防止已开始的 Agent 请求持有旧 MCP 工具并在插件禁用后自动拉起进程。此处依赖当前 AstrBot MCPClient 的重连锁与连接配置字段。

浏览器配置、登录状态、下载文件及工作区保存在 AstrBot 管理的 `data/plugin_data/astrbot_plugin_browser_use/`；升级、禁用插件时保留。每次启用会根据当前安装路径重新生成 `browser-use.json`，不要手动编辑该生成文件。

明确指定浏览器路径，关闭默认扩展自动下载、遥测、云同步、版本检查和费用数据获取；不继承模型 API 密钥。不启用 Browser Use 的 `retry_with_browser_use_agent` 和 `browser_extract_content` 工具，其余基础浏览器工具由 AstrBot 主 Agent 使用。

已有 vLLM 的模型仍需支持正常的工具调用；截图理解需要模型支持图像输入。纯文本模型可以使用页面状态和 HTML。Browser Use 的现成 MCP 共用浏览器状态，适合单一受信用户串行使用；本插件没有实现多聊天会话或多账号隔离。配置开关不等同于网络防火墙；严格离线验收应在禁止外网的机器上进行。

## 验证

```powershell
python -m pytest
ruff format .
ruff check .
# Run against an extracted offline ZIP, without an LLM or AstrBot process.
python scripts/smoke_test.py --plugin-dir D:/offline/astrbot_plugin_browser_use
```

单元测试覆盖启停、重载、启动失败回滚、同名服务保护、关闭失败、并发启停、路径重定位和离线配置。真实浏览器自检使用临时用户目录和 `127.0.0.1` 测试页，不调用 vLLM 或任何云模型。
