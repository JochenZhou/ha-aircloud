"""AirCloud API client (async, based on aiohttp).

对接 /iot/open_api/*：一律 POST + Content-Type: application/json，三鉴权头
authorization(token)/salt/sid（禁止 Bearer 前缀）。common/* 另需 X-Key-Open-Api。

取数口径：**按轮询频率问「此刻在哪」**（latest_location），不读取平台历史
（location_history 按时间升序分页，翻几页只能拿到最老的那一段，反而把
几天前的旧点和当前位置接成一条跨城直线）。轨迹由 coordinator 累积轮询点。
"""
from __future__ import annotations

import asyncio
import base64
import logging
import math
import os
import time

import aiohttp

from .const import (
    API_BASE,
    AUTH_FAIL_CODES,
    RATE_LIMIT_CODE,
    STATUS_TAGS,
    TURN_MIN_SEGMENT_M,
    TURN_THRESHOLD_DEG,
    TAG_GNSS_BIN,
    TAG_LAT,
    TAG_LNG,
    TAG_SAT,
    TAG_SIGNAL,
    TAG_VBAT,
    VBAT_EMPTY_MV,
    VBAT_FULL_MV,
)

_LOGGER = logging.getLogger(__name__)

class AirCloudError(Exception):
    """接口错误基类。

    基类放最前面，让下面两个子类能继承它 —— 否则调用方的
    ``except AirCloudError`` 会漏掉鉴权/限流错误（实测过：
    配置流程里 token 失效会直接 500 崩溃，而不是走兜底分支）。
    """


class AirCloudAuthError(AirCloudError):
    """登录态失效（102/103/105）。"""


class AirCloudRateLimited(AirCloudError):
    """平台频率闸门（429）。"""


# --------------------------------------------------------------------------- #
# RSA / PKCS#1 v1.5 —— 仅用标准库，不引入 pycryptodome
# --------------------------------------------------------------------------- #
def _der_read(buf: bytes, i: int) -> tuple[int, bytes, int]:
    tag = buf[i]
    i += 1
    ln = buf[i]
    i += 1
    if ln & 0x80:
        n = ln & 0x7F
        ln = int.from_bytes(buf[i:i + n], "big")
        i += n
    return tag, buf[i:i + ln], i + ln


def parse_pkcs1_public_key(b64_der: str) -> tuple[int, int]:
    """解析 DER SubjectPublicKeyInfo → (n, e)。

    JSEncrypt v3 只接受 DER 格式；旧式 PEM（带 BEGIN/END 头）先剥掉头尾。
    """
    s = (b64_der or "").strip()
    if "-----BEGIN" in s:
        s = "".join(line for line in s.splitlines() if "-----" not in line)
    der = base64.b64decode(s)
    tag, body, _ = _der_read(der, 0)
    if tag != 0x30:
        raise AirCloudError("公钥不是 DER SEQUENCE")
    _tag, _alg, i = _der_read(body, 0)
    _tag, bits, _ = _der_read(body, i)
    if bits[0] != 0:
        raise AirCloudError("BIT STRING 未对齐")
    _tag, key_body, _ = _der_read(bits, 1)  # 跳过 unused-bits 字节
    _tag, n_der, j = _der_read(key_body, 0)
    _tag, e_der, _ = _der_read(key_body, j)
    return int.from_bytes(n_der.lstrip(b"\x00"), "big"), int.from_bytes(e_der.lstrip(b"\x00"), "big")


def rsa_pkcs1_encrypt_b64(plain: str, public_key: str) -> str:
    """RSA/ECB/PKCS1Padding → Base64（对应 JSEncrypt.encrypt）。"""
    n, e = parse_pkcs1_public_key(public_key)
    msg = plain.encode("utf-8")
    k = (n.bit_length() + 7) // 8
    if len(msg) > k - 11:
        raise AirCloudError("X-Key-Open-Api 明文过长")
    pad = bytearray()
    while len(pad) < k - 3 - len(msg):
        b = os.urandom(1)[0]
        if b:
            pad.append(b)
    em = b"\x00\x02" + bytes(pad) + b"\x00" + msg
    return base64.b64encode(pow(int.from_bytes(em, "big"), e, n).to_bytes(k, "big")).decode()


# --------------------------------------------------------------------------- #
# GCJ02 → WGS84（HA 地图为 WGS84；漏做逆变换会偏移 260~1200 m）
# --------------------------------------------------------------------------- #
_PI = math.pi
_A = 6378245.0
_EE = 0.00669342162296594323


def _transform_lat(x: float, y: float) -> float:
    ret = -100.0 + 2.0 * x + 3.0 * y + 0.2 * y * y + 0.1 * x * y + 0.2 * math.sqrt(abs(x))
    ret += (20.0 * math.sin(6.0 * x * _PI) + 20.0 * math.sin(2.0 * x * _PI)) * 2.0 / 3.0
    ret += (20.0 * math.sin(y * _PI) + 40.0 * math.sin(y / 3.0 * _PI)) * 2.0 / 3.0
    ret += (160.0 * math.sin(y / 12.0 * _PI) + 320.0 * math.sin(y * _PI / 30.0)) * 2.0 / 3.0
    return ret


def _transform_lng(x: float, y: float) -> float:
    ret = 300.0 + x + 2.0 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * math.sqrt(abs(x))
    ret += (20.0 * math.sin(6.0 * x * _PI) + 20.0 * math.sin(2.0 * x * _PI)) * 2.0 / 3.0
    ret += (20.0 * math.sin(x * _PI) + 40.0 * math.sin(x / 3.0 * _PI)) * 2.0 / 3.0
    ret += (150.0 * math.sin(x / 12.0 * _PI) + 300.0 * math.sin(x / 30.0 * _PI)) * 2.0 / 3.0
    return ret


def _delta(lng: float, lat: float) -> tuple[float, float]:
    d_lat = _transform_lat(lng - 105.0, lat - 35.0)
    d_lng = _transform_lng(lng - 105.0, lat - 35.0)
    rad_lat = lat / 180.0 * _PI
    magic = 1 - _EE * math.sin(rad_lat) ** 2
    sqrt_magic = math.sqrt(magic)
    d_lat = (d_lat * 180.0) / ((_A * (1 - _EE)) / (magic * sqrt_magic) * _PI)
    d_lng = (d_lng * 180.0) / (_A / sqrt_magic * math.cos(rad_lat) * _PI)
    return d_lng, d_lat


def in_china(lng: float, lat: float) -> bool:
    return 3.0 <= lat <= 53.55 and 73.66 <= lng <= 135.05


def gcj02_to_wgs84(lng: float, lat: float, iterations: int = 3) -> tuple[float, float]:
    """逆变换：3 次迭代残差 < 0.1 mm（1 次会差 3.8 m）。"""
    if not in_china(lng, lat):
        return lng, lat
    cur_lng, cur_lat = lng, lat
    for _ in range(iterations):
        d_lng, d_lat = _delta(cur_lng, cur_lat)
        cur_lng, cur_lat = lng - d_lng, lat - d_lat
    return cur_lng, cur_lat


def decode_gnss_5x16(hex_str: str, ref_lng: float, ref_lat: float) -> list[dict]:
    """1294 GNSS BINARY 解包：10 字节/样本，5×int16 大端差分。

    经度差×1e-7、纬度差×1e-7、速度×10 m/s、航向×10°、海拔 m。
    """
    clean = "".join(ch for ch in str(hex_str or "") if ch in "0123456789abcdefABCDEF")
    if len(clean) < 20:
        return []
    data = bytes.fromhex(clean)

    def i16(off: int) -> int:
        val = (data[off] << 8) | data[off + 1]
        return val - 0x10000 if val & 0x8000 else val

    out: list[dict] = []
    lng, lat = float(ref_lng), float(ref_lat)
    for off in range(0, len(data) - 9, 10):
        lng += i16(off) * 1e-7
        lat += i16(off + 2) * 1e-7
        out.append({
            "lng": lng,
            "lat": lat,
            "speed": i16(off + 4) / 10,
            "course": i16(off + 6) / 10,
            "altitude": i16(off + 8),
        })
    return out


def haversine_m(p1: dict, p2: dict) -> float | None:
    """两点距离（米）。"""
    try:
        lat1 = math.radians(float(p1["lat"]))
        lat2 = math.radians(float(p2["lat"]))
        d_lat = lat2 - lat1
        d_lng = math.radians(float(p2["lng"]) - float(p1["lng"]))
    except (KeyError, TypeError, ValueError):
        return None
    a = math.sin(d_lat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(d_lng / 2) ** 2
    return 2 * 6371000.0 * math.asin(min(1.0, math.sqrt(a)))


def bearing(p1: dict, p2: dict) -> float | None:
    """两点的方位角（度）。"""
    try:
        lat1 = math.radians(float(p1["lat"]))
        lat2 = math.radians(float(p2["lat"]))
        d_lng = math.radians(float(p2["lng"]) - float(p1["lng"]))
    except (KeyError, TypeError, ValueError):
        return None
    y = math.sin(d_lng) * math.cos(lat2)
    x = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(d_lng)
    if x == 0 and y == 0:
        return None
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def _count_turns(points: list[dict], threshold: float = TURN_THRESHOLD_DEG,
                 min_segment_m: float = TURN_MIN_SEGMENT_M) -> int:
    """统计一段轨迹里的真实转向次数。

    拐弯/掉头在坐标序列上表现为方位角突变，按原始 5s 级点位算才不会漏；
    但设备静止时的 GPS 抖动会让方位角乱跳，必须先按「分段位移」过滤掉抖动，
    否则静止一晚能算出上百次假转弯。
    """
    if len(points) < 3:
        return 0
    turns = 0
    for a, b, c in zip(points, points[1:], points[2:]):
        d1 = haversine_m(a, b)
        d2 = haversine_m(b, c)
        if d1 is None or d2 is None:
            continue
        if d1 < min_segment_m or d2 < min_segment_m:
            continue                      # 抖动或停留，不算转向
        b1 = bearing(a, b)
        b2 = bearing(b, c)
        if b1 is None or b2 is None:
            continue
        delta = abs((b2 - b1 + 540) % 360 - 180)
        if delta > threshold:
            turns += 1
    return turns


def vbat_to_percent(mv: float) -> int | None:
    if not mv or mv <= 0:
        return None
    pct = (mv - VBAT_EMPTY_MV) / (VBAT_FULL_MV - VBAT_EMPTY_MV) * 100
    return max(0, min(100, round(pct)))


def parse_local_time(value: str) -> float | None:
    """平台时间字面即北京时间（UTC+8），不做任何偏移换算，直接按本地钟面解析。"""
    if not value:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return time.mktime(time.strptime(value, fmt))
        except ValueError:
            continue
    return None


# --------------------------------------------------------------------------- #
# 客户端
# --------------------------------------------------------------------------- #
class AirCloudApi:
    """AirCloud 只读客户端。"""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        token: str,
        salt: str,
        sid: str,
        public_key: str = "",
        app_id: str = "move",
        timeout: int = 20,
    ) -> None:
        self._session = session
        self._token = token
        self._salt = salt
        self._sid = sid
        self._public_key = public_key
        self._app_id = app_id or "move"
        self._timeout = aiohttp.ClientTimeout(total=timeout)

    # ---- 只读属性（自动重登后由 coordinator 重建客户端，故需要可读回）----
    @property
    def token(self) -> str:
        return self._token

    @property
    def salt(self) -> str:
        return self._salt

    @property
    def sid(self) -> str:
        return self._sid

    @property
    def public_key(self) -> str:
        return self._public_key

    @property
    def app_id(self) -> str:
        return self._app_id

    # ---- 底层 ----
    def _headers(self, signed: bool = False) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._token and self._salt and self._sid:
            headers["authorization"] = self._token   # 禁止 Bearer 前缀
            headers["salt"] = self._salt
            headers["sid"] = self._sid
        if signed:
            headers["X-Key-Open-Api"] = rsa_pkcs1_encrypt_b64(
                f"{int(time.time() * 1000)},{self._app_id}", self._public_key
            )
        return headers

    async def _post(self, endpoint: str, body: dict | None = None, signed: bool = False) -> dict:
        url = API_BASE + endpoint
        try:
            async with self._session.post(
                url, json=body or {}, headers=self._headers(signed), timeout=self._timeout
            ) as resp:
                resp.raise_for_status()
                payload = await resp.json(content_type=None)
        except aiohttp.ClientError as err:
            raise AirCloudError(f"网络异常: {err}") from err
        except asyncio.TimeoutError as err:
            raise AirCloudError("请求超时") from err

        code = payload.get("code")
        value = payload.get("value")
        try:
            code_int = int(code)
        except (TypeError, ValueError):
            code_int = -1
        # 平台在 HTTP 200 里用 code 表达失败，且限流信息有时落在 value 字符串里
        blob = value if isinstance(value, str) else ""
        if code_int == RATE_LIMIT_CODE or "查询频率" in blob or "请求过于频繁" in blob:
            raise AirCloudRateLimited(blob or "触发平台查询频率限制")
        if code_int in AUTH_FAIL_CODES:
            raise AirCloudAuthError(blob or "登录状态已失效")
        if code_int != 0:
            raise AirCloudError(f"接口 {endpoint} 返回 code={code_int}: {str(value)[:120]}")
        return {"code": code_int, "value": value}

    async def _post_retry_rate_limit(self, endpoint: str, body: dict | None = None,
                                     attempts: int = 3) -> dict:
        """429 是平台频率闸门而非硬失败，退避后重试即可拿到数据。"""
        delay = 2.0
        last: Exception | None = None
        for i in range(attempts):
            try:
                return await self._post(endpoint, body)
            except AirCloudRateLimited as err:
                last = err
                if i == attempts - 1:
                    break
                await asyncio.sleep(delay)
                delay *= 2
        raise AirCloudRateLimited(str(last))

    # ---- 接口 ----
    async def async_list_projects(self) -> list[dict]:
        res = await self._post("/list_my_projects")
        value = res["value"]
        return value if isinstance(value, list) else []

    async def async_default_project_key(self) -> str:
        """「合宙标准模块」的项目 Key（文档 /get_my_default_project_key）。

        扫码绑定的设备都挂在这个项目下。返回空串表示接口没给值。
        """
        try:
            res = await self._post("/get_my_default_project_key")
        except AirCloudError:
            return ""
        value = res.get("value")
        return value.strip() if isinstance(value, str) else ""

    async def async_list_devices(self, project: str, page: int = 1, size: int = 100) -> dict:
        res = await self._post("/list_my_devices", {"project": project, "page": page, "size": size})
        return res["value"] or {}

    async def async_latest_location(self, client_id: str) -> dict | None:
        res = await self._post_retry_rate_limit("/aircloud/latest_location", {"client_id": client_id})
        value = res["value"]
        if not isinstance(value, dict):
            return None
        # 文档说此处返回 percent（电量百分比）。实测该字段时有时无，
        # 有就直接用，没有则回落到 vbat 换算，避免丢掉官方口径。
        pct = value.get("percent")
        if pct not in (None, ""):
            try:
                value["percent"] = int(float(pct))
            except (TypeError, ValueError):
                value.pop("percent", None)
        return value

    async def async_list_by_tags(self, client_id: str, tags: list[int],
                                 page: int = 1, size: int = 10) -> dict:
        res = await self._post_retry_rate_limit(
            "/aircloud/list_by_tags",
            {"client_id": client_id, "tags": list(tags), "page": page, "size": size},
        )
        return res["value"] if isinstance(res["value"], dict) else {}

    # ---- 状态聚合：按轮询频率取「此刻定位」+ tag 状态 ----
    async def async_device_status(self, client_id: str) -> dict:
        """取一次设备状态：当前位置 + 电量/卫星/信号/定位标识。

        **不读平台历史** —— 每轮只问「设备现在在哪」，把这一轮的点交给
        coordinator 累积成轨迹。这样轨迹的密度就是轮询频率，不会像读取
        历史那样一次灌进几天的旧点。
        """
        status: dict = {"client_id": client_id, "found": False}

        # ① 此刻定位（含地址/信号/时间）
        try:
            location = await self.async_latest_location(client_id)
        except (AirCloudRateLimited, AirCloudError):
            location = None
        if location and location.get("lng") is not None and location.get("lat") is not None:
            try:
                status["gcj_lng"] = float(location["lng"])
                status["gcj_lat"] = float(location["lat"])
                status["found"] = True
            except (TypeError, ValueError):
                pass
        if status.get("found"):
            try:
                status["wgs_lng"] = float(location["wlng"])
                status["wgs_lat"] = float(location["wlat"])
            except (KeyError, TypeError, ValueError):
                status["wgs_lng"], status["wgs_lat"] = gcj02_to_wgs84(
                    status["gcj_lng"], status["gcj_lat"])
            status["address"] = location.get("address") or ""
            status["time"] = location.get("time") or ""
            status["ts"] = parse_local_time(status["time"])
            # 官方口径优先：文档说 latest_location 直接给 percent。
            # 实测该字段时有时无，给了就用，省掉 vbat 线性换算的误差。
            if location.get("percent") is not None:
                status["battery"] = location["percent"]
                status["battery_official"] = True
            if location.get("signal") not in (None, ""):
                try:
                    status["signal"] = int(float(location["signal"]))
                except (TypeError, ValueError):
                    pass

        # ② tag 状态：电量/卫星/信号/定位标识（偶发 429 由客户端退避重试兜住）
        try:
            records = (await self.async_list_by_tags(
                client_id, STATUS_TAGS, 1, 10)).get("records") or []
        except AirCloudError:
            records = []
        if records:
            status.setdefault("time", records[0].get("ct") or "")
            status.setdefault("ts", parse_local_time(records[0].get("ct") or ""))
        for rec in records:
            if status.get("vbat") is None and rec.get("val_799") not in (None, ""):
                try:
                    mv = float(rec["val_799"])
                except (TypeError, ValueError):
                    continue
                if mv > 0:
                    status["vbat"] = mv
                    # 官方 percent 已有就别覆盖（vbat→% 是线性估算，仅兜底）
                    if status.get("battery") is None:
                        status["battery"] = vbat_to_percent(mv)
            if status.get("sat") is None and rec.get("val_517") not in (None, ""):
                try:
                    status["sat"] = int(float(rec["val_517"]))
                except (TypeError, ValueError):
                    pass
            if status.get("signal") is None and rec.get("val_782") not in (None, ""):
                try:
                    status["signal"] = int(float(rec["val_782"]))
                except (TypeError, ValueError):
                    pass
            if status.get("fix") is None and rec.get("val_519") not in (None, ""):
                try:
                    status["fix"] = int(float(rec["val_519"]))
                except (TypeError, ValueError):
                    pass
            if status.get("speed") is None and rec.get(f"val_{TAG_GNSS_BIN}"):
                samples = decode_gnss_5x16(
                    str(rec[f"val_{TAG_GNSS_BIN}"]),
                    status.get("gcj_lng") or 0.0,
                    status.get("gcj_lat") or 0.0,
                )
                if samples:
                    last = samples[-1]
                    status["speed"] = round(last["speed"] * 3.6, 1)   # m/s → km/h
                    status["course"] = last["course"]
                    status["altitude"] = last["altitude"]

        # ③ 本轮取到的定位点（最多 1 个）交给 coordinator 累积
        status["points"] = []
        if status.get("found"):
            pt: dict = {"lng": status["gcj_lng"], "lat": status["gcj_lat"]}
            if status.get("time"):
                pt["time"] = status["time"]
            if status.get("ts"):
                pt["ts"] = status["ts"]
            status["points"] = [pt]
        return status
