"""DataUpdateCoordinator for AirCloud.

定位口径：**每个轮询周期问一次「设备现在在哪」**，把该点并入滚动轨迹缓存。
不读取平台历史点位 —— 平台的历史接口按时间升序分页，翻固定页数只会拿到
最老的那一段，反而把几天前的旧点和当前位置连成一条跨城直线。
轨迹密度即轮询频率，长度即「集成启动以来设备真正移动过的路径」。

登录态保活：平台是**单会话**，别处登录会立刻让本集成的 token 失效（code 105）。
条目里保存了手机号 + 密码 + 识别模型，因此失效后集成会自己重新登录，用户无感；
重登失败则每 RELOGIN_RETRY_S（默认 10 分钟）重试一次，只有「密码错/没保存密码」
这类永久性问题才弹给用户处理。
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    AirCloudApi,
    AirCloudAuthError,
    AirCloudError,
    AirCloudRateLimited,
    _count_turns,
    parse_local_time,
)
from .const import (
    CONF_APP_ID,
    CONF_DEVICES,
    CONF_PROJECT,
    CONF_PUBLIC_KEY,
    CONF_SALT,
    CONF_SCAN_INTERVAL,
    CONF_SID,
    CONF_TOKEN,
    DEFAULT_APP_ID,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    ENTITY_TRACK_MAX_POINTS,
    RELOGIN_FIRST_DELAY_S,
    RELOGIN_LOG_AFTER,
    RELOGIN_NOTREADY_RETRY_S,
    RELOGIN_SETUP_WAIT_S,
    RELOGIN_RETRY_S,
    TRACK_CACHE_MAX_POINTS,
    TRACK_FILTER,
    TRACK_MERGE_M,
    TRACK_SPIKE_MIN_M,
    TRACK_SPIKE_RATIO_K,
    TRACK_SPIKE_V_MPS,
)
from .relogin import (
    ReloginNotReady,
    ReloginUnavailable,
    async_relogin,
    relogin_blocker,
)
from .track import filter_track_outliers, thin_track

_LOGGER = logging.getLogger(__name__)

# 多设备时限制并发，避免触发平台频率闸门
_MAX_CONCURRENT_REQUESTS = 2

# 会被自动重登就地改写的凭据字段 —— 它们变化不算「配置变更」，不需要重载。
# app_id 也在内：登录响应里带回来，会自动补进条目。
_VOLATILE_KEYS = (CONF_TOKEN, CONF_SALT, CONF_SID, CONF_PUBLIC_KEY, CONF_APP_ID)


class AirCloudCoordinator(DataUpdateCoordinator[dict[str, dict]]):
    """轮询设备状态（每轮取当前定位，累积成轨迹；登录态自动保活）。"""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        cfg = {**entry.data, **(entry.options or {})}
        self.entry = entry
        # 建 coordinator 时的快照，供 update listener 判断是否需要重载。
        # 凭据（token/salt/sid/public_key）会被自动重登就地改写，不算「配置变更」，
        # 所以单独排除；其余字段（手机号/密码/识别模型/项目/设备）变了就必须重载。
        self._cfg_snapshot: dict = dict(entry.options or {})
        self._data_snapshot: dict = {k: v for k, v in (entry.data or {}).items()
                                     if k not in _VOLATILE_KEYS}
        self.project: str = cfg.get(CONF_PROJECT, "")
        self.devices: list[str] = list(cfg.get(CONF_DEVICES) or [])
        self._sem = asyncio.Semaphore(_MAX_CONCURRENT_REQUESTS)
        self._last_seen_ts: dict[str, float] = {}
        # 每台设备的滚动轨迹缓存：把每轮轮询到的定位点按顺序累积起来。
        # 设备静止时每轮坐标完全相同，_merge_track 会跳过这种连续重复点。
        self._tracks: dict[str, list[dict]] = {}
        # 本轮鉴权失败的设备（用于区分「被踢下线」与「单纯限流」）
        self._auth_failed: set[str] = set()

        # ---- 自动重登状态 ----
        self._relogin_lock = asyncio.Lock()
        self._next_relogin_at: float = 0.0      # monotonic 时间戳，到点才允许再试
        self._relogin_fails: int = 0
        # 记录最近一次自动重登结果，暴露给诊断用
        self.relogin_last_result: str = "尚未触发"
        # 只有在**成功**时才更新；失败走 relogin_last_fail_ts。
        # 两者必须分开：合成一个字段时，一次失败会把「上次成功时间」覆盖成失败时刻，
        # 诊断实体上就出现「last_success_ts = 失败时刻」这种自相矛盾的读数。
        self.relogin_last_ts: float | None = None
        self.relogin_last_fail_ts: float | None = None

        self.api = self._build_api(cfg, hass)
        interval = int(cfg.get(CONF_SCAN_INTERVAL) or DEFAULT_SCAN_INTERVAL)
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN} ({self.project})",
            update_interval=timedelta(seconds=max(10, interval)),
        )

    def _build_api(self, cfg: dict, hass: HomeAssistant | None = None) -> AirCloudApi:
        # hass 允许显式传入：__init__ 里 super().__init__() 之前 self.hass 还不存在
        return AirCloudApi(
            session=async_get_clientsession(hass or self.hass),
            token=cfg.get(CONF_TOKEN, ""),
            salt=cfg.get(CONF_SALT, ""),
            sid=cfg.get(CONF_SID, ""),
            public_key=cfg.get(CONF_PUBLIC_KEY, ""),
            app_id=cfg.get(CONF_APP_ID) or DEFAULT_APP_ID,
        )

    # ------------------------------------------------------------------ #
    # 凭据同步（避免自动重登写入条目时触发无谓的 reload 循环）
    # ------------------------------------------------------------------ #
    def credentials_match_entry(self, entry: ConfigEntry) -> bool:
        """条目的 token/salt/sid 是否已与内存中的客户端一致。

        ``async_update_entry`` 会触发 update listener（默认行为是 reload），
        而自动重登正是通过它把新 token 落盘 —— 若不放行，就会在刷新途中
        把 coordinator 自己卸载掉。判定一致即说明是内部刷新，跳过 reload。
        """
        api = self.api
        return (
            entry.data.get(CONF_TOKEN) == api.token
            and entry.data.get(CONF_SALT) == api.salt
            and entry.data.get(CONF_SID) == api.sid
            and entry.data.get(CONF_PUBLIC_KEY) == api.public_key
        )

    def options_match_entry(self, entry: ConfigEntry) -> bool:
        """options 与 data 的「非凭据部分」是否与建 coordinator 时一致。

        一致 → 这次 ``async_update_entry`` 只是自动重登在刷新 token，跳过 reload。
        不一致 → 用户在选项里改了手机号/密码/识别模型，必须重载才能生效。
        """
        now = {k: v for k, v in (entry.data or {}).items() if k not in _VOLATILE_KEYS}
        return (dict(entry.options or {}) == dict(self._cfg_snapshot)
                and now == dict(self._data_snapshot))

    # ------------------------------------------------------------------ #
    def _merge_track(self, client_id: str, new_points: list[dict]) -> list[dict]:
        """把本轮轮询到的定位点追加进滚动缓存。

        唯一的过滤是**跳过与缓存最后一个点坐标相同的点**：
        设备静止时（300 s 一报）连续几轮都会给出同一个坐标，
        不去重的话缓存会被这个位置塞满，看着「点数在涨但其实一步没动」。

        只跟**最后一个点**比，不做全局去重（踩过）：全局去重会让「开出去又
        回到原处」的返程点被当成重复而丢掉，轨迹就断在去程终点。
        量化到 :data:`_dedupe_key` 的精度再比，避免浮点末位差异漏判。

        这里也**不做**同位折叠与尖刺判罚 —— 那两步是为「画出来好看」服务的，
        折叠会把停留时段压成一个节点，用在缓存上就等于丢点。
        完整过滤留给展示层（见 `display_track`）。
        """
        buf = self._tracks.setdefault(client_id, [])
        for pt in new_points:
            norm = self._normalize_point(pt)
            if buf and self._dedupe_key(buf[-1]) == self._dedupe_key(norm):
                continue
            buf.append(norm)
        # 缓存按到达顺序累积（即时间顺序），无需重排；超长时裁掉最老的
        if len(buf) > TRACK_CACHE_MAX_POINTS:
            del buf[:-TRACK_CACHE_MAX_POINTS]
        return buf

    @staticmethod
    def _normalize_point(pt: dict) -> dict:
        """补一个数值 ``ts``（秒）。

        平台只给 ``time`` 字符串（北京时间字面），而过滤算法的
        「进出是否瞬移」判据依赖数值时间 —— 缺 ts 会被当成瞬移，真实掉头就被误删
        （实测：带 ts 保留 5/5 的 U 型掉头，无 ts 只剩 1）。因此入缓存前统一补上。
        """
        out = dict(pt)
        if out.get("ts") is None:
            ts = parse_local_time(str(out.get("time") or ""))
            if ts is not None:
                out["ts"] = ts
        return out

    @staticmethod
    def _dedupe_key(pt: dict) -> str:
        """坐标量化到 6 位（≈0.1 m）后做去重键。

        不能直接比浮点原值：latest_location 给的是全精度浮点字符串，
        同一位置两次返回可能末位不同，精确相等会漏判。
        """
        try:
            return f"{round(float(pt['lng']), 6)}|{round(float(pt['lat']), 6)}"
        except (KeyError, TypeError, ValueError):
            return f"{pt.get('time')}|{pt.get('lng')}|{pt.get('lat')}"

    def display_track(self, client_id: str) -> tuple[list[dict], dict]:
        """给前端用的轨迹：GPS 抖动/尖刺清理 + 几何简化。

        与缓存的区别：缓存要保真（转向统计要用未简化的点），展示只求
        「看得出路线、不卡」。返回 ``(points, stats)``。

        **注意**：展示层用 :func:`thin_track`（Douglas–Peucker 几何简化），
        不是等距抽稀 —— 等距抽稀会把拐弯抹平、折线偏离真实路线几百米，
        地图上就是「一段一段跳」（详见 `track.thin_track` 的说明）。
        """
        raw = self._tracks.get(client_id) or []
        if not TRACK_FILTER:
            return thin_track(raw, ENTITY_TRACK_MAX_POINTS), {
                "filtered": False, "raw_points": len(raw)}
        stats: dict = {}
        cleaned = filter_track_outliers(
            raw,
            spike_min=TRACK_SPIKE_MIN_M,
            ratio_k=TRACK_SPIKE_RATIO_K,
            merge_m=TRACK_MERGE_M,
            v_spike=TRACK_SPIKE_V_MPS,
            # 缓存是轮询累积来的：坐标重复 = 设备真的回到了这里，
            # 不能当补传副本删掉（否则返程整段消失）
            global_dedupe=False,
            stats=stats,
        )
        thinned = thin_track(cleaned, ENTITY_TRACK_MAX_POINTS)
        stats["raw_points"] = len(raw)
        stats["display_points"] = len(thinned)
        stats["filtered"] = True
        return thinned, stats

    async def _fetch_one(self, client_id: str) -> tuple[str, dict | None]:
        async with self._sem:
            try:
                return client_id, await self.api.async_device_status(client_id)
            except AirCloudRateLimited as err:
                _LOGGER.warning("设备 %s 触发频率限制，本轮跳过: %s", client_id, err)
                return client_id, None
            except AirCloudAuthError as err:
                # 单会话平台：别处登录会踢掉 HA。这里**不 raise**（否则 gather 会
                # 让其它设备白跑），改为打标记，由 _async_update_data 统一判定。
                _LOGGER.warning("设备 %s 登录态失效：%s", client_id, err)
                self._auth_failed.add(client_id)
                return client_id, None
            except AirCloudError as err:
                _LOGGER.debug("设备 %s 取数失败: %s", client_id, err)
                return client_id, None

    # ------------------------------------------------------------------ #
    # 自动重新登录
    # ------------------------------------------------------------------ #
    def _note_relogin_fail(self, detail: str) -> None:
        self._relogin_fails += 1
        self.relogin_last_result = detail
        # 只记失败时间，**不动** relogin_last_ts（那是「上次成功」）
        self.relogin_last_fail_ts = time.time()
        if self._relogin_fails <= RELOGIN_LOG_AFTER:
            _LOGGER.warning("自动重新登录失败（第 %s 次）：%s", self._relogin_fails, detail)
        else:
            # 持续失败时不再刷屏，但仍保留状态供诊断
            _LOGGER.info("自动重新登录仍失败（第 %s 次）：%s", self._relogin_fails, detail)

    async def _async_relogin_and_apply(self) -> bool:
        """跑一次自动重登并把新凭据落盘 + 换到内存客户端。

        返回 True 表示凭据已刷新（调用方应重取数据）。
        失败时抛 :class:`ReloginUnavailable`（永久性，需用户介入）
        或其它 :class:`AirCloudLoginError`（临时性，稍后重试）。
        """
        creds = await async_relogin(self.hass, self.entry)
        merged = {
            **self.entry.data,
            CONF_TOKEN: creds["token"],
            CONF_SALT: creds["salt"],
            CONF_SID: creds["sid"],
            CONF_PUBLIC_KEY: creds.get("public_key", ""),
            CONF_APP_ID: creds.get("app_id") or DEFAULT_APP_ID,
        }
        # 先换内存客户端，再落盘 —— credentials_match_entry() 依赖这个顺序
        self.api = self._build_api(merged)
        self.hass.config_entries.async_update_entry(self.entry, data=merged)
        self._auth_failed.clear()
        self._relogin_fails = 0
        self._next_relogin_at = 0.0
        self.relogin_last_result = "成功"
        self.relogin_last_ts = time.time()
        return True

    async def _handle_auth_failure(self) -> bool:
        """所有设备都鉴权失败时调用。返回 True 表示已重登成功、可继续取数。"""
        blocker = relogin_blocker(dict(self.entry.data or {}))
        if blocker:
            # 没保存密码/没选模型 → 重试多少次都没用，直接交回用户
            raise ConfigEntryAuthFailed(
                f"登录态失效且无法自动重新登录：{blocker}"
                "（平台为单会话：别处登录会踢掉本集成）"
            )

        now = time.monotonic()
        if now < self._next_relogin_at:
            left = int(self._next_relogin_at - now)
            raise UpdateFailed(
                f"登录态失效，{left} 秒后重试自动登录"
                f"（上次结果：{self.relogin_last_result}）"
            )

        async with self._relogin_lock:
            # 拿锁后重新判断：并发的刷新可能已经修好了
            if self._auth_failed and not self._auth_failed.issuperset(self.devices):
                return True
            if time.monotonic() < self._next_relogin_at:
                raise UpdateFailed("登录态失效，自动登录冷却中")

            try:
                await self._async_relogin_and_apply()
            except ReloginNotReady as err:
                # 依赖未就绪（如 llmvision 还没注册服务）→ 短间隔重试，不计失败
                _LOGGER.debug("自动重新登录暂不可用：%s（%s 秒后重试）",
                              err, RELOGIN_NOTREADY_RETRY_S)
                self._next_relogin_at = time.monotonic() + RELOGIN_NOTREADY_RETRY_S
                self.relogin_last_result = f"等待依赖就绪（{err}）"
                raise UpdateFailed(f"自动重新登录暂不可用：{err}") from err
            except ReloginUnavailable as err:
                self._note_relogin_fail(str(err))
                # 永久性问题（密码已错/模型没了）→ 弹给用户，不再自动重试
                raise ConfigEntryAuthFailed(
                    f"自动重新登录失败：{err}，请重新配置集成"
                ) from err
            except Exception as err:  # noqa: BLE001  (网络/识别/限流等临时问题)
                self._note_relogin_fail(str(err))
                self._next_relogin_at = time.monotonic() + RELOGIN_RETRY_S
                raise UpdateFailed(
                    f"自动重新登录失败，{RELOGIN_RETRY_S // 60} 分钟后重试：{err}"
                ) from err

        _LOGGER.info("自动重新登录成功，已刷新登录态")
        return True

    async def _async_update_data(self) -> dict[str, dict]:
        if not self.devices:
            return {}

        # 已知全部设备鉴权失败且还在重登冷却期 → 不浪费 API 调用，直接等下一次
        if (self._auth_failed and len(self._auth_failed) >= len(self.devices)
                and time.monotonic() < self._next_relogin_at):
            left = max(1, int(self._next_relogin_at - time.monotonic()))
            raise UpdateFailed(
                f"登录态失效，{left} 秒后重试自动登录"
                f"（上次结果：{self.relogin_last_result}）"
            )

        relogin_done = False
        for _round in range(2):
            results = await asyncio.gather(*(self._fetch_one(cid) for cid in self.devices))
            # 关键：只有**真的取到数据**才算成功。不能用「沿用上一轮」的结果
            # 当成绩 —— 否则全部鉴权失败时，陈旧数据会把失败永久掩盖成正常。
            if any(st is not None for _cid, st in results):
                return self._collect(results)

            all_auth_failed = bool(self._auth_failed) and len(self._auth_failed) >= len(self.devices)
            if all_auth_failed and not relogin_done:
                relogin_done = True
                # 抛 ConfigEntryAuthFailed（永久问题）或 UpdateFailed（稍后重试）
                await self._handle_auth_failure()
                continue          # 到这里说明重登成功，重取一轮
            # 非鉴权原因（限流/抖动）：沿用上一轮，避免实体闪断
            if self.data:
                return dict(self.data)

        if relogin_done:
            raise UpdateFailed("自动重新登录后仍未取到数据（新会话可能又被别处登录占用）")
        raise UpdateFailed("所有设备均未取到数据（可能触发平台频率限制）")

    def _collect(self, results: list[tuple[str, dict | None]]) -> dict[str, dict]:
        """把本轮结果并入 data（鉴权失败的设备不沿用旧值，避免掩盖失效）。"""
        if not results:
            return {}
        data: dict[str, dict] = {}
        for client_id, status in results:
            if status is None:
                # 鉴权失败说明凭据废了，沿用旧值只会掩盖问题；限流/抖动才沿用
                if self._auth_failed or not (self.data and client_id in self.data):
                    continue
                data[client_id] = self.data[client_id]
                continue
            self._auth_failed.discard(client_id)
            # 本轮定位点并入滚动缓存（与上一点坐标相同则跳过）
            new_points = status.get("points") or []
            before = len(self._tracks.get(client_id) or [])
            merged = self._merge_track(client_id, new_points)
            status["point_count"] = len(merged)
            # new_points = **真正并入缓存的点数**（设备静止时连续几轮坐标相同会被
            # 跳过，此时为 0）；fetched_points = 本轮从平台取回的定位点数。
            # 两者分开才能一眼看出「设备没动」与「接口没给点」。
            status["new_points"] = len(merged) - before
            status["fetched_points"] = len(new_points)
            status["turns"] = _count_turns(merged)
            status["turns_recent"] = _count_turns(merged[-40:])
            # 展示用轨迹：几何简化到上限内（形状保真，点数可控）。
            # 属性是 JSON 进每次 state_changed，点太多会让地图卡片卡顿；
            # 转向统计用的是缓存里的原始点，不受简化影响。
            display_pts, fstats = self.display_track(client_id)
            status["points"] = display_pts
            status["filter_stats"] = fstats
            # 本轮没取到定位时，沿用上一轮的位置信息，避免实体闪断
            if not status.get("found") and self.data and client_id in self.data:
                prev = self.data[client_id]
                for key in ("wgs_lng", "wgs_lat", "gcj_lng", "gcj_lat",
                            "address", "time", "ts", "found"):
                    if not status.get(key) and prev.get(key):
                        status[key] = prev[key]
            if status.get("ts"):
                self._last_seen_ts[client_id] = status["ts"]
            data[client_id] = status
        return data

    @property
    def last_seen_ts(self) -> dict[str, float]:
        return self._last_seen_ts

    async def async_relogin_with_dep_wait(self) -> bool:
        """首次 setup 时把重登做掉，必要时等依赖集成就绪。

        为什么必须在这里做完：HA 的 ``async_config_entry_first_refresh`` 一旦失败
        **不会**排下一轮定时器，集成会永久挂着不动。所以「等下一轮再重登」不成立。

        返回 True 表示已拿到新凭据。
        """
        deadline = time.monotonic() + RELOGIN_SETUP_WAIT_S
        while True:
            try:
                await self._async_relogin_and_apply()
                return True
            except ReloginNotReady as err:
                self.relogin_last_result = f"等待依赖就绪（{err}）"
                if time.monotonic() >= deadline:
                    _LOGGER.warning("等待依赖就绪超时：%s", err)
                    return False
                # 典型场景：HA 启动时本集成比 llmvision 先 setup
                _LOGGER.debug("依赖尚未就绪，%s 秒后重试：%s", RELOGIN_NOTREADY_RETRY_S, err)
                await asyncio.sleep(RELOGIN_NOTREADY_RETRY_S)
            except ReloginUnavailable as err:
                _LOGGER.warning("无法自动重新登录：%s（需要用户重新配置）", err)
                return False
            except Exception as err:  # noqa: BLE001
                self._note_relogin_fail(str(err))
                self._next_relogin_at = time.monotonic() + RELOGIN_RETRY_S
                return False

    async def async_check_credentials(self) -> tuple[bool, str]:
        """凭据自检：接口不报错≠拿到你的数据（匿名调用会返回 code 0 + 空列表）。"""
        try:
            projects = await self.api.async_list_projects()
        except AirCloudAuthError as err:
            return False, f"登录态失效：{err}"
        except AirCloudError as err:
            return False, f"接口异常：{err}"
        if not projects:
            return False, "账号下没有任何项目（凭据可能无效）"
        if self.project and not any(p.get("project_key") == self.project for p in projects):
            return False, "所选项目不在该账号的项目列表中"
        try:
            devices = await self.api.async_list_devices(self.project, 1, 100)
        except AirCloudError as err:
            return False, f"读取设备列表失败：{err}"
        total = int(devices.get("total") or 0)
        if total == 0:
            return False, "该项目下没有设备"
        return True, f"项目 {self.project} 共 {total} 台设备"
