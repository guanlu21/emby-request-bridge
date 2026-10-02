"""TMDB 访问（走网页里配置的代理 / 自定义主机名）。"""
import asyncio
import re
import time

import httpx

from . import settings


def client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=15, proxy=settings.proxy())


async def get(path: str, **params):
    s = settings.get()
    if not s["tmdb_key"]:
        raise RuntimeError("还没有配置 TMDB API Key，请管理员到「设置」页填写")
    params.setdefault("language", s["language"])
    params["api_key"] = s["tmdb_key"]
    async with client() as c:
        r = await c.get(s["api_host"].rstrip("/") + "/3" + path, params=params)
    r.raise_for_status()
    return r.json()


def _year(it):
    return (it.get("release_date") or it.get("first_air_date") or "")[:4]


def split_year(q: str):
    """'流浪地球 2019' / '流浪地球(2019)' → ('流浪地球', '2019')；没有年份返回 (q, '')。"""
    m = re.match(r"^(.*?)[\s(（\[【]*((?:19|20)\d{2})[\s)）\]】]*$", q.strip())
    return (m.group(1).strip(), m.group(2)) if m and m.group(1).strip() else (q.strip(), "")


def _item(it, typ):
    return {"id": it["id"], "type": typ, "title": it.get("title") or it.get("name") or "",
            "year": _year(it), "overview": (it.get("overview") or "")[:140],
            "poster": it.get("poster_path") or "", "rating": round(it.get("vote_average") or 0, 1),
            "pop": it.get("popularity") or 0}


async def search(q: str) -> list[dict]:
    """没有年份：综合搜索取前 3 页；带年份：电影和剧集各按年份搜前 2 页（搜不到再退回综合搜索并按年份过滤）。"""
    name, year = split_year(q)
    items, seen = [], set()

    def add(typ, results):
        for it in results:
            t = typ or it.get("media_type")
            if t in ("movie", "tv") and (t, it["id"]) not in seen:
                seen.add((t, it["id"]))
                items.append(_item(it, t))

    if year:
        jobs = [get("/search/movie", query=name, year=year, page=p) for p in (1, 2)] + \
               [get("/search/tv", query=name, first_air_date_year=year, page=p) for p in (1, 2)]
        res = await asyncio.gather(*jobs, return_exceptions=True)
        for k, r in enumerate(res):
            if isinstance(r, dict):
                add("movie" if k < 2 else "tv", r.get("results", []))
        items.sort(key=lambda x: x["pop"], reverse=True)
        if not items:
            res = await asyncio.gather(*[get("/search/multi", query=name, include_adult="false", page=p) for p in (1, 2, 3)],
                                       return_exceptions=True)
            for r in res:
                if isinstance(r, dict):
                    add(None, r.get("results", []))
            items = [x for x in items if x["year"] == year]
    else:
        res = await asyncio.gather(*[get("/search/multi", query=name, include_adult="false", page=p) for p in (1, 2, 3)],
                                   return_exceptions=True)
        for r in res:
            if isinstance(r, dict):
                add(None, r.get("results", []))
        if not items and isinstance(res[0], Exception):
            raise res[0]
    return items[:60]


async def tv_seasons(tv_id: int):
    d = await get(f"/tv/{tv_id}")
    return {"title": d.get("name", ""), "year": _year(d),
            "seasons": [{"season": s["season_number"], "name": s.get("name", ""), "episodes": s.get("episode_count", 0)}
                        for s in d.get("seasons", []) if s.get("season_number", 0) > 0]}


async def test() -> dict:
    t = time.time()
    try:
        await get("/configuration")
        return {"ok": True, "ms": int((time.time() - t) * 1000)}
    except Exception as e:  # noqa
        return {"ok": False, "error": f"{type(e).__name__}: {e}"[:200]}
