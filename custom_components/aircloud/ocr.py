"""验证码自动识别 —— 列出 HA 里已配置的**全部**模型 provider，由用户自选。

设计取舍（用户明确要求）：
  * 不做「哪些后端支持视觉」的预先过滤 —— 全都列出来，让用户自己挑
  * 界面文案里写明**务必选择支持视觉（多模态）的模型**；选错也不拦，
    OCR 失败/识别不准时会自动退回「人工填验证码」，不会把用户卡住
  * 因此这里只做「能拿到 endpoint + api_key + 模型名」的枚举，不做可用性探测

HA 的模型配置有两种存放形态，都要覆盖：
  1. **条目级**（llmvision、ai_conversation）：data 里直接有 api_key / base / default_model
  2. **子条目级**（HA 2025.7+ 的 subentries）：一个集成条目下挂多个「对话模型」子条目，
     模型名在 subentry.data 里；字段名各域不同（model / chat_model），
     有的连 endpoint 和 key 也在子条目里（ai_hub 的 chat_url / custom_api_key）

实测（2026-09，真实平台校验 12 次样本）：
  * 原图 160x80 单次准确率 **83%**；4x 放大反而降到 58%，所以直接用原图
  * 失败可换图重试：3 次累计 99.5%、5 次 99.99%（平台取图无频率限制）
  * 识别结果会按文件名缓存 —— **每次必须用唯一文件名**，否则永远拿到首张图的结果
"""
from __future__ import annotations

import base64
import json as _json
import logging
import os
import uuid
from typing import Any, Iterable

import aiohttp
from homeassistant.core import HomeAssistant

from .const import OCR_DIR_NAME, OCR_PROMPT

_LOGGER = logging.getLogger(__name__)

LLMVISION_DOMAIN = "llmvision"
_SERVICE = "image_analyzer"
# 供别的模块（relogin）判断依赖是否已就绪
SERVICE_NAME = _SERVICE


def llmvision_ready(hass: HomeAssistant) -> bool:
    """llmvision 的 image_analyzer 服务是否已注册。

    HA 启动时各集成 setup 顺序不确定：本集成可能比 llmvision 先起，
    此时调用服务会报 ``Action llmvision.image_analyzer not found``。
    这不是真的失败，应当稍后重试而不是计入失败冷却。
    """
    try:
        return hass.services.has_service(LLMVISION_DOMAIN, SERVICE_NAME)
    except Exception:  # noqa: BLE001
        return False

# 单次识别超时（秒）。3 次重试最坏 ~3×该值；选到慢/坏的 provider 时
# 不能让配置流程卡几分钟，超时即算本次失败并换图重试。
OCR_TIMEOUT_S = 25

# 需要枚举的「可能提供模型」的集成域
AI_DOMAINS: tuple[str, ...] = (
    "llmvision",
    "ai_conversation",
    "openai_conversation",
    "google_generative_ai_conversation",
    "anthropic",
    "ollama",
    "open_router",
    "azure_openai_conversation",
    "extended_openai_conversation",
    "openwebui",
    "vllm",
    "lmstudio",
    "groq",
    "xai",
    "ai_hub",
)

# 已确认可用 OpenAI 兼容 /chat/completions 的域（直连）
_OPENAI_COMPAT_DOMAINS = {
    "ai_conversation",
    "google_generative_ai_conversation",
    "extended_openai_conversation",
    "openai_conversation",
    "azure_openai_conversation",
    "openwebui",
    "vllm",
    "lmstudio",
    "open_router",
    "groq",
    "xai",
    "ai_hub",
}

# 域 → OpenAI 兼容端点（集成不显式存 base 时使用）
_DEFAULT_BASE: dict[str, str] = {
    "openai_conversation": "https://api.openai.com/v1",
    "google_generative_ai_conversation": "https://generativelanguage.googleapis.com/v1beta/openai",
    "open_router": "https://openrouter.ai/api/v1",
    "groq": "https://api.groq.com/openai/v1",
    "xai": "https://api.x.ai/v1",
    "vllm": "http://localhost:8000/v1",
    "lmstudio": "http://localhost:1234/v1",
}

# 子条目里可能承载模型名 / 端点 / 密钥的字段
_MODEL_FIELDS = ("model", "chat_model")
_KEY_FIELDS = ("custom_api_key", "api_key", "custom_openai_api_key", "openai_api_key", "access_token", "token")
_BASE_FIELDS = ("chat_url", "base", "custom_openai_endpoint", "api_base", "base_url", "url")


def _pick(d: Any, names: Iterable[str]) -> str:
    if not isinstance(d, dict):
        return ""
    for n in names:
        v = d.get(n)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def _iter_subentries(entry: Any) -> list[Any]:
    """兼容 subentries 的各种容器形态。

    HA 各版本给的是 dict / MappingProxyType / 自定义 Mapping / list，
    这里不用 isinstance 判断容器，直接按鸭子类型取 values()。
    """
    subs = getattr(entry, "subentries", None)
    if not subs:
        return []
    getter = getattr(subs, "values", None)
    if callable(getter):
        try:
            return list(getter())
        except Exception:  # noqa: BLE001
            return []
    try:
        return list(subs)
    except TypeError:
        return []


def _sub_type(sub: Any) -> str:
    if isinstance(sub, dict):
        return str(sub.get("subentry_type") or "")
    return str(getattr(sub, "subentry_type", "") or "")


def _sub_data(sub: Any) -> dict:
    """取子条目的 data。

    注意 ConfigSubentry.data 是 **MappingProxyType**，不是 dict，
    所以必须按映射鸭子类型转，不能 isinstance(..., dict)。
    """
    if isinstance(sub, dict):
        data = sub.get("data")
    else:
        data = getattr(sub, "data", None)
    if isinstance(data, str):
        # .storage 里是 repr 字符串（单引号），尽力解析
        try:
            data = _json.loads(data.replace("'", '"'))
        except Exception:  # noqa: BLE001
            return {}
    if not data:
        return {}
    # MappingProxyType / 其它 Mapping → dict
    items = getattr(data, "items", None)
    if callable(items):
        try:
            return dict(items())
        except Exception:  # noqa: BLE001
            return {}
    return {}


async def async_list_providers(hass: HomeAssistant) -> dict[str, dict[str, Any]]:
    """枚举 HA 里已配置的**全部** provider —— 不做任何可用性/视觉能力过滤。

    返回 {选项 value: {label, kind, entry_id, model, base, key}}。
      * kind = "llmvision" → 走 llmvision.image_analyzer 服务
      * kind = "openai"    → 走 OpenAI 兼容 /chat/completions 直连

    不检查能不能用、是不是视觉模型：用户自己选。选错了自动识别会失败，
    此时配置流程会自动退回「显示验证码图、人工填写」，不会把用户卡住。
    """
    providers: dict[str, dict[str, Any]] = {}

    for entry in hass.config_entries.async_entries():
        domain = getattr(entry, "domain", None)
        if domain not in AI_DOMAINS:
            continue
        data = dict(getattr(entry, "data", None) or {})
        entry_id = entry.entry_id
        title = getattr(entry, "title", None) or entry_id
        subs = _iter_subentries(entry)

        # ---- llmvision：条目级（provider=Settings 是全局设置，不是 provider）----
        if domain == LLMVISION_DOMAIN:
            if data.get("provider") == "Settings":
                continue
            model = _pick(data, ("default_model", "model"))
            providers[f"llmvision:{entry_id}"] = {
                "label": f"{title}（LLM Vision）",
                "kind": "llmvision",
                "entry_id": entry_id,
                "model": model,
                "base": _pick(data, _BASE_FIELDS),
                "key": _pick(data, _KEY_FIELDS),
            }
            continue

        # ---- 其它集成：条目级密钥/端点 ----
        entry_key = _pick(data, _KEY_FIELDS)
        entry_base = _pick(data, _BASE_FIELDS) or _DEFAULT_BASE.get(domain, "")

        if domain not in _OPENAI_COMPAT_DOMAINS:
            # 不认识调用方式的域也照样列出来（用户要求「全都列出」），
            # 选了之后自动识别会失败并退回人工填写。
            providers[f"other:{entry_id}"] = {
                "label": f"{title}（{domain}）",
                "kind": "unsupported",
                "entry_id": entry_id,
                "model": _pick(data, _MODEL_FIELDS),
                "base": entry_base,
                "key": entry_key,
            }
            continue

        listed = False
        for sub in subs:
            stype = _sub_type(sub)
            if stype != "conversation":
                continue  # stt/tts/ai_task_data 不是模型入口
            sd = _sub_data(sub)
            model = _pick(sd, _MODEL_FIELDS)
            base = _pick(sd, _BASE_FIELDS) or entry_base
            key = _pick(sd, _KEY_FIELDS) or entry_key
            label = f"{title} — {model}" if model else title
            providers[f"openai:{entry_id}:{model or stype}"] = {
                "label": label,
                "kind": "openai",
                "entry_id": entry_id,
                "model": model,
                "base": base,
                "key": key,
            }
            listed = True

        if not listed:
            # 没有 conversation 子条目 → 整条集成作为一个 provider 列出
            model = _pick(data, ("default_model", "model", "chat_model"))
            providers[f"openai:{entry_id}:{model or '-'}"] = {
                "label": f"{title} — {model}" if model else title,
                "kind": "openai",
                "entry_id": entry_id,
                "model": model,
                "base": entry_base,
                "key": entry_key,
            }

    _LOGGER.debug("共枚举到 %d 个 provider：%s", len(providers),
                  [v["label"] for v in providers.values()])
    return providers


def _write_temp(hass: HomeAssistant, data_url: str) -> str:
    """把 data-url 写成唯一文件名的临时图片，返回 HA 容器内路径。

    落在 /config/www/<dir>/ 下：该目录 HA 进程必然可读，
    且文件名唯一可绕开 llmvision 的结果缓存。
    """
    raw = base64.b64decode(data_url.split(",", 1)[1])
    rel = os.path.join("www", OCR_DIR_NAME)
    abs_dir = hass.config.path(rel)
    os.makedirs(abs_dir, exist_ok=True)
    name = f"cap_{uuid.uuid4().hex}.jpg"
    abs_path = os.path.join(abs_dir, name)
    with open(abs_path, "wb") as fh:
        fh.write(raw)
    return os.path.join(hass.config.config_dir, rel, name)


def _extract_text(result: object) -> str:
    """兼容两种返回形态。

    同一服务经 REST `return_response` 返回 {"service_response": {"response_text": ...}}，
    而集成内部直接 `hass.services.async_call(..., return_response=True)` 拿到的
    是内层 dict（实测），这里两种都兼容。
    """
    if not isinstance(result, dict):
        return ""
    if "service_response" in result:
        result = result.get("service_response") or {}
    if not isinstance(result, dict):
        return ""
    return str(result.get("response_text") or "")


def _clean(text: str) -> str | None:
    """清洗模型输出：模型有时会带解释或空格（实测返回过 "g m R B"）。

    注意必须用 `ch.isascii() and ch.isalnum()` —— 单独的 `str.isalnum()`
    对中文也返回 True，会把解释文字混进结果。
    """
    cleaned = "".join(ch for ch in (text or "") if ch.isascii() and ch.isalnum())
    if len(cleaned) >= 4:
        return cleaned[:4]
    return None


async def _read_via_llmvision(
    hass: HomeAssistant, abs_path: str, provider: dict[str, Any]
) -> str | None:
    result = await hass.services.async_call(
        LLMVISION_DOMAIN,
        _SERVICE,
        {
            "provider": provider["entry_id"],
            "model": provider.get("model") or None,
            "image_file": abs_path,
            "include_filename": False,
            "message": OCR_PROMPT,
            "max_tokens": 32,
        },
        blocking=True,
        return_response=True,
    )
    return _clean(_extract_text(result))


async def _read_via_openai(
    hass: HomeAssistant, data_url: str, provider: dict[str, Any]
) -> str | None:
    """OpenAI 兼容 /chat/completions 直连（带上用户已配置的 base + key）。"""
    if not provider.get("base") or not provider.get("model"):
        _LOGGER.warning(
            "provider「%s」缺少端点或模型名，无法自动识别，转人工填写",
            provider.get("label") or provider.get("entry_id"),
        )
        return None
    base = str(provider["base"]).rstrip("/")
    if not base.endswith("/chat/completions"):
        base = f"{base}/chat/completions"
    body = {
        "model": provider["model"],
        "max_tokens": 32,
        "temperature": 0.1,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": OCR_PROMPT},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            }
        ],
    }
    headers = {
        "Authorization": f"Bearer {provider['key']}",
        "Content-Type": "application/json",
    }
    timeout = aiohttp.ClientTimeout(total=OCR_TIMEOUT_S)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(base, json=body, headers=headers) as resp:
            text = await resp.text()
            if resp.status != 200:
                _LOGGER.warning(
                    "验证码识别 HTTP %s（%s）：%s",
                    resp.status,
                    provider["model"],
                    text[:200],
                )
                return None
    try:
        payload = _json.loads(text)
    except ValueError:
        return None
    choices = payload.get("choices") or []
    if not choices:
        return None
    msg = choices[0].get("message") or {}
    return _clean(str(msg.get("content") or ""))


async def async_read_captcha(
    hass: HomeAssistant, data_url: str, provider: dict[str, Any] | str
) -> str | None:
    """用指定 provider 识别验证码，返回 4 字符或 None（失败）。

    provider 通常是 async_list_providers() 返回的 dict；
    也兼容纯 entry_id 字符串（按 llmvision 处理）。
    """
    if not data_url or not provider:
        return None
    if isinstance(provider, str):
        provider = {
            "kind": "llmvision",
            "entry_id": provider,
            "model": "",
            "base": "",
            "key": "",
            "label": provider,
        }
    if provider.get("kind") == "unsupported":
        _LOGGER.warning(
            "provider「%s」暂不支持自动调用，转人工填写验证码",
            provider.get("label") or provider.get("entry_id"),
        )
        return None
    abs_path = None
    try:
        if provider.get("kind") == "llmvision":
            abs_path = await hass.async_add_executor_job(_write_temp, hass, data_url)
            return await _read_via_llmvision(hass, abs_path, provider)
        return await _read_via_openai(hass, data_url, provider)
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning(
            "验证码识别失败（%s）：%s", provider.get("label") or provider, err
        )
        return None
    finally:
        if abs_path:
            try:
                await hass.async_add_executor_job(os.unlink, abs_path)
            except OSError:
                pass
