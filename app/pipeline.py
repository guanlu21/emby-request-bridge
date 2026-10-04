"""Emby求片 · 求片主流程：搜索 → 逐个候选尝试（失败自动换下一个）→ 过滤文件 → 移入正式目录 → 通知入库。"""
import asyncio
import json
import time

import httpx

from . import db, litepan
from .config import cfg
from . import classify
from .filters import Rules, assign_seasons, pick_best_file, select_files
from . import sources
from .sources import Candidate, build_candidates, parse_115_share


class SetupError(Exception):
    """配置/环境问题（目录无效、115 没登录等）：换资源也没用，直接终止，且不把候选记为已试。"""


def cand_dict(c: Candidate) -> dict:
    return {"title": c.title, "kind": c.kind, "src": c.src, "score": c.score, "url": c.url,
            "password": c.password, "size": c.size, "seeders": c.seeders, "covers": c.covers, "tags": c.tags}


def cand_from(d: dict) -> Candidate:
    return Candidate(d["kind"], d["title"], d["url"], d.get("password", ""), d.get("size", 0), d.get("seeders", 0),
                     d.get("src", ""), score=d.get("score", 0), covers=d.get("covers", []), tags=d.get("tags", []))


def rules() -> Rules:
    return Rules(cfg.MIN_RES, cfg.MIN_GB * 1024 ** 3, cfg.MAX_GB * 1024 ** 3,
                 cfg.PREFER_MIN_GB * 1024 ** 3, cfg.PREFER_MAX_GB * 1024 ** 3, cfg.KEEP_MIN_MB * 1024 ** 2)


class Pipeline:
    def __init__(self, drive, meta_fn, search_fn, after_fn, poll=None):
        self._dest_lock = asyncio.Lock()
        self._active = set()
        self.drive, self.meta_fn, self.search_fn, self.after_fn = drive, meta_fn, search_fn, after_fn
        self.poll = cfg.POLL_SECONDS if poll is None else poll

    # ------------------------------------------------------------ 一组请求（电视剧一次求多季共用一组）
    @staticmethod
    def _rows_for(r) -> list:
        """要一起处理的请求：电视剧一次求多季时，同组里还在排队/处理中的各季；其它情况就是它自己。"""
        if r["media_type"] == "tv" and r.get("grp"):
            rows = [x for x in db.group_rows(r["grp"]) if x["status"] in ("queued", "searching", "downloading")]
            if rows:
                return rows
        return [r]

    @staticmethod
    def _log_all(rows, msg):
        for x in rows:
            db.log(x["id"], msg)

    async def run(self, rid: int, depth: int = 0):
        r = db.get(rid)
        if not r:
            return
        rows = self._rows_for(r)
        ids = [x["id"] for x in rows]
        if any(i in self._active for i in ids):  # 同一组正在处理
            return
        self._active.update(ids)
        token = sources.DEPTH.set(depth) if depth else None
        try:
            await self._run_rows(rows)
        finally:
            self._active.difference_update(ids)
            if token is not None:
                sources.DEPTH.reset(token)

    def _fail_all(self, rows, msg):
        for x in rows:
            if db.get(x["id"])["status"] != "done":
                db.update(x["id"], status="failed", error=msg[:200])

    async def _meta_for(self, rows):
        r = dict(rows[0])
        r["seasons"] = [x["season"] for x in rows if x["season"]]
        return await self.meta_fn(r)

    async def _search_build(self, rows, meta):
        """搜索 + 过滤 + 排序（电视剧一次搜，按覆盖的季数优先）。返回 (原始结果, 候选, 过滤报告, 说明)。"""
        r = rows[0]
        seasons = [x["season"] for x in rows] if r["media_type"] == "tv" else None
        res = await self.search_fn(meta, r["media_type"], seasons)
        raw, notes = res if isinstance(res, tuple) else (res, [])
        report = {}
        built = build_candidates(raw, meta, r["media_type"], seasons, rules(), report)
        return raw, built, report, notes

    def _save_cands(self, rows, built, report):
        data = json.dumps([cand_dict(c) for c in built[:150]], ensure_ascii=False)
        rej = json.dumps(report.get("rejects", []), ensure_ascii=False)
        for x in rows:
            db.update(x["id"], cands=data, rejects=rej, cands_at=time.time())

    async def _run_rows(self, rows):
        r = rows[0]
        try:
            if not cfg.P115_STAGING_CID or not self._library_cid(r):
                raise RuntimeError("还没有设置 115 的下载目录和「影视根目录」，请管理员到「设置 → 115 网盘 / 入库分类」里选择")
            await self.preflight(r)
            for x in rows:
                db.update(x["id"], status="searching", error="")
            meta = await self._meta_for(rows)
            raw, built, report, notes = await self._search_build(rows, meta)
            for n in notes:
                self._log_all(rows, f"提示：{n}")
            tried = set(json.loads(r["tried"]))
            self._save_cands(rows, built, report)
            cands = [c for c in built if c.url not in tried]
            if report.get("counts"):
                self._log_all(rows, "过滤掉：" + "、".join(f"{k} {v}" for k, v in report["counts"].most_common()))
                if not cands:
                    for sm in report["samples"]:
                        self._log_all(rows, "例：" + sm)
            if not getattr(self.drive, "can_receive_share", lambda: True)():
                n = len(cands)
                cands = [c for c in cands if c.kind != "share"]
                if n != len(cands):
                    self._log_all(rows, f"未配置 115 Cookie，跳过 {n - len(cands)} 个分享链接，只用磁力")
            self._log_all(rows, f"搜到 {len(raw)} 条，过滤后 {len(cands)} 个候选")
            for x in rows:
                db.update(x["id"], status="downloading")
            remaining = {x["season"]: x for x in rows}
            for n, c in enumerate(cands[:cfg.MAX_ATTEMPTS], 1):
                if not remaining:
                    break
                want = [remaining[s] for s in remaining if r["media_type"] == "movie" or s in c.covers]
                if not want:
                    continue
                cover = "（覆盖第 " + "、".join(str(x["season"]) for x in want) + " 季）" if r["media_type"] == "tv" else ""
                self._log_all(want, f"尝试 {n}: [{c.kind}/{c.src}] {c.title[:60]} (分 {c.score}){cover}")
                try:
                    placed = await self.attempt_group(want, meta, c)
                except SetupError as e:
                    self._log_all(rows, str(e))
                    self._fail_all(list(remaining.values()), str(e))
                    return
                tried.add(c.url)
                for x in rows:
                    db.update(x["id"], tried=json.dumps(sorted(tried)))
                for s in placed:
                    row = remaining.pop(s)
                    db.update(row["id"], status="done", picked=c.title)
                    await self.after_fn(row)
            self._fail_all(list(remaining.values()), "所有候选均失败或没有符合条件的资源")
        except SetupError as e:
            self._log_all(rows, str(e))
            self._fail_all(rows, str(e))
        except Exception as e:  # noqa
            self._log_all(rows, f"异常: {e!r}")
            self._fail_all(rows, str(e))

    async def search_only(self, rid: int, depth: int = 3):
        """只搜索、刷新候选列表（不下载）：候选里没有想要的时，用更深的搜索再找一遍，再由你挑。"""
        r = db.get(rid)
        token = sources.DEPTH.set(depth)
        try:
            db.log(rid, f"开始搜索深度 {depth} 的重新搜索（只刷新候选，不会下载）")
            meta = await self._meta_for([r])
            raw, built, report, notes = await self._search_build([r], meta)
            for n in notes:
                db.log(rid, f"提示：{n}")
            self._save_cands([r], built, report)
            db.log(rid, f"重新搜索完成：{len(built)} 个候选，{sum(report.get('counts', {}).values())} 个被过滤")
        except Exception as e:  # noqa
            db.log(rid, f"重新搜索失败: {e!r}")
            db.update(rid, cands_at=time.time())
        finally:
            sources.DEPTH.reset(token)

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
            known = next((d for d in json.loads(r["cands"] or "[]") + json.loads(r["rejects"] or "[]") if d["url"] == url), None)
            if known:
                c = cand_from(known)
                if r["media_type"] == "tv" and r["season"] not in c.covers:
                    c.covers = list(c.covers) + [r["season"]]
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
        chain = []
        async with self._dest_lock:
            cid = self._library_cid(r)
            for p in parts:
                cid = await self.drive.ensure_dir(cid, p)
                chain.append(cid)
        r["_chain"] = chain[len(cat):]  # 片名/季这几层，删除时空了就一起清理（分类目录不动）
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

    async def _is_empty(self, cid) -> bool:
        try:
            if await self.drive.list_files(int(cid)):
                return False
            ld = getattr(self.drive, "list_dirs", None)
            return not (ld and await ld(int(cid)))
        except Exception:  # noqa
            return False

    async def purge(self, r) -> int:
        """删除这条请求入库到网盘里的视频文件，并把因此变空的片名/季文件夹一起清掉。返回删除的文件数。
        只删记录在案的文件（placed），不会碰目录里别的东西。"""
        try:
            placed = json.loads(r["placed"] or "null")
        except ValueError:
            placed = None
        if not placed or not placed.get("files"):
            return 0
        await self.drive.delete(placed["files"])
        for cid in reversed(placed.get("chain") or [placed["dest"]]):  # 从最里层往上，空了才删
            if await self._is_empty(cid):
                try:
                    await self.drive.delete([cid])
                except Exception:  # noqa
                    break
            else:
                break
        db.update(r["id"], placed="")
        db.log(r["id"], f"已删除网盘里的 {len(placed['files'])} 个文件")
        return len(placed["files"])

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
        return bool(await self.attempt_group([r], meta, c, replace_old))

    async def attempt_group(self, rows, meta, c, replace_old=None) -> set:
        """下载一个资源，并分给 rows 里的各个请求（电视剧一次求多季时，一个合集按季分到各自的 Season 目录）。
        返回已入库的季集合（电影是 {None}）；这个资源没用上返回空集合。"""
        r0 = rows[0]
        rid0, stage = r0["id"], None
        try:
            stage = await self.drive.mkdir(cfg.P115_STAGING_CID, f"req{rid0}-{int(time.time())}")
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
            plan = []  # [(请求, 它的文件)]
            if r0["media_type"] == "movie":
                if len(keep) > 1:  # 一个分享里有多个版本，只留最合适的一个
                    best = pick_best_file(keep, rules())
                    drop += [f for f in keep if f is not best]
                    keep = [best]
                plan = [(r0, keep)]
            else:
                by = assign_seasons(keep, [x["season"] for x in rows], c.title)
                if not by:
                    raise RuntimeError("无法判断这些文件属于哪一季（文件名和文件夹名里都没有季信息）")
                for x in rows:
                    fl = by.get(x["season"])
                    if not fl:
                        db.log(x["id"], f"这个资源里没有第 {x['season']} 季的文件")
                        continue
                    eps = meta.get("season_eps", {}).get(x["season"]) or meta.get("episodes", 0)
                    if eps >= 4 and len(fl) < max(2, eps // 3):  # 疑似只是单集或残缺的包
                        db.log(x["id"], f"第 {x['season']} 季只有 {len(fl)} 个视频，应有约 {eps} 集，疑似不是整季，跳过")
                        continue
                    plan.append((x, fl))
                if len(by) > 1 or len(rows) > 1:
                    self._log_all(rows, "合集：" + "、".join(f"第{s}季 {len(fl)} 个" for s, fl in sorted(by.items())))
                if not plan:
                    raise RuntimeError("这个资源里没有哪一季的视频数量够（疑似单集或残缺的包）")
            placed = set()
            for x, fl in plan:
                xid = x["id"]
                db.log(xid, f"保留 {len(fl)} 个视频，丢弃 {len(keep) - len(fl) + len(drop)} 个文件")
                try:
                    dest = await self.dest_dir(x, meta)
                except Exception as e:  # noqa
                    raise SetupError(f"无法在 115 正式目录里建文件夹（{e}）。请到「设置 → 115 网盘」重新选择电影/剧集目录")
                if replace_old and len(rows) == 1:
                    await self.remove_placed(xid, replace_old, dest)
                await self.rename_files(xid, fl, x, meta, c.title)
                await self.drive.move([f["id"] for f in fl], dest)
                db.update(xid, placed=json.dumps({"dest": str(dest), "files": [str(f["id"]) for f in fl],
                                                  "chain": [str(c) for c in x.pop("_chain", [dest])]}))
                placed.add(x["season"])
            return placed
        except SetupError:
            raise
        except AttributeError as e:  # 方法不存在：是程序和 115 库版本不匹配，换资源也没用
            raise SetupError(f"程序与 115 客户端库版本不匹配：{e}")
        except Exception as e:  # noqa
            self._log_all(rows, f"失败，换下一个: {e}")
            return set()
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
