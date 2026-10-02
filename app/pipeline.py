"""求片主流程：搜索 → 逐个候选尝试（失败自动换下一个）→ 过滤文件 → 移入正式目录 → 通知入库。"""
import asyncio
import json
import time

import httpx

from . import db, litepan
from .config import cfg
from . import classify
from .filters import Rules, pick_best_file, select_files
from .sources import Candidate, build_candidates, parse_115_share


class SetupError(Exception):
    """配置/环境问题（目录无效、115 没登录等）：换资源也没用，直接终止，且不把候选记为已试。"""


def cand_dict(c: Candidate) -> dict:
    return {"title": c.title, "kind": c.kind, "src": c.src, "score": c.score, "url": c.url,
            "password": c.password, "size": c.size, "seeders": c.seeders}


def cand_from(d: dict) -> Candidate:
    return Candidate(d["kind"], d["title"], d["url"], d.get("password", ""), d.get("size", 0), d.get("seeders", 0),
                     d.get("src", ""), score=d.get("score", 0))


def rules() -> Rules:
    return Rules(cfg.MIN_RES, cfg.MIN_GB * 1024 ** 3, cfg.MAX_GB * 1024 ** 3,
                 cfg.PREFER_MIN_GB * 1024 ** 3, cfg.PREFER_MAX_GB * 1024 ** 3, cfg.KEEP_MIN_MB * 1024 ** 2)


class Pipeline:
    def __init__(self, drive, meta_fn, search_fn, after_fn, poll=None):
        self._dest_lock = asyncio.Lock()
        self.drive, self.meta_fn, self.search_fn, self.after_fn = drive, meta_fn, search_fn, after_fn
        self.poll = cfg.POLL_SECONDS if poll is None else poll

    async def run(self, rid: int):
        r = db.get(rid)
        try:
            if not cfg.P115_STAGING_CID or not self._library_cid(r):
                raise RuntimeError("还没有设置 115 的下载目录和「影视根目录」，请管理员到「设置 → 115 网盘 / 入库分类」里选择")
            await self.preflight(r)
            db.update(rid, status="searching", error="")
            meta = await self.meta_fn(r)
            res = await self.search_fn(meta, r["media_type"], r["season"])
            raw, notes = res if isinstance(res, tuple) else (res, [])
            for n in notes:
                db.log(rid, f"提示：{n}")
            tried = set(json.loads(r["tried"]))
            report = {}
            built = build_candidates(raw, meta, r["media_type"], r["season"], rules(), report)
            db.update(rid, cands=json.dumps([cand_dict(c) for c in built[:40]], ensure_ascii=False))
            cands = [c for c in built if c.url not in tried]
            if report.get("counts"):
                db.log(rid, "过滤掉：" + "、".join(f"{k} {v}" for k, v in report["counts"].most_common()))
                if not cands:
                    for sm in report["samples"]:
                        db.log(rid, "例：" + sm)
            if not getattr(self.drive, "can_receive_share", lambda: True)():
                n = len(cands)
                cands = [c for c in cands if c.kind != "share"]
                if n != len(cands):
                    db.log(rid, f"未配置 115 Cookie，跳过 {n - len(cands)} 个分享链接，只用磁力")
            db.log(rid, f"搜到 {len(raw)} 条，过滤后 {len(cands)} 个候选")
            db.update(rid, status="downloading")
            for n, c in enumerate(cands[:cfg.MAX_ATTEMPTS], 1):
                db.log(rid, f"尝试 {n}: [{c.kind}/{c.src}] {c.title[:60]} (分 {c.score})")
                try:
                    ok = await self.attempt(r, meta, c)
                except SetupError as e:
                    db.log(rid, str(e))
                    db.update(rid, status="failed", error=str(e)[:200])
                    return
                tried.add(c.url)
                db.update(rid, tried=json.dumps(sorted(tried)))
                if ok:
                    db.update(rid, status="done", picked=c.title)
                    await self.after_fn(r)
                    return
            db.update(rid, status="failed", error="所有候选均失败或没有符合条件的资源")
        except SetupError as e:
            db.log(rid, str(e))
            db.update(rid, status="failed", error=str(e)[:200])
        except Exception as e:  # noqa
            db.log(rid, f"异常: {e!r}")
            db.update(rid, status="failed", error=str(e)[:200])

    @staticmethod
    def _library_cid(r) -> int:
        """分类入库的根目录；没设就退回旧的电影/剧集目录。"""
        return cfg.LIBRARY_ROOT_CID or (cfg.P115_DEST_MOVIE_CID if r["media_type"] == "movie" else cfg.P115_DEST_TV_CID)

    async def preflight(self, r):
        """先确认 115 下载目录和入库目录能访问，别等下载完才发现目录 ID 不对。"""
        for label, cid in (("下载目录", cfg.P115_STAGING_CID), ("影视根目录", self._library_cid(r))):
            try:
                await self.drive.list_dirs(cid)
            except AttributeError:
                return  # 测试用的假驱动没有 list_dirs
            except Exception as e:  # noqa
                raise SetupError(f"115 {label}无法访问（{e}）。请到「设置 → 115 网盘 / 入库分类」重新选择该目录，并点「测试 115 连接」")

    async def run_manual(self, rid: int, url: str):
        await self.run_candidate(rid, url, replace=False)

    async def run_candidate(self, rid: int, url: str, replace: bool = False):
        """使用指定资源（候选列表里的，或手动粘贴的 115 分享/磁力链接）。
        replace=True：新资源下载并通过筛选后，才会删除之前入库的文件；新资源失败时原文件保留、状态不变。"""
        r = db.get(rid)
        old = None
        try:
            if replace:
                try:
                    old = json.loads(r["placed"] or "null")
                except ValueError:
                    old = None
            db.update(rid, status="downloading", error="")
            await self.preflight(r)
            meta = await self.meta_fn(r)
            url = url.strip()
            known = next((d for d in json.loads(r["cands"] or "[]") if d["url"] == url), None)
            if known:
                c = cand_from(known)
            elif url.startswith("magnet:"):
                c = Candidate("magnet", "手动指定", url, src="manual")
            elif parse_115_share(url):
                c = Candidate("share", "手动指定", url, src="manual")
            else:
                raise RuntimeError("只支持 magnet 磁力链接或 115 分享链接")
            db.log(rid, ("替换为：" if replace else "手动指定资源：") + f"[{c.kind}] {c.title[:60]}")
            ok = await self.attempt(r, meta, c, replace_old=old)
            tried = set(json.loads(db.get(rid)["tried"]))
            tried.add(c.url)
            db.update(rid, tried=json.dumps(sorted(tried)))
            if ok:
                db.update(rid, status="done", picked=c.title)
                await self.after_fn(r)
            elif replace and old:
                db.update(rid, status="done", error="替换失败，原资源保留（详情看日志）")
            else:
                db.update(rid, status="failed", error="这个资源也失败了，详情看日志")
        except Exception as e:  # noqa
            db.log(rid, str(e) if isinstance(e, SetupError) else f"异常: {e!r}")
            if replace and old:
                db.update(rid, status="done", error=("替换失败，原资源保留：" + str(e))[:200])
            else:
                db.update(rid, status="failed", error=str(e)[:200])

    async def dest_dir(self, r, meta) -> int:
        """分类目录 + 片名目录（+ 季目录），名字按命名模板。同一时刻只允许一个请求在建目录，避免重复。"""
        v = classify.vars_for(r, meta)
        cat = classify.classify(r["media_type"], meta) if cfg.LIBRARY_ROOT_CID else []
        if r["media_type"] == "movie":
            leaf = [classify.render(cfg.NAME_MOVIE_DIR, v)]
        else:
            leaf = [classify.render(cfg.NAME_TV_DIR, v), classify.render(cfg.NAME_SEASON_DIR, v)]
        parts = cat + [x for x in leaf if x]
        async with self._dest_lock:
            cid = self._library_cid(r)
            for p in parts:
                cid = await self.drive.ensure_dir(cid, p)
        db.update(r["id"], category="-".join(cat) or ("电影" if r["media_type"] == "movie" else "电视剧"))
        db.log(r["id"], "入库位置：" + " / ".join(parts))
        return cid

    async def remove_placed(self, rid, old: dict, new_dest):
        """替换成功前删除旧资源的文件；失败只记日志（可能留下重复文件，需要手动清理）。"""
        try:
            await self.drive.delete(old["files"])
            db.log(rid, f"已删除旧资源的 {len(old['files'])} 个文件")
        except Exception as e:  # noqa
            db.log(rid, f"删除旧文件失败（请到 115 手动清理）：{e}")
            return
        if str(old.get("dest")) != str(new_dest):  # 分类变了：旧目录空了就一并删掉
            try:
                if not await self.drive.list_files(int(old["dest"])):
                    await self.drive.delete([old["dest"]])
            except Exception:  # noqa
                pass

    async def rename_files(self, rid, files, r, meta, hint=""):
        """按命名模板改名（带 TMDB 编号等）；任何一步失败都只记日志，保留原文件名，不影响入库。"""
        try:
            fn = self.drive.rename
        except Exception:  # noqa
            return
        used, done = set(), 0
        for f in files:
            new = classify.target_name(r, meta, f["name"], hint, used)
            if not new or new == f["name"]:
                continue
            try:
                await fn(f["id"], new)
                f["name"] = new
                done += 1
            except Exception as e:  # noqa
                db.log(rid, f"重命名失败（保留原名）：{e}")
                return
        if done:
            db.log(rid, f"已按命名格式重命名 {done} 个文件")

    async def attempt(self, r, meta, c, replace_old=None) -> bool:
        rid, stage = r["id"], None
        try:
            stage = await self.drive.mkdir(cfg.P115_STAGING_CID, f"req{rid}-{int(time.time())}")
        except Exception as e:  # noqa
            raise SetupError(f"无法在 115 暂存目录里建文件夹（{e}）。请到「设置 → 115 网盘」重新选择暂存目录")
        try:
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
            if r["media_type"] == "movie" and len(keep) > 1:  # 一个分享里有多个版本，只留最合适的一个
                best = pick_best_file(keep, rules())
                drop += [f for f in keep if f is not best]
                keep = [best]
            db.log(rid, f"保留 {len(keep)} 个视频，丢弃 {len(drop)} 个文件")
            try:
                dest = await self.dest_dir(r, meta)
            except Exception as e:  # noqa
                raise SetupError(f"无法在 115 正式目录里建文件夹（{e}）。请到「设置 → 115 网盘」重新选择电影/剧集目录")
            if replace_old:
                await self.remove_placed(rid, replace_old, dest)
            await self.rename_files(rid, keep, r, meta, c.title)
            await self.drive.move([f["id"] for f in keep], dest)
            db.update(rid, placed=json.dumps({"dest": str(dest), "files": [str(f["id"]) for f in keep]}))
            return True
        except SetupError:
            raise
        except AttributeError as e:  # 方法不存在：是程序和 115 库版本不匹配，换资源也没用
            raise SetupError(f"程序与 115 客户端库版本不匹配：{e}")
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
    """入库通知：配了 LitePan 就触发它的自动联动（来源按分类区分，只处理对应目录）；
    没配就等一会儿，由本服务直接让 Emby 刷新媒体库。"""
    if litepan.configured():
        cat = (db.get(r["id"]) or {}).get("category", "")
        src = litepan.source_for(cat)
        litepan.schedule(r["id"], src)
        db.log(r["id"], f"已排队通知 LitePan（来源 {src or '未带'}；{cfg.LITEPAN_DELAY} 秒内同来源的多个请求合并成一次）")
        return
    await asyncio.sleep(cfg.LITEPAN_WAIT)
    if cfg.EMBY_URL and cfg.EMBY_KEY:
        try:
            async with httpx.AsyncClient(timeout=20) as c:
                await c.post(f"{cfg.EMBY_URL}/emby/Library/Refresh", params={"api_key": cfg.EMBY_KEY})
            db.log(r["id"], "已通知 Emby 刷新媒体库")
        except Exception as e:  # noqa
            db.log(r["id"], f"通知 Emby 失败: {e}")
