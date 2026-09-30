import asyncio
import os
import sys
import tempfile
import unittest

os.environ["DB_PATH"] = tempfile.mktemp(suffix=".db")
os.environ["P115_STAGING_CID"] = "1"
os.environ["P115_DEST_MOVIE_CID"] = "2"
os.environ["LITEPAN_WAIT_SECONDS"] = "0"
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
try:
    import httpx  # noqa: F401
except ImportError:  # 离线沙箱里没有 httpx，测试只用到纯逻辑，打个桩即可
    import types
    _m = types.ModuleType("httpx")
    _m.AsyncClient = object
    sys.modules["httpx"] = _m

from app import db  # noqa: E402
from app.filters import GB, Rules, file_ok, parse_resolution, parse_season, select_files, title_match  # noqa: E402
from app.pipeline import Pipeline  # noqa: E402
from app.sources import Candidate, build_candidates  # noqa: E402

R = Rules(720, 0.5 * GB, 5 * GB)


class Filters(unittest.TestCase):
    def test_resolution(self):
        self.assertEqual(parse_resolution("Dune.2024.2160p.WEB-DL"), 2160)
        self.assertEqual(parse_resolution("沙丘2 4K 中字"), 2160)
        self.assertEqual(parse_resolution("Dune 2024 1080p"), 1080)
        self.assertEqual(parse_resolution("Dune 2024 480p"), 480)
        self.assertEqual(parse_resolution("Dune 2024"), 0)

    def test_file_rules(self):
        self.assertTrue(file_ok("a.2024.1080p.mkv", 3 * GB, R))
        self.assertFalse(file_ok("a.nfo", 3 * GB, R))          # 非视频
        self.assertFalse(file_ok("sample.mkv", 50 * 1024 ** 2, R))  # 太小
        self.assertTrue(file_ok("a.1080p.mkv", 0.6 * GB, R))    # 500MB 以上可以
        self.assertFalse(file_ok("a.1080p.mkv", 0.3 * GB, R))
        self.assertFalse(file_ok("a.mkv", 8 * GB, R))           # 太大
        self.assertFalse(file_ok("a.480p.mkv", 2 * GB, R))      # 分辨率不足
        self.assertTrue(file_ok("a.mkv", 2 * GB, R))            # 没标分辨率，体积兜底

    def test_season(self):
        self.assertEqual(parse_season("Show S01 1080p"), (1, True, False))
        self.assertEqual(parse_season("Show S01E05 1080p"), (1, False, False))
        self.assertEqual(parse_season("Show 第二季 全集"), (2, True, False))
        self.assertTrue(parse_season("Show S01-S03")[2])

    def test_title_match(self):
        self.assertTrue(title_match("Dune.Part.Two.2024.1080p", ["Dune: Part Two"], "2024", "movie", None))
        self.assertFalse(title_match("Dune.Part.Two.2021.1080p", ["Dune: Part Two"], "2024", "movie", None))
        self.assertFalse(title_match("Show S02E01", ["Show"], "2020", "tv", 2))  # 单集不收
        self.assertTrue(title_match("Show S02 1080p", ["Show"], "2020", "tv", 2))

    def test_build_candidates(self):
        meta = {"names": ["Dune: Part Two"], "year": "2024", "episodes": 0}
        raw = [
            Candidate("magnet", "Dune.Part.Two.2024.1080p.WEB-DL.x265 中字", "magnet:?xt=urn:btih:a", size=3 * GB, seeders=30),
            Candidate("magnet", "Dune.Part.Two.2024.720p.CAM", "magnet:?xt=urn:btih:b", size=3 * GB),
            Candidate("magnet", "Dune.Part.Two.2024.1080p.REMUX", "magnet:?xt=urn:btih:c", size=30 * GB),
            Candidate("magnet", "Dune.Part.Two.2024.WEB-DL", "magnet:?xt=urn:btih:d", size=3 * GB),  # 没标分辨率：保留但靠后
            Candidate("magnet", "Dune.Part.Two.2024.1080p.WEB-DL", "magnet:?xt=urn:btih:f", size=0.3 * GB),  # 太小
            Candidate("magnet", "Dune.Part.Two.2024.480p", "magnet:?xt=urn:btih:e", size=1.5 * GB),
            Candidate("share", "沙丘2 Dune Part Two 2024 1080p", "https://115.com/s/abc?password=x"),
        ]
        out = build_candidates(raw, meta, "movie", None, R)
        self.assertEqual([c.url for c in out], ["magnet:?xt=urn:btih:a", "https://115.com/s/abc?password=x", "magnet:?xt=urn:btih:d"])


class FakeDrive:
    """第 1 个候选离线失败；第 2 个成功，里面混着样片/nfo/海报。"""

    def __init__(self):
        self.n, self.dirs, self.files, self.moved, self.deleted = 0, {}, {}, [], []

    async def mkdir(self, parent, name):
        self.n += 1
        self.dirs[self.n + 100] = name
        return self.n + 100

    async def ensure_dir(self, parent, name):
        return await self.mkdir(parent, name)

    async def add_offline(self, magnet, dest):
        self.cur = (magnet, dest)
        return magnet[-1]

    async def offline_state(self, h):
        return "failed" if h == "a" else "done"

    async def receive_share(self, *a):
        raise RuntimeError("分享已失效")

    async def list_files(self, cid):
        return [{"id": 1, "name": "Dune.2024.1080p.mkv", "size": 3 * GB},
                {"id": 2, "name": "sample.mkv", "size": 40 * 1024 ** 2},
                {"id": 3, "name": "poster.jpg", "size": 1000},
                {"id": 4, "name": "readme.nfo", "size": 10}]

    async def move(self, ids, dest):
        self.moved.append((ids, dest))

    async def delete(self, ids):
        self.deleted += ids


class ApprovalTest(unittest.TestCase):
    def test_pending_promote_batch(self):
        a = db.create("movie", 900, None, "A (2020)", "小明", "pending")
        self.assertEqual(db.get(a)["requester"], "小明")
        self.assertIsNone(db.create("movie", 900, None, "A (2020)", "小红", "pending"))  # 重复请求不重复建
        self.assertEqual(db.promote("movie", 900, None), a)
        self.assertEqual(db.get(a)["status"], "queued")
        b = db.create("movie", 901, None, "B (2021)", "小红", "pending")
        c = db.create("movie", 902, None, "C (2022)", "小刚", "pending")
        self.assertEqual(db.batch([b, c, a], "approve"), [b, c])   # a 已不是待审批，不受影响
        db.update(b, status="failed")
        self.assertEqual(db.batch([b, c], "retry"), [b])
        db.update(c, status="done")
        self.assertEqual(db.batch([a, b, c], "delete"), [c])       # 进行中的不删


class UserLinkTest(unittest.TestCase):
    def test_stats_and_quota_count(self):
        db.create("movie", 950, None, "P (2020)", "Alice", "queued", "u1")
        r = db.create("movie", 951, None, "Q (2020)", "alice", "queued", "u1")
        db.update(r, status="done")
        x = db.create("movie", 952, None, "R (2020)", "ALICE", "rejected", "u1")
        self.assertEqual(db.count_recent("alice"), 2)          # 大小写不敏感，被拒绝的不算
        st = {s["requester"].lower(): s for s in db.user_stats()}
        self.assertEqual(st["alice"]["total"], 3)
        self.assertEqual(st["alice"]["done"], 1)
        self.assertEqual(len(db.list_all("alice")), 3)

    def test_emby_check(self):
        from app import emby
        from app.config import cfg
        cfg.EMBY_URL, cfg.EMBY_KEY = "http://x", "k"
        emby._cache.update(t=__import__("time").time(), users={
            "bob": {"id": "b1", "name": "Bob", "disabled": False},
            "old": {"id": "o1", "name": "Old", "disabled": True}})
        run = asyncio.run
        self.assertEqual(run(emby.check("Bob")), (True, "", "b1"))
        ok, why, _ = run(emby.check("old"))
        self.assertFalse(ok); self.assertIn("停用", why)
        ok, why, _ = run(emby.check("ghost"))
        self.assertFalse(ok); self.assertIn("没有该用户", why)
        cfg.EMBY_URL = ""
        self.assertEqual(run(emby.check("anyone")), (True, "", ""))  # Emby 没配则放行


class PipelineTest(unittest.TestCase):
    def test_fallback_and_cleanup(self):
        meta = {"names": ["Dune: Part Two"], "year": "2024", "episodes": 0}
        raw = [Candidate("share", "Dune Part Two 2024 1080p", "https://115.com/s/abc?password=x"),
               Candidate("magnet", "Dune.Part.Two.2024.1080p.WEB-DL", "magnet:?xt=urn:btih:b", size=3 * GB, seeders=5),
               Candidate("magnet", "Dune.Part.Two.2024.1080p.BluRay", "magnet:?xt=urn:btih:a", size=3 * GB, seeders=50)]
        done = []

        async def meta_fn(r): return meta
        async def search_fn(m, t, s): return raw
        async def after(r): done.append(r["id"])

        drive = FakeDrive()
        rid = db.create("movie", 1, None, "Dune: Part Two (2024)")
        asyncio.run(Pipeline(drive, meta_fn, search_fn, after, poll=0).run(rid))
        r = db.get(rid)
        self.assertEqual(r["status"], "done")
        self.assertEqual(done, [rid])
        self.assertEqual(len(drive.moved), 1)
        self.assertEqual(drive.moved[0][0], [1])  # 只移动合格的那个视频
        self.assertEqual(len(drive.deleted), 3)   # 3 次尝试的暂存目录都被清理
        self.assertIn("换下一个", r["log"])

    def test_all_fail(self):
        meta = {"names": ["X"], "year": "2020", "episodes": 0}
        async def meta_fn(r): return meta
        async def search_fn(m, t, s): return []
        async def after(r): pass
        rid = db.create("movie", 2, None, "X (2020)")
        asyncio.run(Pipeline(FakeDrive(), meta_fn, search_fn, after, poll=0).run(rid))
        self.assertEqual(db.get(rid)["status"], "failed")


if __name__ == "__main__":
    unittest.main()
