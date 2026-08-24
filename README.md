# 回复省流闸门 (reply-gate-mai)

用你自己配置的**便宜模型**，在 MaiBot 进入 Planner 前先判定这一轮该不该回复：

```
新消息 → 本地规则放行 → 进入 Planner → before_request Hook
                                    │ 插件用你的便宜模型直连判定（base_url + api_key + model）
                     SKIP(不必说话)  │   REPLY(该说话)
                          ▼         │        ▼
                屏蔽主 planner：      │   原样放行 → MaiBot 主 planner
               输入压到几十 token     │   重新完整规划 → 正式回复
               输出清空 → 不回复       │
```

- 便宜模型判定"不必回复" → 屏蔽主 planner，本轮以"不回复"收尾，主模型几乎零消耗
- 便宜模型判定"该回复" → 原样放行，交给 MaiBot 主 planner 完整规划回复内容

## 通过 Git 安装与更新

本插件以独立 Git 仓库发布，可直接 clone 安装、`git pull` 更新：

```bash
# 安装：clone 到你的 MaiBot plugins 目录（目录名保持 reply-gate-mai）
git clone https://github.com/haitai-git/reply-gate-mai.git <MaiBot 根目录>/plugins/reply-gate-mai

# 更新：在插件目录内拉取最新版本
cd <MaiBot 根目录>/plugins/reply-gate-mai
git pull
```

说明：

- **`config.toml` 不随仓库发布**（已通过 `.gitignore` 排除）：每个用户各自的配置（判定模型、接口地址、API Key）只存在于本地，`git pull` 更新**永远不会与本地配置产生 git 冲突**。
- 首次启用时 MaiBot 会根据 `config.py` 的默认值自动生成 `config.toml`，只需按下方「启用步骤」填写自己的判定模型配置即可。
- 更新后建议重载插件（WebUI → 插件管理 → 重载）或重启机器人；大版本更新请留意 `CHANGELOG.md`。

## 启用步骤

1. **填评判模型配置**（`config.toml`）：
   ```toml
   [plugin]
   enabled = true
   model = "gpt-4o-mini"        # 你选定的便宜模型名（OpenAI 兼容接口上的模型名）

   [llm]
   base_url = "https://api.xxx.com/v1"   # OpenAI 兼容端点
   api_key = "sk-..."                    # 仅保存在本地，不会写入日志
   temperature = 0.0
   timeout_sec = 10.0                    # 判定请求超时（秒），超时按放行处理
   hook_timeout_sec = 12.0               # 判定 Hook 注册超时（秒，须大于 timeout_sec；改后重载生效）
   max_total_tokens = 4096               # 单次判定总 token（输入+输出）上限，超限自动缩窗

   [judge]
   max_messages = 8                      # 判定窗口最近消息数
   max_chars_per_message = 200           # 单条截断长度
   system_prompt = ""                    # 留空使用内置判定规则
   inject_behavior_style = true          # 判定时注入麦麦内置行为风格（bot_config.toml）
   inject_reply_reason = false           # 放行时把判定理由作为预判发给主 planner（省思考，默认关）
   collapse_repeated = false             # 折叠相邻完全重复的用户消息（刷屏减负，默认关）
   at_mention_reply = true               # 被@/提起机器人名字直接放行，跳过判定模型（默认开）
   question_word_reply = false           # 直接提问（含疑问词）直接放行（默认关）
   scan_recent_replies = true            # 真实消息@识别：@/点名与判定扫描最近真实发言（默认开）
   scan_recent_count = 2                 # 真实消息@识别扫描的最近真实发言条数（默认 2）
   summary_enabled = false               # 主 planner 输入超预算时用判定模型压缩旧历史（有损，默认关）
   summary_budget_tokens = 8192          # 主 planner 输入预算，超过触发旧历史摘要
   summary_keep_recent = 8               # 摘要时保留的最近完整消息条数
   shrink_retry_enabled = true           # 超限/结论不清晰时缩小输入重试
   send_tracking_window_sec = 300        # 放行后"期望发送"追踪窗口(s)，须覆盖图片生成耗时（默认 300）

   [safety]
   fail_open = true
   ```
2. **重载插件**（WebUI 或重启机器人）。
3. **验证**：在任意会话发消息触发思考，日志里能看到
   `[回复省流闸门] 判定=SKIP/REPLY 理由: ...`。

## 清单命令

- `/replygate status` — 开关状态 + 判定统计概要（含节省估算一行）
- `/replygate savings` — 完整清单：判定/拦截/放行/失败次数、token 总量估算、净节省估算、按模型明细、按会话 Top5
- `/replygate model` — 当前判定模型配置 + 按模型的拦截/放行次数、放行后未回复次数与 token 消耗明细
- `/replygate realmsg [on|off]` — 查看/临时开关『真实消息@识别』（仅内存，重启后以 `[judge].scan_recent_replies` 为准）
- `/replygate on` / `/replygate off` — 临时启停（仅内存，重启后以 `config.toml` 为准）

示例（`/replygate savings`）：
```
回复省流闸门 · Token 节省估算（约）
判定轮次: 120 次
拦截次数: 48 次
放行次数: 70 次
失败/放弃放行: 2 次
单次总token上限: 4096
主 planner 输入总估算: 480,000 tok
其中 SKIP 拦截估算: 96,000 tok
判定模型实际消耗: 6,200 tok
净节省估算: ~89,800 tok
触发缩小重试: 5 次 | 输出达上限: 0 次

按模型明细:
  gpt-4o-mini: 判定 120 次 | 拦截 48 | 放行 70 | 消耗 6,200 tok | 拦截节约 96,000 tok

按会话明细（Top 5）:
  session-A: 拦截 12 次(节约 19,200 tok) | 放行 18
  ...
```

## 判定规则（内置系统提示词，可在 `judge.system_prompt` 覆盖）

- **REPLY**：明确 @ / 点名麦麦、直接提问 / 求助 / 请求、询问意见，或语境迫切需要麦麦回应。
- **SKIP**：闲聊、内容分享、两人互聊、话题与麦麦无关、水群刷屏等。

## Token 口径与说明

- 估算口径统一为「字符数 ÷ 1.5」（混合中英文粗粒度平均），清单中均标注"约"，非精确计费。
- 主 planner 输入估算统计**全部消息**（user/assistant/system）的文本，口径同为「字符数 ÷ 1.5」，加上固定的 system/结构化开销；因此 SKIP 轮拦截节约 ≈ 该轮完整输入估算，能反映真实的主模型消耗（例如每轮完整输入约 8000 token 时，每次拦截即节约约 8000 token）。
- 判定模型消耗优先取接口返回的 `usage.total_tokens`，接口不返回时回退估算。
- 单次判定 token：输入约 600~1,900（默认窗口），输出通常仅 10~40。`max_total_tokens` 是整请求的规模控制；输出上限由插件内部固定为 512，不单独配置，避免与总预算相互冲突。
- 达到 `max_total_tokens` 预算极限时：自动折半窗口/单条长度重估；即使缩到最小窗口仍超，也按截断后的片段照常判定，不会放弃本轮。
- `inject_behavior_style=true` 时，判定提示词会自动追加麦麦内置行为风格（`config/bot_config.toml` 的 `[personality].behavior_style`，与 Planner 同源），让便宜模型按麦麦的「何时参与 / 何时安静」偏好判断；关闭后只用内置模板或 `system_prompt`。读取失败会记日志并自动回退为不注入，不阻塞判定。
- `inject_reply_reason=true` 时，放行（REPLY）轮会前插一条预判消息（`[预判] 已确认需要回复，请直接组织回复内容`，理由放句尾以保持前缀稳定、利于 provider 缓存）给主 planner，省去主模型"是否回复"的思考段——对带 thinking 的模型收益明显，对 deepseek-v4-flash 这类短输出模型净省有限（可用 `/replygate savings` 对比）。默认关，关闭时放行完全原样；不影响 SKIP 拦截与清空。
- **放行后"最终未发出"统计（v1.10.0 新增）**：主模型产出正文的放行轮会登记一条"期望发送"，若在 `send_tracking_window_sec`（默认 300s，5 分钟）内没有任何真实发送（`send_service.after_send` 成功，含文本/图片等）命中，则计入"最终未发出"。窗口须覆盖图片生成等耗时发送；`status`/`savings`/`model` 清单均会展示该次数。与"主模型未回复"区分：后者是主模型连正文都没产出（只思考/只调工具未说话），前者是产出了正文但群里最终没看到消息。
- **省 token 优化**：判定输出上限内部固定 256；SKIP 占位 user 消息仅保留最新消息前 40 字符；`collapse_repeated=true` 时可折叠相邻完全重复消息（默认关）；`at_mention_reply=true`（默认开）与 `question_word_reply=false`（默认关）是规则前置过滤，只做放行侧决策，不会因规则拦截任何消息（点名/提问轮直接放行、跳过判定模型，省掉判定 token 和延迟）；`summary_enabled=true` 时，当主 planner 输入预估超过 `summary_budget_tokens`（默认 8192），会把最旧对话交给判定模型压缩成 `[历史摘要]`（带指纹缓存、摘要失败自动回退全量），保留最近 `summary_keep_recent` 条完整消息——有损优化，默认关，放行轮生效。

## 安全与边界

- **失败安全（fail-open）**：判定超时、网络错误、解析异常一律放行，绝不吞消息；预算超限只会截断输入，不会放弃判定。
- **`@/点名` 判定已覆盖富文本消息**：含 @/引用等被序列化成「字符串列表」的消息（如 `['<message …>', '@佑树', ' 生成图片：…']`）现在会被正确提取文本；此前这类消息会被当成空文本静默丢弃，导致"明明 @ 了却被 SKIP"。
- **@ 与强制触发受保护**：被 @ 或强制触发的消息仍按现有机制进入 planner；判定 prompt 也要求"@/点名/直接求助必须 REPLY"兜底。
  - 受 `scan_recent_replies=true`（默认）保护：`@/点名` 硬规则与判定"最新消息"会扫描**最近真实用户发言**，跳过 Planner 在消息末尾追加的时间提示/工具提醒/人物画像等注入类消息，避免"明明 @ 了却被当成最新消息是空时间戳而误判 SKIP"；改 `false`（或 `/replygate realmsg off`）可回退到只看最后一条 user 文本的旧逻辑。
- **边界（如实说明）**：`maisaka.planner.before_request` Hook 允许改参但不允许中止请求，因此"不必回复"的轮次主模型仍会被回调 1 次，但输入只有几十 token、输出被清空，成本约为完整 Planner 的 2%~5%。彻底零调用需修改核心调度逻辑，本插件不涉及。
- 统计与判定缓存仅存内存，重启清零。

## 排错

| 现象 | 处理 |
|---|---|
| 日志"判定模型配置不完整" | 检查 `plugin.model` / `llm.base_url` / `llm.api_key` 是否填写 |
| 日志"判定失败，按放行处理" | 检查网络、Key 余额、模型名；确认端点支持 `/chat/completions` |
| 统计里 fail 持续增长 | 调大 `llm.timeout_sec`，或临时 `/replygate off` |
| 日志频繁"缩窗/重试" | 说明该模型输入较长适合缩窗；可调大 `llm.max_total_tokens` |
| 明明 @ 了或发了请求却显示"最新消息只是黑话参考"后 SKIP | 插件已忽略 `[黑话参考]`、`[行为表现参考]`、`[已折叠的历史工具调用]` 等注入消息；若仍误判，可临时 `/replygate realmsg off` 回退旧逻辑，或调大 `judge.scan_recent_count` |
| "最终未发出"计数偏高 | 多为图片生成等耗时发送超过 `judge.send_tracking_window_sec`（默认 300s），把该字段调大（如 600）即可；若想更快观察"未发出"再调小 |
| 想彻底关闭 | `config.toml` 改 `enabled = false` 后重载插件 |