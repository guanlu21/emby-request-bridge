"""搜索源：TMDB 元数据、PanSou（115 分享 + 磁力）、CloudSaver（115 分享）、纸鸢磁力（磁力）。"""
import asyncio
import contextvars
import re
from dataclasses import dataclass, field
from urllib.parse import urlparse, parse_qs

import httpx

from . import cloudsaver, kite
from .config import cfg
from collections import Counter

from .filters import (Rules, parse_resolution, BAD_TAGS, match_reason, score, magnet_size_ok, has_watermark,
                      _years, kw_reason, kw_bonus, season_span, year_rank, priority_tuple, parse_terms)

# 单次深度搜索可以临时覆盖设置里的「搜索深度」
DEPTH: contextvars.ContextVar = contextvars.ContextVar("search_depth", default=0)


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
    covers: list = field(default_factory=list)  # 电视剧：这个资源能覆盖请求的哪几季
    tags: list = field(default_factory=list)    # 满足了哪些要求（给候选列表展示）


async def tmdb_meta(media_type: str, tmdb_id: int, season):
    """返回 {names, year, episodes, season_eps, ...}。season 可以是单个季号或季号列表。
    没配 TMDB key 时返回 None，由调用方用标题兜底。"""
    from . import settings, tmdb
    if not settings.get()["tmdb_key"]:
        return None
    path = f"/{media_type}/{tmdb_id}"
    loc, en = await asyncio.gather(tmdb.get(path), tmdb.get(path, language="en-US"))
    key = "title" if media_type == "movie" else "name"
    okey = "original_title" if media_type == "movie" else "original_name"
    dkey = "release_date" if media_type == "movie" else "first_air_date"
    names = [n for n in dict.fromkeys([loc.get(key), en.get(key), loc.get(okey)]) if n]
    seasons = [season] if isinstance(season, int) else [s for s in (season or []) if s]
    season_eps = {}
    if media_type == "tv" and seasons:
        res = await asyncio.gather(*[tmdb.get(f"{path}/season/{s}") for s in seasons], return_exceptions=True)
        season_eps = {s: len(r.get("episodes", [])) for s, r in zip(seasons, res) if isinstance(r, dict)}
    countries = loc.get("origin_country") or [c.get("iso_3166_1", "") for c in loc.get("production_countries", [])]
    return {"names": names, "year": (loc.get(dkey) or "")[:4], "episodes": next(iter(season_eps.values()), 0),
            "season_eps": season_eps, "genres": [g["id"] for g in loc.get("genres", [])],
            "lang": loc.get("original_language") or "", "countries": countries}


def parse_115_share(url: str, password: str = ""):
    u = urlparse(url)
    m = re.search(r"/s/([a-z0-9]+)", u.path)
    if not m:
        return None
    pw = password or (parse_qs(u.query).get("password") or [""])[0]
    return m.group(1), pw


def depth() -> int:
    d = DEPTH.get() or int(cfg.SEARCH_DEPTH or 2)
    return max(1, min(3, d))


def queries(names, year, media_type, seasons, level: int = 2):
    """多种查询词。名称整体作为一个词，不拆开。
    1 档：片名+年份、片名（、S01）；2 档：再加 1080p/国语/中字（电视剧还有 全集/合集 和 第N季）；3 档：更多变体。"""
    seasons = [s for s in (seasons or []) if s] if not isinstance(seasons, int) else [seasons]
    out = []
    for n in names[:3]:
        out.append(f"{n} {year}" if year else n)
        out.append(n)
    if media_type == "tv":
        for n in names[:2]:
            for s in seasons[:3]:
                out.append(f"{n} S{s:02d}")
    if level >= 2:
        for n in names[:2]:
            out += [f"{n} 1080p", f"{n} 国语", f"{n} 中字"]
            if media_type == "tv":
                out += [f"{n} 全集", f"{n} 合集"] + [f"{n} 第{s}季" for s in seasons[:3]]
    if level >= 3:
        for n in names[:2]:
            out += [f"{n} 4K", f"{n} WEB-DL", f"{n} BluRay", f"{n} 国语中字", f"{n} {year} 1080p" if year else f"{n} HD"]
    return list(dict.fromkeys(out))[:{1: 8, 2: 16, 3: 26}[level]]


async def search_pansou(c: httpx.AsyncClient, kw: str, refresh: bool = False) -> list[Candidate]:
    params = {"kw": kw, "cloud_types": "115,magnet", "res": "merge"}
    if refresh:
        params["refresh"] = "true"  # 深度搜索：不用缓存
    r = await c.get(f"{cfg.PANSOU_URL}/api/search", params=params)
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


async def _kite_search(ks, q: str, limit: int = 50) -> list[Candidate]:
    return [Candidate("magnet", r["title"], r["magnet"], size=r["size"], seeders=r["seeders"],
                      src="kite/" + r["engine"] if r.get("engine") else "kite")
            for r in await ks.search(q, limit) if r["title"]]


async def search_all(meta: dict, media_type: str, seasons):
    """返回 (候选原始结果, 说明列表)。seasons：电视剧请求的季号列表（一次搜，不按季分开搜）。
    搜索深度（设置里选，或单次深度搜索）决定查询词数量、纸鸢磁力返回条数、是否绕过 PanSou 缓存。"""
    level = depth()
    qs = queries(meta["names"], meta["year"], media_type, seasons, level)
    kite_limit = {1: 50, 2: 100, 3: 200}[level]
    n_cs, n_kite = {1: 3, 2: 5, 3: 8}[level], {1: 4, 2: 8, 3: 12}[level]
    notes, jobs, labels = [], [], []
    async with httpx.AsyncClient(timeout=30) as c:
        if cfg.PANSOU_URL:
            jobs += [search_pansou(c, q, refresh=level >= 3) for q in qs]
            labels += ["PanSou"] * len(qs)
        else:
            notes.append("未配置 PanSou 地址，没有搜分享链接")
        if cloudsaver.configured():
            csv = cloudsaver.CloudSaver()
            jobs += [_cloudsaver_search(csv, q) for q in qs[:n_cs]]
            labels += ["CloudSaver"] * len(qs[:n_cs])
        else:
            csv = None
        if kite.configured():
            ks = kite.KiteSession()
            jobs += [_kite_search(ks, q, kite_limit) for q in qs[:n_kite]]
            labels += ["纸鸢磁力"] * len(qs[:n_kite])
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
    notes.append(f"搜索深度 {level}：{len(qs)} 个关键词，共 {len(out)} 条原始结果")
    return out, notes


def _tags(c: Candidate, meta: dict, res: int, per: float, rules: Rules, yr: int, hits: int) -> list[str]:
    tags = ["名称完整匹配"]
    tags.append({2: "年份吻合", 1: "年份相差一年"}.get(yr, "标题没写年份"))
    if res:
        tags.append(f"{res}p")
    if hits:
        tags.append(f"命中优先词×{hits}")
    if per and rules.pref_min <= per <= rules.pref_max:
        tags.append("体积合适")
    tags.append("115 分享" if c.kind == "share" else "磁力")
    return tags


def build_candidates(raw: list[Candidate], meta: dict, media_type: str, season, rules: Rules, report=None):
    """过滤并按优先级排序。season：电影传 None；电视剧传季号或季号列表（多季时优先收覆盖面大的合集）。
    report（可选）记录被丢弃的原因计数、示例和完整的被过滤清单（供「被过滤的资源」列表强制使用）。"""
    wanted = [] if media_type == "movie" else ([season] if isinstance(season, int) else [s for s in (season or []) if s])
    if media_type == "tv" and not wanted:
        wanted = [1]

    def reject(c, why):
        if report is not None:
            report.setdefault("counts", Counter())[why] += 1
            if len(report.setdefault("samples", [])) < 4:
                report["samples"].append(f"[{why}] {c.title[:50]}")
            if len(report.setdefault("rejects", [])) < 150:
                report["rejects"].append({"title": c.title, "reason": why, "kind": c.kind, "src": c.src, "url": c.url,
                                          "password": c.password, "size": c.size, "seeders": c.seeders, "score": 0})

    seen, out = set(), []
    for c in raw:
        if c.url in seen or not c.title:
            continue
        seen.add(c.url)
        if BAD_TAGS.search(c.title):
            reject(c, "枪版/抢先版"); continue
        if has_watermark(c.title):
            reject(c, "带水印"); continue
        if media_type == "movie":
            why = match_reason(c.title, meta["names"], meta["year"], "movie", None)
        else:
            reasons = {s: match_reason(c.title, meta["names"], meta["year"], "tv", s) for s in wanted}
            c.covers = [s for s, w in reasons.items() if not w]
            why = "" if c.covers else next(iter(reasons.values()), "")
        if why:
            reject(c, why); continue
        res = parse_resolution(c.title)
        if res and res < rules.min_res:
            reject(c, "分辨率太低"); continue
        text = c.title + " " + c.text
        why = kw_reason(text, cfg.KW_ALL, cfg.KW_ANY, cfg.KW_EXCLUDE)
        if why:
            reject(c, why); continue
        per = 0
        if c.kind == "magnet" and c.size:
            sp = season_span(c.title)
            multi = bool(sp and sp[0] != sp[1])
            eps = meta.get("season_eps", {}).get(c.covers[0]) if c.covers else None
            eps = eps or meta.get("episodes", 0)
            if not magnet_size_ok(c.size, media_type, eps, rules, multi):
                reject(c, "体积不在范围"); continue
            per = c.size if media_type == "movie" else (c.size / eps if eps and not multi else 0)
        yr = year_rank(c.title, meta["names"], meta["year"])
        hits = len([g for g in parse_terms(cfg.KW_PREFER) if g and any(a in text.lower() for a in g)])
        c.score = score(c.title, c.seeders, c.kind == "share", per, rules) + kw_bonus(text, cfg.KW_PREFER) + {2: 12, 1: 6}.get(yr, 0)
        if media_type == "movie" and not _years(c.title):
            c.score -= 15  # 没写年份：不丢弃，但排在写了年份的后面
        rank = {2160: 2, 1080: 3, 720: 1}.get(res, 0)
        facts = {"year": yr, "quality": rank, "keywords": hits, "size": int(bool(per and rules.pref_min <= per <= rules.pref_max)),
                 "source": int(c.kind == "share"), "seeders": min(c.seeders, 50)}
        c._key = priority_tuple(cfg.PRIORITY, facts)
        c.tags = _tags(c, meta, res, per, rules, yr, hits) + ([f"覆盖第{'/'.join(map(str, c.covers))}季"] if media_type == "tv" else [])
        out.append(c)
    # 电视剧：覆盖请求季数越多越优先（多季合集优先，不是一季一季单独找）；然后按设置的优先级逐项比较，最后看综合分
    return sorted(out, key=lambda x: (len(x.covers), x._key, x.score), reverse=True)
