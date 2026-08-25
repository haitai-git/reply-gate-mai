# 变更记录

## v1.2.0 (2026-08-25) —— 适配 MaiBot 1.2.0+ Hook 载荷，修复若干误判

- **MaiBot 1.2.0 起的破坏性适配**：Planner Hook 载荷从旧式 `messages`/`response` 迁移为 Context Item 结构（`items` / `output_items` + `item_schema_version`）。旧版插件在 MaiBot 1.2.0+ 会"判定不生效 / SKIP 清空失效"；本版本已完整适配（详见 README「兼容性要求」）。
- **@ 与点名单开关拆分**：原 `judge.at_mention_reply` 拆分为 `judge.at_button_reply`（真 @，默认开）与 `judge.name_mention_reply`（口头点名，默认关），配置版本升至 1.11.0（迁移说明见 README）。
- **修复"没 @ 却被判被@"**：忽略 MaiBot 1.2.0+ 新增的记忆/参考注入消息（聊天回想、启发式记忆、长期记忆检索、上下文恢复、行为表现情景分析约束等），避免总结里提到机器人名字被误判。
- **修复假 REPLY**：判定结论正则改为锚定行首，`needed** response: SKIP: …` 这类带杂前缀的输出不再被误读为 REPLY，走缩小重试或安全放行。
- **占位载荷加固**：SKIP 占位 / 预判注入 / 折叠-摘要改写的 `modified_kwargs` 兜底写入 `item_schema_version`，避免宿主因缺失而静默丢弃改写导致拦截退化。
- **可观测性**：判定成功新增 debug 日志，打印判定模型原始输出与解析结论，便于排查判定漂移。

## v1.1.0 (2026-08-24)

- 判定上下文瘦身：图片/表情包/语音消息的长描述统一压缩为 `[图片]` 占位符（判定只需知道消息类型，显著减小输入 token；`@`/点名文本不受影响）
- 判定输出 token 上限 256 → 96：判定只需 `REPLY/SKIP + 一句话理由`，实测 20~60 已足够，让总预算更多留给输入
- 插件 ID 迁移为作者命名空间 `haitai-git.reply-gate-mai`
- 发布规范：新增 `.gitignore`（排除 `config.toml` / `config_back/`）、`LICENSE`（AGPL-3.0）、本文件