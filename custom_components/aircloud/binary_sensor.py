"""binary_sensor：在线状态 / 低电量 / 定位状态。

名称走 ``translation_key``，见 translations/<lang>.json 的 entity 段。
"""
from __future__ import annotations

import logging
from datetime import datetime

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DEVICE_MANUFACTURER,
    DEVICE_MODEL,
    DEVICE_NAME,
    DOMAIN,
    OFFLINE_AFTER_S,
    VBAT_LOW_MV,
)
from .coordinator import AirCloudCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry,
                           async_add_entities: AddEntitiesCallback) -> None:
    coordinator: AirCloudCoordinator = hass.data[DOMAIN][entry.entry_id]
    ents: list[BinarySensorEntity] = []
    for client_id in coordinator.devices:
        ents.append(AirCloudOnline(coordinator, client_id))
        ents.append(AirCloudLowBattery(coordinator, client_id))
        ents.append(AirCloudGpsFixed(coordinator, client_id))
    async_add_entities(ents)


class _Base(CoordinatorEntity[AirCloudCoordinator], BinarySensorEntity):
    _attr_has_entity_name = True

    def __init__(self, coordinator: AirCloudCoordinator, client_id: str, key: str) -> None:
        super().__init__(coordinator)
        self._client_id = client_id
        self._attr_unique_id = f"{DOMAIN}_{client_id}_{key}"
        self._attr_translation_key = key

    @property
    def _status(self) -> dict:
        return (self.coordinator.data or {}).get(self._client_id, {})

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self._client_id)},
            "name": f"{DEVICE_NAME} {self._client_id}",
            "manufacturer": DEVICE_MANUFACTURER,
            "model": DEVICE_MODEL,
        }


class AirCloudOnline(_Base):
    """离线判定：设备移动 5s 一报、静止 300s 一报，超时按 OFFLINE_AFTER_S 判定。"""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_icon = "mdi:access-point-network"

    def __init__(self, coordinator, client_id: str) -> None:
        super().__init__(coordinator, client_id, "online")

    @property
    def is_on(self) -> bool:
        ts = self._status.get("ts")
        if not ts:
            return False
        return (datetime.now().timestamp() - ts) < OFFLINE_AFTER_S

    @property
    def extra_state_attributes(self) -> dict:
        return {"report_interval_hint": "移动约5秒/静止约300秒",
                "offline_threshold_s": OFFLINE_AFTER_S}


class AirCloudLowBattery(_Base):
    _attr_device_class = BinarySensorDeviceClass.BATTERY
    _attr_icon = "mdi:battery-alert"

    def __init__(self, coordinator, client_id: str) -> None:
        super().__init__(coordinator, client_id, "low_battery")

    @property
    def is_on(self) -> bool | None:
        mv = self._status.get("vbat")
        if mv is None:
            return None
        return float(mv) < VBAT_LOW_MV


class AirCloudGpsFixed(_Base):
    _attr_icon = "mdi:crosshairs-gps"

    def __init__(self, coordinator, client_id: str) -> None:
        super().__init__(coordinator, client_id, "gps_fix")

    @property
    def is_on(self) -> bool | None:
        fix = self._status.get("fix")
        if fix is None:
            return None
        return int(fix) == 2
