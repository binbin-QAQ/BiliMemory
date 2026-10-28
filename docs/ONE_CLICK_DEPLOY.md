# 一键部署说明

## 支持范围

`install.cmd` 可以自动完成：

- Python 3.12、Node.js 和 FFmpeg 缺失检测
- 使用 winget 安装缺失基础软件
- 创建 `.venv`
- 安装 Python 依赖
- 安装 Playwright Chromium
- 安装本地向量服务依赖
- 创建示例配置和数据目录
- 自动发现 MaiBot OneKey 配置目录、Python 环境和可执行文件
- 自动开启 A-Memorix，并配置 `local-bge-small-zh`、512 维向量和 `LocalEmbedding` provider
- 备份 MaiBot 配置文件后写入记忆配置，并创建 `data\a-memorix` 数据目录
- 写入 `.env.local` 和用户环境变量
- 创建桌面“一键启动”快捷方式
- 启动 MaiBot/NapCat、向量、管理 API、QQ 监听和 B站私信

## 不自动完成

以下步骤需要用户本人操作：

- DSH Desktop 插件页点击安装 `github:binbin-QAQ/BiliMemory`
- B站扫码登录
- QQ/NapCat 登录机器人账号
- 选择 QQ 群、主人 QQ、B站 UID 和白名单
- DeepSeek / 视觉 API 凭据配置
- 第三方模型许可确认
- 迁移已有的个人 A-Memorix 数据库（自动配置不会复制旧数据）

## 使用

下载 `BiliMemory-OneClick-0.2.1.zip`，解压后双击：

```text
install.cmd
```

安装器会调用：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\install.ps1 -InstallPrerequisites
```

静默检查：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\install.ps1 -DryRun -NoStart
```

只安装不启动：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\install.ps1 -InstallPrerequisites -NoStart
```

指定记忆同步范围：

```powershell
# 默认：global，QQ、B站和 DSH 视频任务共用同一个长期记忆库
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\install.ps1 -MemoryScope global

# 指定聊天流：必须提供有效的 A-Memorix chat_id
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\install.ps1 `
  -MemoryScope chat `
  -MemoryChatId "<chat_id>"
```

只跳过 A-Memorix 配置：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\install.ps1 -SkipMemoryConfiguration
```

手动指定 MaiBot：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\install.ps1 `
  -MaiBotRoot "C:\Users\you\AppData\Roaming\MaiBotOneKeyDesktop\<instance>\modules\MaiBot" `
  -MaiBotPython "C:\Users\you\AppData\Roaming\MaiBotOneKeyDesktop\<instance>\modules\MaiBot\.onekey\environments\<env>\python.exe" `
  -MaiBotExe "C:\Program Files\MaiBot OneKey\MaiBot OneKey.exe"
```

## A-Memorix 自动配置

安装器使用 MaiBot 自己的 Python 环境运行 `tools\configure_memory.py`。它只会修改以下配置：

- 开启 `[a_memorix.plugin]`
- 设置 `data_dir = "data/a-memorix"`
- 使用 `local-bge-small-zh`、`bge-small-zh-v1.5` 和 512 维向量
- 将 `LocalEmbedding` provider 指向 `http://127.0.0.1:11435/v1`
- 将 embedding 任务模型设为 `local-bge-small-zh`

写入前会解析并校验 TOML；写入时会保留带时间戳的 `.bilimemory-backup-*` 备份。安装器不会下载、读取、上传或迁移用户已有的记忆数据库。

如果安装结束时显示 `Memory ready: False`，先确认 MaiBot OneKey 已安装 A-Memorix，再运行上面的手动命令并检查 MaiBot Python 环境。

## 安装后检查

```text
http://127.0.0.1:8765
```

或在 DSH 中调用：

```text
bilimemory_doctor
bilimemory_status
bilimemory_qq_status
```

## 启停

- `start-all.cmd`：启动全部本地服务
- `stop-all.cmd`：停止全部本地服务
- `install.cmd`：重新执行部署检查，不会覆盖已有配置
- 日志：`logs\`

## 常见问题

### winget 安装失败

手动安装 Python 3.12、Node.js 22+、FFmpeg，然后重新运行 `install.cmd`。

### 找不到 MaiBot

先安装并运行一次 MaiBot OneKey，再重新运行安装器；也可以使用 `-MaiBotRoot`、`-MaiBotPython`、`-MaiBotExe` 手动指定。若 MaiBot 中不存在 `src.A_memorix`，请先在 MaiBot OneKey 中升级或重装带 A-Memorix 的版本。

### 记忆配置失败

先用只检查模式运行配置器：

```powershell
& $env:MAIBOT_PYTHON .\tools\configure_memory.py --mai-bot-root $env:MAIBOT_ROOT
```

再确认 127.0.0.1:11435 可用，并检查 MaiBot config 目录中的 .bilimemory-backup-* 备份和安装日志。

### B站登录失败

运行：

```powershell
.\.venv\Scripts\python.exe bilibili-memory\bilibili_tool.py login
```

### QQ 不在线

确认 NapCat 已登录机器人 QQ，并且 `7998`、`8766` 端口正在监听。

## 安全提醒

- 安装器不会下载或上传用户的 Cookie、聊天记录和数据库。
- 不把 8765、8766、8768、11435 暴露到公网。
- QQ 连接仍由 MaiBot/NapCat 管理，BiliMemory 只使用 OneBot 接口。