"""回复省流闸门插件配置模型。

配置结构（对应 config.toml 的分组）：
    [plugin]  —— 插件总开关与判定模型名
    [llm]     —— 便宜判定模型的 OpenAI 兼容接口直连参数与 token 上限
    [judge]   —— 判定窗口、截断长度、判定提示词与缩小重试
    [safety]  —— 失败安全策略

多语言说明
----------
- 小节标题：通过各配置类的 ``__ui_label__`` 与 ``__ui_i18n__`` 提供中文标题与说明。
- 字段标签：通过每字段 ``json_schema_extra["label"]`` 提供中文标签，
  并通过 ``json_schema_extra["i18n"]`` 提供 zh_CN / en_US 双语文本；
  WebUI 会优先按当前语言读取 ``i18n``，再回退到 ``label``。

约定：
    - 所有字段均提供安全默认值，且默认不启用（plugin.enabled=false），
      避免开启高频判定行为影响现有运行。
    - api_key 仅保存在本地 config.toml，插件运行时不会写入日志。
    - token 估算口径统一为「字符数 ÷ 1.5」（混合中英文的粗粒度换算），
      所有估算结果在清单命令中均标注"约"。
"""

from __future__ import annotations

from maibot_sdk import Field, PluginConfigBase


def _ui(label: str, hint: str = "", placeholder: str = "") -> dict:
    """生成字段的 WebUI 元数据，集中管理中文默认标签与中英双语 i18n。"""
    meta = {
        "label": label,
        "i18n": {
            "zh-CN": {"label": label},
            "en-US": {"label": label},
        },
    }
    if hint:
        meta["hint"] = hint
        meta["i18n"]["zh-CN"]["hint"] = hint
        meta["i18n"]["en-US"]["hint"] = hint
    if placeholder:
        meta["placeholder"] = placeholder
        meta["i18n"]["zh-CN"]["placeholder"] = placeholder
        meta["i18n"]["en-US"]["placeholder"] = placeholder
    return meta


class PluginSection(PluginConfigBase):
    """插件总开关配置。

    模型名 model 是判定请求发送时所使用的模型标识（OpenAI 兼容接口上的模型名），
    与 MaiBot 自身的 model_task_config 任务名无任何关联。
    """

    # WebUI 小节标题与说明（中文优先，含英文对照）。
    __ui_label__ = "插件开关"
    __ui_i18n__ = {
        "zh-CN": {"title": "插件开关", "description": "插件总开关与判定模型名"},
        "en-US": {"title": "Plugin Switch", "description": "Plugin enable switch and judge model"},
    }

    # 是否启用回复省流闸门。默认关闭（高频判定行为需显式开启）。
    enabled: bool = Field(
        default=False,
        description="是否启用回复省流闸门",
        json_schema_extra=_ui("启用插件", "是否启用回复省流闸门，默认关闭"),
    )
    # 判定模型名。如 gpt-4o-mini。清单命令会按该名字做模型维度统计。
    model: str = Field(
        default="",
        description="用于判定的便宜模型名（OpenAI 兼容）",
        json_schema_extra=_ui("判定模型名", "OpenAI 兼容接口上的模型名，如 gpt-4o-mini", "gpt-4o-mini"),
    )
    # 配置版本号：MaiBot 要求的保留字段，WebUI 保存配置时依赖它保留在配置里。
    # 声明为 schema 字段后，runtime 的规范化配置会包含该值，保存/切换开关不会把它丢
    # 掉，否则每次 WebUI 保存都会因缺少 plugin.config_version 判定为配置非法。
    # 该字段标记为 hidden，前端不展示、不同步修改。
    config_version: str = Field(
        default="1.10.0",
        description="配置版本号，由插件自带，请勿修改",
        json_schema_extra={**_ui("配置版本", "插件配置版本号，由插件自带，请勿修改"), "hidden": True},
    )


class LlmSection(PluginConfigBase):
    """便宜判定模型直连配置。

    插件直接用 httpx 请求 {base_url}/chat/completions，不走 MaiBot 的模型任务，
    因此这里的 base_url/api_key 与 MaiBot 全局模型配置相互独立。

    两个 token 上限的关系：
    - max_total_tokens  单次判定的「输入+输出」总 token 上限（规模控制）
    - 输出上限由插件内部固定（REQUEST_MAX_OUTPUT_TOKENS），不单独配置，
      避免「输出上限 > 总预算」导致预算判定的自我冲突。
    """

    __ui_label__ = "判定模型接口"
    __ui_i18n__ = {
        "zh-CN": {"title": "判定模型接口", "description": "便宜判定模型的直连参数与 token 上限"},
        "en-US": {"title": "Judge Model API", "description": "Direct connection settings for the cheap judge model"},
    }

    # OpenAI 兼容接口地址。示例：https://api.xxx.com/v1（脚本会自动拼接 /chat/completions）。
    base_url: str = Field(
        default="",
        description="OpenAI 兼容接口地址，如 https://api.xxx.com/v1",
        json_schema_extra=_ui("接口地址", "OpenAI 兼容端点，自动拼接 /chat/completions", "https://api.xxx.com/v1"),
    )
    # API Key，仅保存在本地配置，不会写入日志。
    api_key: str = Field(
        default="",
        description="API Key，仅保存在本地配置，不会写入日志",
        json_schema_extra=_ui("API Key", "仅保存在本地，不会写入日志", "sk-..."),
    )
    # 判定温度：置 0 让结论稳定，避免模型对同一批消息时而拦住时而放行。
    temperature: float = Field(
        default=0.0,
        description="判定温度，固定为 0 保持稳定",
        json_schema_extra=_ui("判定温度", "置 0 让结论稳定，避免判定漂移", "0.0"),
    )
    # 判定请求超时（秒）：超时后按 fail-open 放行，不等便宜模型。
    timeout_sec: float = Field(
        default=10.0,
        description="判定请求超时（秒），超时按放行处理",
        json_schema_extra=_ui("判定超时(s)", "超时后按放行处理，不等便宜模型", "10.0"),
    )
    # 判定 Hook 注册超时（秒）：Host 侧给 before_request Hook 的执行上限。
    # 必须大于 llm.timeout_sec，避免慢判定被 Host 提前切断并触发插件熔断；
    # 该值是注册期静态值，修改后需重载插件才生效。
    hook_timeout_sec: float = Field(
        default=12.0,
        description="判定 Hook 注册超时（秒），须大于判定超时",
        json_schema_extra=_ui("Hook超时(s)", "判定的 Host 端执行上限，须大于判定超时；修改后需重载插件", "12.0"),
    )
    # 单次判定请求的总 token 上限（输入 + 输出）。
    # 超过时先缩小输入窗口重估，仍超则放弃本次判定并放行。
    max_total_tokens: int = Field(
        default=4096,
        description="单次判定总 token（输入+输出）上限",
        json_schema_extra=_ui("单次总token上限", "输入+输出合计上限，超限自动缩小输入窗口", "4096"),
    )


class JudgeSection(PluginConfigBase):
    """判定逻辑配置。

    控制喂给便宜模型的上下文规模与判定规则，直接影响判定质量与输入 token 成本。
    """

    __ui_label__ = "判定逻辑"
    __ui_i18n__ = {
        "zh-CN": {"title": "判定逻辑", "description": "判定窗口、截断长度、判定提示词与缩小重试"},
        "en-US": {"title": "Judging", "description": "Judging window, truncation, prompt and shrink retry"},
    }

    # 判定窗口内取最近几条用户消息（最新一条视为待处理消息，其余作为上下文）。
    max_messages: int = Field(
        default=8,
        description="判定窗口内取最近几条用户消息",
        json_schema_extra=_ui("判定窗口", "取最近几条用户消息作为上下文（最新一条为待处理消息）", "8"),
    )
    # 单条消息截断长度：折叠空白并截断超长文本，控制输入 token。
    max_chars_per_message: int = Field(
        default=200,
        description="单条消息截断长度，用于控制输入 token",
        json_schema_extra=_ui("单条截断长度", "折叠空白并截断超长文本，控制判定输入 token", "200"),
    )
    # 判定系统提示词：留空使用内置模板；如需自定义判定标准可在此覆盖。
    system_prompt: str = Field(
        default="",
        description="判定系统提示词；留空使用内置模板",
        json_schema_extra=_ui("判定提示词", "留空使用内置模板（内置会要求被@/点名/求助必须回复）"),
    )
    # 是否注入麦麦内置行为风格（config/bot_config.toml 的 [personality].behavior_style）：
    #   开启时，判定提示词会追加麦麦的「何时参与 / 何时安静」准则，让判定贴合人设；
    #   关闭时仅使用上方内置模板或自定义提示词。
    inject_behavior_style: bool = Field(
        default=True,
        description="判定提示词注入麦麦内置行为风格，贴合人设判断",
        json_schema_extra=_ui("注入行为风格", "追加麦麦的行为风格（何时参与/何时安静）到判定提示词"),
    )
    # 是否在放行（REPLY）时，把判定理由作为预判发给主 planner：
    #   开启 → 主模型直接收到"该回复"预判，可省去"是否回复"的思考段（对带 thinking 的模型更明显）；
    #   关闭（默认）→ 放行完全原样，主模型自行判断。二者都不会影响 SKIP 拦截逻辑。
    inject_reply_reason: bool = Field(
        default=False,
        description="放行时把判定理由作为预判发给主 planner，减少其思考，默认关",
        json_schema_extra=_ui("放行注入预判", "放行时把判定理由作为预判发给主 planner，减少'是否回复'的思考；默认关"),
    )
    # 是否折叠相邻完全重复的 user 消息：刷屏/重复文本只保留一条，减少历史占用。
    # 默认关（会改写主 planner 看到的历史，虽有语义无损失，先默认关闭）。
    collapse_repeated: bool = Field(
        default=False,
        description="折叠相邻完全重复的用户消息，减少刷屏占用历史",
        json_schema_extra=_ui("折叠重复消息", "把相邻完全相同的用户消息折叠为一条（语义无损失），减少刷屏占用；默认关"),
    )
    # 规则前置过滤：被@或提起机器人名字 → 直接判 REPLY 放行并跳过判定模型（默认开）。
    at_mention_reply: bool = Field(
        default=True,
        description="被@或提起机器人名字时直接放行，跳过判定模型",
        json_schema_extra=_ui("被@直接放行", "最新消息被@或提起机器人名字（昵称/别名）时直接放行主 planner，跳过判定模型"),
    )
    # 规则前置过滤：直接提问（含疑问词）→ 直接判 REPLY 放行并跳过判定模型。
    # 默认关：宽泛的提问词可能误判，先默认关闭，需要时再开。
    question_word_reply: bool = Field(
        default=False,
        description="直接提问时直接放行，跳过判定模型",
        json_schema_extra=_ui("直接提问放行", "最新消息含疑问词（吗/呢/怎么/？等）时直接放行主 planner；默认关"),
    )
    # 规则前置过滤（真实消息 @ 识别）：是否扫描最近的真实用户发言来识别 @/点名。
    # 默认开：Planner 会把时间提示、工具提醒、人物画像等注入类 user 消息追加到
    # 真实发言之后，只取最后一条 user 文本会错过真正被 @ 的发言（表现为"明明@了
    # 却被 SKIP"）。开启后 @/点名规则与判定"最新消息"都基于最近真实发言；关闭则
    # 回退到旧逻辑（只看最后一条 user 文本）。
    # 运行时可用 /replygate realmsg on|off 临时切换（仅内存），重启后以此配置为准。
    scan_recent_replies: bool = Field(
        default=True,
        description="@/点名识别扫描最近真实用户发言（默认开）；关闭回到只看最后一条的旧逻辑",
        json_schema_extra=_ui("真实消息@识别", "开启：@/点名识别与判定扫描最近 N 条真实用户发言，避免被时间/提醒/画像等注入消息顶掉（默认开）；关闭：只看最后一条 user 文本（旧逻辑）"),
    )
    # 真实消息@识别扫描的最近真实用户发言条数（仅当 scan_recent_replies 开启时生效）。
    scan_recent_count: int = Field(
        default=2,
        description="@/点名识别扫描的最近真实用户发言条数",
        json_schema_extra=_ui("扫描最近条数", "@/点名识别扫描最近几条真实用户发言；默认 2（开启真实消息@识别时才生效）", "2"),
    )
    # 旧历史摘要（token 预算式）：当主 planner 输入预估超过 summary_budget_tokens 时，
    # 把最旧的一段对话交给判定模型压成一段 system 摘要替换原文，保留最近 K 条完整消息。
    # 有损优化：默认关，需显式开启；摘要失败自动回退为全量原样放行（绝不吞消息）。
    summary_enabled: bool = Field(
        default=False,
        description="主 planner 输入超预算时用判定模型压缩旧历史为摘要",
        json_schema_extra=_ui("旧历史摘要", "主 planner 输入超预算时，把最旧对话压成摘要、保留最近 K 条（有损）；默认关"),
    )
    # 主 planner 输入预算（token 估算）：超过才触发摘要压缩。
    summary_budget_tokens: int = Field(
        default=8192,
        description="主 planner 输入预算阈值，超过触发旧历史摘要",
        json_schema_extra=_ui("输入预算(token)", "主 planner 输入超过该估算值才自动压缩旧历史；默认对齐 8192", "8192"),
    )
    # 摘要时保留的最近完整消息条数（不压缩的最新对话）。
    summary_keep_recent: int = Field(
        default=8,
        description="摘要时保留的最近完整消息条数",
        json_schema_extra=_ui("保留最近条数", "摘要时保留最近 N 条完整消息不压缩，保证最新语境", "8"),
    )
    # 是否启用"缩小重试"：
    #   - 预算超限时先截断输入到预算内；结论不清晰(UNKNOWN)时窗口减半重新判定一次，
    #     仍失败再按 fail-open 放行；输出被截断不重试，直接按已生成片段判定。
    shrink_retry_enabled: bool = Field(
        default=True,
        description="超限/结论不清晰时缩小输入重试",
        json_schema_extra=_ui("超限时缩小重试", "预算超限截断输入；结论不清晰时窗口折半重判一次；输出截断不重试"),
    )
    # 放行后"期望发送"追踪窗口（秒）：主模型产出正文的放行轮会登记一条期望发送，
    # 若在该窗口内没有任何真实发送（send_service.after_send 成功）命中，则计入
    # "放行后最终未发出"。默认 300s（5 分钟），需覆盖图片生成等耗时发送；
    # 调大可容忍更慢的生图，调小可更快结算"未发出"（但可能把仍在生图的轮次误判）。
    send_tracking_window_sec: float = Field(
        default=300.0,
        description="放行后期望发送追踪窗口（秒），默认 300 覆盖图片生成耗时",
        json_schema_extra=_ui("发送追踪窗口(s)", "放行后有正文的轮次，超过该时长仍无成功发送才计数'最终未发出'；默认 300 秒覆盖图片生成耗时", "300"),
    )


class SafetySection(PluginConfigBase):
    """安全策略配置。"""

    __ui_label__ = "安全策略"
    __ui_i18n__ = {
        "zh-CN": {"title": "安全策略", "description": "失败时是否放行"},
        "en-US": {"title": "Safety", "description": "Fail-open policy"},
    }

    # 判定失败/超时/网络错误时放行进主 planner。
    # 默认开启：宁可多花一次主模型 token 也不吞消息，保证不漏回复。
    fail_open: bool = Field(
        default=True,
        description="判定失败/超时/网络错误时放行进主 planner，宁可多花 token 也不吞消息",
        json_schema_extra=_ui("失败自动放行", "判定失败/超时/网络错误时放行进主 planner，不吞消息"),
    )


class ReplyGateConfig(PluginConfigBase):
    """插件根配置，聚合各分组。"""

    __ui_label__ = "回复省流闸门"
    __ui_i18n__ = {
        "zh-CN": {"title": "回复省流闸门", "description": "用便宜模型预判是否回复，节省主模型 token"},
        "en-US": {"title": "Reply Gate", "description": "Pre-judge whether to reply with a cheap model to save tokens"},
    }

    plugin: PluginSection = Field(default_factory=PluginSection)
    llm: LlmSection = Field(default_factory=LlmSection)
    judge: JudgeSection = Field(default_factory=JudgeSection)
    safety: SafetySection = Field(default_factory=SafetySection)