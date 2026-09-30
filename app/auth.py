"""用 Emby 账号密码登录，并签发桥接服务自己的会话令牌。"""
import base64
import hashlib
import hmac
import json
import time

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


async def emby_login(name: str, pw: str) -> dict:
    if not (cfg.EMBY_URL and cfg.EMBY_KEY):
        raise AuthError("服务端还没有配置 Emby 地址")
    hdr = {"X-Emby-Authorization": 'MediaBrowser Client="RequestBridge", Device="Web", '
                                   'DeviceId="request-bridge", Version="1.0"'}
    async with httpx.AsyncClient(timeout=15) as c:
        try:
            r = await c.post(f"{cfg.EMBY_URL}/emby/Users/AuthenticateByName",
                             json={"Username": name, "Pw": pw}, headers=hdr)
        except Exception as e:  # noqa
            raise AuthError("连不上 Emby 服务器") from e
        if r.status_code != 200:
            raise AuthError("用户名或密码不对")
        d = r.json()
        u, pol = d["User"], d["User"].get("Policy") or {}
        try:  # 用完即退出，免得在 Emby 的设备列表里堆会话
            await c.post(f"{cfg.EMBY_URL}/emby/Sessions/Logout", headers={"X-Emby-Token": d["AccessToken"]})
        except Exception:  # noqa
            pass
    if pol.get("IsDisabled"):
        raise AuthError("这个账号已停用或已到期")
    return {"uid": u["Id"], "name": u["Name"], "admin": bool(pol.get("IsAdministrator"))}
