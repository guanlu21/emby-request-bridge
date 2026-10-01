"""纸鸢磁力（KiteSearch）：它把多个国内磁力站按自定义规则聚合在一起，并提供 MCP（Streamable HTTP）接口。
本模块当作一个磁力搜索源来用：调用它的 magnet_search 工具。

鉴权：Authorization: Bearer <MCP Token>（在纸鸢磁力的「MCP」页面生成）。
工具返回格式我没拿到官方文档，所以结果解析做得比较宽松（JSON 或文本都尝试）。
"""
from __future__ import annotations

import asyncio
import json
import re

import httpx

from .config import cfg

MAGNET_RE = re.compile(r"magnet:\?xt=urn:btih:[0-9a-zA-Z]{32,40}[^\s)\]\"'<>]*")
SIZE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(TB|TiB|GB|GiB|MB|MiB|KB|KiB|G|M|T)\b", re.I)
UNIT = {"kb": 1024, "kib": 1024, "mb": 1024 ** 2, "mib": 1024 ** 2, "m": 1024 ** 2, "gb": 1024 ** 3,
        "gib": 1024 ** 3, "g": 1024 ** 3, "tb": 1024 ** 4, "tib": 1024 ** 4, "t": 1024 ** 4}


def parse_size(v) -> float:
    if isinstance(v, (int, float)):
        return float(v)
    m = SIZE_RE.search(str(v or ""))
    return float(m.group(1)) * UNIT[m.group(2).lower()] if m else 0.0


def _pick(d: dict, *keys):
    for k in keys:
        if d.get(k) not in (None, ""):
            return d[k]
    return None


def _from_items(items) -> list[dict]:
    out = []
    for it in items:
        if not isinstance(it, dict):
            continue
        mag = _pick(it, "magnet", "magnet_link", "magnetLink", "url", "link", "磁力")
        if not (isinstance(mag, str) and mag.startswith("magnet:")):
            hit = MAGNET_RE.search(json.dumps(it, ensure_ascii=False))
            mag = hit.group(0) if hit else ""
        if not mag:
            continue
        out.append({"title": str(_pick(it, "title", "name", "标题", "名称") or ""), "magnet": mag,
                    "size": parse_size(_pick(it, "size", "大小", "file_size", "total_size")),
                    "seeders": int(_pick(it, "seeders", "seeds", "seeder", "做种") or 0)})
    return out


def parse_results(text: str) -> list[dict]:
    """把工具返回的文本解析成 [{title, magnet, size, seeders}]。"""
    text = (text or "").strip()
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            data = next((data[k] for k in ("results", "items", "data", "list", "result") if isinstance(data.get(k), list)), [data])
        if isinstance(data, list):
            res = _from_items(data)
            if res:
                return res
    except ValueError:
        pass
    # 文本/Markdown：按磁力链接切块，标题取磁力前面最近的一行非空文字
    out, last = [], 0
    for m in MAGNET_RE.finditer(text):
        before = text[last:m.start()]
        block = before.strip().splitlines()
        title = ""
        for line in reversed(block):
            line = re.sub(r"^[\s>*#\-\d.、)]+", "", line).strip(" *`|")
            if line and not MAGNET_RE.search(line) and not re.fullmatch(r"(磁力|magnet|链接|大小|size)[:：]?.*", line, re.I):
                title = line
                break
        near = text[max(0, m.start() - 160): m.end() + 160]
        out.append({"title": title, "magnet": m.group(0), "size": parse_size(near), "seeders": 0})
        last = m.end()
    return out


def _unwrap(r: httpx.Response, rid: int):
    """Streamable HTTP：响应可能是 JSON，也可能是 SSE（data: 行）。取出 id 对应的 JSON-RPC 消息。"""
    ctype = r.headers.get("content-type", "")
    if "text/event-stream" in ctype:
        for line in r.text.splitlines():
            if line.startswith("data:"):
                try:
                    msg = json.loads(line[5:].strip())
                except ValueError:
                    continue
                if msg.get("id") == rid:
                    return msg
        raise RuntimeError("没有收到 MCP 响应")
    return r.json()


class KiteSession:
    def __init__(self):
        self._c = httpx.AsyncClient(timeout=40)
        self._sid, self._ready, self._lk, self._n = "", False, asyncio.Lock(), 0

    async def close(self):
        await self._c.aclose()

    def _headers(self):
        h = {"Authorization": "Bearer " + cfg.KITE_TOKEN, "Content-Type": "application/json",
             "Accept": "application/json, text/event-stream"}
        if self._sid:
            h["Mcp-Session-Id"] = self._sid
        return h

    async def _rpc(self, method: str, params: dict, notify: bool = False):
        self._n += 1
        body = {"jsonrpc": "2.0", "method": method, "params": params}
        if not notify:
            body["id"] = self._n
        r = await self._c.post(cfg.KITE_URL, json=body, headers=self._headers())
        if r.status_code in (401, 403):
            raise RuntimeError("MCP Token 无效或已被撤销")
        if notify:
            return None
        if r.status_code >= 400:
            raise RuntimeError(f"HTTP {r.status_code}")
        msg = _unwrap(r, self._n)
        if msg.get("error"):
            raise RuntimeError(str(msg["error"].get("message") or msg["error"])[:150])
        if method == "initialize":
            self._sid = r.headers.get("mcp-session-id", "") or self._sid
        return msg.get("result") or {}

    async def _init(self):
        async with self._lk:
            if self._ready:
                return
            try:
                await self._rpc("initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
                                               "clientInfo": {"name": "request-bridge", "version": "1.0"}})
                await self._rpc("notifications/initialized", {}, notify=True)
            except RuntimeError as e:
                if "Token" in str(e):
                    raise  # 无状态服务可能不需要 initialize；只有鉴权失败才算错
            self._ready = True

    async def tools(self) -> list[str]:
        await self._init()
        return [t.get("name", "") for t in (await self._rpc("tools/list", {})).get("tools", [])]

    async def search(self, query: str, limit: int = 50) -> list[dict]:
        await self._init()
        res = await self._rpc("tools/call", {"name": "magnet_search", "arguments": {"query": query, "limit": limit}})
        if res.get("isError"):
            raise RuntimeError("magnet_search 返回错误：" + json.dumps(res.get("content"), ensure_ascii=False)[:150])
        sc = res.get("structuredContent")
        if sc:
            items = sc if isinstance(sc, list) else next((sc[k] for k in ("results", "items", "data", "list") if isinstance(sc.get(k), list)), [])
            parsed = _from_items(items)
            if parsed:
                return parsed
        text = "\n".join(c.get("text", "") for c in res.get("content", []) if c.get("type") == "text")
        return parse_results(text)


def configured() -> bool:
    return bool(cfg.KITE_URL and cfg.KITE_TOKEN)


async def test() -> dict:
    s = KiteSession()
    try:
        tools = await s.tools()
        sample = await s.search("流浪地球", 5)
        return {"ok": True, "tools": tools, "sample_count": len(sample), "first": (sample[0]["title"] if sample else "")}
    except Exception as e:  # noqa
        return {"ok": False, "error": f"{type(e).__name__}: {e}"[:200]}
    finally:
        await s.close()
