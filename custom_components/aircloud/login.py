"""合宙账号登录 —— 在 HA 配置界面内完成（含图形验证码）。

实测链路（2026-09）：
  1. GET  api-iot.luatos.com/iam/luat_oauth/authorize?return_to=...
         → 302 → iot.openluat.com/auth/login?client_id=..&code_challenge=..&state=..
  2. GET  iot.openluat.com/api/tool/v1/captcha        → {data:{cap_id, img(data-url)}}
  3. POST iot.openluat.com/api/auth/v2/login          → {code:0, redirect_url}
  4. GET  redirect_url（不跟随）                      → Location 含 token=
  5. POST api-iot.luatos.com/iam/luat_oauth/v2/login?token=.. → {value:{auth,service,sets}}

关键实测结论：
  * 服务端**先验验证码、后验密码**（有效验证码 + 错密码 → 报「密码错误」；
    空/错验证码 + 错密码 → 报「验证码错误」），所以验证码无法绕过。
  * 验证码一次有效、区分大小写、约 40-60s 过期，失败必须重新取图。
  * code_challenge 必须用平台下发值，伪造会导致回调无 token。
  * 平台单会话：登录成功会让别处（含 HA 自己）的旧 token 失效。
"""
from __future__ import annotations

import logging
import re
from urllib.parse import parse_qs, quote, urlparse

import aiohttp

_LOGGER = logging.getLogger(__name__)

PORTAL = "https://iot.openluat.com"
API_HOST = "https://api-iot.luatos.com"
RETURN_TO = "https://iot.luatos.com/ai_app/luatos/move/login.html"
REDIRECT_URI = f"{API_HOST}/iam/luat_oauth/callback"
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
_TIMEOUT = aiohttp.ClientTimeout(total=25)


class AirCloudLoginError(Exception):
    """登录链路失败。"""


class AirCloudCaptchaError(AirCloudLoginError):
    """验证码错误或已过期 —— 必须重新取图后再试。"""


class AirCloudPasswordError(AirCloudLoginError):
    """账号或密码错误。"""


def _headers(referer: str | None = None) -> dict[str, str]:
    return {
        "User-Agent": _UA,
        "Referer": referer or PORTAL + "/",
        "Origin": PORTAL,
        "Accept": "application/json, text/plain, */*",
    }


async def async_oauth_params(session: aiohttp.ClientSession) -> dict[str, str]:
    """① 取 OAuth 授权参数（必须不跟随 302，否则拿不到 code_challenge）。"""
    url = f"{API_HOST}/iam/luat_oauth/authorize?return_to={quote(RETURN_TO, safe='')}"
    async with session.get(
        url, headers=_headers(), allow_redirects=False, timeout=_TIMEOUT
    ) as resp:
        loc = resp.headers.get("Location") or resp.headers.get("location")
    if not loc:
        raise AirCloudLoginError("授权接口未返回跳转地址（平台可能已改版）")
    q = parse_qs(urlparse(loc).query)
    params = {
        k: (q.get(k) or [""])[0]
        for k in ("client_id", "app_info", "code_challenge", "redirect_uri", "state")
    }
    if not all(params.values()):
        raise AirCloudLoginError(f"授权参数不完整：{params}")
    return params


async def async_captcha(session: aiohttp.ClientSession) -> tuple[str, str]:
    """② 取图形验证码 → (cap_id, data-url)。"""
    async with session.get(
        f"{PORTAL}/api/tool/v1/captcha",
        headers=_headers(PORTAL + "/auth/login"),
        timeout=_TIMEOUT,
    ) as resp:
        data = await resp.json(content_type=None) or {}
    d = data.get("data") or {}
    if not d.get("cap_id") or not d.get("img"):
        raise AirCloudLoginError("验证码接口返回异常")
    return str(d["cap_id"]), str(d["img"])


async def async_login(
    session: aiohttp.ClientSession,
    params: dict[str, str],
    phone: str,
    password: str,
    cap_id: str,
    captcha: str,
) -> dict[str, str]:
    """③④⑤ 提交登录，返回可直接使用的业务凭据。"""
    body = {
        "name": phone,
        "password": password,
        "captcha_id": str(cap_id),
        "captcha": captcha,
        **params,
    }
    async with session.post(
        f"{PORTAL}/api/auth/v2/login",
        json=body,
        headers=_headers(PORTAL + "/auth/login"),
        timeout=_TIMEOUT,
    ) as resp:
        data = await resp.json(content_type=None) or {}

    code = data.get("code")
    if code != 0:
        msg = str(data.get("msg") or "")
        if "验证码" in msg:
            raise AirCloudCaptchaError(msg or "验证码错误或已过期")
        if "密码" in msg or code == 4:
            raise AirCloudPasswordError(msg or "账号或密码错误")
        raise AirCloudLoginError(msg or f"登录失败（code={code}）")

    redirect_url = data.get("redirect_url")
    if not redirect_url:
        raise AirCloudLoginError("登录成功但未返回回调地址")

    async with session.get(
        redirect_url, headers=_headers(), allow_redirects=False, timeout=_TIMEOUT
    ) as resp:
        loc = resp.headers.get("Location") or ""
    token = (
        parse_qs(urlparse(loc).query).get("token", [""])[0] if "token=" in loc else ""
    )
    if not token:
        raise AirCloudLoginError("回调未取到登录 token（code_challenge 可能未用平台下发值）")

    async with session.post(
        f"{API_HOST}/iam/luat_oauth/v2/login?token={quote(token)}",
        json={},
        headers=_headers(),
        timeout=_TIMEOUT,
    ) as resp:
        out = await resp.json(content_type=None) or {}
    if out.get("code") != 0:
        raise AirCloudLoginError(str(out.get("value") or out.get("msg") or "换取凭据失败"))

    value = out.get("value") or {}
    auth = value.get("auth") or {}
    nav = str(auth.get("nav") or "")
    m = re.search(r"/ai_app/luatos/([^/]+)/", nav)
    creds = {
        "token": str(auth.get("token") or ""),
        "salt": str(auth.get("salt") or ""),
        "sid": str((value.get("service") or {}).get("sid") or ""),
        "public_key": str((value.get("sets") or {}).get("publicKey") or ""),
        "app_id": m.group(1) if m else "move",
        "expire_at": auth.get("accessExpireAt"),
    }
    if not (creds["token"] and creds["salt"] and creds["sid"]):
        raise AirCloudLoginError("凭据字段缺失")
    return creds
