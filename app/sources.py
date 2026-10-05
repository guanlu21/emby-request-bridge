"""搜索源：TMDB 元数据、PanSou（115 分享 + 磁力）、CloudSaver（115 分享）、纸鸢磁力（磁力）。"""
import asyncio
import contextvars
import re
from dataclasses import dataclass, field
from urllib.parse import urlparse, parse_qs

import httpx

from . import cloudsaver, dyg, haisou, kite, quark
from .config import cfg
from collections import Counter

from .filters import (Rules, parse_resolution, BAD_TAGS, match_reason, score, magnet_size_ok, has_watermark,
                      _years, kw_reason, kw_bonus, season_span, year_rank, priority_tuple, parse_terms, similarity, chinese_rank)

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
    provider: str = "115"  # 分享链接属于哪个网盘：115 | quark（磁力只能走 115 离线下载）
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
    params = {"kw": kw, "cloud_types": "115,quark,magnet" if quark.configured() else "115,magnet", "res": "merge"}
    if refresh:
        params["refresh"] = "true"  # 深度搜索：不用缓存
    r = await c.get(f"{cfg.PANSOU_URL}/api/search", params=params)
    data = (r.json().get("data") or {}).get("merged_by_type") or {}
    out = []
    for it in data.get("115", []):
        if parse_115_share(it.get("url", ""), it.get("password", "")):
            out.append(Candidate("share", it.get("note", ""), it["url"], it.get("password", ""), src="pansou"))
    for it in data.get("quark", []):
        if quark.parse_quark_share(it.get("url", ""), it.get("password", "")):
            out.append(Candidate("share", it.get("note", ""), it["url"], it.get("password", ""), src="pansou", provider="quark"))
    for it in data.get("magnet", []):
        if str(it.get("url", "")).startswith("magnet:"):
            out.append(Candidate("magnet", it.get("note", ""), it["url"], src="pansou"))
    return out


async def _cloudsaver_search(cs, q: str) -> list[Candidate]:
    return [Candidate("share", r["title"], r["url"], r["password"], src="cloudsaver", text=r["text"],
                      provider=r.get("provider", "115")) for r in await cs.search(q)]


async def _kite_search(ks, q: str, limit: int = 50) -> list[Candidate]:
    return [Candidate("magnet", r["title"], r["magnet"], size=r["size"], seeders=r["seeders"],
                      src="kite/" + r["engine"] if r.get("engine") else "kite")
            for r in await ks.search(q, limit) if r["title"]]


# 每个搜索源：(同时最多几个请求, 单个请求超时秒数)。并发太多时 PanSou / CloudSaver 会被压垮，结果全部超时，
# 所以一个源一次只放几个请求，其余排队；整体有个期限，到点就用已经拿到的结果，不会因为个别请求卡住而一无所获。
LIMITS = {"PanSou": (3, 30), "CloudSaver": (2, 60), "纸鸢磁力": (2, 45), "电影港": (1, 60), "海搜": (1, 60)}
DEADLINE = {1: 60, 2: 120, 3: 200}


async def _guard(sem, timeout, factory):
    async with sem:
        try:
            return await asyncio.wait_for(factory(), timeout)
        except asyncio.TimeoutError:
            raise
        except Exception:  # noqa — 偶发的错误（连接被重置、5xx）重试一次
            await asyncio.sleep(1)
            return await asyncio.wait_for(factory(), timeout)


async def gather_limited(jobs, deadline: float, limits=None):
    """jobs: [(来源名, 无参函数→协程)]，按来源限流并发、各自超时，整体期限到了就放弃没做完的。
    返回 (全部结果, {来源: {"total","ok","timeout","error","abandoned","items","errors"}})。"""
    limits = limits or LIMITS
    sems = {lb: asyncio.Semaphore(limits.get(lb, (3, 30))[0]) for lb, _ in jobs}
    tasks = [(lb, asyncio.create_task(_guard(sems[lb], limits.get(lb, (3, 30))[1], fn))) for lb, fn in jobs]
    if tasks:
        _, pending = await asyncio.wait([t for _, t in tasks], timeout=deadline)
    else:
        pending = set()
    for t in pending:
        t.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    out, stats = [], {}
    for lb, t in tasks:
        s = stats.setdefault(lb, {"total": 0, "ok": 0, "timeout": 0, "error": 0, "abandoned": 0, "items": 0, "errors": Counter()})
        s["total"] += 1
        if t in pending:
            s["abandoned"] += 1
        elif t.exception() is None:
            r = t.result()
            s["ok"] += 1
            if isinstance(r, list):
                out += r
                s["items"] += len(r)
        elif isinstance(t.exception(), asyncio.TimeoutError):
            s["timeout"] += 1
        else:
            s["error"] += 1
            s["errors"][type(t.exception()).__name__] += 1
    return out, stats


def _stat_note(lb: str, s: dict) -> str:
    bits = [f"{s['ok']}/{s['total']} 个请求成功"]
    if s["timeout"]:
        bits.append(f"{s['timeout']} 个超时")
    if s["abandoned"]:
        bits.append(f"{s['abandoned']} 个来不及做完被放弃")
    if s["error"]:
        bits.append(f"{s['error']} 个出错（{'、'.join(f'{k}×{v}' for k, v in s['errors'].items())}）")
    return f"{lb}：" + "，".join(bits) + f"，得到 {s['items']} 条"


async def search_all(meta: dict, media_type: str, seasons):
    """返回 (候选原始结果, 说明列表)。seasons：电视剧请求的季号列表（一次搜，不按季分开搜）。
    搜索深度（设置里选，或单次深度搜索）决定查询词数量、纸鸢磁力返回条数、是否绕过 PanSou 缓存。
    各来源限流并发、单个请求有超时、整体有期限；说明里会写每个来源成功/超时/出错各多少。"""
    level = depth()
    qs = queries(meta["names"], meta["year"], media_type, seasons, level)
    kite_limit = {1: 50, 2: 100, 3: 200}[level]
    n_cs, n_kite = {1: 3, 2: 5, 3: 8}[level], {1: 4, 2: 8, 3: 12}[level]
    notes, jobs = [], []
    csv = ks = None
    async with httpx.AsyncClient(timeout=30) as c:
        try:
            if cfg.PANSOU_URL:
                jobs += [("PanSou", lambda q=q: search_pansou(c, q, refresh=level >= 3)) for q in qs]
            else:
                notes.append("未配置 PanSou 地址，没有搜分享链接")
            if cloudsaver.configured():
                csv = cloudsaver.CloudSaver()
                jobs += [("CloudSaver", lambda q=q: _cloudsaver_search(csv, q)) for q in qs[:n_cs]]
            if cfg.DYG_ON and cfg.DYG_URL:
                jobs.append(("电影港", lambda: dyg.search_many(meta["names"], qs[:3], media_type)))
            if cfg.HAISOU_ON:
                jobs.append(("海搜", lambda: haisou.search_many(meta["names"], qs[:3])))
            if kite.configured():
                ks = kite.KiteSession()
                jobs += [("纸鸢磁力", lambda q=q: _kite_search(ks, q, kite_limit)) for q in qs[:n_kite]]
            out, stats = await gather_limited(jobs, DEADLINE[level])
        finally:
            if ks:
                await ks.close()
            if csv:
                await csv.close()
    notes += [_stat_note(lb, s) for lb, s in stats.items()]
    notes.append(f"搜索深度 {level}：{len(qs)} 个关键词，共 {len(out)} 条原始结果")
    return out, notes


def _tags(c: Candidate, meta: dict, res: int, per: float, rules: Rules, yr: int, hits: int) -> list[str]:
    tags = ["名称完整匹配"]
    if chinese_rank(c.title, meta["names"]) == 2:
        tags.append("中文片名")
    elif chinese_rank(c.title, meta["names"]) == 1:
        tags.append("含中文")
    tags.append({2: "年份吻合", 1: "年份相差一年"}.get(yr, "标题没写年份"))
    if res:
        tags.append(f"{res}p")
    if hits:
        tags.append(f"命中优先词×{hits}")
    if per and rules.pref_min <= per <= rules.pref_max:
        tags.append("体积合适")
    tags.append(("夸克分享" if c.provider == "quark" else "115 分享") if c.kind == "share" else "磁力")
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
            if len(report.setdefault("rejects", [])) < 400:
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
        partial = ""
        if media_type == "tv":
            if re.search(r"全集|合集|打包|完结|全\s*\d+\s*集|(?i:complete)", c.title):
                c.score += 8
            rng = re.search(r"(?<![\dSsEe第季.])(\d{1,3})\s*[-~至]\s*(\d{1,3})(?![\dpPkK季])", c.title)
            eps_total = meta.get("season_eps", {}).get(c.covers[0]) if c.covers else None
            eps_total = eps_total or meta.get("episodes", 0)
            if rng and eps_total >= 4 and int(rng.group(2)) > int(rng.group(1)) and int(rng.group(2)) - int(rng.group(1)) + 1 < eps_total * 0.8:
                c.score -= 30  # 只有其中几集（如 01-08），排在整包后面
                partial = f"仅第{rng.group(1)}-{rng.group(2)}集"
        rank = {2160: 2, 1080: 3, 720: 1}.get(res, 0)
        zh = chinese_rank(c.title, meta["names"])
        facts = {"chinese": zh, "year": yr, "quality": rank, "drive": int(cfg.DRIVE_PREFER in ("115", "quark") and c.provider == cfg.DRIVE_PREFER), "keywords": hits, "size": int(bool(per and rules.pref_min <= per <= rules.pref_max)),
                 "source": int(c.kind == "share"), "seeders": min(c.seeders, 50)}
        c._key = priority_tuple(cfg.PRIORITY, facts)
        c.tags = _tags(c, meta, res, per, rules, yr, hits) + ([f"覆盖第{'/'.join(map(str, c.covers))}季"] if media_type == "tv" else []) \
            + ([partial] if partial else [])
        out.append(c)
    if report is not None and report.get("rejects"):
        names = meta["names"]
        report["rejects"].sort(key=lambda x: (x["reason"] != "标题不匹配", similarity(x["title"], names)), reverse=True)
        del report["rejects"][150:]
    # 电视剧：覆盖请求季数越多越优先（多季合集优先，不是一季一季单独找）；然后按设置的优先级逐项比较，最后看综合分
    return sorted(out, key=lambda x: (len(x.covers), 0 if "仅第" in " ".join(x.tags) else 1, x._key, x.score), reverse=True)
