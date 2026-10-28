# BiliMemory 后端部署

DSH 插件通过本地 HTTP API 调用 BiliMemory。插件安装不会自动安装 Python、MaiBot、NapCat、浏览器或向量模型。

## 1. 环境

- Windows 10/11 x64
- Python 3.12
- Node.js 22+
- MaiBot OneKey
- NapCat（QQ 功能）
- Playwright 兼容浏览器
- FFmpeg / FFprobe
- 可选 NVIDIA CUDA 环境

## 2. 安装 Python 依赖

```powershell
cd C:\path\to\BiliMemory
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## 3. 创建配置

```powershell
New-Item -ItemType Directory -Force bilibili-memory\data | Out-Null
Copy-Item bilibili-memory\config.example.json bilibili-memory\config.json
Copy-Item bilibili-memory\examples\mention_allowlist.example.json bilibili-memory\mention_allowlist.json
Copy-Item bilibili-memory\examples\qq_user_allowlist.example.json bilibili-memory\data\qq_user_allowlist.json
Copy-Item bilibili-memory\examples\persona.example.md bilibili-memory\persona.md
```

填写：

- B站主人 UID
- QQ 群号与主人 QQ
- 白名单
- MaiBot 记忆目标
- 媒体理解选项

## 4. 环境变量

```powershell
$env:BILIMEMORY_ROOT = 'C:\path\to\BiliMemory'
$env:MAIBOT_ROOT = 'C:\Users\<user>\AppData\Roaming\MaiBotOneKeyDesktop\<instance>\modules\MaiBot'
$env:MAIBOT_PYTHON = 'C:\Users\<user>\AppData\Roaming\MaiBotOneKeyDesktop\<instance>\modules\MaiBot\.onekey\environments\<env>\python.exe'
$env:BILIMEMORY_MEDIA_PYTHON = 'C:\path\to\BiliMemory\.venv\Scripts\python.exe'
$env:BILIMEMORY_ADMIN_URL = 'http://127.0.0.1:8765'
```

## 5. A-Memorix 长期记忆

先启动本地 embedding 服务，再让 MaiBot 的 Python 环境执行配置器：

```powershell
& $env:MAIBOT_PYTHON .\tools\configure_memory.py `
  --mai-bot-root $env:MAIBOT_ROOT `
  --embedding-url http://127.0.0.1:11435/v1 `
  --model-name local-bge-small-zh `
  --model-id bge-small-zh-v1.5 `
  --dimension 512 `
  --apply
```

配置器会开启 A-Memorix、设置 `data/a-memorix`、写入 embedding 模型与 provider，并在修改前备份 `bot_config.toml` 和 `model_config.toml`。省略 `--apply` 时只检查。

默认建议在 `bilibili-memory\config.json` 中设置：

```json
{
  "memory_scope": "global",
  "chat_id": ""
}
```

若要使用聊天流范围，将 `memory_scope` 改为 `chat` 并填写有效 `chat_id`。

## 6. 本地向量服务

```powershell
cd local-embedding
npm ci
node server.mjs
```

默认监听 `127.0.0.1:11435`。

## 7. 管理 API

```powershell
cd bilibili-memory\admin
$env:PYTHONIOENCODING='utf-8'
$env:PYTHONUTF8='1'
node server.mjs
```

默认监听 `127.0.0.1:8765`。

## 8. B站登录

```powershell
cd ..
python bilibili_tool.py login
```

完成后会生成 `storage_state.json` 和 `browser-profile`，两者都不会进入 Git。

## 9. QQ 集成

在 MaiBot OneKey 中启动：

- NapCat WebUI：`6099`
- NapCat OneBot：`7998`
- MaiBot WebUI：`8001`

然后启动：

```powershell
python qq_link_watcher.py
python bili_dm_service.py --run
```

QQ 白名单和 B站 @ 白名单可在管理面板维护。

## 10. 视频理解可选依赖

字幕可直接读取。没有字幕时，可安装 Whisper/Transformers 和 FFmpeg；视觉抽帧使用 OpenCV。CPU 可运行，NVIDIA CUDA 会明显加快处理。

## 11. 检查

```powershell
python bilibili_tool.py status
Invoke-RestMethod http://127.0.0.1:11435/health
Invoke-RestMethod http://127.0.0.1:8765/api/status
```
