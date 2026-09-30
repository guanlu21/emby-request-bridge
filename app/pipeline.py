"""求片主流程：搜索 → 逐个候选尝试（失败自动换下一个）→ 过滤文件 → 移入正式目录 → 通知入库。"""
import asyncio
import json
import time

import httpx

from . import db
from .config import cfg
from .filters import Rules, select_files
from .sources import build_candidates, parse_115_share


def rules() -> Rules:
    return Rules(cfg.MIN_RES, cfg.MIN_GB * 1024 ** 3, cfg.MAX_GB * 1024 ** 3)


class Pipeline:
    def __init__(self, drive, meta_fn, search_fn, after_fn, poll=None):
        self.drive, self.meta_fn, self.search_fn, self.after_fn = drive, meta_fn, search_fn, after_fn
        self.poll = cfg.POLL_SECONDS if poll is None else poll

    async def run(self, rid: int):
        r = db.get(rid)
        try:
            db.update(rid, status="searching", error="")
            meta = await self.meta_fn(r)
            raw = await self.search_fn(meta, r["media_type"], r["season"])
            tried = set(json.loads(r["tried"]))
            cands = [c for c in build_candidates(raw, meta, r["media_type"], r["season"], rules())
                     if c.url not in tried]
            db.log(rid, f"搜到 {len(raw)} 条，过滤后 {len(cands)} 个候选")
            db.update(rid, status="downloading")
            for n, c in enumerate(cands[:cfg.MAX_ATTEMPTS], 1):
                db.log(rid, f"尝试 {n}: [{c.kind}/{c.src}] {c.title[:60]} (分 {c.score})")
                ok = await self.attempt(r, meta, c)
                tried.add(c.url)
                db.update(rid, tried=json.dumps(sorted(tried)))
                if ok:
                    db.update(rid, status="done", picked=c.title)
                    await self.after_fn(r)
                    return
            db.update(rid, status="failed", error="所有候选均失败或没有符合条件的资源")
        except Exception as e:  # noqa
            db.log(rid, f"异常: {e!r}")
            db.update(rid, status="failed", error=str(e)[:200])

    async def dest_dir(self, r, meta) -> int:
        name = f"{meta['names'][0]} ({meta['year']})".replace("/", " ")
        if r["media_type"] == "movie":
            return await self.drive.ensure_dir(cfg.P115_DEST_MOVIE_CID, name)
        show = await self.drive.ensure_dir(cfg.P115_DEST_TV_CID, name)
        return await self.drive.ensure_dir(show, f"Season {r['season']:02d}")

    async def attempt(self, r, meta, c) -> bool:
        rid, stage = r["id"], None
        try:
            stage = await self.drive.mkdir(cfg.P115_STAGING_CID, f"req{rid}-{int(time.time())}")
            if c.kind == "share":
                code, pw = parse_115_share(c.url, c.password)
                await self.drive.receive_share(code, pw, stage)
            else:
                h = await self.drive.add_offline(c.url, stage)
                deadline = time.time() + cfg.OFFLINE_TIMEOUT
                while True:
                    st = await self.drive.offline_state(h)
                    if st == "done":
                        break
                    if st == "failed" or time.time() > deadline:
                        raise RuntimeError("离线下载失败或超时")
                    await asyncio.sleep(self.poll)
            keep, drop = select_files(await self.drive.list_files(stage), rules())
            if not keep:
                raise RuntimeError("没有符合条件的视频文件（非视频/太小/太大/分辨率不足）")
            db.log(rid, f"保留 {len(keep)} 个视频，丢弃 {len(drop)} 个文件")
            await self.drive.move([f["id"] for f in keep], await self.dest_dir(r, meta))
            return True
        except Exception as e:  # noqa
            db.log(rid, f"失败，换下一个: {e}")
            return False
        finally:
            if stage:
                try:
                    await self.drive.delete([stage])
                except Exception:  # noqa
                    pass


async def notify_after(r):
    """等 LitePan 生成 strm，再让 Emby 刷新媒体库。"""
    async with httpx.AsyncClient(timeout=20) as c:
        if cfg.LITEPAN_TRIGGER_URL:
            try:
                await c.post(cfg.LITEPAN_TRIGGER_URL)
            except Exception as e:  # noqa
                db.log(r["id"], f"触发 LitePan 失败: {e}")
        await asyncio.sleep(cfg.LITEPAN_WAIT)
        if cfg.EMBY_URL and cfg.EMBY_KEY:
            try:
                await c.post(f"{cfg.EMBY_URL}/emby/Library/Refresh", params={"api_key": cfg.EMBY_KEY})
                db.log(r["id"], "已通知 Emby 刷新媒体库")
            except Exception as e:  # noqa
                db.log(r["id"], f"通知 Emby 失败: {e}")
