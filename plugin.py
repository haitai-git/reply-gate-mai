"""回复省流闸门 —— 用自配便宜模型在进入 Planner 前判定该不该回复。

背景
----
MaiBot 在每轮消息经过本地规则判定放行后，会进入 Planner 调用主模型做完整规划。
即使主模型最终决定"不回复"，这一整轮仍会消耗完整上下文的 token。

本插件利用 MaiBot 插件 SDK 的命名 Hook：
    1. `maisaka.planner.before_request` ：
        在 Planner 请求真正发出前触发（允许改写入参，但不允许中止请求）。
        插件用自己的便宜模型直连判定本批消息是否值得回复：
            - 判定 SKIP（不必回复）：
                把发给主 planner 的消息压缩为「占位 system + 最新一条用户消息」，
                同时清空工具定义 → 主模型输入从数千 token 降到几十 token。
            - 判定 REPLY（该回复）：
                原样放行，把规划权完全交还给 MaiBot 主 planner。
    2. `maisaka.planner.after_response` ：
        在主模型返回后触发。若本会话最近一次判定为 SKIP，
        则把响应正文与工具调用全部清空，让本轮以"不回复"自然收尾。

token 控制
----------
- max_total_tokens：单次判定「输入+输出」总 token 上限（默认 4096）。
  超限时插件会自动把输入窗口折半后重估；即使缩到最小窗口仍超预算，
  也会按截断后的最小片段照常判定，不会放弃本轮。
- 输出上限由插件内部固定为 REQUEST_MAX_OUTPUT_TOKENS（96），不单独配置，避免
  与总预算冲突（判定只需 REPLY/SKIP + 一句话理由，用不到更大输出）。

安全策略
--------
判定阶段任何异常（网络错误、超时、解析失败）都会按"放行"处理，
绝不会因为便宜模型不稳定而导致漏回复，宁可多花主模型 token 也不吞消息。

说明：由于 Hook 不允许 abort，SKIP 轮主模型仍会被回调 1 次，
但输入极小且输出被清空，成本约为完整 Planner 的 2%~5%。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

import asyncio  # 异步等待与超时控制
import hashlib  # 被摘历史指纹（摘要缓存命中判断）
import os  # 定位插件自身目录以只读 config.toml
import re  # 判定结果文本解析
import time  # 统计时间戳
import tomllib  # 解析主配置 bot_config.toml（Python 3.11+ 标准库，只读）
import uuid  # 生成 Context Item 快照的 item_id

from datetime import datetime  # 生成 Context Item 快照的 meta.timestamp

import httpx  # OpenAI 兼容接口直连

from maibot_sdk import Command, HookHandler, MaiBotPlugin
from maibot_sdk.types import ErrorPolicy, HookMode, HookOrder

from .config import ReplyGateConfig

# ==================== 判定提示词 ====================

# 内置判定规则：便宜模型看到的上下文很短，必须把判定标准写清楚，
# 减少"该不该回复"的误判。用户可通过 judge.system_prompt 覆盖。
DEFAULT_JUDGE_SYSTEM_PROMPT = """你是麦麦（MaiBot）的回复必要性预判员，任务是根据对话判定麦麦这一轮该不该回复。
判定标准：
- REPLY：消息里明确 @ 或点名麦麦、直接向麦麦提问/求助/请求、询问意见，或语境迫切需要麦麦回应。
- SKIP：闲聊、内容分享、两人互聊、话题与麦麦无关、纯打卡/刷屏/水群、或只是无意义的话。

只输出一行：REPLY（必回）或 SKIP（不必回），然后跟一个冒号和一句话理由，例如：
REPLY: 有人在直接问麦麦怎么看
SKIP: 群友在聊与麦麦无关的话题
不要输出其他任何内容。"""

# SKIP 轮发给主 planner 的占位 system 提示。
# 目的是让主模型在失去全部历史上下文的情况下依然输出一个可控的极短标记，
# 配合 after_response 清空输出，保证本轮绝不会发出任何内容。
SLIM_SYSTEM_PROMPT = "你是 MaiBot 的占位轮处理器。当前已判定本批消息无需回应，请不要调用任何工具，直接回复两个字母：SKIP"

# 用于从模型输出文本中提取判定结论的正则。
# 必须是「行首结论词」：只允许输出以少量空白/符号装饰（括号、引号、星号、短横、冒号等）
# 后紧跟 REPLY/SKIP 才采信。若之前有较长的杂前缀（例如 "needed** response: SKIP: ..."），
# 不匹配 → 判 UNKNOWN → 走缩小重试或 fail-open，避免把混乱输出误读成 REPLY/SKIP。
VERDICT_PATTERN = re.compile(
    r"^\s*[\-–—*•·\"'“”‘’「」【】\[\]()（）<>:：,，.]*\s*(REPLY|SKIP)\b",
    re.IGNORECASE,
)

# 判定上下文中的媒体描述（图片/表情包/语音）只保留占位符：
# 判定只需知道"这条消息是发图/表情/语音"，整段描述对要不要回复无帮助，
# 却可能占满整条截断预算，把它压成 [图片] 可显著减小判定输入。
MEDIA_BRACKET_RE = re.compile(r"\[(?:图片|表情包|语音消息)[^\]]*\]")


def _strip_media_brackets(text: str) -> str:
    """把图片/表情包/语音消息的描述占位压缩为 [图片]。"""
    if not text:
        return text
    return MEDIA_BRACKET_RE.sub("[图片]", text)

# 预估"主 planner 完整输入"时，为 system 提示与结构化开销预留的固定 token 底数。
BASE_SYSTEM_TOKEN_ESTIMATE = 512

# 判定请求中，除正文内容外的固定结构性开销（分隔符、字段名等）的 token 估算。
REQUEST_FIXED_OVERHEAD_TOKENS = 64

# 判定请求的输出 token 上限：请求体 max_tokens、预算预估中的输出余量、
# 以及"输出接近截断"的判定基准都使用该固定值（不单独配置）。
REQUEST_MAX_OUTPUT_TOKENS = 96

# 判定请求本身带内容长的 min/max 系数，用于把字符数换算成 token 的粗粒度估算。
CHARS_PER_TOKEN = 1.5

# SKIP 轮发给主 planner 的占位 user 消息最大长度（瘦身：只保留最新消息的最前片段，
# 让主模型稳定回 SKIP 两个字母即可，比全文每次再省几十 token）。
SLIM_MAX_USER_CHARS = 40

# 规则前置过滤：@/点名扫描的"最近真实用户发言"条数。
# Planner 会把时间提示、工具提醒、人物画像等注入类 user 消息追加到真实发言之后，
# 只取 user_texts[-1] 会错过真正被 @ 的发言，因此改为扫描最近 N 条真实发言。
REAL_MSG_REPLY_SCAN_RECENT = 2

# 判定上下文与规则过滤中应跳过的注入/装饰类 user 文本前缀（非真实聊天发言）。
# 命中任一前缀即视为非真实发言；真实发言（含 @、回复引用等）不在此列。
# 注意：MaiBot 会在真实发言之后追加黑话参考([黑话参考])、行为表现参考([行为表现参考])、
# 已折叠工具调用([已折叠的历史工具调用])、聊天回想/启发式记忆/长期记忆检索/人物画像等
# user 角色注入消息，若不忽略它们，会把真正的发言挤出 @/点名 规则的"最近真实发言"
# 扫描窗口（注入总结里通常提到机器人名字），导致"没 @ 也被判成被@，且误判 SKIP"。
SKIPPED_USER_TEXT_PREFIXES = (
    "时间：",
    "<system-reminder>",
    "【人物画像",
    "【历史摘要",
    "【聊天回想",
    "【启发式记忆",
    "【长期记忆检索结果",
    "[预判]",
    "[黑话参考]",
    "[行为表现参考]",
    "[行为表现情景分析约束]",
    "[已折叠的历史工具调用]",
    "[上下文恢复]",
    "[参考消息]",
    "[回复效果评分任务]",
    "[表情包选择任务]",
    "[表情包拼图候选]",
    "当前聊天额外注意事项：",
)

# 规则前置过滤的「直接提问」关键字（命中即视为必须回复，跳过便宜判定模型）。
# 该开关默认关闭（judge.question_word_reply=false），避免误判。
QUESTION_WORDS = ("吗", "呢", "怎么", "怎样", "如何", "能不能", "可以", "？", "?", "谁", "什么", "为什么", "咋", "帮", "求")

# 麦麦主配置文件路径（相对 MaiBot 根目录，插件进程 CWD 即根目录）与行为风格所在节/字段。
# 只读该文件，绝不写入，用于让判定提示词对齐 Planner 的「何时参与/何时安静」准则。
BOT_CONFIG_PATH = "config/bot_config.toml"
BEHAVIOR_STYLE_SECTION = "personality"
BEHAVIOR_STYLE_KEY = "behavior_style"

# ---- Hook 超时（可配置，单位秒） ----
# HookHandler 的 timeout_ms 是注册期静态值，Host 侧运行时读的是注册元数据，无法在运行中
# 修改。但插件重载会清模块缓存并重新 import 本文件，因此这里在模块加载时只读一次插件
# 自身 config.toml 的 llm.hook_timeout_sec（单位秒，下同）；用户修改该配置并重载插件后
# 对新值生效。默认 12s：须大于判定 LLM 超时（llm.timeout_sec，默认 10s），避免慢判定
# 被 Host 提前切断并触发插件熔断。
_PLUGIN_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.toml")
_DEFAULT_HOOK_TIMEOUT_SEC = 12.0


def _resolved_hook_timeout_sec() -> float:
    """从插件自身 config.toml 读取 llm.hook_timeout_sec（秒），缺失或异常回退默认值。"""
    try:
        with open(_PLUGIN_CONFIG_PATH, "rb") as f:
            section = tomllib.load(f).get("llm") or {}
        value = section.get("hook_timeout_sec")
        if value is not None:
            return max(float(value), 0.5)
    except (OSError, ValueError, TypeError):
        pass
    return _DEFAULT_HOOK_TIMEOUT_SEC


# 判定 Hook（before_request）注册超时（毫秒，装饰器要求）：改 config.toml 的重载后生效。
HOOK_TIMEOUT_MS = int(_resolved_hook_timeout_sec() * 1000)

# 放行后"期望发送"追踪：主模型产出正文的放行轮，等待一次真实发送命中。
# 窗口必须覆盖图片生成等耗时发送（默认 300s），超窗仍未命中才计入"最终未发出"。
SEND_TRACKING_WINDOW_SEC = 300.0
# 后台清扫"期望发送"超时的任务执行间隔（秒）。
SEND_SWEEP_INTERVAL_SEC = 15.0


def _estimate_tokens(text: str) -> int:
    """把一段文本粗略换算成 token 数。

    换算口径：字符数 ÷ 1.5（混合中英文的粗粒度平均）。
    仅用于估算与展示，不参与任何计费。
    """
    return max(1, round(len(text) / CHARS_PER_TOKEN))


def _text_from_item(item: Any) -> str:
    """从一个 Context Item 快照中提取纯文本。

    MaiBot 1.2.3 起 Planner Hook 载荷是「Context Item」序列化结构：
    ``{"item_type": "UserMessageItem", "meta": {...}, "parts": [...]}``。
    文本只存在于 parts 的 text/refusal 片段里，图片等非文本片段忽略，
    避免把图片的 base64 之类的大体积内容塞进便宜模型的输入。

    Args:
        item: 单个 Item 快照字典。

    Returns:
        str: 提取出的纯文本；无文本时返回空字符串。
    """
    if not isinstance(item, dict):
        return ""
    parts = item.get("parts")
    if not isinstance(parts, list):
        return ""
    text_parts: List[str] = []
    for part in parts:
        if not isinstance(part, dict):
            continue
        part_type = part.get("type")
        if part_type == "text":
            text = part.get("text")
            if isinstance(text, str):
                text_parts.append(text)
        elif part_type == "refusal":
            refusal = part.get("refusal")
            if isinstance(refusal, str):
                text_parts.append(refusal)
    return "".join(text_parts)


def _build_item_snapshot(item_type: str, text: str) -> Dict[str, Any]:
    """构造一个合法的 Context Item 快照（供 SKIP 占位与预判注入使用）。

    Hook 改写时返回的快照会被宿主用 deserialize_context_item_snapshot 还原为
    Context Item，字段必须满足：meta.item_id 非空、meta.logical_turn_id 键存在
    （可为 None）、meta.timestamp 为 ISO 字符串、parts 至少含一个非空文本片段。
    """
    return {
        "item_type": item_type,
        "meta": {
            "item_id": uuid.uuid4().hex,
            "logical_turn_id": None,
            "timestamp": datetime.now().isoformat(),
        },
        "parts": [{"type": "text", "text": text}],
    }


def _is_user_item(item: Any) -> bool:
    """判断一个 Item 快照是否为用户消息。"""
    return isinstance(item, dict) and item.get("item_type") == "UserMessageItem"


class ReplyGatePlugin(MaiBotPlugin):
    """回复省流闸门插件主类。

    负责在 Planner 请求前后挂载 Hook，通过自配的 OpenAI 兼容接口用便宜模型
    判断本批消息是否值得让主 planner 认真规划回复。所有状态仅保存在内存中。
    """

    config_model = ReplyGateConfig

    def __init__(self) -> None:
        super().__init__()
        # 便宜判定模型的 HTTP 客户端；在 on_load 中按 llm 配置创建，避免硬编码超时。
        self._http_client: Optional[httpx.AsyncClient] = None
        # 运行时开关：/replygate on|off 只改内存标志，不写回 config.toml；
        # 重启后以配置文件中的 plugin.enabled 为准。
        self._runtime_enabled: bool = False
        # 真实消息 @ 识别开关：/replygate realmsg on|off 只改内存标志，不写回配置；
        # 重启后以 [judge].scan_recent_replies / scan_recent_count 为准。
        # 开启（默认）：@/点名规则与判定"最新消息"扫描最近真实用户发言；
        # 关闭：回退到旧逻辑（只看最后一条 user 文本）。
        self._realmsg_scan_enabled: bool = True
        self._realmsg_scan_count: int = 2
        # 会话级判定缓存：key 为 session_id，value 为最近一次判定结论（SKIP/REPLY）。
        # 供 after_response 判断是否要清空本轮主模型输出；每次 before_request 都会覆盖，
        # 天然处理"请求被新消息打断导致的残留标记"。
        self._verdicts: Dict[str, str] = {}
        # 放行后"期望发送"追踪：session_id -> [期望发送的 deadline (monotonic), ...]。
        # 主模型产出正文的放行轮会登记一条期望发送；send_service.after_send 成功发送后消费；
        # 后台清扫任务把超窗仍未消费的条目计入"放行后最终未发出"。
        self._expected_sends: Dict[str, List[float]] = {}
        # 后台清扫任务引用；on_load 启动，on_unload 取消。
        self._sweep_task: Optional[asyncio.Task] = None
        # 各会话最近一次取到的 user 文本列表（按时间顺序），供 SKIP 占位取最新一条。
        self._last_user_texts: Dict[str, List[str]] = {}
        # 麦麦内置行为风格（config/bot_config.toml 的 [personality].behavior_style），
        # on_load 时只读一次；为空或读取失败表示不注入。
        self._behavior_style: str = ""
        # 麦麦昵称列表（config/bot_config.toml 的 [bot].nickname + alias_names），
        # 供规则前置过滤识别"被@/提起机器人名字"；读不到时为空（规则不命中，安全）。
        self._bot_names: List[str] = []
        # 真 @ 识别候选：昵称 + 别名 + 备用账号（[bot].qq_account 等）。
        # MaiBot 会把 @ 富文本渲染成 "@名字" 或 "@QQ号"，二者都作为被 @ 的判别 token。
        self._bot_at_tokens: List[str] = []
        # 旧历史摘要缓存：session_id -> (被摘区指纹, 摘要文本)。
        # 被摘历史未变时直接复用摘要，避免每轮都重复调用判定模型。
        self._summary_cache: Dict[str, Tuple[str, str]] = {}
        # 内存统计。运行期间持续累计，重启清零。
        self._stats: Dict[str, Any] = {
            # 基础计数
            "total": 0,  # 判定轮次总数（进入判定流程的轮次）
            "reply": 0,  # 放行次数（判定 REPLY）
            "skip": 0,  # 拦截次数（判定 SKIP）
            "fail": 0,  # 异常失败放行次数（网络/超时/解析错误）
            "no_reply_after_reply": 0,  # 放行后主模型未产出可见文本的次数（只思考/只调工具未说话）
            "no_send_after_reply": 0,  # 放行后最终未发出次数（主模型有正文，但超窗仍无成功发送）
            "rule_reply": 0,  # 规则前置命中直接放行次数（点名/提问，跳过判定模型）
            "summary_count": 0,  # 触发旧历史摘要压缩的次数（含缓存命中复用）
            "giveup": 0,  # 判定无结论次数（重试后仍结论不清晰）
            "shrinks": 0,  # 触发缩小重估/重试的次数
            "output_at_limit": 0,  # 输出接近上限（疑似截断）次数，仅统计不重试
            # token 估算（口径见 _estimate_tokens，均标注"约"）
            "input_tokens_total": 0,  # 所有轮次"完整 planner 输入"估算总和（拦截基准）
            "saved_input_tokens": 0,  # SKIP 轮被拦截的"完整输入"估算累计（节省主体）
            "judge_usage_tokens": 0,  # 判定模型实际消耗（优先接口 usage，缺失回退估算）
            "output_tokens_estimate": 0,  # 判定输出 token 估算累计（辅助展示）
            # 最近一次判定信息
            "last_verdict": "",
            "last_reason": "",
            "last_timestamp": 0.0,
            # 分组明细
            "by_model": {},  # model_name -> 统计字段
            "by_session": {},  # session_id -> 统计字段
        }

    # ==================== 生命周期 ====================

    async def on_load(self) -> None:
        """插件加载：重建 HTTP 客户端并校验配置。

        - 始终先重建客户端（覆盖旧配置）；
        - 配置未启用或判定模型配置不完整时只打印日志，不让插件注册失败；
        - llm 配置不完整时判定会被跳过并走 fail-open，等用户补全后重载。
        """
        await self._rebuild_client()
        self._behavior_style = self._load_behavior_style()
        self._bot_names = self._load_bot_names()
        self._bot_at_tokens = self._load_bot_at_tokens()
        # 启动"期望发送"超时清扫任务（卸载时取消；重复加载有去重保护）。
        self._start_sweeper()
        # 同步真实消息 @ 识别开关（重载/热更新后以配置为准）。
        self._realmsg_scan_enabled = bool(self.config.judge.scan_recent_replies)
        try:
            self._realmsg_scan_count = max(int(self.config.judge.scan_recent_count or 0), 1)
        except (TypeError, ValueError):
            self._realmsg_scan_count = REAL_MSG_REPLY_SCAN_RECENT
        if self.config.judge.inject_behavior_style and not self._behavior_style:
            self.ctx.logger.debug(
                "[回复省流闸门] 已开启注入行为风格，但读取 bot_config.toml 行为风格为空，"
                "本次不注入（判定仍正常进行）"
            )
        if not self.config.plugin.enabled:
            self.ctx.logger.info("[回复省流闸门] 已加载但处于关闭状态（plugin.enabled=false）")
            return
        missing = self._missing_llm_config()
        if missing:
            self.ctx.logger.warning(
                f"[回复省流闸门] 判定模型配置不完整，缺少: {', '.join(missing)}；"
                "判定将被跳过（fail-open），请在 config.toml 补全后重载插件"
            )
            return
        self._runtime_enabled = True
        self.ctx.logger.info(
            f"[回复省流闸门] 已启用，判定模型={self.config.plugin.model} "
            f"端点={self.config.llm.base_url} "
            f"总token上限={self.config.llm.max_total_tokens}"
        )

    async def on_unload(self) -> None:
        """插件卸载：取消清扫任务、清空内存缓存并关闭 HTTP 客户端。"""
        if self._sweep_task is not None:
            self._sweep_task.cancel()
            self._sweep_task = None
        self._verdicts.clear()
        self._expected_sends.clear()
        if self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None
        self.ctx.logger.info("[回复省流闸门] 已卸载")

    async def on_config_update(self, *args: Any, **kwargs: Any) -> None:
        """配置热更新：重建客户端并重新走一遍加载流程。"""
        await self.on_unload()
        await self.on_load()

    # ==================== 内部工具 ====================

    def _missing_llm_config(self) -> List[str]:
        """检查判定模型必填配置项，返回缺失项列表（空列表表示全部已填）。

        便于 on_load 与 /replygate 命令给出可操作的提示。
        """
        missing = []
        cfg = self.config
        if not (cfg.plugin.model or "").strip():
            missing.append("plugin.model")
        if not (cfg.llm.base_url or "").strip():
            missing.append("llm.base_url")
        if not (cfg.llm.api_key or "").strip():
            missing.append("llm.api_key")
        return missing

    def _load_behavior_style(self) -> str:
        """只读加载麦麦内置行为风格（config/bot_config.toml 的 [personality].behavior_style）。

        与 Planner 使用同一来源（见 src/maisaka/chat_loop_service.py 的 behavior_style_prompt），
        让便宜判定模型按麦麦的「何时参与 / 何时安静」偏好决定 REPLY/SKIP。

        Returns:
            行为风格文本；文件不存在、缺字段或解析失败时返回空字符串（不阻塞判定）。
        """
        try:
            with open(BOT_CONFIG_PATH, "rb") as f:
                data = tomllib.load(f)
            section = data.get(BEHAVIOR_STYLE_SECTION) or {}
            value = section.get(BEHAVIOR_STYLE_KEY)
            if isinstance(value, str):
                return value.strip()
        except (OSError, ValueError, TypeError):
            self.ctx.logger.debug(
                f"[回复省流闸门] 读取 {BOT_CONFIG_PATH} 的行为风格失败，本次不注入",
                exc_info=True,
            )
        return ""

    def _load_bot_names(self) -> List[str]:
        """只读加载麦麦昵称/别名（config/bot_config.toml 的 [bot].nickname + alias_names）。

        供规则前置过滤识别"被@或提起机器人名字"。读不到时返回空列表（规则不命中，安全）。

        Returns:
            List[str]: 去重后的昵称列表；可能为空。
        """
        names: List[str] = []
        try:
            with open(BOT_CONFIG_PATH, "rb") as f:
                data = tomllib.load(f)
            section = data.get("bot") or {}
            candidates = [section.get("nickname"), section.get("alias_names")]
            for item in candidates:
                if isinstance(item, str) and item.strip():
                    names.append(item.strip())
                elif isinstance(item, (list, tuple)):
                    names.extend(str(x).strip() for x in item if isinstance(x, str) and x.strip())
        except (OSError, ValueError, TypeError):
            self.ctx.logger.debug(
                f"[回复省流闸门] 读取 {BOT_CONFIG_PATH} 的昵称失败，规则点名不生效",
                exc_info=True,
            )
        # 去重并保持顺序；单个字符昵称忽略（太宽容易误判）。
        seen = set()
        unique = []
        for name in names:
            if name and name not in seen and len(name) > 1:
                seen.add(name)
                unique.append(name)
        return unique

    def _load_bot_at_tokens(self) -> List[str]:
        """只读加载真 @ 识别候选（config/bot_config.toml 的 [bot].nickname + alias_names + 备用账号）。

        MaiBot 把 @ 富文本渲染成 "@目标名"（见 src/maisaka/context/message_adapter.py 的
        _render_at_component_text）；目标名优先用卡片名/昵称，都没有时回退为 QQ 号。
        因此这里把昵称、别名和备用账号都纳入 "@token" 比对，确保 `@麦麦`、`@2136...` 都能命中。

        Returns:
            List[str]: 去重后的 @ 候选列表（含账号，不做长度过滤）；可能为空。
        """
        tokens: List[str] = []
        try:
            with open(BOT_CONFIG_PATH, "rb") as f:
                data = tomllib.load(f)
            section = data.get("bot") or {}
            candidates = [section.get("nickname"), section.get("alias_names")]
            for item in candidates:
                if isinstance(item, str) and item.strip():
                    tokens.append(item.strip())
                elif isinstance(item, (list, tuple)):
                    tokens.extend(str(x).strip() for x in item if isinstance(x, str) and x.strip())
            for account_key in ("qq_account", "account", "robot_id"):
                account = section.get(account_key)
                if isinstance(account, str) and account.strip():
                    tokens.append(account.strip())
        except (OSError, ValueError, TypeError):
            self.ctx.logger.debug(
                f"[回复省流闸门] 读取 {BOT_CONFIG_PATH} 的 @ 识别候选失败，被@规则不生效",
                exc_info=True,
            )
        seen = set()
        unique = []
        for token in tokens:
            if token and token not in seen:
                seen.add(token)
                unique.append(token)
        return unique

    async def _rebuild_client(self) -> None:
        """按当前 llm 配置创建/重建 HTTP 客户端。

        超时取自 llm.timeout_sec（下限 0.5s），作为直连调用的基础超时；
        _judge 里还会用 asyncio.wait_for 再做一层整体超时，双保险。
        """
        if self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None
        timeout_sec = max(float(self.config.llm.timeout_sec or 10.0), 0.5)
        self._http_client = httpx.AsyncClient(timeout=httpx.Timeout(timeout_sec))

    def _is_active(self) -> bool:
        """判断插件当前是否真正参与判定。

        条件：配置文件开启、运行时开关打开、且判定模型配置完整。
        """
        if not self.config.plugin.enabled or not self._runtime_enabled:
            return False
        return not self._missing_llm_config()

    # ---- token 估算（口径统一为字符数 ÷ 1.5）----

    def _estimate_request_total(
        self,
        system_prompt: str,
        context_lines: List[str],
        pending_text: str,
        max_output: int,
    ) -> int:
        """估算一次判定请求的总 token（输入 + 输出上限）。"""
        input_tokens = (
            _estimate_tokens(system_prompt)
            + sum(_estimate_tokens(line) for line in context_lines)
            + _estimate_tokens(pending_text)
            + REQUEST_FIXED_OVERHEAD_TOKENS
        )
        return input_tokens + int(max_output)

    def _estimate_full_planner_input(self, messages: List[Dict[str, Any]]) -> int:
        """估算"完整传入 messages"被主 planner 消费时的输入 token（拦截基准）。

        统计全部消息（user/assistant/system）的文本字符总数 ÷1.5
        + 预留的 system/结构化开销。
        """
        total_chars = 0
        for message in messages:
            if not isinstance(message, dict):
                continue
            total_chars += len(_text_from_item(message))
        return round(total_chars / CHARS_PER_TOKEN) + BASE_SYSTEM_TOKEN_ESTIMATE

    # ---- 上下文构建（窗口参数化，便于降载与缩小重试复用）----

    def _collect_user_texts(self, messages: List[Dict[str, Any]]) -> List[str]:
        """从 Context Item 序列化载荷中收集全部 user 文本（未截断、已去空）。"""
        texts = []
        for message in messages:
            if not _is_user_item(message):
                continue
            text = _text_from_item(message).strip()
            if text:
                texts.append(text)
        return texts

    def _collapse_repeated_messages(
        self, messages: List[Dict[str, Any]]
    ) -> Tuple[List[Dict[str, Any]], bool]:
        """把 messages 中「相邻且文本完全相同」的 user 消息折叠为一条（语义无损失）。

        用于减少刷屏/重复消息塞进主 planner 历史与判定上下文。仅折叠相邻完全重复的
        user 消息（保留第一条，丢弃后续相同项），不改变角色、顺序与工具定义。

        Args:
            messages: 原始消息列表。

        Returns:
            (折叠后的列表, 是否发生了折叠)。
        """
        if not messages:
            return messages, False
        collapsed: List[Dict[str, Any]] = []
        changed = False
        prev_user_text: Optional[str] = None
        for message in messages:
            if not isinstance(message, dict):
                collapsed.append(message)
                continue
            text = _text_from_item(message).strip() if _is_user_item(message) else ""
            if _is_user_item(message) and text:
                if text == prev_user_text:
                    # 与上一条 user 完全相同：丢弃，仅保留前一条。
                    changed = True
                    continue
                prev_user_text = text
            else:
                # 非 user 消息（system/assistant 等）会中断"相邻"比较。
                prev_user_text = None
            collapsed.append(message)
        return collapsed, changed

    def _is_injected_user_text(self, text: str) -> bool:
        """判断一段 user 文本是否为注入/装饰类内容（非真实聊天发言）。

        Planner 会追加时间提示、工具不可见提醒、人物画像、历史摘要、
        预判占位、聊天注意等 user 类注入消息；这些不属于用户真实发言，
        判定上下文与 @/点名规则都不应把它们当作"最新消息"。
        """
        normalized_text = " ".join((text or "").split()).strip()
        if not normalized_text:
            return True
        return normalized_text.startswith(SKIPPED_USER_TEXT_PREFIXES)

    def _recent_real_user_texts(self, user_texts: List[str], recent: int) -> List[str]:
        """返回最近 recent 条真实用户发言（跳过注入文本），保持时间顺序。

        最新一条真实发言位于列表末尾。找不到真实发言时返回空列表。
        """
        if not user_texts:
            return []
        real_texts = [text for text in reversed(user_texts) if not self._is_injected_user_text(text)]
        return list(reversed(real_texts[: max(int(recent), 1)]))

    def _latest_real_user_text(self, user_texts: List[str]) -> str:
        """返回最近一条真实用户发言；找不到时回退最后一条 user 文本。"""
        if not user_texts:
            return ""
        real_texts = [text for text in reversed(user_texts) if not self._is_injected_user_text(text)]
        if real_texts:
            return " ".join(str(real_texts[0] or "").split()).strip()
        return " ".join(str(user_texts[-1] or "").split()).strip()

    def _rule_latest_texts(self, user_texts: List[str]) -> List[str]:
        """返回 @/点名规则要扫描的候选文本列表。

        开启 scan_recent_replies（/replygate realmsg on）时返回最近 N 条真实
        用户发言（跳过注入类消息）；关闭时回退到原逻辑（仅最后一条 user 文本）。
        """
        if not user_texts:
            return []
        if not self._realmsg_scan_enabled:
            return [user_texts[-1]]
        return self._recent_real_user_texts(user_texts, self._realmsg_scan_count)

    def _judge_pending_text(self, user_texts: List[str]) -> str:
        """返回判定"最新消息"应为的文本。

        开启 scan_recent_replies 时取最近一条真实用户发言，避免被末尾注入的
        时间/提醒/画像等 user 消息顶掉；关闭时回退到原逻辑（最后一条 user 文本）。
        """
        if not user_texts:
            return ""
        if not self._realmsg_scan_enabled:
            return user_texts[-1]
        return self._latest_real_user_text(user_texts)

    def _build_context(
        self,
        user_texts: List[str],
        *,
        window: int,
        chars: int,
    ) -> Tuple[List[str], str]:
        """按窗口/截断长度构建判定上下文。

        最新一条 user 消息始终作为"待处理消息"，之前的最近 window 条作为上下文。
        待处理消息优先取最近一条真实用户发言（见 _judge_pending_text），
        避免被 Planner 末尾注入的时间/提醒等消息顶掉真实 @ 内容。

        Args:
            user_texts: 全部 user 文本（按时间顺序）。
            window: 上下文窗口条数。
            chars: 单条最大字符数。

        Returns:
            (上下文行列表, 最新待处理消息文本)。无内容时返回 ([], "")。
        """
        if not user_texts:
            return [], ""
        window = max(int(window), 1)
        chars = max(int(chars), 20)

        def truncate(text: str) -> str:
            folded = " ".join(text.split()).strip()
            folded = _strip_media_brackets(folded)
            return folded if len(folded) <= chars else folded[:chars] + "…"

        pending_text = truncate(self._judge_pending_text(user_texts))
        context_lines = [truncate(t) for t in user_texts[-window - 1 : -1]]
        return context_lines, pending_text

    def _shrunk_params(self, window: int, chars: int) -> Tuple[int, int]:
        """把判定窗口/单条长度折半，用于预算超限或缩小重试。

        下限保护：window >= 1、chars >= 20，避免死循环。
        """
        new_window = max(1, int(window) // 2)
        new_chars = max(20, int(chars) // 2)
        return new_window, new_chars

    def _rule_reply_reason(self, user_texts: List[str]) -> Optional[str]:
        """规则前置过滤（纯规则，不调判定模型）：命中"必须回复"时直接放行。

        只做 REPLY 侧规则（被@/点名、直接提问），不做任何 SKIP 侧规则（表情/打卡/刷屏
        一概不拦），保证绝无因规则而吞消息。命中时返回命中原因，未命中返回 None。

        Args:
            user_texts: 全部 user 文本（按时间顺序）。

        Returns:
            Optional[str]: 命中原因；未命中返回 None。
        """
        if not user_texts:
            return None
        # Planner 会把时间/提醒/画像等注入类 user 消息追加到真实发言之后，
        # 只取 user_texts[-1] 会错过真正被 @ 的发言。这里扫描最近 N 条真实发言
        # （开后端开关 scan_recent_replies 时生效；关闭则回到只看最后一条的旧逻辑）。
        for latest in self._rule_latest_texts(user_texts):
            # 真 @：消息含 "@昵称" 或 "@QQ号"（MaiBot 会把 @ 富文本渲染成 @目标名，
            # 见 src/maisaka/context/message_adapter.py 的 _render_at_component_text）。
            # 与口头点名分开：@ 是"按钮 @"，本开关默认开，被真 @ 才直接放行。
            if self.config.judge.at_button_reply and self._bot_at_tokens:
                for token in self._bot_at_tokens:
                    if f"@{token}" in latest:
                        return f"被@（{token}）"
            # 口头点名（无 @，但消息里提到机器人名字）：独立开关，默认关。
            # 打开后任何提到昵称/别名的消息都会直接放行，可能把普通聊天误判为必回，
            # 因此由用户自行选择是否开启。
            if self.config.judge.name_mention_reply and self._bot_names:
                for name in self._bot_names:
                    if name in latest:
                        return f"口头点名（{name}）"
            # 直接提问：含疑问关键字（默认关，避免误判）。
            if self.config.judge.question_word_reply:
                for word in QUESTION_WORDS:
                    if word in latest:
                        return f"直接提问（含“{word}”）"
        return None

    # ---- 旧历史摘要（token 预算式） ----

    def _estimate_messages_tokens(self, messages: List[Dict[str, Any]]) -> int:
        """粗估 messages 发送给主 planner 的输入 token（全部消息文本 ÷1.5 + 固定开销）。"""
        total_chars = 0
        for message in messages:
            if not isinstance(message, dict):
                continue
            total_chars += len(_text_from_item(message))
        return round(total_chars / CHARS_PER_TOKEN) + REQUEST_FIXED_OVERHEAD_TOKENS

    async def _call_summarizer(self, text: str) -> Optional[str]:
        """用便宜判定模型把一段旧对话压成一段摘要（缓存未命中时）。

        Args:
            text: 被摘的旧对话文本。

        Returns:
            摘要文本；调用失败/解析失败时返回 None（调用方回退全量）。
        """
        if self._http_client is None:
            return None
        cfg = self.config
        system_prompt = (
            "你是对话摘要器。请把下面这段群聊/私聊历史压缩成一段简洁的中文摘要，"
            "保留：人物关系、关键约定、正在推进的话题、重要事实。不要评价，不要寒暄，"
            "控制在 300 字符以内。只输出摘要正文。"
        )
        payload = {
            "model": cfg.plugin.model.strip(),
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": text[:4000]},
            ],
            "temperature": 0.0,
            "max_tokens": 256,
        }
        url = cfg.llm.base_url.rstrip("/") + "/chat/completions"
        headers = {"Authorization": f"Bearer {cfg.llm.api_key.strip()}"}
        timeout_s = max(float(cfg.llm.timeout_sec or 10.0), 0.5)
        try:
            response = await asyncio.wait_for(
                self._http_client.post(url, json=payload, headers=headers),
                timeout=timeout_s,
            )
            response.raise_for_status()
            data = response.json()
            content = data["choices"][0]["message"]["content"] or ""
        except Exception as exc:
            self.ctx.logger.warning(f"[回复省流闸门] 旧历史摘要生成失败，本次回退全量: {exc}")
            return None
        content = " ".join(content.split()).strip()
        return content or None

    async def _maybe_summarize(
        self,
        session_id: str,
        messages: List[Dict[str, Any]],
    ) -> Tuple[List[Dict[str, Any]], bool]:
        """主 planner 输入超预算时，把最旧对话压缩为摘要（带指纹缓存）。

        - 不拆分第一条 system（人格头部），只对之后的旧 user/assistant 历史压缩；
        - 保留最近 summary_keep_recent 条完整消息；
        - 被摘区指纹不变时直接复用缓存摘要，不重复调用判定模型；
        - 摘要失败/无足够历史 → 回退为原列表（changed=False）。

        Args:
            session_id: 会话 ID（缓存 key）。
            messages: 当前发给主 planner 的消息列表（已折叠后）。

        Returns:
            (处理后的列表, 是否发生了摘要替换)。
        """
        cfg = self.config
        budget = max(int(cfg.judge.summary_budget_tokens or 0), 0)
        if not (self.config.judge.summary_enabled and budget > 0):
            return messages, False
        if self._estimate_messages_tokens(messages) <= budget:
            return messages, False

        keep = max(int(cfg.judge.summary_keep_recent or 8), 1)
        # 拆分：只把「前导连续的 system」（一般为第一条人格 system）作为不可动头部；
        # 其余消息（含历史中间偶发的记忆/参考 system）按原顺序进入 body 再摘旧区。
        # 这样不会因移动中间 system 破坏消息顺序。
        head: List[Dict[str, Any]] = []
        body: List[Dict[str, Any]] = []
        in_body = False
        for message in messages:
            if not in_body and isinstance(message, dict) and message.get("item_type") == "SystemMessageItem":
                head.append(message)
            else:
                in_body = True
                body.append(message)
        if not body:
            return messages, False
        keep_tail = body[-keep:]
        old_zone = body[:-keep] if len(body) > keep else []
        if not old_zone:
            return messages, False

        old_text = "\n".join(_text_from_item(m) for m in old_zone).strip()
        if not old_text:
            return messages, False

        # 指纹缓存：被摘区内容没变就直接复用上次摘要。
        fingerprint = hashlib.sha1(old_text.encode("utf-8")).hexdigest()
        cached = self._summary_cache.get(session_id)
        if cached is not None and cached[0] == fingerprint:
            summary_text = cached[1]
            self._stats["summary_count"] += 1
        else:
            summary_text = await self._call_summarizer(old_text)
            if not summary_text:
                return messages, False
            self._summary_cache[session_id] = (fingerprint, summary_text)
            self._stats["summary_count"] += 1
            self.ctx.logger.info(
                f"[回复省流闸门] 已生成旧历史摘要（源 {len(old_zone)} 条 → 摘要），"
                f"保留最近 {len(keep_tail)} 条完整消息"
            )

        summarized = [*head, _build_item_snapshot("SystemMessageItem", f"[历史摘要] {summary_text}"), *keep_tail]
        return summarized, True

    # ---- 判定调用 ----

    async def _judge_once(
        self,
        system_prompt: str,
        context_lines: List[str],
        pending_text: str,
    ) -> Tuple[str, str, int, bool]:
        """执行一次判定请求（不包含缩小重试逻辑）。

        Returns:
            (判定结论, 理由, 实际消耗token, 输出是否接近上限)。
            实际消耗 token 优先取接口 usage.total_tokens，缺失时按估算回退。
        """
        if self._http_client is None:
            raise RuntimeError("HTTP 客户端未初始化")
        cfg = self.config

        # 组装判定的 user 输入：上下文在前，最新消息在后，方便模型对比。
        context_block = "\n".join(f"- {line}" for line in context_lines) or "（暂无）"
        user_prompt = (
            "========== 最近对话 ==========\n"
            f"{context_block}\n"
            "========== 最新消息 ==========\n"
            f"- {pending_text}\n\n"
            "请判定麦麦该不该回复。"
        )

        payload = {
            "model": cfg.plugin.model.strip(),
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": float(cfg.llm.temperature or 0.0),
            "max_tokens": REQUEST_MAX_OUTPUT_TOKENS,
        }
        url = cfg.llm.base_url.rstrip("/") + "/chat/completions"
        headers = {"Authorization": f"Bearer {cfg.llm.api_key.strip()}"}
        timeout_s = max(float(cfg.llm.timeout_sec or 10.0), 0.5)

        response = await asyncio.wait_for(
            self._http_client.post(url, json=payload, headers=headers),
            timeout=timeout_s,
        )
        response.raise_for_status()
        data = response.json()

        # 提取模型输出正文。
        content = ""
        try:
            content = data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError):
            content = ""
        verdict, reason = self._parse_verdict(content)
        # 记录判定模型原始输出与解析结果（debug），便于排查判定模型飘忽/假 REPLY。
        self.ctx.logger.debug(
            f"[回复省流闸门] 判定模型原始输出: "
            f"{ ' '.join(str(content).split())[:200] or '（空）' }（解析为 {verdict}）"
        )

        # 提取实际消耗：优先接口 usage，缺失回退估算（输入估算 + 输出估算）。
        usage_tokens = 0
        completion_tokens = 0
        try:
            usage = data.get("usage") or {}
            usage_tokens = int(usage.get("total_tokens") or 0)
            completion_tokens = int(usage.get("completion_tokens") or 0)
        except (TypeError, ValueError, AttributeError):
            usage_tokens = 0
        if usage_tokens <= 0:
            usage_tokens = self._estimate_request_total(
                system_prompt, context_lines, pending_text, _estimate_tokens(content)
            )
        if completion_tokens <= 0:
            completion_tokens = _estimate_tokens(content)

        output_capped = completion_tokens >= REQUEST_MAX_OUTPUT_TOKENS * 0.9
        return verdict, reason, usage_tokens, output_capped

    async def _judge(
        self,
        user_texts: List[str],
        *,
        window: int,
        chars: int,
    ) -> Tuple[Optional[str], str, int]:
        """带缩小重试的判定入口。

        流程：
            1. 用当前窗口构建上下文，预估总 token；若超过 max_total_tokens 且允许缩小，
               循环折半窗口/单条长度，直到预估达标或到最小窗口；缩到最小仍超预算
               也不放弃，按截断后的片段直接判定；
            2. 执行判定；若结论不清晰(UNKNOWN)且允许缩小，再缩小一档重试一次；
               输出疑似被截断不重试，直接按已生成片段判定；
            3. 仍无法得到清晰结论时返回 None（由调用方按 fail-open 放行）。

        Returns:
            (判定结论 或 None, 理由, 实际消耗token累计)。
            None 表示放弃判定，应由调用方放行。
        """
        cfg = self.config
        base_prompt = (cfg.judge.system_prompt or "").strip() or DEFAULT_JUDGE_SYSTEM_PROMPT
        system_prompt = base_prompt
        # 注入麦麦内置行为风格：判定是否回复时须兼顾人设的「参与/安静」偏好。
        behavior = self._behavior_style.strip()
        if cfg.judge.inject_behavior_style and behavior:
            system_prompt = (
                base_prompt
                + "\n\n麦麦的行为风格（决定是否回复，判定时须兼顾）：\n"
                + behavior
            )
        max_output = REQUEST_MAX_OUTPUT_TOKENS
        shrink_enabled = bool(cfg.judge.shrink_retry_enabled)
        total_budget = max(int(cfg.llm.max_total_tokens or 0), 0)

        # 第 0 阶段：超长直接截断（缩小到满足预算为止；缩到最小仍略超预算也不放弃，
        # 按截断后的内容照常判定，保证每一轮都有判定结论）。
        cur_window, cur_chars = window, chars
        if shrink_enabled and total_budget > 0:
            while cur_window > 1 or cur_chars > 20:
                ctx, pending = self._build_context(user_texts, window=cur_window, chars=cur_chars)
                estimate = self._estimate_request_total(system_prompt, ctx, pending, max_output)
                if estimate <= total_budget:
                    break
                self._stats["shrinks"] += 1
                cur_window, cur_chars = self._shrunk_params(cur_window, cur_chars)
            else:
                # while 循环条件耗尽（已缩到最小窗口）仍超预算：不再放弃，
                # 记录一条 debug 后继续用当前最小片段判定。
                self.ctx.logger.debug(
                    f"[回复省流闸门] 缩到最小窗口后预估 {estimate} tok 仍略超预算 "
                    f"{total_budget}，按截断后的片段直接判定（模型={cfg.plugin.model}）"
                )

        # 第 1 次判定。
        ctx, pending = self._build_context(user_texts, window=cur_window, chars=cur_chars)
        verdict, reason, usage, output_capped = await self._judge_once(system_prompt, ctx, pending)

        # 输出疑似截断（接近上限）不再触发重判：结论词 REPLY/SKIP 通常在开头，
        # 被截断也照常按已生成的片段判定；仅记录次数便于观察。
        if output_capped:
            self._stats["output_at_limit"] += 1

        # 第 2 次判定：只有结论不清晰(UNKNOWN)时才缩小一档重试一次。
        can_retry = shrink_enabled and verdict == "UNKNOWN" and (
            cur_window > 1 or cur_chars > 20
        )
        if can_retry:
            self._stats["shrinks"] += 1
            self.ctx.logger.info(
                f"[回复省流闸门] 判定UNKNOWN（结论不清晰），缩小输入窗口重试一次"
            )
            retry_window, retry_chars = self._shrunk_params(cur_window, cur_chars)
            ctx, pending = self._build_context(user_texts, window=retry_window, chars=retry_chars)
            verdict, reason, retry_usage, _ = await self._judge_once(system_prompt, ctx, pending)
            usage += retry_usage

        if verdict == "UNKNOWN":
            self._stats["giveup"] += 1
            return None, reason, usage
        return verdict, reason, usage

    @staticmethod
    def _parse_verdict(content: str) -> Tuple[str, str]:
        """从模型输出的正文里解析判定结论（REPLY/SKIP）与理由。

        结论词必须位于输出**开头**（允许少量空白/括号/引号等装饰），避免模型输出的
        杂前缀（如 "needed** response: SKIP: ..."）被误读成 REPLY/SKIP；这一类输出
        统一判 UNKNOWN，由上层缩小窗口重试一次，仍失败则走 fail-open 放行（安全）。

        Returns:
            (判定结论, 理由文本)。
        """
        text = " ".join((content or "").split()).strip()
        match = VERDICT_PATTERN.search(text)
        if not match:
            return "UNKNOWN", text[:120]
        verdict = match.group(1).upper()
        # 去掉结论词，剩余部分即为理由；清理可能的冒号/逗号等分隔符。
        reason = text[match.end() :].lstrip(":：，, ").strip() or "（无理由）"
        return verdict, reason

    # ---- 统计 ----

    def _record_judge_usage(self, model_name: str, session_id: str, usage: int) -> None:
        """把一次判定消耗记入全局、按模型、按会话统计。"""
        self._stats["judge_usage_tokens"] += usage
        for bucket in (self._stats["by_model"].setdefault(model_name, {}), self._stats["by_session"].setdefault(session_id, {})):
            bucket["judge_tokens"] = bucket.get("judge_tokens", 0) + usage

    def _record_result(
        self,
        *,
        verdict: str,
        reason: str,
        session_id: str,
        usage: int,
        full_estimate: int,
        original_messages: Optional[List[Dict[str, Any]]] = None,
        hook_kwargs: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        """记录判定结果并返回 Hook 改写结果。

        SKIP -> 返回压缩请求的 Hook 结果（占位 Context Items + 空工具定义）；
        REPLY/其他 -> 默认返回 None 原样放行；若开启放行注入预判
        （judge.inject_reply_reason）且传入 original_messages，则前插一条预判 Item。
        """
        model_name = self.config.plugin.model.strip() or "（未填）"
        self._stats["total"] += 1
        self._stats["input_tokens_total"] += full_estimate
        self._record_judge_usage(model_name, session_id, usage)
        self._stats["last_verdict"] = verdict
        self._stats["last_reason"] = reason
        self._stats["last_timestamp"] = time.time()

        skip = verdict == "SKIP"
        bucket_key = "skip" if skip else "reply"
        self._stats[bucket_key] += 1
        for bucket in (self._stats["by_model"].setdefault(model_name, {}), self._stats["by_session"].setdefault(session_id, {})):
            bucket["total"] = bucket.get("total", 0) + 1
            bucket[bucket_key] = bucket.get(bucket_key, 0) + 1
            bucket["saved_tokens"] = bucket.get("saved_tokens", 0) + (full_estimate if skip else 0)

        if skip:
            self._stats["saved_input_tokens"] += full_estimate
            self._verdicts[session_id] = "SKIP"
            self.ctx.logger.info(f"[回复省流闸门] 判定=SKIP（拦截，屏蔽主 Planner）理由: {reason}")
            slim_items = [
                _build_item_snapshot("SystemMessageItem", SLIM_SYSTEM_PROMPT),
                _build_item_snapshot("UserMessageItem", self._latest_user_text_for_slim(session_id)),
            ]
            modified_kwargs = dict(hook_kwargs or {})
            # 兜底保证 item_schema_version 存在，避免宿主反序列化时因缺失而静默丢弃占位。
            modified_kwargs.setdefault("item_schema_version", 1)
            modified_kwargs["items"] = slim_items
            modified_kwargs["tool_definitions"] = []
            return {
                "action": "continue",
                "modified_kwargs": modified_kwargs,
            }

        self._verdicts[session_id] = "REPLY"
        self.ctx.logger.info(f"[回复省流闸门] 判定={verdict}（放行主 Planner）理由: {reason}")
        if self.config.judge.inject_reply_reason and original_messages:
            # 放行注入预判：把判定理由作为一条 user Item 发给主 planner，
            # 让主模型省去"是否回复、怎么切入"的思考，直接组织回复内容。
            # 采用固定前缀模板（reason 放句尾）：消息前缀保持稳定，连续轮次更易
            # 命中 provider 的 prefix cache（缓存友好）。
            # 插入位置：第一条非 system Item 之前（保持人格 system 在最前），
            # 若全部是 system 则追加到末尾。不改动工具定义。
            hint_content = (
                "[预判] 判定模型已确认本批消息需要回复。"
                "请直接组织回复内容，无需再判断是否回复。\n"
                f"理由参考：{reason}"
            )
            injected_messages = list(original_messages)
            insert_at = len(injected_messages)
            for i, msg in enumerate(injected_messages):
                if not (isinstance(msg, dict) and msg.get("item_type") == "SystemMessageItem"):
                    insert_at = i
                    break
            injected_messages.insert(insert_at, _build_item_snapshot("UserMessageItem", hint_content))
            modified_kwargs = dict(hook_kwargs or {})
            # 兜底保证 item_schema_version 存在，避免宿主反序列化时因缺失而静默丢弃注入。
            modified_kwargs.setdefault("item_schema_version", 1)
            modified_kwargs["items"] = injected_messages
            return {
                "action": "continue",
                "modified_kwargs": modified_kwargs,
            }
        return None

    def _latest_user_text_for_slim(self, session_id: str) -> str:
        """返回 SKIP 轮发给主 planner 的占位 user 文本（瘦身版）。

        占位输入只需最新消息的最前片段（上限 SLIM_MAX_USER_CHARS），配合 SLIM_SYSTEM_PROMPT
        让主模型稳定回两个字母 SKIP，每轮比全文再省几十 token。
        """
        texts = self._last_user_texts.get(session_id, [])
        text = texts[-1] if texts else "（本批消息无需回复）"
        text = " ".join(text.split()).strip()
        if len(text) <= SLIM_MAX_USER_CHARS:
            return text
        return text[:SLIM_MAX_USER_CHARS] + "…"

    # ==================== Planner Hooks ====================

    @HookHandler(
        "maisaka.planner.before_request",
        name="reply_gate_precheck",
        mode=HookMode.BLOCKING,
        order=HookOrder.EARLY,
        timeout_ms=HOOK_TIMEOUT_MS,
        error_policy=ErrorPolicy.SKIP,
    )
    async def on_planner_before_request(
        self,
        **kwargs: Any,
    ) -> Optional[Dict[str, Any]]:
        """主 planner 请求前的判定 Hook（MaiBot 1.2.3+ Context Item 载荷）。

        流程：
            1. 插件未激活或缺少必要入参 → 不做任何事，原样放行；
            2. （开关开启时）折叠相邻完全重复的 user 消息，减少刷屏占用；
            3. 规则前置过滤：被@/点名 或（开启时）直接提问 → 直接放行，跳过判定模型；
            4. 否则用便宜模型判定（结论不清晰 UNKNOWN 时缩小重试一次；截断不重试）；
            5. 判定结果走 _record_result：SKIP 压缩请求；其他放行。
            6. 任何异常 → 记 fail 并放行（fail-open）。

        载荷说明：新版本 Planner Hook 传入的是 Context Item 序列化快照列表
        （``items``，形如 {"item_type": ..., "meta": ..., "parts": [...]}），不再
        是旧式 {role, content} messages。改写 ``items`` 并把 ``tool_definitions``
        置空，同时保留 ``item_schema_version`` 等键，宿主才能反序列化回 Context Items。

        Returns:
            需要改写请求时返回 Hook 结果字典（含 modified_kwargs），
            否则返回 None 表示保持原参。
        """
        if not self._is_active():
            return None
        session_id = str(kwargs.get("session_id") or "")
        items = kwargs.get("items")
        if not session_id or not isinstance(items, list) or not items:
            return None

        # 相邻重复消息折叠（开关默认关；命中时改写主 planner 历史为折叠后的列表）。
        folded_messages = items
        collapsed = False
        if self.config.judge.collapse_repeated:
            folded_messages, collapsed = self._collapse_repeated_messages(items)
            if collapsed:
                self.ctx.logger.debug(
                    f"[回复省流闸门] 折叠了 {len(items) - len(folded_messages)} 条相邻重复消息"
                )

        # 收集并暂存 user 文本（供 SKIP 占位取最新一条）。
        user_texts = self._collect_user_texts(folded_messages)
        if not user_texts:
            return None
        self._last_user_texts[session_id] = user_texts

        # 完整 planner 输入的拦截基准估算。
        full_estimate = self._estimate_full_planner_input(folded_messages)

        # 规则前置过滤：被@/点名（默认开）或直接提问（默认关）→ 直接 REPLY 放行，
        # 不调用便宜判定模型（省 token 与延迟，且点名必回更稳）。
        verdict: Optional[str] = None
        reason = ""
        usage = 0
        rule_reason = self._rule_reply_reason(user_texts)
        if rule_reason:
            self._stats["rule_reply"] += 1
            self.ctx.logger.info(f"[回复省流闸门] 规则命中（{rule_reason}），直接放行主 Planner")
            verdict, reason = "REPLY", rule_reason
        else:
            # 预算降载 + 判定（含缩小重试）。失败/放弃均由内部返回 None。
            try:
                verdict, reason, usage = await self._judge(
                    user_texts,
                    window=max(int(self.config.judge.max_messages or 8), 1),
                    chars=max(int(self.config.judge.max_chars_per_message or 200), 20),
                )
            except Exception as exc:
                self._stats["fail"] += 1
                self.ctx.logger.warning(f"[回复省流闸门] 判定失败，按放行处理: {exc}")
                return None

            if verdict is None:
                # 重试后仍无清晰结论：统计并按 fail-open 放行。
                reason = reason or "（未取得清晰结论）"
                self._stats["fail"] += 1
                self.ctx.logger.warning(f"[回复省流闸门] 判定无结论({reason})，按放行处理")
                return None

        # 放行轮：若开启旧历史摘要且输入超预算，用判定模型压缩最旧历史（内部有缓存）。
        # SKIP 轮会走 slim 覆盖，无需摘要。
        display_messages = folded_messages
        summarized_changed = False
        if verdict == "REPLY" and self.config.judge.summary_enabled:
            display_messages, summarized_changed = await self._maybe_summarize(
                session_id, folded_messages
            )

        result = self._record_result(
            verdict=verdict,
            reason=reason,
            session_id=session_id,
            usage=usage,
            full_estimate=full_estimate,
            original_messages=display_messages,
            hook_kwargs=dict(kwargs),
        )
        # 折叠或摘要发生在"放行且未注入预判"的路径：_record_result 返回 None（原样放行），
        # 但改写后的历史需要生效，因此单独返回一次改写结果。
        if result is None and (collapsed or summarized_changed):
            modified_kwargs = dict(kwargs)
            # 兜底保证 item_schema_version 存在，避免宿主反序列化时因缺失而静默丢弃改写。
            modified_kwargs.setdefault("item_schema_version", 1)
            modified_kwargs["items"] = display_messages
            return {
                "action": "continue",
                "modified_kwargs": modified_kwargs,
            }
        return result

    @HookHandler(
        "maisaka.planner.after_response",
        name="reply_gate_after_response",
        mode=HookMode.BLOCKING,
        order=HookOrder.EARLY,
        timeout_ms=3000,
        error_policy=ErrorPolicy.SKIP,
    )
    async def on_planner_after_response(
        self,
        **kwargs: Any,
    ) -> Optional[Dict[str, Any]]:
        """主模型响应后的 Hook（MaiBot 1.2.3+ Context Item 载荷）。

        若本会话最近一次判定为 SKIP：
            - 从缓存表中移除该标记（消费）；
            - 把 output_items 清空，主模型占位输出不会产生任何可见内容，
              核心按"无工具且无文本"自然收尾，本轮以"不回复"结束。
        若最近一次判定为 REPLY：
            - 主模型产出了可见正文（output_items 中的 AssistantMessageItem）
              → 登记一条"期望发送"（等群里真实发送命中，超窗未命中则计入
              "放行后最终未发出"）；
            - 主模型未产出任何可见文本（只思考、只调工具未说话）→ 计入"放行后未回复"。
        否则不修改任何东西。

        Returns:
            需要改写响应时返回 Hook 结果字典，否则返回 None。
        """
        if not self._is_active():
            return None
        session_id = str(kwargs.get("session_id") or "")
        if not session_id:
            return None

        verdict = self._verdicts.pop(session_id, None)
        if verdict == "SKIP":
            # 清空所有输出 Item，确保本轮绝无可见发言或副作用动作。
            modified_kwargs = dict(kwargs)
            modified_kwargs["output_items"] = []
            return {
                "action": "continue",
                "modified_kwargs": modified_kwargs,
            }
        if verdict == "REPLY":
            response_text = self._output_items_text(kwargs.get("output_items"))
            if response_text:
                # 放行且主模型说话了：期望群里会出现一次真实发送（可能是文本，也可能是生图等）。
                self._expect_send(session_id)
            else:
                # 放行但主模型没说话：只思考、或只调了工具没说话，都算"未回复"。
                self._record_no_reply(session_id)
        return None

    def _output_items_text(self, output_items: Any) -> str:
        """从 after_response 的 output_items 载荷提取主模型可见正文。

        模型输出可能是多个 Item 混合（reasoning / assistant 正文 / 工具调用等），
        只有 AssistantMessageItem 的文本会被真正发送，仅拼接这些正文文本。
        """
        if not isinstance(output_items, list):
            return ""
        text_parts = [
            _text_from_item(item)
            for item in output_items
            if isinstance(item, dict) and item.get("item_type") == "AssistantMessageItem"
        ]
        return "".join(text_parts).strip()

    def _expect_send(self, session_id: str) -> None:
        """为一次"放行且主模型产出正文"的轮次登记一条期望发送。

        判断该轮是否真正发出，交给 send_service.after_send 消费；
        后台清扫任务负责把超窗仍未消费的条目结算为"放行后最终未发出"。
        """
        try:
            window = max(
                float(getattr(self.config.judge, "send_tracking_window_sec", None) or SEND_TRACKING_WINDOW_SEC),
                5.0,
            )
        except (TypeError, ValueError):
            window = SEND_TRACKING_WINDOW_SEC
        self._expected_sends.setdefault(session_id, []).append(time.monotonic() + window)

    def _record_no_send(self, session_id: str, count: int = 1) -> None:
        """记录一次/多条"放行后最终未发出"（有正文但超窗仍无成功发送），计入全局/按模型/按会话。"""
        model_name = self.config.plugin.model.strip() or "（未填）"
        self._stats["no_send_after_reply"] += count
        for bucket in (
            self._stats["by_model"].setdefault(model_name, {}),
            self._stats["by_session"].setdefault(session_id, {}),
        ):
            bucket["no_send"] = bucket.get("no_send", 0) + count

    def _record_no_reply(self, session_id: str) -> None:
        """记录一次"放行后主模型未回复"（未产出任何可见文本），计入全局/按模型/按会话。"""
        model_name = self.config.plugin.model.strip() or "（未填）"
        self._stats["no_reply_after_reply"] += 1
        for bucket in (
            self._stats["by_model"].setdefault(model_name, {}),
            self._stats["by_session"].setdefault(session_id, {}),
        ):
            bucket["no_reply"] = bucket.get("no_reply", 0) + 1

    # ---- 放行后"最终是否发出"的发送侧追踪 ----

    @HookHandler(
        "send_service.after_send",
        name="reply_gate_after_send",
        mode=HookMode.BLOCKING,
        order=HookOrder.EARLY,
        timeout_ms=3000,
        error_policy=ErrorPolicy.SKIP,
    )
    async def on_send_service_after_send(
        self,
        message: Optional[Dict[str, Any]] = None,
        sent: bool = False,
        **kwargs: Any,
    ) -> Optional[Dict[str, Any]]:
        """发送完成后的观察 Hook：真实发送命中时，消费对应会话的一条"期望发送"。

        只读观察（不改写任何参数）；即使插件未启用也保持 no-op。
        消息序列化载荷自带顶层 session_id（与 planner 会话 ID 一致），优先用它定位；
        缺失时回退按 platform + group/user id 拼装。
        """
        del kwargs
        if not self._expected_sends or not sent:
            return None
        session_id = self._session_id_from_message(message)
        if not session_id:
            return None
        deadlines = self._expected_sends.get(session_id)
        if not deadlines:
            return None
        # 消费一条期望发送（先 pop 最先到期的）。
        deadlines.pop(0)
        if not deadlines:
            self._expected_sends.pop(session_id, None)
        return None

    @staticmethod
    def _session_id_from_message(message: Optional[Dict[str, Any]]) -> str:
        """从 send_service 序列化消息载荷中解析会话 ID，回退按群/单聊 ID 拼装。"""
        if not isinstance(message, dict):
            return ""
        session_id = str(message.get("session_id") or "").strip()
        if session_id:
            return session_id
        platform = str(message.get("platform") or "").strip()
        info = message.get("message_info") if isinstance(message.get("message_info"), dict) else {}
        group_info = info.get("group_info") if isinstance(info.get("group_info"), dict) else {}
        group_id = str(group_info.get("group_id") or "").strip()
        if platform and group_id:
            return f"{platform}_group_{group_id}"
        user_info = info.get("user_info") if isinstance(info.get("user_info"), dict) else {}
        user_id = str(user_info.get("user_id") or "").strip()
        if platform and user_id:
            return f"{platform}_private_{user_id}"
        return ""

    def _start_sweeper(self) -> None:
        """启动"期望发送"超时清扫任务（幂等）。"""
        if self._sweep_task is not None:
            return
        self._sweep_task = asyncio.create_task(self._sweep_loop())

    async def _sweep_loop(self) -> None:
        """周期性地把超窗仍未消费的"期望发送"结算为"放行后最终未发出"。"""
        while True:
            try:
                await asyncio.sleep(SEND_SWEEP_INTERVAL_SEC)
            except asyncio.CancelledError:
                return
            self._sweep_expired_sends()

    def _sweep_expired_sends(self) -> None:
        """结算已过期的期望发送：计入"最终未发出"并从表中移除。"""
        if not self._expected_sends:
            return
        now = time.monotonic()
        for session_id, deadlines in list(self._expected_sends.items()):
            remaining = [d for d in deadlines if d > now]
            expired = len(deadlines) - len(remaining)
            if expired > 0:
                self._record_no_send(session_id, expired)
                self.ctx.logger.info(
                    f"[回复省流闸门] 放行后有正文但超窗未发出 {expired} 条（session={session_id}），"
                    f"计入'最终未发出'（窗口={self._send_window_sec_str()}s）"
                )
            if remaining:
                self._expected_sends[session_id] = remaining
            else:
                self._expected_sends.pop(session_id, None)

    def _send_window_sec_str(self) -> str:
        try:
            return (
                f"{max(float(getattr(self.config.judge, 'send_tracking_window_sec', None) or SEND_TRACKING_WINDOW_SEC), 5.0):g}"
            )
        except (TypeError, ValueError):
            return f"{SEND_TRACKING_WINDOW_SEC:g}"

    # ==================== 手动命令 ====================

    def _status_text(self) -> str:
        """汇总当前开关状态、配置完整度与判定统计，供 /replygate status 展示。"""
        stats = self._stats
        configured = not self._missing_llm_config()
        active = self._is_active()
        saved = stats["saved_input_tokens"]
        last = stats["last_reason"]
        last_line = f"最近判定: {stats['last_verdict']} - {last}" if last else "最近判定: （暂无）"
        return (
            "回复省流闸门\n"
            f"状态: {'运行中' if active else '未启用'}"
            f"（config.enabled={self.config.plugin.enabled}, 配置完整={configured}）\n"
            f"判定模型: {self.config.plugin.model or '（未填）'}\n"
            f"规则直放: 被@(按钮)={'开' if self.config.judge.at_button_reply else '关'} | "
            f"口头点名(提名字)={'开' if self.config.judge.name_mention_reply else '关'} | "
            f"直接提问={'开' if self.config.judge.question_word_reply else '关'}\n"
            f"真实消息@识别: {'开（最近 %d 条）' % self._realmsg_scan_count if self._realmsg_scan_enabled else '关（旧逻辑：只看最后一条 user 文本）'}\n"
            f"判定: {stats['total']} 次 | 拦截(SKIP) {stats['skip']} | "
            f"放行(REPLY) {stats['reply']} | 失败放行 {stats['fail']}\n"
            f"放行后未回复(主模型无正文): {stats['no_reply_after_reply']} 次 | "
            f"最终未发出(有正文超窗): {stats['no_send_after_reply']} 次\n"
            f"规则直放(点名/提问): {stats['rule_reply']} 次 | 旧历史摘要: {stats['summary_count']} 次\n"
            f"节省估算: 拦截约 {saved} tok\n"
            f"{last_line}"
        )

    def _format_tokens(self, value: int) -> str:
        """把 token 数格式化为易读的带千分位文本。"""
        return f"{int(value):,}"

    def _savings_text(self) -> str:
        """生成 /replygate savings 清单：总览 + 按模型 + 按会话明细。"""
        stats = self._stats
        model_name = self.config.plugin.model.strip() or "（未填）"
        saved = stats["saved_input_tokens"]
        judge_cost = stats["judge_usage_tokens"]
        net_saved = max(0, saved - judge_cost)

        lines = [
            "回复省流闸门 · Token 节省估算（约）",
            f"判定轮次: {stats['total']} 次",
            f"拦截次数: {stats['skip']} 次",
            f"放行次数: {stats['reply']} 次（其中主模型未回复 {stats['no_reply_after_reply']} 次，"
            f"最终未发出 {stats['no_send_after_reply']} 次）",
            f"失败/放弃放行: {stats['fail']} 次",
            f"规则直放(点名/提问): {stats['rule_reply']} 次",
            f"单次总token上限: {self.config.llm.max_total_tokens}",
            f"主 planner 输入总估算: {self._format_tokens(stats['input_tokens_total'])} tok",
            f"其中 SKIP 拦截估算: {self._format_tokens(saved)} tok",
            f"判定模型实际消耗: {self._format_tokens(judge_cost)} tok",
            f"净节省估算: ~{self._format_tokens(net_saved)} tok",
            f"触发缩小重试: {stats['shrinks']} 次 | 输出达上限: {stats['output_at_limit']} 次",
        ]

        # 按模型明细。
        by_model = stats["by_model"]
        if by_model:
            lines.append("")
            lines.append("按模型明细:")
            for name, bucket in sorted(by_model.items()):
                lines.append(
                    f"  {name}: 判定 {bucket.get('total', 0)} 次 | "
                    f"拦截 {bucket.get('skip', 0)} | 放行 {bucket.get('reply', 0)} | "
                    f"消耗 {self._format_tokens(bucket.get('judge_tokens', 0))} tok | "
                    f"拦截节约 {self._format_tokens(bucket.get('saved_tokens', 0))} tok"
                )

        # 按会话明细（Top 5）。
        by_session = stats["by_session"]
        if by_session:
            lines.append("")
            lines.append("按会话明细（Top 5）:")
            ranked = sorted(by_session.items(), key=lambda kv: kv[1].get("saved_tokens", 0), reverse=True)
            for sid, bucket in ranked[:5]:
                lines.append(
                    f"  {sid}: 拦截 {bucket.get('skip', 0)} 次(节约 "
                    f"{self._format_tokens(bucket.get('saved_tokens', 0))} tok) | 放行 {bucket.get('reply', 0)}"
                )
        return "\n".join(lines)

    def _model_text(self) -> str:
        """生成 /replygate model 清单：当前模型配置 + 按模型统计。"""
        stats = self._stats
        cfg = self.config
        model_name = cfg.plugin.model.strip() or "（未填）"
        lines = [
            "回复省流闸门 · 模型清单",
            f"配置模型: {model_name}",
            f"接口: {cfg.llm.base_url or '（未填）'}",
            f"单次总token上限: {cfg.llm.max_total_tokens}",
            f"状态: {'运行中' if self._is_active() else '未启用'}",
        ]
        bucket = stats["by_model"].get(model_name, {})
        lines.extend(
            [
                "按模型统计:",
                f"  {model_name}",
                f"    判定次数: {bucket.get('total', 0)}",
                f"    拦截次数: {bucket.get('skip', 0)}",
                f"    放行次数: {bucket.get('reply', 0)}",
                f"    放行后未回复: {bucket.get('no_reply', 0)}",
                f"    放行后最终未发出: {bucket.get('no_send', 0)}",
                f"    消耗token: {self._format_tokens(bucket.get('judge_tokens', 0))}",
                f"    拦截节约: {self._format_tokens(bucket.get('saved_tokens', 0))} tok",
            ]
        )
        return "\n".join(lines)

    @Command("replygate_status", description="查看回复省流闸门状态与判定统计", pattern=r"^/replygate\s*status\s*$", aliases=["/状态"])
    async def cmd_status(self, **kwargs: Any) -> Tuple[bool, str, int]:
        """/replygate status（或 /状态）—— 展示插件状态与判定统计概要。"""
        stream_id = kwargs.get("stream_id", "")
        text = self._status_text()
        # Command 机制不会自动把返回文本发到聊天，需要主动发送到原会话。
        if stream_id:
            await self.ctx.send.text(text, stream_id)
        # 拦截等级 2：已在此回复，避免 AI 再对命令消息补一次回复。
        return True, text, 2

    @Command("replygate_savings", description="查看回复省流闸门 Token 节省明细清单", pattern=r"^/replygate\s*savings\s*$")
    async def cmd_savings(self, **kwargs: Any) -> Tuple[bool, str, int]:
        """/replygate savings —— 展示判定/拦截/放行次数与 Token 节省估算清单（含按模型、按会话明细）。"""
        stream_id = kwargs.get("stream_id", "")
        text = self._savings_text()
        if stream_id:
            await self.ctx.send.text(text, stream_id)
        return True, text, 2

    @Command("replygate_model", description="查看回复省流闸门判定模型配置与按模型统计清单", pattern=r"^/模型\s*$")
    async def cmd_model(self, **kwargs: Any) -> Tuple[bool, str, int]:
        """/模型 —— 展示当前判定模型配置、拦截/放行次数、放行后未回复次数与消耗明细。"""
        stream_id = kwargs.get("stream_id", "")
        text = self._model_text()
        if stream_id:
            await self.ctx.send.text(text, stream_id)
        return True, text, 2

    @Command("replygate_on", description="临时开启回复省流闸门（仅内存，重启后以配置为准）", pattern=r"^/replygate\s*on\s*$")
    async def cmd_on(self, **kwargs: Any) -> Tuple[bool, str, int]:
        """/replygate on —— 临时开启。

        仅改内存标志，不写回配置文件；重启后以 config.toml 的 plugin.enabled 为准。
        """
        stream_id = kwargs.get("stream_id", "")
        if not self.config.plugin.enabled:
            text = "config.toml 中 plugin.enabled=false，开启失败；请先改配置并重载插件"
        elif self._missing_llm_config():
            text = "判定模型配置不完整，请补全 plugin.model / llm.base_url / llm.api_key"
        else:
            self._runtime_enabled = True
            text = "回复省流闸门已临时开启"
        if stream_id:
            await self.ctx.send.text(text, stream_id)
        return True, text, 2

    @Command("replygate_off", description="临时关闭回复省流闸门（仅内存）", pattern=r"^/replygate\s*off\s*$")
    async def cmd_off(self, **kwargs: Any) -> Tuple[bool, str, int]:
        """/replygate off —— 临时关闭，并清空会话判定缓存与期望发送表。"""
        stream_id = kwargs.get("stream_id", "")
        self._runtime_enabled = False
        self._verdicts.clear()
        self._expected_sends.clear()
        text = "回复省流闸门已临时关闭"
        if stream_id:
            await self.ctx.send.text(text, stream_id)
        return True, text, 2

    @Command(
        "replygate_realmsg",
        description="查看/开关『真实消息@识别』：/replygate realmsg 查看，realmsg on|off 切换（仅内存）",
        pattern=r"^/replygate\s+realmsg(?:\s+(?P<action>on|off))?\s*$",
    )
    async def cmd_realmsg(self, **kwargs: Any) -> Tuple[bool, str, int]:
        """/replygate realmsg [on|off] —— 查看/临时切换『真实消息 @ 识别』。

        开启（默认，对齐 [judge].scan_recent_replies=true）：@/点名规则与判定"最新
        消息"扫描最近 N 条**真实**用户发言，避免被 Planner 末尾注入的时间/提醒/
        画像等消息顶掉、导致"明明 @ 了却被 SKIP"。
        关闭（回退旧逻辑，对齐 scan_recent_replies=false）：规则与待处理消息只看
        最后一条 user 文本。
        仅改内存标志，不写回配置；重启后以 config.toml 的 [judge].scan_recent_*
        为准。
        """
        stream_id = kwargs.get("stream_id", "")
        matched = kwargs.get("matched_groups") or {}
        action = str(matched.get("action") or "").strip().lower()
        if action == "on":
            self._realmsg_scan_enabled = True
            text = (
                f"真实消息@识别已开启：扫描最近 {self._realmsg_scan_count} 条真实发言进行"
                "@/点名识别与判定（重启后以配置为准）"
            )
        elif action == "off":
            self._realmsg_scan_enabled = False
            text = "真实消息@识别已关闭，回到旧逻辑（只看最后一条 user 文本；重启后以配置为准）"
        else:
            state = (
                "开（最近 %d 条）" % self._realmsg_scan_count
                if self._realmsg_scan_enabled
                else "关（旧逻辑：只看最后一条 user 文本）"
            )
            text = f"回复省流闸门 · 真实消息@识别：{state}（用法 /replygate realmsg on|off）"
        if stream_id:
            await self.ctx.send.text(text, stream_id)
        return True, text, 2


def create_plugin() -> ReplyGatePlugin:
    """插件入口，供 MaiBot 插件运行时实例化。"""
    return ReplyGatePlugin()