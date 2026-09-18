"""AirCloud (合宙 IoT) integration.

登录态保活：平台是单会话，别处登录会让本集成的 token 立刻失效（code 105）。
条目里保存了手机号 + 密码 + 验证码识别模型，所以集成自己会重新登录，用户无感。

关键点：**重登必须在 setup 里做完**。HA 的 ``async_config_entry_first_refresh``
一旦失败不会排下一轮定时器，集成会永久卡住；因此 ``async_relogin_with_dep_wait``
会等依赖集成（llmvision）就绪后再重试，而不是「等下一轮」。
"""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryNotReady,
)

from .api import AirCloudAuthError
from .const import DOMAIN
from .coordinator import AirCloudCoordinator
from .relogin import relogin_blocker

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.DEVICE_TRACKER,
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    coordinator = AirCloudCoordinator(hass, entry)

    # token 很可能在 HA 停机期间被别处登录顶掉了（平台单会话）。
    # 先自己重登一次；不能自动重登时保持原样，交给下面/用户处理。
    if not relogin_blocker(dict(entry.data or {})):
        await coordinator.async_relogin_with_dep_wait()

    try:
        await coordinator.async_config_entry_first_refresh()
    except ConfigEntryAuthFailed:
        raise
    except ConfigEntryNotReady as err:
        blocker = relogin_blocker(dict(entry.data or {}))
        if blocker:
            # 永久性问题（没存密码 / 密码已错 / 识别模型没了）→ 明确交给用户
            raise ConfigEntryAuthFailed(
                f"登录态失效且无法自动重新登录：{blocker}"
            ) from err
        # 能自动重登但这次没成功（识别没中、模型限流等）：
        # 让 HA 自己重试 setup（内部退避），下次 setup 会再走一遍重登。
        raise ConfigEntryNotReady(
            f"暂时取不到数据，将重试；自动登录状态：{coordinator.relogin_last_result}"
        ) from err

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unloaded


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """条目变动时的处理：只有「凭据之外的内容变了」才需要重载。

    自动重登会 ``async_update_entry`` 写回新 token —— 那次写入必须由 coordinator
    自己在内存里换客户端完成，**绝不能重载**（重载会在刷新途中把 coordinator
    自己卸载掉，登录态又白刷了）。判定「条目里的凭据仍与内存客户端一致」即说明
    是内部刷新，跳过。
    """
    coordinator: AirCloudCoordinator | None = (
        hass.data.get(DOMAIN, {}).get(entry.entry_id)
    )
    if coordinator is None:
        return
    if coordinator.credentials_match_entry(entry) and coordinator.options_match_entry(entry):
        return
    await hass.config_entries.async_reload(entry.entry_id)
