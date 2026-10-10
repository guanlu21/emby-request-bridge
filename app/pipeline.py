"""Emby求片 · 求片主流程：搜索 → 逐个候选尝试（失败自动换下一个）→ 过滤文件 → 移入正式目录 → 通知入库。"""
import asyncio
import json
import time

import httpx

from . import db, litepan
from .config import cfg
from . import classify
import math

from .filters import Rules, assign_seasons, explain_files, pair_subs, pick_best_file, select_files
from . import sources
from . import quark as quark_mod
from .sources import Candidate, build_candidates, parse_115_share, share_provider, split_manual_url


class SetupError(Exception):
    """配置/环境问题（目录无效、115 没登录等）：换资源也没用，直接终止，且不把候选记为已试。"""


def cand_dict(c: Candidate) -> dict:
    return {"title": c.title, "kind": c.kind, "src": c.src, "score": c.score, "url": c.url,
            "password": c.password, "size": c.size, "seeders": c.seeders, "covers": c.covers, "tags": c.tags,
            "provider": c.provider}


def cand_from(d: dict) -> Candidate:
    return Candidate(d["kind"], d["title"], d["url"], d.get("password", ""), d.get("size", 0), d.get("seeders", 0),
                     d.get("src", ""), provider=d.get("provider", "115"), score=d.get("score", 0), covers=d.get("covers", []),
                     tags=d.get("tags", []))


def rules() -> Rules:
    return Rules(cfg.MIN_RES, cfg.MIN_GB * 1024 ** 3, cfg.MAX_GB * 1024 ** 3,
                 cfg.PREFER_MIN_GB * 1024 ** 3, cfg.PREFER_MAX_GB * 1024 ** 3, cfg.KEEP_MIN_MB * 1024 ** 2)


class Pipeline:
    def __init__(self, drive, meta_fn, search_fn, after_fn, poll=None, quark=None):
        self._dest_lock = asyncio.Lock()
        self._active = set()
        self.drive, self.meta_fn, self.search_fn, self.after_fn = drive, meta_fn, search_fn, after_fn
        self.quark = quark  # 夸克驱动（可选）
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

    async def run(self, rid: int, depth: int = 0, append: dict = None):
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
            await self._run_rows(rows, append=append)
        finally:
            self._active.difference_update(ids)
            if token is not None:
                sources.DEPTH.reset(token)

    def _fail_all(self, rows, msg, restore_done=False):
        for x in rows:
            if restore_done and x.get("_was_done"):
                db.update(x["id"], status="done", error="追加集数失败，原有集数不受影响（详情看日志）")
                continue
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

    async def _run_rows(self, rows, append: dict = None):
        r = rows[0]
        if append:  # 追加模式：记住这些请求原本已入库，失败时恢复 done 而不是标成 failed
            for x in rows:
                x["_was_done"] = True
        try:
            usable = await self.usable_providers(r)
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
            if "115" in usable and not getattr(self.drive, "can_receive_share", lambda: True)():
                n = len(cands)
                cands = [c for c in cands if not (c.kind == "share" and c.provider == "115")]
                if n != len(cands):
                    self._log_all(rows, f"未配置 115 Cookie，跳过 {n - len(cands)} 个 115 分享链接")
            n = len(cands)
            cands = [c for c in cands if (c.provider if c.kind == "share" else "115") in usable]  # 磁力只能走 115 离线；夸克没配就不用夸克的分享
            if n != len(cands):
                self._log_all(rows, f"跳过 {n - len(cands)} 个需要未配置网盘的资源（已配置：{'、'.join('夸克' if p == 'quark' else p for p in usable)}）")
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
                    placed = await self.attempt_group(want, meta, c, append_eps=append)
                except SetupError as e:
                    self._log_all(rows, str(e))
                    self._fail_all(list(remaining.values()), str(e), restore_done=append is not None)
                    return
                tried.add(c.url)
                for x in rows:
                    db.update(x["id"], tried=json.dumps(sorted(tried)))
                for s in placed:
                    row = remaining.pop(s)
                    db.update(row["id"], status="done", picked=c.title, done_at=time.time())
                    await self.after_fn(row)
            self._fail_all(list(remaining.values()), "所有候选均失败或没有符合条件的资源", restore_done=append is not None)
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

    # ------------------------------------------------------------ 网盘（115 / 夸克）
    @staticmethod
    def _library_cid(r) -> int:
        """115 的影视根目录；没设就退回旧的电影/剧集目录。"""
        return cfg.LIBRARY_ROOT_CID or (cfg.P115_DEST_MOVIE_CID if r["media_type"] == "movie" else cfg.P115_DEST_TV_CID)

    def _drive(self, provider: str):
        return self.quark if provider == "quark" else self.drive

    @staticmethod
    def _staging(provider: str):
        return str(cfg.QUARK_STAGING_FID) if provider == "quark" else cfg.P115_STAGING_CID

    def _root(self, r, provider: str):
        return str(cfg.QUARK_LIBRARY_FID) if provider == "quark" else self._library_cid(r)

    def _configured(self, provider: str, r) -> bool:
        if provider == "quark":
            return self.quark is not None and quark_mod.configured()
        return bool(cfg.P115_STAGING_CID and self._library_cid(r))

    @staticmethod
    def _cid(provider: str, v):
        return str(v) if provider == "quark" else int(v)

    async def preflight(self, r, provider: str = "115"):
        """先确认下载目录和入库根目录能访问，别等下载完才发现目录 ID 不对。"""
        name = "夸克" if provider == "quark" else "115"
        drive = self._drive(provider)
        for label, cid in (("下载目录", self._staging(provider)), ("影视根目录", self._root(r, provider))):
            try:
                await drive.list_dirs(cid)
            except AttributeError:
                return  # 测试用的假驱动没有 list_dirs
            except Exception as e:  # noqa
                raise SetupError(f"{name} {label}无法访问（{e}）。请到「设置」里重新选择该目录，并点「测试 {name} 连接」")

    async def usable_providers(self, r) -> list:
        """哪些网盘配好了、目录也能访问。一个都没有就报错；有一个坏了不影响另一个。"""
        ok, errs = [], []
        for p in ("115", "quark"):
            if not self._configured(p, r):
                continue
            try:
                await self.preflight(r, p)
                ok.append(p)
            except SetupError as e:
                errs.append(str(e))
        if not ok:
            raise SetupError("；".join(errs) or "还没有设置网盘：请到「设置」里配置 115（或夸克）的下载目录和影视根目录")
        return ok

    async def run_manual(self, rid: int, url: str, append: bool = False):
        """手动指定资源。已完成（已入库）的请求：append=True 按追加集数处理（只补新集，失败恢复已完成），否则按「替换」处理。"""
        r = db.get(rid)
        await self.run_candidate(rid, url, replace=bool(r and r["status"] == "done" and not append), append=append)

    async def _eps_in_library(self, rid: int) -> dict:
        """查这条请求已入库 Season 目录里的现有集号集合。没入库过/读不到就返回 None。"""
        r = db.get(rid)
        try:
            p = json.loads(r["placed"] or "null")
        except ValueError:
            p = None
        if not p or not p.get("dest") or not p.get("files"):
            return None
        provider = p.get("provider", "115")
        drive = self._drive(provider)
        have = set()
        try:
            listing = await drive.list_files(self._cid(provider, p["dest"]))
        except Exception:  # noqa
            return None
        for f in listing:
            ep = classify.episode_of(f["name"])
            if ep is not None:
                have.add(ep)
        if not have:  # 目录里认不出任何集号（可能是电影或命名无法解析），不启用追加
            return None
        return {r["season"]: have}

    async def run_append(self, rid: int, depth: int = 0):
        """追加集数：连载剧已入库后，重新搜索并只把库中没有的新集补进同一个 Season 目录。
        目录里认不出已有集数时提示改用重试整季。"""
        try:
            have = await self._eps_in_library(rid)
            if have is None:
                db.log(rid, "追加前检查：找不到已入库的 Season 目录或认不出已有集号，请改用「重新搜索/重试」整季处理")
                return
            season = next(iter(have))
            db.log(rid, f"追加集数：库里第 {season} 季已有 {len(have[season])} 集（{min(have[season])}~{max(have[season])}），开始搜索，只补新集")
            await self.run(rid, depth, append=have)
        except Exception as e:  # noqa：后台任务里的异常没人接，必须落日志，否则看起来就是"没反应"
            db.log(rid, f"追加集数失败: {e!r}")
            if db.get(rid)["status"] != "done":
                db.update(rid, status="failed", error=("追加失败：" + str(e))[:200])

    async def run_candidate(self, rid: int, url: str, replace: bool = False, append: bool = False):
        """使用指定资源（候选列表里的，或手动粘贴的 115 分享/磁力链接）。
        replace=True：新资源下载并通过筛选后，才会删除之前入库的文件；新资源失败时原文件保留、状态不变。
        append=True：追加模式——已入库的剧只补库里没有的新集，原文件不动（append 优先于 replace）。"""
        r = db.get(rid)
        old = None
        try:
            if replace:
                try:
                    old = json.loads(r["placed"] or "null")
                except ValueError:
                    old = None
            db.update(rid, status="downloading", error="")
            await self.usable_providers(r)
            meta = await self.meta_fn(r)
            url, pw = split_manual_url(url.strip())  # 支持粘贴「链接 提取码：xxx」这样的文本
            known = next((d for d in json.loads(r["cands"] or "[]") + json.loads(r["rejects"] or "[]") if d["url"] == url), None)
            if known:
                c = cand_from(known)
                if pw and not c.password:
                    c.password = pw  # 粘贴文本里带来的提取码优先于候选里的空值
                if r["media_type"] == "tv" and r["season"] not in c.covers:
                    c.covers = list(c.covers) + [r["season"]]
            elif url.startswith("magnet:"):
                c = Candidate("magnet", "手动指定", url, src="manual")
            else:
                sp = share_provider(url, pw)
                if not sp:
                    raise RuntimeError("只支持 magnet 磁力链接、115 分享链接或夸克分享链接（可带提取码）")
                c = Candidate("share", "手动指定", url, pw, src="manual", provider=sp)
            if c.kind == "share":
                sp = share_provider(c.url, c.password)
                if sp and sp != c.provider:  # 纠正候选里可能错误的网盘标注
                    db.log(rid, f"该链接实际是{('夸克' if sp == 'quark' else '115')}分享，改用对应的网盘转存")
                    c.provider = sp
            db.log(rid, ("替换为：" if replace else "追加资源：" if append else "手动指定资源：") + f"[{c.kind}] {c.title[:60]}")
            append_eps = await self._eps_in_library(rid) if (append and r["media_type"] == "tv") else None
            if append and append_eps is None:
                db.log(rid, "追加前检查：找不到已入库的 Season 目录或认不出已有集号，按整季处理")
            ok = await self.attempt(r, meta, c, replace_old=None if append else old, append_eps=append_eps)
            tried = set(json.loads(db.get(rid)["tried"]))
            tried.add(c.url)
            db.update(rid, tried=json.dumps(sorted(tried)))
            if ok:
                db.update(rid, status="done", picked=c.title, done_at=time.time())
                await self.after_fn(r)
            elif replace and old:
                db.update(rid, status="done", error="替换失败，原资源保留（详情看日志）")
            elif append and append_eps is not None:
                db.update(rid, status="done", error="追加失败，原有集数不受影响（详情看日志）")
            else:
                db.update(rid, status="failed", error="这个资源也失败了，详情看日志")
        except Exception as e:  # noqa
            db.log(rid, str(e) if isinstance(e, SetupError) else f"异常: {e!r}")
            if replace and old:
                db.update(rid, status="done", error=("替换失败，原资源保留：" + str(e))[:200])
            elif append and append_eps is not None:
                db.update(rid, status="done", error=("追加失败，原有集数不受影响：" + str(e))[:200])
            else:
                db.update(rid, status="failed", error=str(e)[:200])

    async def dest_dir(self, r, meta, provider: str = "115"):
        """分类目录 + 片名目录（+ 季目录），名字按命名模板。同一时刻只允许一个请求在建目录，避免重复。"""
        drive = self._drive(provider)
        v = classify.vars_for(r, meta)
        use_root = cfg.QUARK_LIBRARY_FID if provider == "quark" else cfg.LIBRARY_ROOT_CID
        cat = classify.classify(r["media_type"], meta) if use_root else []
        if r["media_type"] == "movie":
            leaf = [classify.render(cfg.NAME_MOVIE_DIR, v)]
        else:
            leaf = [classify.render(cfg.NAME_TV_DIR, v), classify.render(cfg.NAME_SEASON_DIR, v)]
        parts = cat + [x for x in leaf if x]
        chain = []
        async with self._dest_lock:
            cid = self._root(r, provider)
            for p in parts:
                cid = await drive.ensure_dir(cid, p)
                chain.append(cid)
        r["_chain"] = chain[len(cat):]  # 片名/季这几层，删除时空了就一起清理（分类目录不动）
        label = "-".join(cat) or ("电影" if r["media_type"] == "movie" else "电视剧")
        db.update(r["id"], category=("夸克-" if provider == "quark" else "") + label)  # 联动来源按网盘+分类区分
        db.log(r["id"], ("夸克 " if provider == "quark" else "") + "入库位置：" + " / ".join(parts))
        return cid

    async def remove_placed(self, rid, old: dict, new_dest, new_provider: str = "115"):
        """替换成功前删除旧资源的文件；失败只记日志（可能留下重复文件，需要手动清理）。"""
        op = old.get("provider", "115")
        drive = self._drive(op)
        try:
            await drive.delete(old["files"])
            db.log(rid, f"已删除旧资源的 {len(old['files'])} 个文件")
        except Exception as e:  # noqa
            db.log(rid, f"删除旧文件失败（请到网盘手动清理）：{e}")
            return
        if str(old.get("dest")) != str(new_dest) or op != new_provider:  # 分类/网盘变了：旧目录空了就一并删掉
            try:
                if not await drive.list_files(self._cid(op, old["dest"])):
                    await drive.delete([old["dest"]])
            except Exception:  # noqa
                pass

    async def _is_empty(self, cid, drive, provider: str = "115") -> bool:
        try:
            if await drive.list_files(self._cid(provider, cid)):
                return False
            ld = getattr(drive, "list_dirs", None)
            return not (ld and await ld(self._cid(provider, cid)))
        except Exception:  # noqa
            return False

    async def purge(self, r) -> int:
        """删除这条请求入库到网盘（115 或夸克）里的视频文件，并把因此变空的片名/季文件夹一起清掉。返回删除的文件数。
        只删记录在案的文件（placed），不会碰目录里别的东西。"""
        try:
            placed = json.loads(r["placed"] or "null")
        except ValueError:
            placed = None
        if not placed or not placed.get("files"):
            return 0
        provider = placed.get("provider", "115")
        drive = self._drive(provider)
        await drive.delete(placed["files"])
        for cid in reversed(placed.get("chain") or [placed["dest"]]):  # 从最里层往上，空了才删
            if await self._is_empty(cid, drive, provider):
                try:
                    await drive.delete([cid])
                except Exception:  # noqa
                    break
            else:
                break
        db.update(r["id"], placed="")
        db.log(r["id"], f"已删除{'夸克' if provider == 'quark' else '115'}网盘里的 {len(placed['files'])} 个文件")
        return len(placed["files"])

    async def rename_files(self, rid, files, r, meta, hint="", drive=None, sub_map=None):
        """按命名模板改名（带 TMDB 编号等）；字幕跟着对应视频一起改（Emby 才能挂上字幕）。
        任何一步失败都只记日志，保留原文件名，不影响入库。"""
        try:
            fn = (drive or self.drive).rename
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
                for s in (sub_map or {}).get(f["id"], []):  # 字幕沿用视频新名 + 原字幕后缀：20.chs.srt -> 新名.chs.srt
                    stem, _, ext = s["name"].partition(".")
                    tail = s["name"][len(stem):]  # 保留 .chs.srt 这类语言标记
                    sname = new.rsplit(".", 1)[0] + tail
                    try:
                        await fn(s["id"], sname)
                        s["name"] = sname
                        done += 1
                    except Exception:  # noqa
                        pass
            except Exception as e:  # noqa
                db.log(rid, f"重命名失败（保留原名）：{e}")
                return
        if done:
            db.log(rid, f"已按命名格式重命名 {done} 个文件")

    async def attempt(self, r, meta, c, replace_old=None, append_eps=None) -> bool:
        return bool(await self.attempt_group([r], meta, c, replace_old, append_eps))

    async def attempt_group(self, rows, meta, c, replace_old=None, append_eps=None) -> set:
        """下载一个资源，并分给 rows 里的各个请求（电视剧一次求多季时，一个合集按季分到各自的 Season 目录）。
        返回已入库的季集合（电影是 {None}）；这个资源没用上返回空集合。
        append_eps：{季: 已入库的集号集合}。给了就是「追加模式」——只保留库中没有的新集、跳过整季占比检查。"""
        r0 = rows[0]
        rid0, stage, placed = r0["id"], None, set()
        provider = c.provider if c.kind == "share" else "115"
        drive = self._drive(provider)
        pname = "夸克" if provider == "quark" else "115"
        try:
            stage = await drive.mkdir(self._staging(provider), f"req{rid0}-{int(time.time())}")
        except Exception as e:  # noqa
            raise SetupError(f"无法在 {pname} 下载目录里建文件夹（{e}）。请到「设置」里重新选择{pname}的下载目录")
        try:
            if c.kind == "share":
                real = share_provider(c.url, c.password)  # 转存前按链接本身确认网盘，纠正候选里可能错误的标注
                if real and real != provider:
                    db.log(rid0, f"该链接实际是{('夸克' if real == 'quark' else '115')}分享，改用对应的网盘转存")
                    provider, c.provider = real, real
                drive = self._drive(provider)
                pname = "夸克" if provider == "quark" else "115"
                code, pw = (quark_mod.parse_quark_share(c.url, c.password) if provider == "quark" else parse_115_share(c.url, c.password))
                await drive.receive_share(code, pw, stage)
            else:
                h = await drive.add_offline(c.url, stage)
                deadline = time.time() + cfg.OFFLINE_TIMEOUT
                while True:
                    st = await drive.offline_state(h)
                    if st == "done":
                        break
                    if st == "failed" or time.time() > deadline:
                        raise RuntimeError("离线下载失败或超时")
                    await asyncio.sleep(self.poll)
            listing = await drive.list_files(stage)
            keep, drop = select_files(listing, rules())
            sub_map, sub_orphan = pair_subs(listing, keep)  # 字幕跟视频一起入库，不能丢
            if not keep:
                raise RuntimeError("没有符合条件的视频文件（" + explain_files(listing, rules()) + f"；每个视频要在 {cfg.KEEP_MIN_MB:g}MB~{cfg.MAX_GB:g}GB 之间）")
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
                    have = (append_eps or {}).get(x["season"])
                    if have is not None:  # 追加模式：只留库里没有的新集，不做整季占比检查
                        new_fl, seen = [], set()
                        for f in fl:
                            ep = classify.episode_of(f["name"])
                            if ep is None or ep in have or ep in seen:
                                continue
                            seen.add(ep)
                            new_fl.append(f)
                        dropped = len(fl) - len(new_fl)
                        if not new_fl:
                            db.log(x["id"], f"资源里第 {x['season']} 季的 {len(fl)} 个视频都已在库中，没有新集")
                            continue
                        db.log(x["id"], f"追加模式：资源里第 {x['season']} 季共 {len(fl)} 个视频，其中 {len(new_fl)} 个是新集（跳过已在库的 {dropped} 个）")
                        fl = new_fl
                    else:
                        have = None
                        eps = meta.get("season_eps", {}).get(x["season"]) or meta.get("episodes", 0)
                        need = max(2, math.ceil(eps * cfg.SEASON_RATIO / 100)) if (eps >= 4 and cfg.SEASON_RATIO > 0) else 0
                        if len(fl) < need:  # 疑似只是单集或残缺的包
                            db.log(x["id"], f"第 {x['season']} 季只有 {len(fl)} 个视频，已播出约 {eps} 集，至少要 {need} 个（占比 {cfg.SEASON_RATIO:g}%），疑似不是整季，跳过")
                            continue
                    plan.append((x, fl))
                if len(by) > 1 or len(rows) > 1:
                    self._log_all(rows, "合集：" + "、".join(f"第{s}季 {len(fl)} 个" for s, fl in sorted(by.items())))
                if not plan:
                    raise RuntimeError("这个资源里没有哪一季的视频数量够（疑似单集或残缺的包；可以在设置里调低「整季最少集数占比」）")
            for x, fl in plan:
                xid = x["id"]
                db.log(xid, f"保留 {len(fl)} 个视频，丢弃 {len(keep) - len(fl) + len(drop)} 个文件")
                try:
                    dest = await self.dest_dir(x, meta, provider)
                except Exception as e:  # noqa
                    raise SetupError(f"无法在 {pname} 影视目录里建文件夹（{e}）。请到「设置」里重新选择{pname}的影视根目录")
                if replace_old and len(rows) == 1:
                    await self.remove_placed(xid, replace_old, dest, provider)
                await self.rename_files(xid, fl, x, meta, c.title, drive, sub_map=sub_map)
                ids = [f["id"] for f in fl]
                nsub = 0
                for f in fl:
                    for s in sub_map.get(f["id"], []):
                        ids.append(s["id"])
                        nsub += 1
                await drive.move(ids, dest)
                if nsub:
                    db.log(xid, f"随视频一起入库 {nsub} 个字幕文件")
                files = [str(f["id"]) for f in fl]  # placed 里只记视频，purge/替换按视频清理
                if have is not None:  # 追加模式：placed 里合并旧文件的记录，purge/替换时能整体删干净
                    try:
                        oldp = json.loads(db.get(xid)["placed"] or "null") or {}
                        files = sorted(set(oldp.get("files") or []) | set(files), key=lambda v: int(v))
                    except (ValueError, TypeError):
                        pass
                db.update(xid, placed=json.dumps({"dest": str(dest), "files": files, "provider": provider,
                                                  "chain": [str(ch) for ch in x.pop("_chain", [dest])]}))
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
                    if cfg.KEEP_FAILED and not placed:  # 没入库的下载不删，改名留在下载目录里
                        await drive.rename(stage, f"未入库-{rid0}-{int(time.time())}")
                        self._log_all(rows, "这次下载的内容没有删除，已留在下载目录里（文件夹名以「未入库-」开头）")
                    else:
                        await drive.delete([stage])
                        if not placed:
                            self._log_all(rows, "这次下载的内容已从下载目录删除（想保留可以在设置里开启「保留没通过检查的下载」）")
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
