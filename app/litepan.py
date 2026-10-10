"""对接 LitePan「自动联动」：文件落进正式目录后，POST 一个第三方通知，由它的联动完成
刷新目录 → STRM → 刮削 → Emby 扫库。

接口（与 LitePan 联动页「第三方通知」弹窗里的「调用方式」一致）：
  POST {LitePan 地址}/api/open/automation/events
  Authorization: Bearer lpk_api_xxx      # 系统设置 → API 秘钥
  {"event": "<通知名称>", "message": "...", "source": "<通知来源，可选>"}

只处理新增文件所在的目录：「联动来源」里可以写 {category}，按分类（电影-国产、电视剧-国产剧、动漫…）
发出不同的来源，在 LitePan 里为每个分类建一条联动，各自只扫对应目录。
"""
import asyncio
import json

import httpx

from . import classify, db
from .config import cfg

ENDPOINT = "/api/open/automation/events"
_timers: dict = {}
_rids: dict = {}


def _base() -> str:
    u = (cfg.LITEPAN_URL or "").strip().rstrip("/")
    if u.endswith(ENDPOINT):
        u = u[: -len(ENDPOINT)]
    return u if not u or "://" in u else "http://" + u


def configured() -> bool:
    return bool(cfg.LITEPAN_URL and cfg.LITEPAN_KEY)


def source_for(category: str) -> str:
    """联动来源模板里的 {category} 换成分类名；模板里没有 {category} 就所有分类用同一个来源。
    115 和夸克分开联动：夸克入库的 category 带「夸克-」前缀，模板没写 {category} 时也按前缀补上，
    保证夸克和 115 走 LitePan 里不同的联动。"""
    s = (cfg.LITEPAN_SOURCE or "").strip() or "RequestBridge"  # 留空时用默认基础名，保证来源和 LitePan 联动里的填写值能对上
    if "{category}" not in s:
        tag = "夸克-" if (category or "").startswith("夸克") else ""
        return (tag + s).strip("-")
    return s.replace("{category}", category or "").strip("-")


def _drives() -> list[tuple[str, str]]:
    """启用了哪些网盘 → [(来源里的前缀, 目录路径里的前缀)]。115 是默认的，夸克的来源前面加「夸克-」。"""
    out = [("", "")]
    try:
        if cfg.QUARK_COOKIE and cfg.QUARK_LIBRARY_FID:
            out.append(("夸克-", "夸克:"))
    except AttributeError:
        pass
    return out


def all_sources() -> list[str]:
    """当前设置下会用到的全部联动来源（给你在 LitePan 里逐个建联动时对照）。"""
    return list(dict.fromkeys(x["source"] for x in source_table()))


def source_table() -> list[dict]:
    """每个来源对应的分类、以及 LitePan 里那条联动的 STRM 任务应该扫描的目录路径（影视根目录/分类目录）。"""
    try:
        root = cfg.LIBRARY_ROOT_LABEL or "影视根目录"
    except AttributeError:
        root = "影视根目录"
    try:
        qroot = cfg.QUARK_LIBRARY_LABEL or "影视根目录"
    except AttributeError:
        qroot = "影视根目录"
    cats = [[cfg.DIR_MOVIE, r] for r in classify.region_names()] + \
           [[cfg.DIR_TV, r + cfg.TV_SUFFIX] for r in classify.region_names()] + \
           [[cfg.DIR_ANIME], [cfg.DIR_VARIETY], [cfg.DIR_DOC]]
    out, seen = [], set()
    for prefix, pp in _drives():
        for c in cats:
            s = source_for(prefix + "-".join(c))
            key = (s, "/".join(c), prefix)
            if key not in seen:
                seen.add(key)
                out.append({"source": s, "category": prefix + "-".join(c),
                            "path": (f"夸克网盘：{qroot}" if prefix else root) + "/" + "/".join(c)})
    return out


def _body(event: str, message: str, source: str) -> dict:
    body = {"event": event, "message": message or f"{event}，请执行联动"}
    if source:
        body["source"] = source
    return body


def preview(event: str, source: str) -> dict:
    url = _base() + ENDPOINT
    body = _body(event, "", source)
    curl = (f"curl -X POST '{url}' -H 'Authorization: Bearer 你的lpk_api_秘钥' -H 'Content-Type: application/json' "
            f"-d '{json.dumps(body, ensure_ascii=False)}'")
    return {"url": url, "body": body, "curl": curl}


def hint(status: int, err: str) -> str:
    if err:
        if "Connect" in err or "Timeout" in err:
            return ("连不上 LitePan：检查地址和端口是否正确。桥接服务在容器里，地址不能写 127.0.0.1，"
                    "要写 NAS 的局域网 IP 加 LitePan 实际使用的端口")
        return err
    if status in (401, 403):
        return "API 秘钥无效，或类型不对：需要「任务执行」型普通秘钥（lpk_api_ 开头）"
    if status == 404:
        return "接口不存在：地址/端口可能指到了别的服务，或 LitePan 版本不支持（需要联动页里能看到「第三方通知」）"
    if 200 <= status < 300:
        return ("LitePan 已接收通知。如果联动没有执行：核对 LitePan 联动里的「通知名称」和「通知来源」是否与上面请求里的一致，"
                "以及联动是否已保存并启用")
    return f"LitePan 返回 HTTP {status}"


async def send_detail(event: str, message: str = "", source=None) -> dict:
    src = cfg.LITEPAN_SOURCE if source is None else source
    pv = preview(event, src)
    if not configured():
        return {"ok": False, "status": 0, "response": "", "hint": "还没有配置 LitePan 地址和 API 秘钥", **pv}
    status, text, err = 0, "", ""
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(pv["url"], json=pv["body"], headers={"Authorization": "Bearer " + cfg.LITEPAN_KEY})
        status, text = r.status_code, (r.text or "")[:300]
    except Exception as e:  # noqa
        err = f"{type(e).__name__}"
    return {"ok": 200 <= status < 300, "status": status, "response": text, "hint": hint(status, err), **pv}


async def send(event: str, message: str = "", source=None) -> tuple[bool, str]:
    d = await send_detail(event, message, source)
    return d["ok"], (f"HTTP {d['status']} {d['response']}".strip() if d["ok"] else d["hint"])


def schedule(rid: int, source: str):
    """同来源的多次落盘合并成一次通知：最后一个文件进目录后再等 LITEPAN_DELAY 秒，免得整理到半截的文件。"""
    _rids.setdefault(source, set()).add(rid)
    t = _timers.get(source)
    if t and not t.done():
        t.cancel()
    _timers[source] = asyncio.create_task(_later(source))


async def _later(source: str):
    try:
        await asyncio.sleep(cfg.LITEPAN_DELAY)
    except asyncio.CancelledError:
        return
    rids = sorted(_rids.pop(source, set()))
    d = await send_detail(cfg.LITEPAN_EVENT, source=source)
    for rid in rids:
        db.log(rid, (f"已通知 LitePan 联动（事件 {cfg.LITEPAN_EVENT}，来源 {source or '未带'}）：HTTP {d['status']} {d['response'][:80]}。"
                     "没有执行的话，检查 LitePan 联动里的通知名称和来源是否与此一致" if d["ok"]
                     else f"通知 LitePan 失败：{d['hint']}"))
