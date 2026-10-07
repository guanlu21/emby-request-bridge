"""按「纸鸢磁力」的规则文件，由本服务自己去请求磁力站（磁力帝、BitSearch…）。

纸鸢磁力的这类引擎是在浏览器里按规则请求各站的，服务器上的 MCP 接口拿不到，所以这里直接解释同一份规则：
  request：method、url（相对引擎地址或绝对地址，%SearchKey% %Page% %Offset%）、headers、payloadType/payloadTemplate
  parser ：responseType（html 用 CSS 选择器 / json 用点号路径）、listPointer、itemFields{src, action(text|attr:X|value), template}
  nextStep：列表里没有磁力时，再请求详情页取（%detailLink%）
规则文件：优先 /data/rules.json（你自己导出的，可追加/覆盖），否则用自带的（已去掉成人类引擎）。成人类（porn 标签）引擎一律不使用。
"""
from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path
from urllib.parse import quote, urljoin, urlsplit

import httpx

from . import settings
from .config import cfg
from .kite import parse_size

BUNDLED = Path(__file__).parent / "data" / "rules.json"
BAD_TAGS = {"porn", "adult", "av", "18+", "xxx", "r18"}
HASH = re.compile(r"(?<![0-9a-zA-Z])([0-9a-fA-F]{40}|[A-Za-z2-7]{32})(?![0-9a-zA-Z])")
DROP_HEADERS = ("sec-", "priority", "upgrade-insecure", "cache-control")


def load_rules() -> dict:
    """{引擎名: 规则}。用户的 /data/rules.json 优先；成人类和已停用的引擎不要。"""
    out = {}
    for p in (Path(os.path.dirname(cfg.DB_PATH) or ".") / "rules.json", BUNDLED):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:  # noqa
            continue
        for e in data.get("regulars", []):
            tags = {str(t).lower() for t in e.get("engineTags", [])}
            if tags & BAD_TAGS or e.get("engineStatus") == "disable":
                continue
            out.setdefault(e["engineName"], e)
    return out


def selected() -> list[str]:
    names = [n.strip() for n in re.split(r"[,，;；\n]+", cfg.RULE_ENGINES or "") if n.strip()]
    rules = load_rules()
    return [n for n in names if n in rules]


def configured() -> bool:
    return bool(selected())


# ---------------------------------------------------------------- 请求
def _json_escape(s: str) -> str:
    return json.dumps(s, ensure_ascii=False)[1:-1]


def build_request(rule: dict, key: str, page: int = 0):
    """→ (method, url, headers, json|None)。page 从 navigation.pageStart 起。"""
    sc = rule["stepConfig"]
    rq = sc["request"]
    nav = sc.get("navigation") or sc.get("parser", {}).get("navigation") or {}
    pg = (nav.get("pageStart") or rule.get("DefaultPage", 1)) + page
    offset = page * (nav.get("pageSize") or 200)
    sub = lambda s, js=False: (s.replace("%SearchKey%", _json_escape(key) if js else quote(key, safe=""))
                               .replace("%Page%", str(pg)).replace("%Offset%", str(offset)))
    url = sub(rq["url"])
    if not url.startswith("http"):
        url = rule["engineUrl"].rstrip("/") + "/" + url.lstrip("/")
    headers = {k: v for k, v in (rq.get("headers") or {}).items() if not k.lower().startswith(DROP_HEADERS)}
    body = None
    if rq.get("payloadType") == "json" and (rq.get("payloadTemplate") or "").strip():
        raw = sub(rq["payloadTemplate"], True)
        try:
            body = json.loads(raw)
        except ValueError:
            body = None
    return rq.get("method", "GET").upper(), url, headers, body


# ---------------------------------------------------------------- 解析
def _path(o, dotted: str):
    for part in [p for p in (dotted or "").split(".") if p]:
        if isinstance(o, dict):
            o = o.get(part)
        elif isinstance(o, list) and part.isdigit() and int(part) < len(o):
            o = o[int(part)]
        else:
            return None
    return o


def _apply(raw, spec: dict):
    """取出的原始值 → 套模板（%btih% 取其中的哈希，%value% 就是原值）。"""
    if raw is None:
        return None
    raw = str(raw).strip()
    tpl = spec.get("template")
    if not tpl:
        return raw or None
    if "%btih%" in tpl:
        m = HASH.search(raw)
        return tpl.replace("%btih%", m.group(1)) if m else None
    return tpl.replace("%value%", raw) if raw else None


def _html_field(node, spec: dict):
    el = node.select_one(spec["src"]) if spec.get("src") else node
    if el is None:
        return None
    act = spec.get("action", "text")
    if act == "text":
        raw = el.get_text(" ", strip=True)
    elif act.startswith("attr:"):
        raw = el.get(act[5:])
        raw = " ".join(raw) if isinstance(raw, list) else raw
    else:
        raw = el.get_text(" ", strip=True)
    return _apply(raw, spec)


def _soup(text: str):
    from bs4 import BeautifulSoup
    try:
        return BeautifulSoup(text, "lxml")
    except Exception:  # noqa
        return BeautifulSoup(text, "html.parser")


def parse_list(rule: dict, text: str) -> list[dict]:
    """搜索结果页 → [{title, magnet, size, detailLink, ...}]（字段取不到的就是 None）。"""
    parser = rule["stepConfig"]["parser"]
    fields = parser.get("itemFields", {})
    rows = []
    if parser.get("responseType") == "json":
        try:
            data = json.loads(text)
        except ValueError:
            return []
        lst = _path(data, parser.get("listPointer", ""))
        for it in lst if isinstance(lst, list) else []:
            row = {}
            for name, spec in fields.items():
                v = _path(it, spec.get("src", ""))
                row[name] = _apply(v if not isinstance(v, (dict, list)) else None, spec)
            rows.append(row)
    else:
        soup = _soup(text)
        for node in soup.select(parser.get("listPointer", "body")):
            rows.append({name: _html_field(node, spec) for name, spec in fields.items()})
    return rows


def parse_detail(step: dict, text: str) -> dict:
    """nextStep 的详情页 → 字段（如 magnet）。"""
    parser = step.get("parser", {})
    fields = parser.get("itemFields", {})
    if parser.get("responseType") == "json":
        try:
            data = json.loads(text)
        except ValueError:
            return {}
        return {n: _apply(_path(data, s.get("src", "")), s) for n, s in fields.items()}
    soup = _soup(text)
    return {n: _html_field(soup, s) for n, s in fields.items()}


# ---------------------------------------------------------------- 搜索
def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=20, follow_redirects=True, proxy=settings.proxy() if cfg.RULES_PROXY else None)


async def _request(c: httpx.AsyncClient, method, url, headers, body):
    if method == "POST":
        return await c.post(url, headers=headers, json=body) if body is not None else await c.post(url, headers=headers)
    return await c.get(url, headers=headers)


async def search(engine: str, key: str, page: int, names: list[str], max_detail: int = 6):
    """搜一个引擎的一页 → Candidate 列表。标题名称完整匹配的才会去取详情页（省请求）。"""
    from .filters import name_hit
    from .sources import Candidate
    rule = load_rules()[engine]
    method, url, headers, body = build_request(rule, key, page)
    out = []
    async with _client() as c:
        r = await _request(c, method, url, headers, body)
        if r.status_code != 200:
            raise RuntimeError(f"{engine} 返回 HTTP {r.status_code}")
        rows = parse_list(rule, r.text)
        step = rule["stepConfig"]["parser"].get("nextStep") or {}
        fetched = 0
        for row in rows:
            title = row.get("title") or ""
            if not title or not name_hit(title, names):
                continue
            if not row.get("magnet") and step.get("request") and row.get("detailLink") and fetched < max_detail:
                fetched += 1
                u2 = urljoin(rule["engineUrl"].rstrip("/") + "/", row["detailLink"])
                try:
                    d = await _request(c, "GET", u2, headers, None)
                    row.update({k: v for k, v in parse_detail(step, d.text).items() if v})
                except Exception:  # noqa
                    continue
            mag = row.get("magnet") or ""
            if not mag.startswith("magnet:"):
                continue
            out.append(Candidate("magnet", title, mag, size=parse_size(row.get("size") or ""), src=engine))
    return out


async def test() -> dict:
    rules = load_rules()
    res = []
    for name in selected():
        try:
            method, url, headers, body = build_request(rules[name], "流浪地球", 0)
            async with _client() as c:
                r = await _request(c, method, url, headers, body)
            rows = parse_list(rules[name], r.text) if r.status_code == 200 else []
            with_magnet = sum(1 for x in rows if x.get("magnet"))
            res.append({"engine": name, "status": r.status_code, "items": len(rows), "magnets": with_magnet,
                        "first": (rows[0].get("title") or "")[:40] if rows else "", "url": url})
        except Exception as e:  # noqa
            res.append({"engine": name, "error": f"{type(e).__name__}: {e}"[:150]})
    return {"ok": True, "available": sorted(rules), "selected": selected(), "results": res}
