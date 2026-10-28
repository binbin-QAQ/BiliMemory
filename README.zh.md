# BiliMemory for DeepSeekHarness

`dsh-bilimemory` 是一个 DeepSeekHarness / DSH Cordis 插件，用来把本地 BiliMemory 后端接入 DSH 对话。安装后可以直接在 DSH 中检查服务状态、登录 B站、搜索和采集视频、查询任务日志，并管理 QQ 链接监听。一键部署器还会配置 MaiBot A-Memorix，让 B站采集、QQ 任务和 DSH 视频任务共用长期记忆。

当前版本**不包含** QQ 语音、人声提取、RVC、MeloTTS 或翻唱功能。

## 使用须知

- 本插件是**连接器**，不是完整的一键运行环境。使用前必须启动本地 BiliMemory 管理 API。
- 默认只连接本机：`http://127.0.0.1:8765`。
- 第一次使用建议先调用 `bilimemory_doctor`，再调用 `bilimemory_status`。
- B站搜索、采集和登录都依赖本机 Python、Playwright/Edge、FFmpeg 和有效登录状态。
- QQ 群聊/私聊功能需要另行运行 MaiBot OneKey、NapCat 和 QQ 监听服务。
- QQ 记忆需要 MaiBot 环境中已安装 A-Memorix；一键部署器会自动开启和配置它。
- 插件不会读取或返回 Cookie、Token、数据库内容。
- 管理 API 和向量服务不要直接暴露到公网。
- 视频采集属于长时间任务，调用后使用返回的任务 ID 查询进度，不要重复提交相同任务。

## 一键部署本地环境

下载 Release 中的 `BiliMemory-OneClick-0.2.1.zip`，解压后双击：

```text
install.cmd
```

安装器会自动：

- 检查或安装 Python 3.12、Node.js、FFmpeg
- 创建 Python 虚拟环境并安装依赖
- 安装 `local-embedding` 依赖
- 创建 `config.json`、白名单和人格示例
- 自动发现 MaiBot OneKey 配置和 Python 环境
- 自动开启 A-Memorix，并配置 `local-bge-small-zh`、512 维向量和本地 embedding 服务
- 备份 MaiBot 的 `bot_config.toml` 和 `model_config.toml` 后再写入记忆配置
- 写入用户环境变量和 `.env.local`
- 创建桌面快捷方式
- 启动 MaiBot/NapCat、向量服务、管理 API、QQ 监听和 B站私信
- 检查端口健康状态

记忆同步范围默认使用 `global`，QQ、B站和 DSH 视频任务共用同一个长期记忆库。若只想同步到指定聊天流，可使用：

```powershell
.\install.ps1 -MemoryScope chat -MemoryChatId "<A-Memorix chat_id>"
```

自动配置会开启 A-Memorix、配置本地向量模型并创建数据目录，但**不会迁移或复制已有的个人记忆数据库**。若 MaiBot 未安装或环境中没有 A-Memorix，安装器会给出警告，B站和 QQ 功能仍可继续使用，但长期记忆不可用。已有配置会先备份，再写入新配置。

安装完成后仍需手动完成：

1. 在 DSH Desktop 插件页安装 `github:binbin-QAQ/BiliMemory`
2. 在 B站浏览器中完成账号登录
3. 在 NapCat 中登录机器人 QQ
4. 在管理面板或 DSH 中添加 QQ/B站白名单
5. 在 MaiBot 和管理面板中填写 DeepSeek、视觉 API 等个人凭据

## 直接安装 DSH 插件


在 **DSH Desktop → 插件 → 安装插件** 中填写：

```text
github:binbin-QAQ/BiliMemory
```

安装包名：

```text
dsh-bilimemory
```

确认安装后重启 DSH Desktop。

> DSH Desktop 的 `desktop` profile 由 Electron 应用独占管理，不能执行 `dsh plugin --profile desktop add ...`。

CLI 测试或非 Desktop profile：

```powershell
dsh plugin --profile bilimemory-test add github:binbin-QAQ/BiliMemory
```

## 第一次使用

1. 启动本地向量服务 `127.0.0.1:11435`；一键部署器已完成 A-Memorix 基础配置。
2. 启动 BiliMemory 管理 API `127.0.0.1:8765`。
3. 在 DSH 中调用 `bilimemory_doctor`。
4. 未登录 B站时调用 `bilimemory_login`。
5. 调用 `bilimemory_status` 确认登录、向量和 MaiBot 记忆状态。
6. 使用 QQ 功能前启动 MaiBot、NapCat 和 QQ 监听，再调用 `bilimemory_qq_status`。

## 插件工具

| 工具 | 作用 |
|---|---|
| `bilimemory_status` | 查询 B站登录、向量服务、MaiBot 记忆、最近任务和私信状态 |
| `bilimemory_doctor` | 检查插件所需环境并返回修复提示 |
| `bilimemory_collect` | 采集首页、标签、分区或指定 BV/b23 链接 |
| `bilimemory_search` | 在 B站搜索视频并创建解析任务 |
| `bilimemory_task` | 查询任务进度和完整日志 |
| `bilimemory_login` | 启动 B站浏览器登录流程 |
| `bilimemory_stop` | 停止当前 B站任务 |
| `bilimemory_qq_status` | 查询 QQ 监听进程、NapCat 端口、群配置和白名单 |
| `bilimemory_qq_allowlist` | 查看、添加或移除 QQ 普通功能白名单 |
| `bilimemory_qq_restart` | 重启 QQ 链接监听进程 |

## QQ 功能

QQ 链接处理保留在 BiliMemory 后端中：

- 群聊和私聊中的 B站链接
- B站关键词搜索指令
- `/看类型`、视频状态等普通功能
- QQ 用户白名单
- NapCat OneBot 连接
- 与 B站采集、视频理解和长期记忆共用任务队列

DSH 插件提供 QQ 状态、白名单和监听重启工具，但不会授予语音、翻唱、任意 Shell 或系统控制权限。

## 调用示例

检查环境：

```text
调用 bilimemory_doctor，告诉我还缺什么。
```

采集指定视频：

```text
调用 bilimemory_collect，使用 direct 模式处理这个链接：
https://www.bilibili.com/video/BV...
```

按关键词搜索：

```text
调用 bilimemory_search，搜索“DeepSeek RAG”，最多返回 3 个结果。
```

查询任务：

```text
调用 bilimemory_task，任务 ID 是 <task-id>。
```

检查 QQ：

```text
调用 bilimemory_qq_status，检查 QQ 监听和 NapCat 是否在线。
```

添加 QQ 白名单：

```text
调用 bilimemory_qq_allowlist，把 QQ 123456789 加入链接和搜索白名单。
```

## 必需环境

### DSH

- DeepSeekHarness Desktop `0.2.1-rc.2` 或兼容版本
- Node.js `22.19+` 或 `24+`

### BiliMemory 后端

- Windows 10/11 x64
- Python 3.12
- Node.js 22+
- `requirements.txt` 中的 Python 依赖
- Playwright 可用的 Edge/Chromium 浏览器
- FFmpeg / FFprobe
- B站登录状态
- 本地 `bge-small-zh-v1.5` 向量服务：`127.0.0.1:11435`
- MaiBot OneKey + A-Memorix（长期记忆）
- NapCat（QQ 链接监听）

可选视频理解能力：

- PyTorch / Transformers
- Whisper 模型
- 支持 CUDA 的 NVIDIA 环境

## 配置后端

```powershell
git clone https://github.com/binbin-QAQ/BiliMemory.git
cd BiliMemory
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

New-Item -ItemType Directory -Force bilibili-memory\data | Out-Null
Copy-Item bilibili-memory\config.example.json bilibili-memory\config.json
Copy-Item bilibili-memory\examples\mention_allowlist.example.json bilibili-memory\mention_allowlist.json
Copy-Item bilibili-memory\examples\qq_user_allowlist.example.json bilibili-memory\data\qq_user_allowlist.json
Copy-Item bilibili-memory\examples\persona.example.md bilibili-memory\persona.md
```

环境变量：

```powershell
$env:BILIMEMORY_ROOT = 'C:\path\to\BiliMemory'
$env:MAIBOT_ROOT = 'C:\Users\<user>\AppData\Roaming\MaiBotOneKeyDesktop\<instance>\modules\MaiBot'
$env:MAIBOT_PYTHON = 'C:\Users\<user>\AppData\Roaming\MaiBotOneKeyDesktop\<instance>\modules\MaiBot\.onekey\environments\<env>\python.exe'
$env:BILIMEMORY_MEDIA_PYTHON = 'C:\path\to\BiliMemory\.venv\Scripts\python.exe'
$env:BILIMEMORY_ADMIN_URL = 'http://127.0.0.1:8765'
$env:BILIMEMORY_MEMORY_SCOPE = 'global'
```

启动本地依赖：

```powershell
cd local-embedding
npm ci
node server.mjs
```

```powershell
cd ..\bilibili-memory\admin
$env:PYTHONIOENCODING='utf-8'
$env:PYTHONUTF8='1'
node server.mjs
```

手动配置 A-Memorix（一键部署器会自动执行）：

```powershell
& $env:MAIBOT_PYTHON .\tools\configure_memory.py `
  --mai-bot-root $env:MAIBOT_ROOT `
  --embedding-url http://127.0.0.1:11435/v1 `
  --model-name local-bge-small-zh `
  --model-id bge-small-zh-v1.5 `
  --dimension 512 `
  --apply
```

该命令会先校验 TOML，再备份并写入 MaiBot 配置。省略 `--apply` 时只检查，不修改文件。

首次登录 B站：

```powershell
cd ..
python bilibili_tool.py login
```

## 常见问题

### `bilimemory_doctor` 提示 API 不可用

确认 `127.0.0.1:8765` 正在监听，并检查管理 API 窗口日志。

### 搜索或采集返回失败

确认 B站登录状态有效，Playwright 浏览器存在，FFmpeg 可用。

### 记忆功能不可用

确认 `127.0.0.1:11435` 正在运行，并在 `bilimemory_status` 中检查 MaiBot `runtimeReady` 和 `degraded`。若自动配置失败，可在 MaiBot Python 环境中运行 `tools\configure_memory.py`；不要同时手动修改模型名和向量维度。

### QQ 监听不可用

确认 NapCat 的 `7998` 端口和 QQ 监听的 `8766` 端口正在监听，然后在 DSH 中调用 `bilimemory_qq_restart`。

### 插件安装后找不到工具

重启 DSH Desktop，并确认插件页中 `dsh-bilimemory` 已启用。

## 安全

- 插件只允许连接 `localhost`、`127.0.0.1` 或 `::1`。
- 不读取或返回 Cookie、Token 和数据库内容。
- 管理 API 不应暴露到公网。
- 详细说明见 [SECURITY.md](SECURITY.md)。

## 测试

```powershell
cd bilibili-memory
python -m unittest discover -s tests -v
```