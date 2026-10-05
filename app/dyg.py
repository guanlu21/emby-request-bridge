"""电影港（dyg7.com，帝国 CMS 站）：先搜影片页，再从影片页里取磁力 / 夸克 / 115 链接。

影片页（已对照真实页面核对）：「【下载地址】」下面有多行「磁力：[标签](magnet:?xt=urn:btih:…&xl=字节数)」，
以及「夸克云盘链接：https://pan.quark.cn/s/…」等。标签里常写明分辨率、国语中字、无水印、全集打包；
页面里还有「◎年 代」。这些都会拼进候选标题，让后面的名称/年份/关键词筛选能用上。

搜索：帝国 CMS 的标准搜索是 POST /e/search/index.php（keyboard、show、tempid、tbname）。站点用的表名我无法确认，
所以依次试几个常见的；失败只记一条提示，不影响其它搜索源。设置页「测试电影港」会显示每次尝试的结果。
"""
from __future__ import annotations

import asyncio
import html as htmllib
import re
from urllib.parse import parse_qs, urljoin

import httpx

from .config import cfg

TBNAMES = ("news", "movie", "article")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
MAGNET_A = re.compile(r"<a[^>]*href=[\"'](magnet:\?xt=urn:btih:[^\"']+)[\"'][^>]*>(.*?)</a>", re.S | re.I)
MAGNET_ANY = re.compile(r"magnet:\?xt=urn:btih:([0-9a-fA-F]{40}|[A-Za-z2-7]{32})[^\s\"'<>]*")
QUARK = re.compile(r"https?://pan\.quark\.cn/s/[A-Za-z0-9]+(?:\?[^\s\"'<>]*)?")
S115 = re.compile(r"https?://(?:www\.)?(?:115|115cdn|anxia)\.com/s/[A-Za-z0-9]+(?:\?[^\s\"'<>]*)?")
DETAIL_A = re.compile(r"<a[^>]+href=[\"']([^\"']*/(?:dy|dsj|dongman|zyjm|duanju)/[A-Za-z0-9/]*?\d+\.html)[\"'][^>]*>(.*?)</a>", re.S | re.I)
TITLE = re.compile(r"<title>(.*?)</title>", re.S | re.I)


def _base() -> str:
    u = (cfg.DYG_URL or "").strip().rstrip("/")
    return u if not u or "://" in u else "https://" + u


def _text(s: str) -> str:
    return re.sub(r"\s+", " ", htmllib.unescape(re.sub(r"<[^>]+>", " ", s))).strip()


def result_links(page: str, base: str) -> list[tuple[str, str]]:
    """搜索结果页里的影片页链接 → [(标题, 绝对地址)]（按出现顺序去重）。"""
    out, seen = [], set()
    for href, inner in DETAIL_A.findall(page):
        url = urljoin(base + "/", htmllib.unescape(href))
        title = _text(inner)
        if url not in seen and title:
            seen.add(url)
            out.append((title, url))
    return out


def parse_page(page: str):
    """影片页 → (标题, 年份, [{kind, url, label, size, provider}])。"""
    m = TITLE.search(page)
    title = _text(m.group(1)).split("_")[0] if m else ""
    ym = re.search(r"年\s*代[:：]?\s*((?:19|20)\d{2})", _text(page))
    items, seen = [], set()
    for href, inner in MAGNET_A.findall(page):
        mag = htmllib.unescape(href)
        h = MAGNET_ANY.search(mag).group(1).lower()
        if h in seen:
            continue
        seen.add(h)
        qs = parse_qs(mag.split("?", 1)[1]) if "?" in mag else {}
        size = float((qs.get("xl") or ["0"])[0] or 0)
        items.append({"kind": "magnet", "url": mag, "label": _text(inner), "size": size, "provider": "115"})
    for m2 in MAGNET_ANY.finditer(page):  # 不在 <a> 里的裸磁力
        h = m2.group(1).lower()
        if h not in seen:
            seen.add(h)
            items.append({"kind": "magnet", "url": htmllib.unescape(m2.group(0)), "label": "", "size": 0, "provider": "115"})
    plain = htmllib.unescape(re.sub(r"<br\s*/?>|</p>|</div>|</li>", "\n", page, flags=re.I))
    plain = re.sub(r"<[^>]+>", "", plain)
    for rx, provider in ((QUARK, "quark"), (S115, "115")):
        for m3 in rx.finditer(plain):
            url = m3.group(0)
            if any(i["url"] == url for i in items):
                continue
            before = plain[max(0, m3.start() - 160): m3.start()].splitlines()
            ctx = next((ln.strip() for ln in reversed(before) if ln.strip() and "链接" not in ln), "")  # 链接上面那行说明（如 01-30全集）
            items.append({"kind": "share", "url": url, "label": ctx[:40], "size": 0, "provider": provider})
    return title, (ym.group(1) if ym else ""), items


async def _search_pages(c: httpx.AsyncClient, kw: str):
    base, attempts = _base(), []
    for tb in TBNAMES:
        try:
            r = await c.post(base + "/e/search/index.php", data={"keyboard": kw, "show": "title", "tempid": "1", "tbname": tb, "mid": "1"})
        except Exception as e:  # noqa
            attempts.append(f"表 {tb}：{type(e).__name__}")
            continue
        links = result_links(r.text, base)
        note = "（提示：搜索有时间间隔限制）" if "间隔" in r.text and not links else ""
        attempts.append(f"表 {tb}：HTTP {r.status_code}，{len(links)} 个影片页{note}")
        if links:
            return links, attempts
    return [], attempts


def _candidates(title: str, year: str, items: list[dict], url: str):
    from .sources import Candidate
    head = f"{title} ({year})" if year and year not in title else title
    return [Candidate(i["kind"], f"{head} {i['label']}".strip(), i["url"], size=i["size"], src="dyg7", provider=i["provider"])
            for i in items]


async def search_many(names: list[str], queries: list[str], media_type: str):
    from .filters import name_hit
    out, pages, notes = [], {}, []
    async with httpx.AsyncClient(timeout=20, follow_redirects=True, headers={"User-Agent": UA}) as c:
        res = await asyncio.gather(*[_search_pages(c, q) for q in dict.fromkeys(queries)], return_exceptions=True)
        for r in res:
            if isinstance(r, tuple):
                for title, url in r[0]:
                    if name_hit(title, names):
                        pages.setdefault(url, title)
        sem = asyncio.Semaphore(4)

        async def one(url):
            async with sem:
                r = await c.get(url)
            title, year, items = parse_page(r.text)
            return _candidates(title or pages[url], year, items, url)
        got = await asyncio.gather(*[one(u) for u in list(pages)[:8]], return_exceptions=True)
        for g in got:
            if isinstance(g, list):
                out += g
    return out


async def test() -> dict:
    base = _base()
    async with httpx.AsyncClient(timeout=20, follow_redirects=True, headers={"User-Agent": UA}) as c:
        info = {"ok": True}
        try:
            links, attempts = await _search_pages(c, "流浪地球")
            info.update(search_ok=bool(links), attempts=attempts, pages=[t for t, _ in links[:5]])
        except Exception as e:  # noqa
            info.update(search_ok=False, attempts=[f"{type(e).__name__}: {e}"], pages=[])
        try:  # 影片页解析：用搜到的第一页，搜不到就用首页上的任意一个影片页
            target = links[0][1] if info.get("search_ok") else None
            if not target:
                home = await c.get(base + "/")
                hl = result_links(home.text, base)
                target = hl[0][1] if hl else None
            if target:
                r = await c.get(target)
                title, year, items = parse_page(r.text)
                info.update(page=target, page_title=title, page_year=year,
                            magnets=sum(1 for i in items if i["kind"] == "magnet"),
                            quark=sum(1 for i in items if i["provider"] == "quark"), s115=sum(1 for i in items if i["kind"] == "share" and i["provider"] == "115"),
                            sample=(items[0]["label"] if items else ""))
        except Exception as e:  # noqa
            info.update(page_error=f"{type(e).__name__}: {e}")
        return info
