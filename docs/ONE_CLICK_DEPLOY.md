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

## 使用

下载 `BiliMemory-OneClick-0.2.0.zip`，解压后双击：

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

手动指定 MaiBot：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\install.ps1 `
  -MaiBotRoot "C:\Users\you\AppData\Roaming\MaiBotOneKeyDesktop\<instance>\modules\MaiBot" `
  -MaiBotPython "C:\Users\you\AppData\Roaming\MaiBotOneKeyDesktop\<instance>\modules\MaiBot\.onekey\environments\<env>\python.exe" `
  -MaiBotExe "C:\Program Files\MaiBot OneKey\MaiBot OneKey.exe"
```

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

先安装并运行一次 MaiBot OneKey，再重新运行安装器；也可以使用 `-MaiBotRoot`、`-MaiBotPython`、`-MaiBotExe` 手动指定。

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