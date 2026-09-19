"""sensor：电量/电压/信号/卫星/地址/上报时间/轨迹/转向。

实体名称走 ``translation_key`` + ``strings.json`` 的 ``entity`` 段，
这样中英文都能跟着 HA 界面语言切换（旧实现把中文写死在 ``_attr_name`` 里，
英文环境下也是中文）。
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    UnitOfElectricPotential,
    UnitOfSpeed,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DEVICE_MANUFACTURER,
    DEVICE_MODEL,
    DEVICE_NAME,
    DOMAIN,
    RELOGIN_RETRY_S,
    TRACK_COORD_DIGITS,
    TURN_MIN_SEGMENT_M,
    TURN_THRESHOLD_DEG,
    VBAT_LOW_MV,
    VBAT_MID_MV,
)
from .coordinator import AirCloudCoordinator


def _retry_text() -> str:
    """重试间隔的可读文案（小于 1 分钟时按秒显示，避免出现「每 0 分钟」）。"""
    if RELOGIN_RETRY_S < 60:
        return f"{RELOGIN_RETRY_S} 秒"
    if RELOGIN_RETRY_S % 60 == 0:
        return f"{RELOGIN_RETRY_S // 60} 分钟"
    return f"{RELOGIN_RETRY_S // 60} 分 {RELOGIN_RETRY_S % 60} 秒"


@dataclass(frozen=True, kw_only=True)
class AirCloudSensor(SensorEntityDescription):
    """一个状态传感器（``key`` 同时作为 translation_key）。"""


SENSORS: tuple[AirCloudSensor, ...] = (
    AirCloudSensor(
        key="battery",
        native_unit_of_measurement=PERCENTAGE,
        device_class=SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:battery",
    ),
    AirCloudSensor(
        key="vbat",
        native_unit_of_measurement=UnitOfElectricPotential.MILLIVOLT,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:flash",
    ),
    AirCloudSensor(
        key="signal",
        native_unit_of_measurement="dBm",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:signal",
    ),
    AirCloudSensor(
        key="speed",
        native_unit_of_measurement=UnitOfSpeed.KILOMETERS_PER_HOUR,
        device_class=SensorDeviceClass.SPEED,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:speedometer",
    ),
    AirCloudSensor(
        key="sat",
        native_unit_of_measurement="颗",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:satellite-variant",
    ),
)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry,
                           async_add_entities: AddEntitiesCallback) -> None:
    coordinator: AirCloudCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[SensorEntity] = []
    for client_id in coordinator.devices:
        entities.extend(AirCloudStatusSensor(coordinator, client_id, desc) for desc in SENSORS)
        entities.append(AirCloudAddressSensor(coordinator, client_id))
        entities.append(AirCloudLastSeenSensor(coordinator, client_id))
        entities.append(AirCloudTrackSensor(coordinator, client_id))
        entities.append(AirCloudTurnSensor(coordinator, client_id))
    entities.append(AirCloudReloginSensor(coordinator))
    async_add_entities(entities)


class _Base(CoordinatorEntity[AirCloudCoordinator], SensorEntity):
    _attr_has_entity_name = True

    def __init__(self, coordinator: AirCloudCoordinator, client_id: str, key: str) -> None:
        super().__init__(coordinator)
        self._client_id = client_id
        self._attr_unique_id = f"{DOMAIN}_{client_id}_{key}"
        # 名称交给 translations/<lang>.json 的 entity 段
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


class AirCloudStatusSensor(_Base):
    entity_description: AirCloudSensor

    def __init__(self, coordinator, client_id: str, description: AirCloudSensor) -> None:
        super().__init__(coordinator, client_id, description.key)
        self.entity_description = description

    @property
    def native_value(self):
        return self._status.get(self.entity_description.key)

    @property
    def available(self) -> bool:
        return self.native_value is not None

    @property
    def extra_state_attributes(self) -> dict | None:
        if self.entity_description.key == "vbat":
            mv = self.native_value
            if isinstance(mv, (int, float)):
                if mv < VBAT_LOW_MV:
                    level = "低"
                elif mv < VBAT_MID_MV:
                    level = "中"
                else:
                    level = "充足"
                return {"level": level}
        return None


class AirCloudAddressSensor(_Base):
    """地址文本（定位地址，诊断类信息）。"""

    _attr_icon = "mdi:map-marker"

    def __init__(self, coordinator, client_id: str) -> None:
        super().__init__(coordinator, client_id, "address")

    @property
    def native_value(self):
        return self._status.get("address") or None


class AirCloudTrackSensor(_Base):
    """累积轨迹的点数（集成启动以来轮询到的定位点）。"""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:map-marker-path"

    def __init__(self, coordinator, client_id: str) -> None:
        super().__init__(coordinator, client_id, "track_points")

    @property
    def native_value(self):
        return self._status.get("point_count")

    @property
    def extra_state_attributes(self) -> dict:
        st = self._status
        attrs: dict = {
            "new_points_this_round": st.get("new_points"),
            "fetched_points_this_round": st.get("fetched_points"),
        }
        pts = st.get("points") or []
        if pts:
            # 轨迹以 [lat, lng] 数组暴露，便于卡片/自动化直接画线。
            # 坐标量化到 6 位（≈0.11 m，远小于 GPS 精度）：不量化的话全精度浮点
            # 会让属性体积翻倍（801 点 18 KB → 4 KB），白白拖慢地图卡片。
            attrs["track"] = [
                [round(float(p.get("lat")), TRACK_COORD_DIGITS),
                 round(float(p.get("lng")), TRACK_COORD_DIGITS)]
                for p in pts
                if p.get("lat") is not None and p.get("lng") is not None
            ]
            attrs["track_time_first"] = pts[0].get("time")
            attrs["track_time_last"] = pts[-1].get("time")
        # 过滤/抽稀口径：track 属性里的点数与原始取回点数的差别一目了然
        fs = st.get("filter_stats") or {}
        if fs:
            attrs["track_display_points"] = fs.get("display_points")
            if fs.get("filtered"):
                attrs["track_raw_points"] = fs.get("raw_points")
                attrs["track_dupes_removed"] = fs.get("dupes")
                attrs["track_outliers_removed"] = fs.get("dropped")
        if attrs.get("track"):
            attrs["track_note"] = (
                "轨迹由每次轮询的定位点累积而成，不含集成启动前的历史位置；"
                "重启 HA 后从零开始累积"
            )
        return {k: v for k, v in attrs.items() if v not in (None, [])}


class AirCloudTurnSensor(_Base):
    """累积轨迹内的转向次数（方位角变化 > 阈值）。"""

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:rotate-right"

    def __init__(self, coordinator, client_id: str) -> None:
        super().__init__(coordinator, client_id, "turns")

    @property
    def native_value(self):
        return self._status.get("turns")

    @property
    def extra_state_attributes(self) -> dict:
        return {
            "turn_threshold_deg": TURN_THRESHOLD_DEG,
            "min_segment_m": TURN_MIN_SEGMENT_M,
            "recent_turns_last40pts": self._status.get("turns_recent"),
            "note": "已按最小分段位移过滤静止抖动",
        }


class AirCloudReloginSensor(_Base):
    """自动重新登录状态（账号级，不挂在某台设备上）。

    平台是单会话，token 会被别处登录踢掉；集成拿到新会话后会自动重登。
    这个实体用来判断「是否已恢复」以及看失败原因。
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:login-variant"

    def __init__(self, coordinator) -> None:
        # 不继承 _Base：它不是某台设备的状态
        super().__init__(coordinator, "_account", "auto_relogin")
        # 不带设备前缀，显式加命名空间避免与其它集成撞名（如 xiaomi 的 auto_sign_in）
        self.entity_id = f"sensor.{DOMAIN}_auto_relogin"

    @property
    def device_info(self) -> None:
        return None       # 账号级诊断，不挂设备

    @property
    def native_value(self):
        return self.coordinator.relogin_last_result

    @property
    def available(self) -> bool:
        return True

    @property
    def extra_state_attributes(self) -> dict:
        co = self.coordinator
        return {
            "last_success_ts": co.relogin_last_ts,
            "failure_count": co._relogin_fails,
            "retry_interval_s": RELOGIN_RETRY_S,
            "next_retry_in_s": (
                max(0, int(co._next_relogin_at - time.monotonic()))
                if co._next_relogin_at else 0
            ),
            "note": (
                "平台为单会话：别处登录会让本集成的 token 失效。"
                "条目里已保存手机号+密码+识别模型，失效后会自动重新登录，"
                f"失败则每 {_retry_text()}重试一次。"
            ),
        }


class AirCloudLastSeenSensor(_Base):
    """最后上报时间（平台时间字面即北京时间，直接展示）。"""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:clock-outline"

    def __init__(self, coordinator, client_id: str) -> None:
        super().__init__(coordinator, client_id, "last_seen")

    @property
    def native_value(self):
        ts = self._status.get("ts")
        if not ts:
            return None
        return datetime.fromtimestamp(ts, tz=datetime.now().astimezone().tzinfo)
