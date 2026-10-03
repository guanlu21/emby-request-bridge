"""CloudSaver（网盘资源搜索，聚合 Telegram 频道等来源的分享链接）作为 115 分享链接的搜索源。

CloudSaver 没有公开的接口文档，这里按它常见的用法写：
  登录  POST {地址}/user/login   {"username","password"} → token（也试 /api/user/login）
  搜索  GET  {地址}/api/search?keyword=…   Authorization: Bearer <token>
返回结构解析做得比较宽松（找带 cloudLinks 的条目）；只取 115 的分享链接。
"""
from __future__ import annotations

import asyncio
import re

import httpx

from .config import cfg

SHARE_115 = re.compile(r"https?://(?:www\.)?(?:115|115cdn|anxia)\.com/s/[A-Za-z0-9]+[^\s\"'<>)\]]*")
PWD_RE = re.compile(r"(?:password|pwd|访问码|提取码|密码)\s*[=:：]?\s*([A-Za-z0-9]{4})", re.I)


def _base() -> str:
    u = (cfg.CLOUDSAVER_URL or "").strip().rstrip("/")
    return u if not u or "://" in u else "http://" + u


def configured() -> bool:
    return bool(cfg.CLOUDSAVER_URL and cfg.CLOUDSAVER_USER)


def find_token(o):
    if isinstance(o, dict):
        for k in ("token", "access_token", "accessToken"):
            if isinstance(o.get(k), str) and o[k]:
                return o[k]
        for v in o.values():
            t = find_token(v)
            if t:
                return t
    elif isinstance(o, list):
        for v in o:
            t = find_token(v)
            if t:
                return t
    return ""


def _walk(o):
    if isinstance(o, dict):
        if any(k in o for k in ("cloudLinks", "cloudLink", "links")):
            yield o
        for v in o.values():
            yield from _walk(v)
    elif isinstance(o, list):
        for v in o:
            yield from _walk(v)


def parse_results(data) -> list[dict]:
    """→ [{title, text, url, password}]，只保留 115 分享链接。"""
    out, seen = [], set()
    for it in _walk(data):
        title = str(it.get("title") or "").strip()
        content = str(it.get("content") or it.get("description") or "")
        links = it.get("cloudLinks") or it.get("cloudLink") or it.get("links") or []
        if isinstance(links, (str, dict)):
            links = [links]
        for l in links:
            raw = l if isinstance(l, str) else str(l.get("link") or l.get("url") or "")
            m = SHARE_115.search(raw)
            if not m or m.group(0) in seen:
                continue
            seen.add(m.group(0))
            pwd = (l.get("password") or l.get("pwd") or "") if isinstance(l, dict) else ""
            if not pwd:
                pm = PWD_RE.search(raw) or PWD_RE.search(content)
                pwd = pm.group(1) if pm else ""
            out.append({"title": title or content.strip().splitlines()[0][:80] if (title or content.strip()) else "",
                        "text": content[:300], "url": m.group(0), "password": pwd})
    return [x for x in out if x["title"]]


class CloudSaver:
    """登录/搜索接口路径各试几种：CloudSaver 的前端页面和接口常在同一个端口，
    /user/login 这类路径可能被前端的静态服务接走（返回 405/404 或 HTML），真正的接口在 /api/ 下。"""

    LOGIN_PATHS = ("/api/user/login", "/user/login")
    SEARCH_PATHS = ("/api/search", "/search")

    def __init__(self):
        self._c = httpx.AsyncClient(timeout=60)
        self._tok, self._lk, self._search_path = "", asyncio.Lock(), ""

    async def close(self):
        await self._c.aclose()

    async def _login(self):
        async with self._lk:
            if self._tok:
                return
            tried = []
            for path in self.LOGIN_PATHS:
                r = await self._c.post(_base() + path, json={"username": cfg.CLOUDSAVER_USER, "password": cfg.CLOUDSAVER_PASS})
                try:
                    j = r.json()
                except Exception:  # noqa
                    tried.append(f"{path}→HTTP {r.status_code}（不是 JSON）")
                    continue
                tok = find_token(j)
                if tok:
                    self._tok = tok
                    return
                if r.status_code in (404, 405):
                    tried.append(f"{path}→HTTP {r.status_code}")
                    continue
                raise RuntimeError("CloudSaver 登录失败：" + str(j.get("message") or j)[:100] + "（检查用户名和密码）")
            raise RuntimeError("找不到可用的 CloudSaver 登录接口，试过：" + "；".join(tried) + "。请确认地址指向 CloudSaver")

    async def _get(self, keyword: str):
        paths = [self._search_path] if self._search_path else list(self.SEARCH_PATHS)
        tried = []
        for path in paths:
            r = await self._c.get(_base() + path, params={"keyword": keyword}, headers={"Authorization": "Bearer " + self._tok})
            if r.status_code in (401, 403):
                return r, path
            if r.status_code in (404, 405):
                tried.append(f"{path}→HTTP {r.status_code}")
                continue
            try:
                r.json()
            except Exception:  # noqa
                tried.append(f"{path}→HTTP {r.status_code}（不是 JSON）")
                continue
            self._search_path = path
            return r, path
        raise RuntimeError("找不到 CloudSaver 搜索接口，试过：" + "；".join(tried))

    async def search(self, keyword: str) -> list[dict]:
        if not self._tok:
            await self._login()
        for attempt in (0, 1):
            r, _ = await self._get(keyword)
            if r.status_code in (401, 403) and attempt == 0:
                self._tok = ""
                await self._login()
                continue
            r.raise_for_status()
            return parse_results(r.json())
        return []


async def test() -> dict:
    cs = CloudSaver()
    try:
        res = await cs.search("流浪地球")
        return {"ok": True, "count": len(res), "first": res[0]["title"] if res else ""}
    except Exception as e:  # noqa
        return {"ok": False, "error": f"{type(e).__name__}: {e}"[:200]}
    finally:
        await cs.close()
