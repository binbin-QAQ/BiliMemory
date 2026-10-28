# Security Policy

## 不应提交的内容

- `storage_state.json`
- `browser-profile/`
- `config.json`
- `data/` 下的数据库、聊天记录、封面、音频和摘要
- Cookie、Access Token、API Key、密码
- 个性化表情包、人格文本和白名单
- `voice-env`、`node_modules`、`vendor`、`maibot-ok` 等大体积运行环境

这些路径均已加入 `.gitignore`。提交前仍建议运行密钥扫描。

## DSH 插件边界

- 插件只连接回环地址上的 BiliMemory API。
- 不直接读取 Cookie、模型或数据库。
- 不包含 QQ 语音、人声提取、RVC 或翻唱工具。
- 不向公网暴露管理面板或控制接口。

## 凭据处理

B站 Cookie 保存在本地 `storage_state.json`。MaiBot、DeepSeek 和视觉 API 的凭据由 MaiBot 本地配置读取，不应写入源码或示例配置。

## 漏洞报告

请不要在公开 Issue 中附带 Cookie、Token、聊天记录或可识别个人身份的数据。使用私密渠道向仓库所有者报告。
