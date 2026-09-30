"""TMDB 访问（走网页里配置的代理 / 自定义主机名）。"""
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


async def search(q: str) -> list[dict]:
    d = await get("/search/multi", query=q, include_adult="false")
    out = []
    for it in d.get("results", []):
        if it.get("media_type") in ("movie", "tv"):
            out.append({"id": it["id"], "type": it["media_type"], "title": it.get("title") or it.get("name") or "",
                        "year": _year(it), "overview": (it.get("overview") or "")[:140],
                        "poster": it.get("poster_path") or "", "rating": round(it.get("vote_average") or 0, 1)})
    return out[:20]


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
