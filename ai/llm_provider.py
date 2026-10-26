# LlmProvider：模型池（models 表）+ agent 绑定（agent_model 表）驱动实际调用
# 设计对齐 AI 方案 §1.1「模型池与 agent 绑定」：
#   1. 各 agent 实际调用 = agent_model 绑定的 active 模型
#   2. 绑定模型被停用 → 回退同能力 is_default=1 且 active 的模型
#   3. 无可用模型 → 抛 LlmUnavailableError，由上层降级为确定性规则（链路不中断）
import json
import os
import random
import re
import threading
import time
from urllib.parse import urlsplit

import httpx
from openai import OpenAI, RateLimitError

from sqlalchemy import select

from ai import trace
from app.models import AgentModel, LlmModel
from app.session import SessionLocal

# agent 清单（管理端「Agent 模型绑定」数据源；retriever 为确定性检索无 LLM 不绑定）
# planner / reviewer 为 v4 主-子编排新增 agent_key（详细设计 §3.8 / v4 §2.1）。
# 运行时二者都只用普通 chat + 本地 JSON 解析，不走 tools/function calling API：
# planner 保留 function_call 标签（既有绑定口径）；reviewer 按 chat 校验，以便绑定本地对话模型
AGENT_DEFS: dict[str, dict] = {
    "main":             {"label": "主 Agent（汇总/生成）",    "capability": "chat"},
    "planner":          {"label": "规划主 Agent（Planner）",   "capability": "function_call"},
    "reviewer":         {"label": "复审质检 Agent（Reviewer）", "capability": "chat"},
    "qa_router":        {"label": "检索路由 / 查询改写",      "capability": "chat"},
    "summarizer":       {"label": "汇总 / 综述",             "capability": "chat"},
    "analyst-method":   {"label": "方法抽取 Analyst",        "capability": "chat"},
    "analyst-critic":   {"label": "批判审阅 Analyst",        "capability": "chat"},
    "analyst-compare":  {"label": "横向对比 Analyst",        "capability": "chat"},
}


class LlmUnavailableError(Exception):
    """无可用模型（模型池空/全部停用）——上层应降级为确定性规则"""


# 客户端缓存：同 (base_url, api_key) 复用 OpenAI 实例
_clients: dict[tuple[str, str], OpenAI] = {}

CLIENT_TIMEOUT = 60   # 单次对话补全超时（秒）

# 429 退避重试：只重试「限流」，超时/5xx/连接错误重试也救不了，交给上层降级。
# 退避序列 1s → 2s → 4s（每次加 0~0.3s 抖动，避免多个线程同时醒来再次撞车）。
LLM_RETRY_MAX = 3
LLM_BACKOFF_BASE = 1.0
LLM_BACKOFF_JITTER = 0.3

# LLM 调用并发闸门：所有 agent（planner/reviewer/analyst/summarizer/router）共用。
# 多问答并发时单条流水线会再派生 3 个 analyst 线程，上游瞬间被几十个请求打满；
# 这里把同时在途的上游请求压到上限内，其余排队。
# 实测（10 并发）：上限 3 与 8 的 Reviewer 失败率一样（都是 4/10），故失败与本闸门无关；
# 闸门只管"别把上游打爆"，缺省 8。
LLM_MAX_CONCURRENCY = 8


def _llm_max_concurrency() -> int:
    """上游并发上限（缺省 8；LITRADAR_LLM_MAX_CONCURRENCY 可覆盖）"""
    try:
        return max(1, int(os.getenv("LITRADAR_LLM_MAX_CONCURRENCY", "8")))
    except ValueError:
        return LLM_MAX_CONCURRENCY


_LLM_SLOTS = threading.BoundedSemaphore(_llm_max_concurrency())

# 本机 endpoint（回环地址）：这类调用必须直连、绕开系统/环境代理。
#   openai SDK 底层的 httpx 在 trust_env=True 时经 urllib 读取 Windows 系统代理（WinINET 注册表），
#   且只用 no_proxy 环境变量判绕过、不读系统 ProxyOverride——开着系统代理的机器上，
#   发往 localhost 的请求会被塞进代理并返回 502，导致本地模型调用全线失败。
#   远端 endpoint 仍保留 trust_env 行为，以免失去访问外网所需的代理。
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}


def _is_local_base(base_url: str) -> bool:
    """base_url 是否指向本机（回环地址）"""
    try:
        host = urlsplit(base_url or "").hostname
    except ValueError:
        return False
    return (host or "").lower() in _LOCAL_HOSTS


def _get_client(base_url: str, api_key: str) -> OpenAI:
    key = (base_url, api_key)
    if key not in _clients:
        kwargs: dict = {"base_url": base_url or None, "api_key": api_key,
                        "timeout": CLIENT_TIMEOUT}
        if _is_local_base(base_url):
            kwargs["http_client"] = httpx.Client(trust_env=False, timeout=CLIENT_TIMEOUT)
        _clients[key] = OpenAI(**kwargs)
    return _clients[key]


def _has_capability(model_row: dict, capability: str) -> bool:
    caps = model_row["capabilities"]
    if isinstance(caps, str):
        try:
            caps = json.loads(caps)
        except json.JSONDecodeError:
            caps = []
    return capability in (caps or [])


def _model_extra(model_row: dict) -> dict:
    """模型级额外请求参数（models.extra 列）→ 透传给 OpenAI SDK 的 extra_body

    例：Ollama 上的 Qwen3 思考模型传 {"reasoning_effort": "none"} 关闭思考过程，
    避免推理 token 挤占 agent 的 max_tokens 预算
    """
    extra = model_row.get("extra")
    if isinstance(extra, str):
        try:
            extra = json.loads(extra)
        except json.JSONDecodeError:
            extra = None
    return extra if isinstance(extra, dict) else {}


def get_agent_model(agent_key: str) -> dict | None:
    """解析 agent 当前应使用的模型：绑定 → 默认兜底 → None"""
    cap = AGENT_DEFS.get(agent_key, {}).get("capability", "chat")
    t = LlmModel.__table__   # 列形态 select：mappings() 键即列名，下游 model["base_url"] 等读法不变
    with SessionLocal() as db_session:
        # 1. agent_model 绑定（仅启用状态模型有效）
        row = db_session.execute(
            select(t).join(AgentModel, AgentModel.model_id == t.c.id)
            .where(AgentModel.agent_key == agent_key, t.c.status == "active")
        ).mappings().first()
        if row and _has_capability(row, cap):
            return row
        # 2. 默认兜底：同能力 is_default=1 且 active（对齐 F-MODEL-05）
        for m in db_session.execute(
            select(t).where(t.c.is_default.is_(True), t.c.status == "active")
        ).mappings().all():
            if _has_capability(m, cap):
                return m
    return None


def chat(agent_key: str, prompt: str, system: str | None = None,
         timeout: int = 60, max_tokens: int = 2048,
         temperature: float | None = None) -> str:
    """按 agent 绑定的模型执行一次对话补全

    :param temperature: 传了就显式指定采样温度（0 表示固定取概率最高的词，结果可复现）；
                       不传则用服务端默认值（保持原有调用行为）
    :raises LlmUnavailableError: 无可用模型
    :raises Exception: 上游调用失败（上层按"失败回退"原则处理）
    """
    rec = trace.active()
    t0 = time.perf_counter()
    model = get_agent_model(agent_key)
    if not model:
        if rec:
            rec.llm(agent_key, "", prompt, error="无可用模型（模型池为空或已停用）")
        raise LlmUnavailableError(f"agent {agent_key} 无可用模型（模型池为空或已停用）")
    client = _get_client(model["base_url"] or "", model["api_key_enc"] or "")
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    extra = _model_extra(model)
    with _LLM_SLOTS:   # 排队等槽位的时间不计入请求超时，拿到槽位后才真正发起上游调用
        resp = _create_with_retry(agent_key, model, client, messages, max_tokens,
                                  timeout, temperature, extra, rec, prompt, t0)
    text = (resp.choices[0].message.content or "").strip()
    if rec:
        rec.llm(agent_key, model["model_id"], prompt, text,
                usage=_usage_dict(getattr(resp, "usage", None)),
                duration_ms=int((time.perf_counter() - t0) * 1000))
    return text


def _is_rate_limited(exc: Exception) -> bool:
    """是否 429 限流：openai 的 RateLimitError，或任何带 status_code=429 的异常（网关/自建代理）"""
    if isinstance(exc, RateLimitError):
        return True
    return getattr(exc, "status_code", None) == 429


def _retry_delay(attempt: int) -> float:
    """指数退避间隔：1s / 2s / 4s，再加 0~0.3s 抖动（attempt 从 0 起）"""
    return LLM_BACKOFF_BASE * (2 ** attempt) + random.uniform(0, LLM_BACKOFF_JITTER)


def _create_with_retry(agent_key: str, model: dict, client: OpenAI, messages: list,
                       max_tokens: int, timeout: int, temperature: float | None,
                       extra: dict, rec, prompt: str, t0: float):
    """上游调用 + 429 指数退避重试（其他异常一律不重试，直接抛给上层降级）

    重试期间不释放 _LLM_SLOTS：上游正在限流，放别的请求进来只会继续被拒；
    单次调用的 timeout 仍是 60s，仅对 429 追加等待，最坏多等 7s 左右。
    """
    for attempt in range(LLM_RETRY_MAX + 1):
        try:
            return client.chat.completions.create(
                model=model["model_id"],
                messages=messages,
                max_tokens=max_tokens,
                timeout=timeout,
                **({"temperature": temperature} if temperature is not None else {}),
                **({"extra_body": extra} if extra else {}),
            )
        except Exception as e:
            if not _is_rate_limited(e) or attempt >= LLM_RETRY_MAX:
                if rec:  # 最终失败才记一条错误 trace，避免重试过程把记录刷成多份
                    rec.llm(agent_key, model["model_id"], prompt,
                            error=f"{type(e).__name__}: {e}",
                            duration_ms=int((time.perf_counter() - t0) * 1000))
                raise
            delay = _retry_delay(attempt)
            print(f"[llm_provider] 429 限流，{delay:.1f}s 后重试"
                  f"（{attempt + 1}/{LLM_RETRY_MAX}）")
            time.sleep(delay)


def _usage_dict(usage) -> dict:
    """OpenAI 兼容响应的 token 用量 → 统一 dict（缺失时为空值）"""
    if usage is None:
        return {}
    return {
        "prompt_tokens": getattr(usage, "prompt_tokens", None),
        "completion_tokens": getattr(usage, "completion_tokens", None),
        "total_tokens": getattr(usage, "total_tokens", None),
    }


def chat_json(agent_key: str, prompt: str, **kwargs) -> dict | None:
    """要求 JSON 输出的对话；解析失败返回 None（不抛异常，由调用方回退）"""
    text = chat(agent_key, prompt, **kwargs)
    return parse_json_loose(text)


def parse_json_loose(text: str) -> dict | None:
    """宽松 JSON 解析：剥掉 ```json 围栏 / 提取首个平衡大括号块"""
    if not text:
        return None
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.S)
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        pass
    # 提取首个 {...} 平衡块
    depth, start = 0, -1
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                try:
                    obj = json.loads(text[start:i + 1])
                    return obj if isinstance(obj, dict) else None
                except json.JSONDecodeError:
                    start = -1
    return None
