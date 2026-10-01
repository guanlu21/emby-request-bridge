"""对接 LitePan「自动联动」：文件落进正式目录后，POST 一个第三方通知，由它的联动完成
整理 → STRM → 元数据 → Emby 刷库。

接口（见 LitePan 文档「自动联动」）：
  POST {LitePan 地址}/api/open/automation/events
  Authorization: Bearer lpk_api_xxx      # 系统设置 → API 秘钥 里创建「任务执行」型
  {"event": "<联动里填的通知名称>", "message": "...", "source": "<可选来源>"}
"""
import asyncio

import httpx

from . import db
from .config import cfg

_timer = None
_rids: set = set()


def configured() -> bool:
    return bool(cfg.LITEPAN_URL and cfg.LITEPAN_KEY)


async def send(event: str, message: str = "") -> tuple[bool, str]:
    if not configured():
        return False, "还没有配置 LitePan 地址和 API 秘钥"
    body = {"event": event, "message": message or f"{event}，请执行联动"}
    if cfg.LITEPAN_SOURCE:
        body["source"] = cfg.LITEPAN_SOURCE
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(cfg.LITEPAN_URL.rstrip("/") + "/api/open/automation/events", json=body,
                             headers={"Authorization": "Bearer " + cfg.LITEPAN_KEY})
    except Exception as e:  # noqa
        return False, f"连不上 LitePan：{type(e).__name__}"
    if r.status_code in (401, 403):
        return False, f"API 秘钥无效或类型不对（HTTP {r.status_code}）：需要「任务执行」型普通秘钥"
    return 200 <= r.status_code < 300, f"HTTP {r.status_code} {r.text[:120]}".strip()


def schedule(rid: int):
    """合并多次落盘：最后一个文件进目录后再等 LITEPAN_DELAY 秒，只发一次通知，免得整理到半截的文件。"""
    global _timer
    _rids.add(rid)
    if _timer and not _timer.done():
        _timer.cancel()
    _timer = asyncio.create_task(_later())


async def _later():
    global _timer
    try:
        await asyncio.sleep(cfg.LITEPAN_DELAY)
    except asyncio.CancelledError:
        return
    rids = sorted(_rids)
    _rids.clear()
    ok, info = await send(cfg.LITEPAN_EVENT)
    src = cfg.LITEPAN_SOURCE or "未带来源"
    for rid in rids:
        db.log(rid, (f"已通知 LitePan 联动（事件 {cfg.LITEPAN_EVENT}，来源 {src}）：{info}。"
                     "没有执行的话，检查 LitePan 联动里的通知名称和来源是否与此一致" if ok
                     else f"通知 LitePan 失败：{info}"))
