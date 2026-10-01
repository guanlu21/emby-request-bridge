"""用 Emby 账号密码登录，并签发桥接服务自己的会话令牌。"""
import base64
import hashlib
import hmac
import ipaddress
import json
import time
from urllib.parse import urlsplit

import httpx

from .config import cfg


class AuthError(Exception):
    pass


def _sig(payload: bytes) -> str:
    return hmac.new(cfg.TOKEN.encode() + b"|session", payload, hashlib.sha256).hexdigest()


def make(user: dict, ttl: int = 7 * 86400) -> str:
    p = base64.urlsafe_b64encode(json.dumps({**user, "exp": int(time.time()) + ttl}).encode())
    return p.decode() + "." + _sig(p)


def verify(tok: str):
    try:
        p, sig = tok.rsplit(".", 1)
        if not hmac.compare_digest(sig, _sig(p.encode())):
            return None
        d = json.loads(base64.urlsafe_b64decode(p.encode()))
        return d if d.get("exp", 0) > time.time() else None
    except Exception:  # noqa
        return None


def normalize_url(u: str) -> str:
    u = (u or "").strip().rstrip("/")
    return u if not u or "://" in u else "http://" + u


def is_private_host(url: str) -> bool:
    """首次设置时只接受局域网地址，防止有人抢在你之前把 Emby 地址指到别处。"""
    h = urlsplit(url).hostname or ""
    try:
        ip = ipaddress.ip_address(h)
        return ip.is_private or ip.is_loopback or ip.is_link_local
    except ValueError:
        return bool(h) and ("." not in h or h.endswith((".local", ".lan", ".home", ".internal")))


async def _make_api_key(c: httpx.AsyncClient, base: str, token: str) -> str:
    """用管理员令牌给桥接服务创建一个 Emby API 密钥；失败返回空串（可在设置里手动填）。"""
    h = {"X-Emby-Token": token}
    try:
        await c.post(f"{base}/emby/Auth/Keys", params={"App": "RequestBridge"}, headers=h)
        items = [i for i in (await c.get(f"{base}/emby/Auth/Keys", headers=h)).json().get("Items", [])
                 if i.get("AppName") == "RequestBridge"]
        items.sort(key=lambda i: i.get("DateCreated", ""))
        return items[-1]["AccessToken"] if items else ""
    except Exception:  # noqa
        return ""


async def emby_login(name: str, pw: str, base: str = "", setup: bool = False) -> dict:
    base = base or cfg.EMBY_URL
    if not base:
        raise AuthError("还没有配置 Emby 地址")
    hdr = {"X-Emby-Authorization": 'MediaBrowser Client="RequestBridge", Device="Web", '
                                   'DeviceId="request-bridge", Version="1.0"'}
    async with httpx.AsyncClient(timeout=15) as c:
        try:
            r = await c.post(f"{base}/emby/Users/AuthenticateByName", json={"Username": name, "Pw": pw}, headers=hdr)
        except Exception as e:  # noqa
            raise AuthError("连不上 Emby 服务器，请检查地址") from e
        if r.status_code != 200:
            raise AuthError("用户名或密码不对")
        d = r.json()
        u, pol = d["User"], d["User"].get("Policy") or {}
        api_key = ""
        if setup and pol.get("IsAdministrator"):
            api_key = await _make_api_key(c, base, d["AccessToken"])
        try:  # 用完即退出，免得在 Emby 的设备列表里堆会话
            await c.post(f"{base}/emby/Sessions/Logout", headers={"X-Emby-Token": d["AccessToken"]})
        except Exception:  # noqa
            pass
    if pol.get("IsDisabled"):
        raise AuthError("这个账号已停用或已到期")
    return {"uid": u["Id"], "name": u["Name"], "emby_admin": bool(pol.get("IsAdministrator")), "api_key": api_key}
