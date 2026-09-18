#!/usr/bin/env python3
"""合宙 iot.openluat.com 账号登录 → 导出 AirCloud 业务凭据（token/salt/sid/publicKey）。

平台登录链路（实测 2026-09）：
  1. GET  api-iot.luatos.com/iam/luat_oauth/authorize?return_to=...
         → 302 到 iot.openluat.com/auth/login?client_id=..&app_info=..&code_challenge=..&redirect_uri=..&state=..
  2. GET  iot.openluat.com/api/tool/v1/captcha              → {data:{cap_id, img(data:image/jpeg;base64)}}
  3. POST iot.openluat.com/api/auth/v2/login  (JSON, credentials:include)
         body = {name,password,captcha_id,captcha,client_id,app_info,code_challenge,redirect_uri,state}
         → {code:0, redirect_url:"...callback?code=..&state=.."}
  4. GET  redirect_url → 302 Location: https://iot.luatos.com/ai_app/luatos/{appId}/login.html?token=..
  5. POST api-iot.luatos.com/iam/luat_oauth/v2/login?token=..  (body {} , CT: application/json)
         → {code:0, value:{auth:{token,salt},service:{sid},sets:{publicKey}}}

注意：验证码一次有效、区分大小写，识别错误必须重新取图；code_challenge 必须用平台下发值（不要伪造）。

用法：
  python3 aircloud_login.py --phone 13800000000 --save        # 交互式：图片存到 /tmp 后手动看图输入
  python3 aircloud_login.py --phone 13800000000 --captcha uWhU --save   # 已识别出验证码时直接传
"""
from __future__ import annotations

import argparse
import base64
import http.cookiejar
import json
import os
import re
import sys
import urllib.parse
import urllib.request

AUTHORIZE = ("https://api-iot.luatos.com/iam/luat_oauth/authorize"
             "?return_to=https%3A%2F%2Fiot.luatos.com%2Fai_app%2Fluatos%2Fmove%2Flogin.html")
PORTAL = "https://iot.openluat.com"
SECRETS = os.path.expanduser("~/.aircloud.json")

_jar = http.cookiejar.CookieJar()
_opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(_jar),
                                      urllib.request.HTTPRedirectHandler())


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_opener_noredirect = urllib.request.build_opener(
    urllib.request.HTTPCookieProcessor(_jar), NoRedirect())


def get(url, opener=None, headers=None):
    req = urllib.request.Request(url, headers=headers or {"Referer": PORTAL + "/"})
    try:
        with (opener or _opener).open(req, timeout=25) as r:
            return r.status, r.read().decode("utf-8", "replace"), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace"), dict(e.headers)


def post_json(url, body, opener=None, headers=None):
    h = {"Content-Type": "application/json", "Referer": PORTAL + "/"}
    h.update(headers or {})
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=h, method="POST")
    try:
        with (opener or _opener).open(req, timeout=25) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def step1_oauth_params():
    st, _, hdrs = get(AUTHORIZE, opener=_opener_noredirect)
    loc = hdrs.get("Location") or hdrs.get("location")
    if not loc:
        raise RuntimeError("authorize 未返回 302 Location")
    q = urllib.parse.parse_qs(urllib.parse.urlparse(loc).query)
    keys = ("client_id", "app_info", "code_challenge", "redirect_uri", "state")
    p = {k: (q.get(k) or [""])[0] for k in keys}
    if not all(p[k] for k in keys):
        raise RuntimeError(f"授权参数不完整: {p}")
    return p


def step2_captcha(path="/tmp/aircloud_captcha.jpg"):
    st, body, _ = get(PORTAL + "/api/tool/v1/captcha",
                      headers={"Referer": PORTAL + "/auth/login"})
    d = json.loads(body)["data"]
    with open(path, "wb") as f:
        f.write(base64.b64decode(d["img"].split(",", 1)[1]))
    return str(d["cap_id"]), path


def step3_login(params, phone, password, cap_id, captcha):
    body = {"name": phone, "password": password, "captcha_id": str(cap_id),
            "captcha": captcha, **params}
    st, txt = post_json(PORTAL + "/api/auth/v2/login", body)
    try:
        r = json.loads(txt)
    except Exception:
        raise RuntimeError(f"登录响应非 JSON: {txt[:200]}")
    if r.get("code") != 0:
        raise RuntimeError(f"登录失败: {r.get('msg')}（验证码一次有效且区分大小写，需重新取图）")
    return r["redirect_url"]


def step4_callback_token(redirect_url):
    st, _, hdrs = get(redirect_url, opener=_opener_noredirect, headers={"Referer": PORTAL + "/"})
    loc = hdrs.get("Location") or hdrs.get("location")
    if not loc or "token=" not in loc:
        raise RuntimeError(f"回调未返回带 token 的 Location: {loc}")
    q = urllib.parse.parse_qs(urllib.parse.urlparse(loc).query)
    return q["token"][0], loc


def step5_exchange(token, app_id=None):
    st, txt = post_json(
        f"https://api-iot.luatos.com/iam/luat_oauth/v2/login?token={urllib.parse.quote(token)}", {})
    r = json.loads(txt)
    if r.get("code") != 0:
        raise RuntimeError(f"换取业务凭据失败: {r.get('value')}")
    v = r["value"]
    aid = app_id
    if not aid:
        m = re.search(r"/ai_app/luatos/([^/]+)/", str(v.get("auth", {}).get("nav", ""))) or None
        aid = m.group(1) if m else "move"
    return {"token": v["auth"]["token"], "salt": v["auth"]["salt"],
            "sid": v["service"]["sid"], "pubkey": (v.get("sets") or {}).get("publicKey", ""),
            "app_id": aid, "expire_at": v["auth"].get("accessExpireAt")}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--phone", required=True)
    ap.add_argument("--password", default=os.environ.get("AIRCLOUD_PASSWORD", ""))
    ap.add_argument("--captcha", help="已识别的验证码；不传则打印图片路径后交互输入")
    ap.add_argument("--out", default=SECRETS, help="凭据输出路径（默认 ~/.aircloud.json）")
    ap.add_argument("--save", action="store_true", help=f"写入 {SECRETS}")
    ap.add_argument("--app-id")
    a = ap.parse_args()
    if not a.password:
        import getpass
        a.password = getpass.getpass("密码: ")

    params = step1_oauth_params()
    print("① 授权参数获取成功（client_id=%s）" % params["client_id"])
    cap_id, img = step2_captcha()
    captcha = a.captcha
    if not captcha:
        print(f"② 验证码图片已保存：{img}（cap_id={cap_id}）")
        captcha = input("   请输入图中 4 个字符（区分大小写）: ").strip()
    else:
        print(f"② 使用给定验证码 {captcha}（cap_id={cap_id}）")
    url = step3_login(params, a.phone, a.password, cap_id, captcha)
    print("③ 登录成功，拿到 OAuth code")
    token, loc = step4_callback_token(url)
    print(f"④ 回调拿到 token（len={len(token)}）→ {loc.split('?')[0]}")
    creds = step5_exchange(token, a.app_id)
    print(f"⑤ 换取业务凭据成功：sid={creds['sid']}，publicKey={'有' if creds['pubkey'] else '无'}，"
          f"到期 {creds['expire_at']}")
    if a.save:
        dest = os.path.expanduser(a.out)
        os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
        with open(dest, "w") as f:
            json.dump(creds, f, ensure_ascii=False, indent=2)
        os.chmod(dest, 0o600)
        print("已保存到", dest)
    else:
        print(json.dumps({k: (v[:16] + "…" if isinstance(v, str) and len(v) > 24 else v)
                          for k, v in creds.items()}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
