# DSH 插件安装与使用

## 安装包结构

```text
package.json          npm / DSH 插件清单
cordis.patch.yml      Cordis 插件注入
lib/index.js          Host 工具入口
```

DSH 从 `package.json.dsh.bundle.patch` 加载 `cordis.patch.yml`，再把 `dsh-bilimemory` 插入当前 profile。

## 安装

在 DSH Desktop 的插件页选择从 GitHub 安装，并填写：

```text
github:binbin-QAQ/BiliMemory
```

或者安装 Release 文件：

```text
dsh-bilimemory-0.2.0.tgz
```

Desktop 的 `desktop` profile 由 Electron 应用独占，不能通过下面的 CLI 命令修改。CLI 只建议用于其他 profile：

```powershell
dsh plugin --profile bilimemory-test add github:binbin-QAQ/BiliMemory
```

## 使用须知

1. 插件只是连接器，必须启动 BiliMemory 管理 API。
2. 默认后端地址是 `http://127.0.0.1:8765`。
3. 先调用 `bilimemory_doctor`，再调用 `bilimemory_status`。
4. B站采集需要登录、Playwright/Edge 和 FFmpeg。
5. 长期记忆需要本地向量服务和 MaiBot A-Memorix。
6. QQ 功能需要 MaiBot OneKey、NapCat 和 QQ 监听服务。
7. 长时间采集任务通过任务 ID 查询，不要重复提交。
8. 不要把管理 API 暴露到公网。

## 工具

### `bilimemory_doctor`

检查 API、B站登录、向量服务、MaiBot 记忆和私信服务。

### `bilimemory_status`

返回 B站登录、向量服务、MaiBot 健康状态、当前任务和最近任务。

### `bilimemory_collect`

支持以下模式：

- `home`：首页推荐
- `tag`：标签或关键词
- `section`：B站分区 URL
- `direct`：BV 或 b23 链接

### `bilimemory_search`

搜索 B站并返回后端解析任务 ID。

### `bilimemory_task`

按任务 ID 读取状态、退出码和日志。

### `bilimemory_login`

打开浏览器进行 B站登录。

### `bilimemory_stop`

停止当前 B站任务。

### `bilimemory_qq_status`

查询 QQ 监听进程、NapCat OneBot 端口、群配置、搜索策略和 QQ 用户白名单状态。

### `bilimemory_qq_allowlist`

查看、添加或移除 QQ 用户白名单。当前只支持：

- `links`：普通 B站链接
- `search`：B站关键词搜索

语音、翻唱、代码执行和系统控制权限不会通过该白名单开放。

### `bilimemory_qq_restart`

重启 QQ 链接监听进程。需要 NapCat 和已配置的机器人 QQ。

QQ 监听负责群聊/私聊 B站链接、搜索指令、视频状态和白名单校验；它不包含 QQ 语音、人声提取或翻唱。

## 配置

```text
BILIMEMORY_ADMIN_URL=http://127.0.0.1:8765
```

出于安全考虑，插件只允许连接本机回环地址。

## 验证安装

```powershell
dsh --profile bilimemory-test --dump-config
```

输出应包含：

```yaml
- id: dsh-bilimemory
  name: dsh-bilimemory
  config:
    adminUrl: http://127.0.0.1:8765
```

重启 DSH Desktop 后，在工具列表中可以找到 `bilimemory_*` 工具。

## 卸载

在 DSH Desktop 插件页停用并移除 `dsh-bilimemory`。非 Desktop profile 可使用：

```powershell
dsh plugin --profile bilimemory-test remove dsh-bilimemory
```