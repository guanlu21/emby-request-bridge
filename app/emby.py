"""按 Emby 账号校验求片用户。emby-manager 管理的就是 Emby 账号（到期会停用），所以以 Emby 为准即可关联。"""
import logging
import re
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


_log = logging.getLogger("uvicorn.error")
_found: dict = {}  # (类型, tmdb_id) -> (时间, 条目或 None)


def _norm(s) -> str:
    """名字归一化：小写，只留字母数字和汉字（去掉空格、标点、冒号等）。"""
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]", "", str(s or "").lower())


def _match_by_name(items: list, tmdb_id: int, names: set, year: str):
    """按名字+年份从 Emby 搜索结果里挑出同一部片。条目自带别的 TMDB ID 时一律不算（避免同名不同片误判）。"""
    for it in items:
        have = str((it.get("ProviderIds") or {}).get("Tmdb", "") or "")
        if have and have != str(tmdb_id):
            continue
        if not ({_norm(it.get("Name")), _norm(it.get("OriginalTitle"))} & names):
            continue
        py = it.get("ProductionYear")
        if year and py and abs(int(py) - int(year)) > 1:
            continue
        return it
    return None


async def find_item(media_type: str, tmdb_id: int, title: str = "", year: str = "", orig: str = ""):
    """在 Emby 库里找这部片，返回条目（含 Id）或 None。
    先按 TMDB ID 精确找；找不到再按「名字 + 年份」找——库里的片可能没刮到 TMDB ID（用别的刮削源、
    只有 IMDb ID、手动命名等），只靠 TMDB ID 会把已入库的片误判成没有。查询失败时不缓存，也会写日志。"""
    if not (cfg.EMBY_URL and cfg.EMBY_KEY):
        return None
    key = (media_type, int(tmdb_id))
    hit = _found.get(key)
    if hit and time.time() - hit[0] < (60 if hit[1] else 20):
        return hit[1]
    typ = "Movie" if media_type == "movie" else "Series"
    base = {"api_key": cfg.EMBY_KEY, "Recursive": "true", "IncludeItemTypes": typ,
            "Fields": "ProviderIds,ProductionYear,OriginalTitle"}
    found, ok = None, True
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(f"{cfg.EMBY_URL}/emby/Items", params={
                **base, "Limit": 5, "AnyProviderIdEquals": f"tmdb.{tmdb_id}"})
            for i in r.json().get("Items", []):
                if str((i.get("ProviderIds") or {}).get("Tmdb", "")) == str(tmdb_id):
                    found = i
                    break
            if not found:
                names = {_norm(title), _norm(orig)} - {""}
                for term in dict.fromkeys(x for x in (title, orig) if x and x.strip()):
                    r = await c.get(f"{cfg.EMBY_URL}/emby/Items", params={
                        **base, "Limit": 30, "SearchTerm": term})
                    found = _match_by_name(r.json().get("Items", []), tmdb_id, names, str(year or ""))
                    if found:
                        break
    except Exception as e:  # noqa
        ok = False
        _log.warning(f"Emby 查询入库状态失败 {media_type} tmdb={tmdb_id}: {e}")
    if ok:
        _found[key] = (time.time(), found)
    return found


async def series_episode_counts(tmdb_id: int, title: str = "", year: str = "", orig: str = "") -> dict:
    """库里这部剧每一季的集数 {季号: 已有集数}；查不到返回 {}。用于对比 TMDB 总集数判断是否缺集（求更）。"""
    it = await find_item("tv", tmdb_id, title, year, orig)
    if not it:
        return {}
    try:
        sid = it["Id"]
        out = {}
        async with httpx.AsyncClient(timeout=10) as c:
            # 首选 /Shows/{id}/Episodes（标准端点，带季号集号）；失败再回退 ParentId 查询
            try:
                r2 = await c.get(f"{cfg.EMBY_URL}/emby/Shows/{sid}/Episodes", params={
                    "api_key": cfg.EMBY_KEY, "Limit": 2000})
                eps = r2.json().get("Items", [])
            except Exception:  # noqa
                eps = []
            if not eps:
                r2 = await c.get(f"{cfg.EMBY_URL}/emby/Items", params={
                    "api_key": cfg.EMBY_KEY, "ParentId": sid, "Recursive": "true",
                    "IncludeItemTypes": "Episode", "Fields": "ParentIndexNumber", "Limit": 2000})
                eps = r2.json().get("Items", [])
        for x in eps:
            sn, ep = x.get("ParentIndexNumber"), x.get("IndexNumber")
            if not sn:
                sn = 1  # 无季号时按第 1 季
            if sn and ep:
                out[sn] = max(out.get(sn, 0), ep)
        return out
    except Exception:  # noqa
        return {}


async def has_item(media_type: str, tmdb_id: int, title: str = "", year: str = "", orig: str = "") -> bool:
    """库里是否已有这部片（先比 TMDB ID，再比名字+年份；查询失败按"没有"处理）。"""
    return bool(await find_item(media_type, tmdb_id, title, year, orig))


async def series_seasons(tmdb_id: int, title: str = "", year: str = "", orig: str = "") -> list:
    """库里这部剧的季号列表；查询失败或没配 Emby 时返回 []（按"缺季"处理，保留补季入口）。"""
    it = await find_item("tv", tmdb_id, title, year, orig)
    if not it:
        return []
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r2 = await c.get(f"{cfg.EMBY_URL}/emby/Items", params={
                "api_key": cfg.EMBY_KEY, "ParentId": it["Id"],
                "IncludeItemTypes": "Season", "Fields": "IndexNumber"})
        seasons = [x.get("IndexNumber") for x in r2.json().get("Items", []) if x.get("IndexNumber")]
        return sorted(set(seasons))
    except Exception:  # noqa
        return []


async def diagnose(term: str = "") -> dict:
    """设置页「测试 Emby」用：看地址/密钥是否可用、库里有多少片、用关键词能搜到什么（含它们的 TMDB ID）。"""
    out = {"url": cfg.EMBY_URL or "", "has_key": bool(cfg.EMBY_KEY), "ok": False}
    if not (cfg.EMBY_URL and cfg.EMBY_KEY):
        out["error"] = "没有配置 Emby 地址或 API 密钥（首次登录自动生成失败时，需到 Emby 控制台 → 高级 → API 密钥手动创建，再填到设置里）"
        return out
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(f"{cfg.EMBY_URL}/emby/System/Info", params={"api_key": cfg.EMBY_KEY})
            if r.status_code != 200:
                out["error"] = f"Emby 返回 {r.status_code}（密钥无效或地址不对）"
                return out
            out["server"] = r.json().get("ServerName", "") + " " + str(r.json().get("Version", ""))
            for typ, k in (("Movie", "movies"), ("Series", "series")):
                r = await c.get(f"{cfg.EMBY_URL}/emby/Items", params={
                    "api_key": cfg.EMBY_KEY, "Recursive": "true", "IncludeItemTypes": typ, "Limit": 1})
                out[k] = r.json().get("TotalRecordCount", 0)
            if term.strip():
                r = await c.get(f"{cfg.EMBY_URL}/emby/Items", params={
                    "api_key": cfg.EMBY_KEY, "Recursive": "true", "IncludeItemTypes": "Movie,Series", "Limit": 20,
                    "SearchTerm": term.strip(), "Fields": "ProviderIds,ProductionYear,OriginalTitle"})
                out["found"] = [{"name": i.get("Name"), "type": i.get("Type"), "year": i.get("ProductionYear"),
                                 "tmdb": (i.get("ProviderIds") or {}).get("Tmdb", "")} for i in r.json().get("Items", [])]
        out["ok"] = True
    except Exception as e:  # noqa
        out["error"] = f"连接 Emby 出错：{e}"
    return out
