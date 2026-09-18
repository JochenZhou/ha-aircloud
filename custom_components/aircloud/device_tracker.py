"""device_tracker：设备定位（坐标转成 WGS84，HA 地图直接可用）。"""
from __future__ import annotations

import logging

from homeassistant.components.device_tracker import SourceType, TrackerEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DEVICE_MANUFACTURER,
    DEVICE_MODEL,
    DEVICE_NAME,
    DOMAIN,
)
from .coordinator import AirCloudCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry,
                           async_add_entities: AddEntitiesCallback) -> None:
    coordinator: AirCloudCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        AirCloudTracker(coordinator, client_id) for client_id in coordinator.devices
    )


class AirCloudTracker(CoordinatorEntity[AirCloudCoordinator], TrackerEntity):
    """一台合宙 IoT 设备的位置。"""

    _attr_has_entity_name = True
    _attr_name = None
    _attr_source_type = SourceType.GPS

    def __init__(self, coordinator: AirCloudCoordinator, client_id: str) -> None:
        super().__init__(coordinator)
        self._client_id = client_id
        self._attr_unique_id = f"{DOMAIN}_{client_id}_tracker"

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

    @property
    def available(self) -> bool:
        return bool(self._status.get("found"))

    @property
    def latitude(self) -> float | None:
        return self._status.get("wgs_lat")

    @property
    def longitude(self) -> float | None:
        return self._status.get("wgs_lng")

    @property
    def location_accuracy(self) -> int:
        # 平台不提供定位精度，按 GPS 常态给一个保守值
        return 20

    @property
    def extra_state_attributes(self) -> dict:
        st = self._status
        attrs: dict = {
            "imei": self._client_id,
            "address": st.get("address"),
            "report_time": st.get("time"),
            "fix": st.get("fix"),
            "coord_source": "platform_wgs84" if st.get("wgs_lng") is not None else None,
        }
        if st.get("ts"):
            import datetime
            attrs["last_seen"] = datetime.datetime.fromtimestamp(
                st["ts"], tz=datetime.timezone.utc).isoformat()
        return {k: v for k, v in attrs.items() if v is not None}
