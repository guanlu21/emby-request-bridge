"""搜索源：TMDB 元数据、PanSou（115 分享 + 磁力）、CloudSaver（115 分享）、纸鸢磁力（磁力）。"""
import asyncio
import re
from dataclasses import dataclass
from urllib.parse import urlparse, parse_qs

import httpx

from . import cloudsaver, kite
from .config import cfg
from collections import Counter

from .filters import (Rules, parse_resolution, BAD_TAGS, match_reason, score,
                      magnet_size_ok, has_watermark, _years, kw_reason, kw_bonus)


@dataclass
class Candidate:
    kind: str  # share | magnet
    title: str
    url: str
    password: str = ""
    size: float = 0
    seeders: int = 0
    src: str = ""
    text: str = ""  # 附加描述（如 CloudSaver 的正文），只参与关键词筛选，不参与片名匹配
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
    countries = loc.get("origin_country") or [c.get("iso_3166_1", "") for c in loc.get("production_countries", [])]
    return {"names": names, "year": (loc.get(dkey) or "")[:4], "episodes": episodes,
            "genres": [g["id"] for g in loc.get("genres", [])], "lang": loc.get("original_language") or "",
            "countries": countries}


def parse_115_share(url: str, password: str = ""):
    u = urlparse(url)
    m = re.search(r"/s/([a-z0-9]+)", u.path)
    if not m:
        return None
    pw = password or (parse_qs(u.query).get("password") or [""])[0]
    return m.group(1), pw


def queries(names, year, media_type, season):
    """多种查询词：片名+年份、纯片名；剧集再加 S01。资源站标题写法不统一，只靠一种搜不全。"""
    out = []
    for n in names[:3]:
        out.append(f"{n} {year}" if year else n)
        out.append(n)
        if media_type == "tv" and season:
            out.append(f"{n} S{season:02d}")
    return list(dict.fromkeys(out))[:8]


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


async def _cloudsaver_search(cs, q: str) -> list[Candidate]:
    return [Candidate("share", r["title"], r["url"], r["password"], src="cloudsaver", text=r["text"])
            for r in await cs.search(q)]


async def _kite_search(ks, q: str) -> list[Candidate]:
    return [Candidate("magnet", r["title"], r["magnet"], size=r["size"], seeders=r["seeders"], src="kite")
            for r in await ks.search(q) if r["title"]]


async def search_all(meta: dict, media_type: str, season):
    """返回 (候选原始结果, 说明列表)。说明里记录没配置的源和出错的源，便于排查为什么搜不到。"""
    qs = queries(meta["names"], meta["year"], media_type, season)
    notes, jobs, labels = [], [], []
    async with httpx.AsyncClient(timeout=30) as c:
        if cfg.PANSOU_URL:
            jobs += [search_pansou(c, q) for q in qs]
            labels += ["PanSou"] * len(qs)
        else:
            notes.append("未配置 PanSou 地址，没有搜分享链接")
        if cloudsaver.configured():
            csv = cloudsaver.CloudSaver()
            jobs += [_cloudsaver_search(csv, q) for q in qs[:3]]
            labels += ["CloudSaver"] * len(qs[:3])
        else:
            csv = None
        if kite.configured():
            ks = kite.KiteSession()
            jobs += [_kite_search(ks, q) for q in qs[:4]]
            labels += ["纸鸢磁力"] * len(qs[:4])
        else:
            ks = None
        try:
            res = await asyncio.gather(*jobs, return_exceptions=True)
        finally:
            if ks:
                await ks.close()
            if csv:
                await csv.close()
    out, errs = [], Counter()
    for lb, r in zip(labels, res):
        if isinstance(r, list):
            out += r
        else:
            errs[f"{lb}：{type(r).__name__}"] += 1
    notes += [f"{k}（{v} 次请求出错）" for k, v in errs.items()]
    return out, notes


def build_candidates(raw: list[Candidate], meta: dict, media_type: str, season, rules: Rules, report=None):
    """过滤并排序。report（可选）会记录被丢弃的原因计数和示例，写进请求日志方便排查。"""
    def reject(c, why):
        if report is not None:
            report.setdefault("counts", Counter())[why] += 1
            if len(report.setdefault("samples", [])) < 4:
                report["samples"].append(f"[{why}] {c.title[:50]}")

    seen, out = set(), []
    for c in raw:
        if c.url in seen or not c.title:
            continue
        seen.add(c.url)
        if BAD_TAGS.search(c.title):
            reject(c, "枪版/抢先版"); continue
        if has_watermark(c.title):
            reject(c, "带水印"); continue
        why = match_reason(c.title, meta["names"], meta["year"], media_type, season)
        if why:
            reject(c, why); continue
        res = parse_resolution(c.title)
        if res and res < rules.min_res:
            reject(c, "分辨率太低"); continue
        why = kw_reason(c.title + " " + c.text, cfg.KW_ALL, cfg.KW_ANY, cfg.KW_EXCLUDE)
        if why:
            reject(c, why); continue
        per = 0
        if c.kind == "magnet" and c.size:
            if not magnet_size_ok(c.size, media_type, meta.get("episodes", 0), rules):
                reject(c, "体积不在范围"); continue
            per = c.size if media_type == "movie" else c.size / max(meta.get("episodes", 0), 1)
        c.score = score(c.title, c.seeders, c.kind == "share", per, rules) + kw_bonus(c.title + " " + c.text, cfg.KW_PREFER)
        if media_type == "movie" and not _years(c.title):
            c.score -= 15  # 没写年份：不丢弃，但排在写了年份的后面
        out.append(c)
    return sorted(out, key=lambda x: x.score, reverse=True)
