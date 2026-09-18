"""自动重新登录 —— 凭据失效时由 coordinator 调用，无需用户介入。

背景：合宙 IoT 平台是**单会话**。任何其它地方（手机 App、网页）登录，
都会立刻让本集成的 token 失效（接口报 code 105）。只要把
手机号 + 密码 + 验证码识别模型留在条目里，集成就能自己重新登录把状态拉回来。

可行性依据（实测）：
  * 登录页永远要验证码，服务端**先验验证码再验密码**，所以必须能自动识别验证码；
    识别走用户在配置界面里选的模型，单次 83%，换图重试 3 次累计 99.5%
  * 验证码取图接口无频率限制（实测连取 15 次全部成功，0.17~0.20s/次）
  * 密码错误（code 4）是**永久性**失败 —— 直接放弃重试并要求用户重新配置，
    避免反复错密码把账号撞锁
"""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    CONF_OCR_PROVIDER,
    CONF_PASSWORD,
    CONF_PHONE,
    OCR_MANUAL,
    OCR_MAX_ATTEMPTS,
)
from .ocr import llmvision_ready
from .login import (
    AirCloudCaptchaError,
    AirCloudLoginError,
    AirCloudPasswordError,
    async_captcha,
    async_login,
    async_oauth_params,
)
from .ocr import async_list_providers, async_read_captcha

_LOGGER = logging.getLogger(__name__)


class ReloginUnavailable(Exception):
    """缺前提条件（没存密码 / 没选识别模型 / 选的模型没了）—— 必须用户介入。"""


class ReloginNotReady(Exception):
    """前提条件**暂时**不满足（依赖的集成还没加载完）—— 短间隔重试，不算失败。

    典型场景：HA 启动时本集成比 llmvision 先 setup，此时 ``image_analyzer``
    服务还没注册。这是启动顺序问题，几秒后就正常了，绝不能因此计入失败、
    更不能按小时级冷却等下去。
    """


def relogin_blocker(data: dict) -> str:
    """返回「无法自动重登」的原因；能自动重登时返回空串。

    coordinator 用它决定：自动重登失败时是继续自己重试（UpdateFailed），
    还是弹给用户处理（ConfigEntryAuthFailed）。
    """
    if not str(data.get(CONF_PHONE) or "").strip():
        return "条目里没有保存手机号"
    if not str(data.get(CONF_PASSWORD) or ""):
        return "条目里没有保存密码，请在集成选项里补填"
    key = str(data.get(CONF_OCR_PROVIDER) or "")
    if not key or key == OCR_MANUAL:
        return "未选择验证码识别模型（选「不自动识别」时无法自动重登）"
    return ""


async def async_relogin(hass: HomeAssistant, entry: ConfigEntry) -> dict[str, str]:
    """用保存的手机号 + 密码 + 识别模型跑一遍完整登录，返回新凭据。

    失败分类：
      * :class:`ReloginUnavailable` —— 前提不足或密码已错，重试无意义
      * :class:`AirCloudLoginError` —— 临时性失败（识别不准、网络抖动、限流），可重试
    """
    data = dict(entry.data or {})
    blocker = relogin_blocker(data)
    if blocker:
        raise ReloginUnavailable(blocker)

    phone = str(data[CONF_PHONE]).strip()
    password = str(data[CONF_PASSWORD])
    key = str(data[CONF_OCR_PROVIDER])

    # 每次重登都重新枚举 provider：用户可能换了模型、或改了端点/密钥
    providers = await async_list_providers(hass)
    provider = providers.get(key)
    if provider is None:
        raise ReloginUnavailable(
            f"验证码识别模型「{key}」已不存在，请重新配置集成"
        )

    if provider.get("kind") == "llmvision" and not llmvision_ready(hass):
        # 依赖集成还没加载完（HA 启动顺序），稍后重试即可
        raise ReloginNotReady("llmvision 尚未就绪，稍后重试")

    session = async_get_clientsession(hass)
    last_error = ""
    for attempt in range(1, OCR_MAX_ATTEMPTS + 1):
        # oauth 参数（client_id/code_challenge/state）可能过期，每轮重取，开销极小
        oauth = await async_oauth_params(session)
        cap_id, img = await async_captcha(session)

        code = await async_read_captcha(hass, img, provider)
        if not code:
            last_error = "验证码识别未取到结果"
            _LOGGER.debug("自动重登：第 %s 次识别无结果", attempt)
            continue

        try:
            creds = await async_login(session, oauth, phone, password, cap_id, code)
        except AirCloudCaptchaError as err:
            # 识别不准 → 换一张图重试
            last_error = str(err)
            _LOGGER.debug("自动重登：第 %s 次识别结果 %s 未被接受", attempt, code)
            continue
        except AirCloudPasswordError as err:
            # 密码错是永久性问题，继续试只会撞锁
            raise ReloginUnavailable(f"账号或密码已失效：{err}") from err
        except AirCloudLoginError as err:
            last_error = str(err)
            continue

        _LOGGER.info("自动重新登录成功（第 %s 次识别）", attempt)
        return creds

    raise AirCloudLoginError(last_error or "验证码识别多次未通过")
