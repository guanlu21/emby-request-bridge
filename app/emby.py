"""按 Emby 账号校验求片用户。emby-manager 管理的就是 Emby 账号（到期会停用），所以以 Emby 为准即可关联。"""
import time

import httpx

from .config import cfg

_cache = {"t": 0.0, "users": {}}


async def users() -> dict:
    """{用户名小写: {id, name, disabled}}，缓存 60 秒。"""
    if _cache["users"] and time.time() - _cache["t"] < 60:
        return _cache["users"]
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get(f"{cfg.EMBY_URL}/emby/Users", params={"api_key": cfg.EMBY_KEY})
    d = {u["Name"].lower(): {"id": u["Id"], "name": u["Name"],
                             "disabled": bool((u.get("Policy") or {}).get("IsDisabled"))} for u in r.json()}
    _cache.update(t=time.time(), users=d)
    return d


async def check(name: str):
    """返回 (是否允许, 原因, Emby 用户 ID)。Emby 没配或查询失败时放行，不因为通知链路故障拒绝所有人。"""
    if not (cfg.EMBY_URL and cfg.EMBY_KEY and name):
        return True, "", ""
    try:
        u = (await users()).get(name.lower())
    except Exception:  # noqa
        return True, "", ""
    if not u:
        return False, "Emby 中没有该用户（可能已被删除）", ""
    if u["disabled"]:
        return False, "该 Emby 账号已停用或已到期", u["id"]
    return True, "", u["id"]
