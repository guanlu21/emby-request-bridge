import asyncio
import os
import sys
import tempfile
import unittest

os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "bridge.db")
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
from app.filters import (GB, Rules, file_ok, has_watermark, parse_resolution, parse_season,  # noqa: E402
                         pick_best_file, score, select_files, title_match)
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


class NewRules(unittest.TestCase):
    def test_watermark(self):
        self.assertTrue(has_watermark("Dune.2024.1080p.带水印"))
        self.assertTrue(has_watermark("Dune 2024 1080p watermark"))
        self.assertFalse(has_watermark("Dune 2024 1080p 无水印"))
        self.assertFalse(has_watermark("Dune 2024 1080p No Watermark"))
        self.assertFalse(has_watermark("Dune 2024 1080p"))
        self.assertFalse(file_ok("a.1080p.水印版.mkv", 2 * GB, R))
        out = build_candidates([Candidate("magnet", "Dune.Part.Two.2024.1080p.水印", "magnet:?xt=urn:btih:w", size=2 * GB)],
                               {"names": ["Dune: Part Two"], "year": "2024", "episodes": 0}, "movie", None, R)
        self.assertEqual(out, [])

    def test_prefer_1080_and_size_window(self):
        r = Rules(720, 0.5 * GB, 5 * GB, 1 * GB, 3 * GB)
        s1080_in = score("Dune 2024 1080p WEB-DL", per_file=2 * GB, rules=r)
        s1080_big = score("Dune 2024 1080p WEB-DL", per_file=4.5 * GB, rules=r)
        s2160_in = score("Dune 2024 2160p WEB-DL", per_file=2.5 * GB, rules=r)
        s720_in = score("Dune 2024 720p WEB-DL", per_file=2 * GB, rules=r)
        self.assertGreater(s1080_in, s1080_big)   # 1-3GB 区间内更优先
        self.assertGreater(s1080_in, s2160_in)    # 1080p 优先于 2160p
        self.assertGreater(s1080_in, s720_in)

    def test_pick_best_file(self):
        r = Rules(720, 0.5 * GB, 5 * GB, 1 * GB, 3 * GB)
        files = [{"id": 1, "name": "a.2160p.mkv", "size": 4.8 * GB},
                 {"id": 2, "name": "a.1080p.mkv", "size": 2.2 * GB},
                 {"id": 3, "name": "a.720p.mkv", "size": 1.2 * GB}]
        self.assertEqual(pick_best_file(files, r)["id"], 2)


class SettingsAuthTest(unittest.TestCase):
    def test_settings_roundtrip_and_proxy(self):
        from app import settings
        settings.save({"proxy_enabled": True, "proxy_url": "192.168.1.5:7891", "proxy_user": "u", "proxy_pass": "p@ss",
                       "tmdb_key": "K1", "language": "zh-CN"})
        self.assertEqual(settings.proxy(), "http://u:p%40ss@192.168.1.5:7891")
        settings.save({"tmdb_key": "", "proxy_pass": ""})           # 留空 = 不修改
        self.assertEqual(settings.get()["tmdb_key"], "K1")
        pub = settings.public()
        self.assertNotIn("K1", str(pub)); self.assertTrue(pub["secret_set"]["tmdb_key"])   # 密钥不回传
        self.assertEqual(pub["values"]["tmdb_key"], "")
        settings.save({"proxy_enabled": False})
        self.assertIsNone(settings.proxy())

    def test_folder_number_and_admins(self):
        from app import settings
        settings.save({"p115_dest_movie_cid": "12345", "p115_dest_movie_label": "影视/电影", "max_gb": "3", "quota_weekly": "2",
                       "emby_url": "http://192.168.1.10:8096/"})
        s = settings.get()
        self.assertEqual((s["p115_dest_movie_cid"], s["p115_dest_movie_label"]), (12345, "影视/电影"))
        self.assertEqual((s["max_gb"], s["quota_weekly"]), (3, 2))
        self.assertEqual(s["emby_url"], "http://192.168.1.10:8096")              # 去掉结尾斜杠
        from app.config import cfg
        self.assertEqual(cfg.P115_DEST_MOVIE_CID, 12345)                          # cfg 读到网页设置
        settings.set_internal(admins="Alice, bob")
        self.assertEqual(settings.admins(), {"alice", "bob"})

    def test_token_autogen(self):
        from app import settings
        self.assertTrue(len(settings.get()["token"]) >= 16)

    def test_private_host(self):
        from app.auth import is_private_host, normalize_url
        self.assertEqual(normalize_url("192.168.1.10:8096/"), "http://192.168.1.10:8096")
        for u in ("http://192.168.1.10:8096", "http://10.0.0.2", "http://localhost:8096", "http://emby:8096", "http://nas.local"):
            self.assertTrue(is_private_host(u), u)
        for u in ("http://example.com", "https://emby.mydomain.net:8920", "http://8.8.8.8"):
            self.assertFalse(is_private_host(u), u)

    def test_session_token(self):
        from app import auth
        tok = auth.make({"uid": "1", "name": "bob", "admin": False})
        self.assertEqual(auth.verify(tok)["name"], "bob")
        self.assertIsNone(auth.verify(tok[:-2] + "xx"))              # 篡改签名
        self.assertIsNone(auth.verify(auth.make({"name": "x"}, ttl=-1)))  # 过期


class LitePanTest(unittest.TestCase):
    def _fake_httpx(self, calls, status=200):
        class Resp:
            status_code, text = status, "ok"
        class Client:
            def __init__(self, *a, **k): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def post(self, url, json=None, headers=None):
                calls.append((url, json, headers)); return Resp()
        from app import litepan
        litepan.httpx = type("H", (), {"AsyncClient": Client})

    def test_send_format(self):
        from app import litepan
        from app.config import cfg
        calls = []
        self._fake_httpx(calls)
        cfg.LITEPAN_URL, cfg.LITEPAN_KEY = "http://192.168.1.10:5211/", "lpk_api_abc"
        cfg.LITEPAN_EVENT, cfg.LITEPAN_SOURCE = "download_completed", "RequestBridge"
        ok, _ = asyncio.run(litepan.send("download_completed"))
        url, body, hdr = calls[0]
        self.assertTrue(ok)
        self.assertEqual(url, "http://192.168.1.10:5211/api/open/automation/events")
        self.assertEqual(hdr["Authorization"], "Bearer lpk_api_abc")
        self.assertEqual(body["event"], "download_completed")
        self.assertEqual(body["source"], "RequestBridge")
        cfg.LITEPAN_SOURCE = ""
        asyncio.run(litepan.send("x"))
        self.assertNotIn("source", calls[1][1])            # 来源留空则不带

    def test_bad_key_message(self):
        from app import litepan
        from app.config import cfg
        self._fake_httpx([], status=401)
        cfg.LITEPAN_URL, cfg.LITEPAN_KEY = "http://x:5211", "k"
        ok, info = asyncio.run(litepan.send("e"))
        self.assertFalse(ok); self.assertIn("任务执行", info)

    def test_debounce_merges_requests(self):
        from app import litepan
        from app.config import cfg
        calls = []
        self._fake_httpx(calls)
        cfg.LITEPAN_URL, cfg.LITEPAN_KEY, cfg.LITEPAN_DELAY = "http://x:5211", "k", 0.05
        cfg.LITEPAN_SOURCE = "RequestBridge"
        a = db.create("movie", 801, None, "A (2020)")
        b = db.create("movie", 802, None, "B (2021)")

        async def go():
            litepan.schedule(a)
            await asyncio.sleep(0.02)
            litepan.schedule(b)            # 窗口内再来一个：合并
            await asyncio.sleep(0.25)
        asyncio.run(go())
        self.assertEqual(len(calls), 1)
        self.assertIn("已通知 LitePan", db.get(a)["log"])
        self.assertIn("已通知 LitePan", db.get(b)["log"])


class CookieOnlyTest(unittest.TestCase):
    def test_backend_selection(self):
        from app import drive115, settings
        from app.config import cfg

        class FakeCookieDrive:
            def __init__(self, cookie): self.cookie = cookie
            async def list_dirs(self, cid): return [{"id": 9, "name": "电影"}]
        orig = drive115.P115Drive
        drive115.P115Drive = FakeCookieDrive
        try:
            settings.set_internal(p115_refresh="", p115_access="")
            cfg.P115_COOKIE = ""
            d = drive115.CompositeDrive()
            with self.assertRaises(RuntimeError) as cm:        # 都没有：明确提示
                d.backend()
            self.assertIn("Cookie", str(cm.exception))
            cfg.P115_COOKIE = "UID=1; CID=2; SEID=3"
            self.assertEqual(d.mode(), "cookie")
            self.assertEqual(asyncio.run(d.list_dirs(0)), [{"id": 9, "name": "电影"}])   # 只有 Cookie 也能选目录
            self.assertTrue(d.can_receive_share())
            settings.set_internal(p115_refresh="R", p115_expires=0)   # 授权了开放平台 → 优先走 Open
            self.assertIs(d.backend(), d.open)
            self.assertEqual(d.mode(), "open")
        finally:
            drive115.P115Drive = orig
            settings.set_internal(p115_refresh="")
            cfg.P115_COOKIE = ""


class MatchingTest(unittest.TestCase):
    def test_looser_matching(self):
        from app.filters import match_reason
        # 英文片名标点不同、词序不同
        self.assertEqual(match_reason("Part.Two.Dune.2024.1080p", ["Dune: Part Two"], "2024", "movie", None), "")
        # 副标题不写
        self.assertEqual(match_reason("沙丘 2024 4K 中字", ["沙丘：第二部"], "2024", "movie", None), "")
        # 年份 ±1 可以，差太多不行，没写年份可以
        self.assertEqual(match_reason("Movie.2023.1080p", ["Movie"], "2024", "movie", None), "")
        self.assertEqual(match_reason("Movie.2019.1080p", ["Movie"], "2024", "movie", None), "年份不符")
        self.assertEqual(match_reason("Movie.1080p", ["Movie"], "2024", "movie", None), "")
        # 片名本身带数字
        self.assertEqual(match_reason("1917.2019.1080p", ["1917"], "2019", "movie", None), "")
        self.assertEqual(match_reason("完全无关 2024", ["Dune"], "2024", "movie", None), "标题不匹配")

    def test_tv_rules(self):
        from app.filters import match_reason
        n = ["笑傲江湖"]
        self.assertEqual(match_reason("笑傲江湖 全40集 1080p", n, "2001", "tv", 1), "")              # 单季剧不写 S01
        self.assertNotEqual(match_reason("笑傲江湖 全40集 1080p", n, "2001", "tv", 2), "")
        self.assertNotEqual(match_reason("笑傲江湖.2013.E01-E50.1080p", n, "2001", "tv", 1), "")     # 另一个年代的版本
        self.assertEqual(match_reason("笑傲江湖.2001.E01-E40.1080p", n, "2001", "tv", 1), "")
        self.assertEqual(match_reason("Show S01E01-E10 1080p", ["Show"], "2020", "tv", 1), "")
        self.assertEqual(match_reason("Show S01E05 1080p", ["Show"], "2020", "tv", 1), "只有单集")
        self.assertNotEqual(match_reason("笑傲江湖 合集", n, "2001", "tv", 1), "")                    # 含糊的"合集"不收

    def test_report(self):
        from collections import Counter
        meta = {"names": ["Dune"], "year": "2024", "episodes": 0}
        rep = {}
        build_candidates([Candidate("magnet", "Other.Movie.2024.1080p", "magnet:?xt=urn:btih:1", size=2 * GB),
                          Candidate("magnet", "Dune.2019.1080p", "magnet:?xt=urn:btih:2", size=2 * GB),
                          Candidate("magnet", "Dune.2024.1080p.带水印", "magnet:?xt=urn:btih:3", size=2 * GB)],
                         meta, "movie", None, R, rep)
        self.assertEqual(dict(rep["counts"]), {"标题不匹配": 1, "年份不符": 1, "带水印": 1})
        self.assertEqual(len(rep["samples"]), 3)


class KiteTest(unittest.TestCase):
    def test_parse_json_list_and_dict(self):
        from app import kite
        m1 = "magnet:?xt=urn:btih:" + "a" * 40
        m2 = "magnet:?xt=urn:btih:" + "b" * 40
        r = kite.parse_results('[{"title":"流浪地球2 2023 1080p","magnet":"%s","size":"2.3 GB","seeders":12},{"name":"x","url":"%s"}]' % (m1, m2))
        self.assertEqual((r[0]["title"], r[0]["magnet"], r[0]["seeders"]), ("流浪地球2 2023 1080p", m1, 12))
        self.assertAlmostEqual(r[0]["size"], 2.3 * GB, delta=1)
        self.assertEqual(r[1]["magnet"], m2)
        r = kite.parse_results('{"results":[{"title":"T","magnet_link":"%s","size":2500000000}]}' % m1)
        self.assertEqual(r[0]["size"], 2500000000)

    def test_parse_plain_text(self):
        from app import kite
        m1 = "magnet:?xt=urn:btih:" + "c" * 40
        m2 = "magnet:?xt=urn:btih:" + "d" * 40
        text = f"1. 流浪地球2.2023.1080p.WEB-DL.mkv\n   大小: 3.1 GB\n   {m1}\n\n2. The.Wandering.Earth.2.2023.2160p\n   大小: 12 GB\n   磁力: {m2}"
        r = kite.parse_results(text)
        self.assertEqual(len(r), 2)
        self.assertIn("流浪地球2", r[0]["title"]); self.assertAlmostEqual(r[0]["size"], 3.1 * GB, delta=1)
        self.assertIn("Wandering", r[1]["title"])

    def test_session_flow_and_sse(self):
        from app import kite
        from app.config import cfg
        cfg.KITE_URL, cfg.KITE_TOKEN = "https://magnet.example/mcp", "mcp__tok"
        calls = []
        mag = "magnet:?xt=urn:btih:" + "e" * 40

        class Resp:
            def __init__(self, code=200, body=None, sse=False, hdr=None):
                self.status_code, self._b, self._sse = code, body, sse
                self.headers = {"content-type": "text/event-stream" if sse else "application/json", **(hdr or {})}
                import json as _j
                self.text = ("event: message\ndata: " + _j.dumps(body) + "\n\n") if sse else ""
            def json(self): return self._b

        class Client:
            def __init__(self, *a, **k): pass
            async def aclose(self): pass
            async def post(self, url, json=None, headers=None):
                calls.append((json, dict(headers)))
                m = json["method"]
                if m == "initialize":
                    return Resp(body={"jsonrpc": "2.0", "id": json["id"], "result": {}}, hdr={"mcp-session-id": "S1"})
                if m == "notifications/initialized":
                    return Resp(202, {})
                if m == "tools/call":   # 用 SSE 回包
                    return Resp(sse=True, body={"jsonrpc": "2.0", "id": json["id"], "result": {"content": [
                        {"type": "text", "text": '[{"title":"片 2023 1080p","magnet":"%s","size":"2 GB"}]' % mag}]}})
                return Resp(body={"jsonrpc": "2.0", "id": json["id"], "result": {"tools": [{"name": "magnet_search"}]}})
        kite.httpx = type("H", (), {"AsyncClient": Client, "Response": object})

        async def go():
            s = kite.KiteSession()
            r1 = await s.search("片 2023", 20)
            r2 = await s.search("片", 20)
            return r1, r2, await s.tools()
        r1, r2, tools = asyncio.run(go())
        self.assertEqual(r1[0]["magnet"], mag); self.assertEqual(len(r2), 1)
        self.assertEqual(tools, ["magnet_search"])
        methods = [c[0]["method"] for c in calls]
        self.assertEqual(methods.count("initialize"), 1)               # 会话只初始化一次
        call = next(c for c in calls if c[0]["method"] == "tools/call")
        self.assertEqual(call[0]["params"], {"name": "magnet_search", "arguments": {"query": "片 2023", "limit": 20}})
        self.assertEqual(call[1]["Authorization"], "Bearer mcp__tok")
        self.assertEqual(call[1]["Mcp-Session-Id"], "S1")             # 沿用服务端给的会话 ID


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


class OpenDriveTest(unittest.TestCase):
    def test_share_skipped_without_cookie(self):
        from app.config import cfg
        from app.drive115 import CompositeDrive
        cfg.P115_COOKIE = ""
        d = CompositeDrive()
        self.assertFalse(d.can_receive_share())
        with self.assertRaises(RuntimeError):
            asyncio.run(d.receive_share("abc", "x", 1))

    def test_pipeline_skips_shares_and_uses_magnet(self):
        from app.config import cfg
        cfg.P115_COOKIE = ""
        meta = {"names": ["Dune: Part Two"], "year": "2024", "episodes": 0}
        raw = [Candidate("share", "Dune Part Two 2024 1080p 中字", "https://115.com/s/abc?password=x"),
               Candidate("magnet", "Dune.Part.Two.2024.1080p.WEB-DL", "magnet:?xt=urn:btih:b", size=2 * GB, seeders=5)]

        class D(FakeDrive):
            def can_receive_share(self): return False
        drive = D()
        async def meta_fn(r): return meta
        async def search_fn(m, t, s): return raw
        async def after(r): pass
        rid = db.create("movie", 777, None, "Dune: Part Two (2024)")
        asyncio.run(Pipeline(drive, meta_fn, search_fn, after, poll=0).run(rid))
        r = db.get(rid)
        self.assertEqual(r["status"], "done")
        self.assertIn("跳过 1 个分享链接", r["log"])


class PipelineTest(unittest.TestCase):
    def test_fallback_and_cleanup(self):
        meta = {"names": ["Dune: Part Two"], "year": "2024", "episodes": 0}
        raw = [Candidate("share", "Dune Part Two 2024 1080p 中字 无水印", "https://115.com/s/abc?password=x"),
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
