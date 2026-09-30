"""搜索源：TMDB 元数据、PanSou（115 分享 + 磁力）、Prowlarr（磁力）。"""
import asyncio
import re
from dataclasses import dataclass
from urllib.parse import urlparse, parse_qs

import httpx

from .config import cfg
from .filters import (Rules, parse_resolution, BAD_TAGS, title_match, score,
                      magnet_size_ok, has_watermark)


@dataclass
class Candidate:
    kind: str  # share | magnet
    title: str
    url: str
    password: str = ""
    size: float = 0
    seeders: int = 0
    src: str = ""
    score: int = 0


async def tmdb_meta(media_type: str, tmdb_id: int, season):
    """返回 {names, year, episodes}。没配 TMDB key 时返回 None，由调用方用标题兜底。"""
    from . import settings, tmdb
    if not settings.get()["tmdb_key"]:
        return None
    path = f"/{media_type}/{tmdb_id}"
    loc, en = await asyncio.gather(tmdb.get(path), tmdb.get(path, language="en-US"))
    key = "title" if media_type == "movie" else "name"
    okey = "original_title" if media_type == "movie" else "original_name"
    dkey = "release_date" if media_type == "movie" else "first_air_date"
    names = [n for n in dict.fromkeys([loc.get(key), en.get(key), loc.get(okey)]) if n]
    episodes = 0
    if media_type == "tv" and season:
        episodes = len((await tmdb.get(f"{path}/season/{season}")).get("episodes", []))
    return {"names": names, "year": (loc.get(dkey) or "")[:4], "episodes": episodes}


def parse_115_share(url: str, password: str = ""):
    u = urlparse(url)
    m = re.search(r"/s/([a-z0-9]+)", u.path)
    if not m:
        return None
    pw = password or (parse_qs(u.query).get("password") or [""])[0]
    return m.group(1), pw


def queries(names, year, media_type, season):
    out = []
    for n in names[:3]:
        out.append(f"{n} {year}" if media_type == "movie" and year else
                   f"{n} S{season:02d}" if media_type == "tv" else n)
    return list(dict.fromkeys(out))


async def search_pansou(c: httpx.AsyncClient, kw: str) -> list[Candidate]:
    r = await c.get(f"{cfg.PANSOU_URL}/api/search",
                    params={"kw": kw, "cloud_types": "115,magnet", "res": "merge"})
    data = (r.json().get("data") or {}).get("merged_by_type") or {}
    out = []
    for it in data.get("115", []):
        if parse_115_share(it.get("url", ""), it.get("password", "")):
            out.append(Candidate("share", it.get("note", ""), it["url"], it.get("password", ""), src="pansou"))
    for it in data.get("magnet", []):
        if str(it.get("url", "")).startswith("magnet:"):
            out.append(Candidate("magnet", it.get("note", ""), it["url"], src="pansou"))
    return out


async def search_prowlarr(c: httpx.AsyncClient, kw: str, media_type: str) -> list[Candidate]:
    if not cfg.PROWLARR_KEY:
        return []
    r = await c.get(f"{cfg.PROWLARR_URL}/api/v1/search",
                    params={"query": kw, "type": "search", "limit": 100,
                            "categories": 2000 if media_type == "movie" else 5000},
                    headers={"X-Api-Key": cfg.PROWLARR_KEY})
    out = []
    for it in r.json():
        mag = it.get("magnetUrl") or ""
        if mag.startswith("magnet:"):  # 只有磁力能交给 115 离线；.torrent 代理链接 115 访问不到
            out.append(Candidate("magnet", it.get("title", ""), mag, size=it.get("size") or 0,
                                 seeders=it.get("seeders") or 0, src=f"prowlarr/{it.get('indexer', '')}"))
    return out


async def search_all(meta: dict, media_type: str, season):
    qs = queries(meta["names"], meta["year"], media_type, season)
    async with httpx.AsyncClient(timeout=30) as c:
        jobs = [search_pansou(c, q) for q in qs] + [search_prowlarr(c, q, media_type) for q in qs]
        res = await asyncio.gather(*jobs, return_exceptions=True)
    return [x for r in res if isinstance(r, list) for x in r]


def build_candidates(raw: list[Candidate], meta: dict, media_type: str, season, rules: Rules):
    seen, out = set(), []
    for c in raw:
        if c.url in seen or not c.title or BAD_TAGS.search(c.title) or has_watermark(c.title):
            continue
        seen.add(c.url)
        if not title_match(c.title, meta["names"], meta["year"], media_type, season):
            continue
        res = parse_resolution(c.title)
        if res and res < rules.min_res:
            continue
        if c.kind == "magnet":
            if c.size and not magnet_size_ok(c.size, media_type, meta.get("episodes", 0), rules):
                continue
        per = 0
        if c.kind == "magnet" and c.size:
            per = c.size if media_type == "movie" else c.size / max(meta.get("episodes", 0), 1)
        c.score = score(c.title, c.seeders, c.kind == "share", per, rules)
        out.append(c)
    return sorted(out, key=lambda x: x.score, reverse=True)
