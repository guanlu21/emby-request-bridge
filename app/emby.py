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


async def series_episode_counts(tmdb_id: int) -> dict:
    """库里这部剧每一季的集数 {季号: 已有集数}；查不到返回 {}。用于对比 TMDB 总集数判断是否缺集（求更）。"""
    if not (cfg.EMBY_URL and cfg.EMBY_KEY):
        return {}
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(f"{cfg.EMBY_URL}/emby/Items", params={
                "api_key": cfg.EMBY_KEY, "Recursive": "true", "Limit": 5, "Fields": "ProviderIds",
                "IncludeItemTypes": "Series",
                "AnyProviderIdEquals": f"tmdb.{tmdb_id}"})
            items = r.json().get("Items", [])
            if not items:
                return {}
            sid = items[0]["Id"]
            out = {}
            # 首选 /Shows/{id}/Episodes（标准端点，带季号集号）；失败再回退 ParentId 查询
            try:
                r2 = await c.get(f"{cfg.EMBY_URL}/emby/Shows/{sid}/Episodes", params={
                    "api_key": cfg.EMBY_KEY, "Limit": 2000})
                eps = r2.json().get("Items", [])
            except Exception:  # noqa
                eps = []
            if not eps:
                r2 = await c.get(f"{cfg.EMBY_URL}/emby/Items", params={
                    "api_key": cfg.EMBY_KEY, "ParentId": sid,
                    "IncludeItemTypes": "Episode", "Fields": "ParentIndexNumber", "Limit": 2000})
                eps = r2.json().get("Items", [])
            for x in eps:
                sn, ep = x.get("ParentIndexNumber"), x.get("IndexNumber")
                if not sn:
                    sn = (x.get("SeriesName") and x.get("SeasonName") and 1) or 1  # 无季号时按第 1 季
                if sn and ep:
                    out[sn] = max(out.get(sn, 0), ep)
            return out
    except Exception:  # noqa
        return {}


async def has_item(media_type: str, tmdb_id: int) -> bool:
    """库里是否已有这部片（靠 TMDB ID 比对；查询失败按"没有"处理）。"""
    if not (cfg.EMBY_URL and cfg.EMBY_KEY):
        return False
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(f"{cfg.EMBY_URL}/emby/Items", params={
                "api_key": cfg.EMBY_KEY, "Recursive": "true", "Limit": 5, "Fields": "ProviderIds",
                "IncludeItemTypes": "Movie" if media_type == "movie" else "Series",
                "AnyProviderIdEquals": f"tmdb.{tmdb_id}"})
        return any(str((i.get("ProviderIds") or {}).get("Tmdb", "")) == str(tmdb_id)
                   for i in r.json().get("Items", []))
    except Exception:  # noqa
        return False

async def series_seasons(tmdb_id: int) -> list:
    """库里这部剧（按 TMDB ID 比对）的季号列表；查询失败或没配 Emby 时返回 []（按"缺季"处理，保留补季入口）。"""
    if not (cfg.EMBY_URL and cfg.EMBY_KEY):
        return []
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(f"{cfg.EMBY_URL}/emby/Items", params={
                "api_key": cfg.EMBY_KEY, "Recursive": "true", "Limit": 5, "Fields": "ProviderIds",
                "IncludeItemTypes": "Series",
                "AnyProviderIdEquals": f"tmdb.{tmdb_id}"})
            items = r.json().get("Items", [])
            if not items:
                return []
            sid = items[0]["Id"]
            r2 = await c.get(f"{cfg.EMBY_URL}/emby/Items", params={
                "api_key": cfg.EMBY_KEY, "ParentId": sid,
                "IncludeItemTypes": "Season", "Fields": "IndexNumber"})
            seasons = [x.get("IndexNumber") for x in r2.json().get("Items", [])
                       if x.get("IndexNumber")]
            return sorted(set(seasons))
    except Exception:  # noqa
        return []
