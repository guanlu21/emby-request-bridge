"""海搜（haisou.cc）：网盘分享搜索。已知接口（来自浏览器抓包，只拿到了地址，请求体和返回结构还没拿到）：
  POST /api/v2/shares/search           搜索
  POST /api/v2/shares/{id}/fetch       取某个分享的链接和提取码（页面上显示「获取后可见」，点了才请求）

因为不知道请求体和返回结构，这里做成「可调」的：搜索请求体用设置里的 JSON 模板（{kw} 代表关键词），
返回结构按常见字段名宽松解析；取链接那步不依赖字段名，直接在返回里找夸克/115 的分享链接和提取码。
默认关闭。设置页「测试海搜」会显示实际返回的前几百字，据此改模板即可。

注意：网页版给每个请求都带了一个 x-hs-client-context 请求头（每次不同的一长串，看起来是页面脚本生成的签名）。
本服务不会去模拟这个头，也不会使用你浏览器里的会话 Cookie——发的是普通请求。站点如果因此拒绝（401/403 等），
海搜就用不了，夸克资源请走 PanSou、CloudSaver、电影港。
"""
from __future__ import annotations

import asyncio
import json
import re

import httpx

from .config import cfg

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
QUARK = re.compile(r"https?://pan\.quark\.cn/s/[A-Za-z0-9]+(?:\?[^\s\"'<>\\]*)?")
S115 = re.compile(r"https?://(?:www\.)?(?:115|115cdn|anxia)\.com/s/[A-Za-z0-9]+(?:\?[^\s\"'<>\\]*)?")
ID_KEYS = ("id", "share_id", "shareId", "code", "key", "uid", "slug")
TITLE_KEYS = ("title", "name", "share_name", "shareName", "file_name", "fileName", "filename")
PLAT_KEYS = ("platform", "type", "pan", "source", "drive", "cloud", "disk", "storage", "provider")
SIZE_KEYS = ("size", "total_size", "totalSize", "file_size")
PWD_KEYS = ("pwd", "password", "passcode", "pass_code", "extract_code", "extractCode", "code")
DEFAULT_BODY = '{"query": "{kw}", "filters": {"scope": "title", "platforms": ["quark", "115"], "include_filtered": false, "exclude_same_file_hsids": []}, "pagination": {"page": 1, "page_size": 20}}'
OLD_DEFAULTS = {'{"keyword": "{kw}", "page": 1, "size": 30}'}  # 之前版本猜的请求体，已经按真实抓包换掉；设置里还留着旧默认值的自动用新的


def _base() -> str:
    u = (cfg.HAISOU_URL or "https://haisou.cc").strip().rstrip("/")
    return u if "://" in u else "https://" + u


def _fill(o, kw: str):
    if isinstance(o, str):
        return o.replace("{kw}", kw)
    if isinstance(o, list):
        return [_fill(x, kw) for x in o]
    if isinstance(o, dict):
        return {k: _fill(v, kw) for k, v in o.items()}
    return o


def build_body(template: str, kw: str, page: int = 1):
    if not template or template in OLD_DEFAULTS:
        template = DEFAULT_BODY
    try:
        body = _fill(json.loads(template), kw)
        if isinstance(body.get("pagination"), dict):
            body["pagination"]["page"] = page  # 翻页
        return body
    except ValueError:
        raise RuntimeError("「海搜搜索请求体」不是合法的 JSON（关键词位置写 {kw}）")


def _pick(d: dict, keys):
    for k in keys:
        if d.get(k) not in (None, "", [], {}):
            return d[k]
    return None


def platform_of(d: dict) -> str:
    v = str(_pick(d, PLAT_KEYS) or "").lower()
    if "quark" in v or "夸克" in v:
        return "quark"
    if "115" in v:
        return "115"
    return "other" if v else ""


def find_items(data) -> list[dict]:
    """在返回 JSON 里找「像分享条目」的字典：有 id 类字段（字符串）和标题类字段。"""
    out, seen = [], set()

    def walk(o):
        if isinstance(o, dict):
            sid, title = _pick(o, ID_KEYS), _pick(o, TITLE_KEYS)
            if isinstance(sid, str) and isinstance(title, str) and sid not in seen:
                seen.add(sid)
                size = _pick(o, SIZE_KEYS)
                out.append({"id": sid, "title": title.strip(), "platform": platform_of(o),
                            "size": float(size) if isinstance(size, (int, float)) else 0.0})
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(data)
    return out


def extract_links(data) -> list[dict]:
    """取链接那步：不依赖字段名，在整个返回里找夸克/115 分享链接，并找提取码。"""
    text = json.dumps(data, ensure_ascii=False).replace("\\/", "/")
    pwd = ""

    def find_pwd(o):
        nonlocal pwd
        if isinstance(o, dict):
            for k in PWD_KEYS:
                v = o.get(k)
                if isinstance(v, str) and 3 <= len(v) <= 8 and re.fullmatch(r"[A-Za-z0-9]+", v) and not pwd:
                    pwd = v
            for v in o.values():
                find_pwd(v)
        elif isinstance(o, list):
            for v in o:
                find_pwd(v)
    find_pwd(data)
    out = []
    for rx, provider in ((QUARK, "quark"), (S115, "115")):
        for m in rx.finditer(text):
            out.append({"url": m.group(0), "provider": provider, "password": pwd})
    return out


def _headers() -> dict:
    return {"User-Agent": UA, "Content-Type": "application/json", "Accept": "application/json",
            "Referer": _base() + "/", "Origin": _base()}


async def _search(c: httpx.AsyncClient, kw: str, page: int = 1):
    r = await c.post(_base() + "/api/v2/shares/search", json=build_body(cfg.HAISOU_BODY, kw, page), headers=_headers())
    try:
        return r.status_code, r.json(), r.text
    except Exception:  # noqa
        return r.status_code, None, r.text


async def _fetch(c: httpx.AsyncClient, sid: str):
    body = json.loads(cfg.HAISOU_FETCH_BODY or "{}")
    r = await c.post(f"{_base()}/api/v2/shares/{sid}/fetch", json=body, headers=_headers())
    try:
        return r.status_code, r.json(), r.text
    except Exception:  # noqa
        return r.status_code, None, r.text


def sources_depth() -> int:
    from .sources import depth
    return depth()


async def search_many(names: list[str], queries: list[str]):
    """搜若干个关键词 → 过滤出名称完整匹配的夸克/115 分享 → 逐个取链接 → Candidate 列表。"""
    from .filters import name_hit
    from .sources import Candidate
    shares = {}
    async with httpx.AsyncClient(timeout=25, follow_redirects=True) as c:
        for q in dict.fromkeys(queries):
            for page in range(1, {1: 1, 2: 2, 3: 3}[sources_depth()] + 1):
                status, data, _ = await _search(c, q, page)
                if status != 200 or data is None:
                    raise RuntimeError(f"海搜搜索返回 HTTP {status}（页面脚本会给每个请求带一个签名头 x-hs-client-context，本服务不带它，站点可能因此拒绝）")
                found = find_items(data)
                for it in found:
                    if it["platform"] in ("", "quark", "115") and name_hit(it["title"], names):
                        shares.setdefault(it["id"], it)
                if len(found) < 20:
                    break
        sem = asyncio.Semaphore(3)

        async def one(it):
            async with sem:
                status, data, _ = await _fetch(c, it["id"])
            if status != 200 or data is None:
                return []
            return [Candidate("share", it["title"], l["url"], l["password"], size=it["size"], src="haisou", provider=l["provider"])
                    for l in extract_links(data)]
        got = await asyncio.gather(*[one(it) for it in list(shares.values())[:12]], return_exceptions=True)
    return [c for g in got if isinstance(g, list) for c in g]


async def test() -> dict:
    out = {"ok": True, "url": _base() + "/api/v2/shares/search"}
    async with httpx.AsyncClient(timeout=25, follow_redirects=True) as c:
        try:
            status, data, text = await _search(c, "流浪地球")
        except Exception as e:  # noqa
            return {"ok": False, "error": f"{type(e).__name__}: {e}"[:200]}
        items = find_items(data) if data is not None else []
        out.update(status=status, raw=text[:600], parsed=len(items), sample=[f"{i['title'][:30]}（{i['platform'] or '平台未知'}）" for i in items[:3]])
        target = next((i for i in items if i["platform"] in ("", "quark", "115")), None)
        if target:
            try:
                s2, d2, t2 = await _fetch(c, target["id"])
                links = extract_links(d2) if d2 is not None else []
                out.update(fetch_status=s2, fetch_raw=t2[:400], links=[l["url"] for l in links[:3]])
            except Exception as e:  # noqa
                out.update(fetch_error=f"{type(e).__name__}: {e}"[:150])
    return out
