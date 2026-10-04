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
        self.assertTrue(file_ok("a.1080p.mkv", 0.3 * GB, R))        # 下载后保留的下限是 100MB（不是 500MB）
        self.assertFalse(file_ok("a.1080p.mkv", 60 * 1024 ** 2, R))   # 60MB 当样片删掉
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

        c2 = db.create("movie", 803, None, "C (2022)")

        async def go():
            litepan.schedule(a, "Emby求片-电影-国产")
            await asyncio.sleep(0.02)
            litepan.schedule(b, "Emby求片-电影-国产")     # 同来源窗口内再来一个：合并
            litepan.schedule(c2, "Emby求片-电影-欧美")     # 不同来源：单独发一次
            await asyncio.sleep(0.3)
        asyncio.run(go())
        self.assertEqual(sorted(c[1]["source"] for c in calls), ["Emby求片-电影-国产", "Emby求片-电影-欧美"])
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
    def test_strict_name_and_year(self):
        from app.filters import match_reason, year_rank
        mr = lambda t, names, y="2015", mt="movie", ss=None: match_reason(t, names, y, mt, ss)
        n = ["唐人街探案", "Detective Chinatown"]
        self.assertEqual(mr("唐人街探案.2015.1080p.国语中字", n), "")
        self.assertEqual(mr("Detective.Chinatown.2015.1080p", n), "")
        self.assertEqual(mr("【高清】唐人街 探案 (2015) 1080P", n), "")              # 空格/标点不影响
        # 名称不能拆开：只含「唐人街」或「探案」的不算
        self.assertEqual(mr("唐人街之探案.2015.1080p", n), "标题不匹配")
        self.assertEqual(mr("唐人街.2015.1080p", n), "标题不匹配")
        self.assertEqual(mr("警察探案 唐人街 2015", n), "标题不匹配")
        # 英文名也要完整、按顺序：不分词、不乱序
        self.assertEqual(mr("Part.Two.Dune.2024.1080p", ["Dune: Part Two"], "2024"), "标题不匹配")
        self.assertEqual(mr("Dune.Part.Two.2024.1080p", ["Dune: Part Two"], "2024"), "")
        # 不去副标题
        self.assertEqual(mr("沙丘 2024 4K 中字", ["沙丘：第二部"], "2024"), "标题不匹配")
        # 续集不算（片名后面紧跟数字）
        self.assertEqual(mr("唐人街探案2.2018.1080p", n), "标题不匹配")
        self.assertEqual(mr("唐人街探案 3 2021", n), "标题不匹配")
        # 年份是辅助条件：同年、差一年、没写年份都行；差太多不行
        self.assertEqual(mr("唐人街探案.2015.1080p", n), "")
        self.assertEqual(mr("唐人街探案.2016.1080p", n), "")
        self.assertEqual(mr("唐人街探案.1080p.国语", n), "")
        self.assertEqual(mr("唐人街探案.2012.1080p", n), "年份不符")
        self.assertEqual([year_rank(t, n, "2015") for t in ("唐人街探案 2015", "唐人街探案 2014", "唐人街探案")], [2, 1, 0])
        # 片名本身带数字
        self.assertEqual(mr("1917.2019.1080p", ["1917"], "2019"), "")

    def test_sequels_and_hdtc(self):
        from app.filters import match_reason, BAD_TAGS
        n = ["流浪地球", "The Wandering Earth"]
        self.assertNotEqual(match_reason("[DBD-Raws][流浪地球2/The Wandering Earth Ⅱ/The Wandering Earth 2][1080P]", n, "2019", "movie", None), "")
        self.assertNotEqual(match_reason("Iron.Man.2.2010.1080p", ["Iron Man"], "2008", "movie", None), "")
        self.assertEqual(match_reason("流浪地球.The.Wandering.Earth.2019.1080p.WEB-DL", n, "2019", "movie", None), "")
        self.assertEqual(match_reason("流浪地球 2019 国语中字 1080p", n, "2019", "movie", None), "")
        self.assertEqual(match_reason("流浪地球2 2023 1080p", ["流浪地球2"], "2023", "movie", None), "")   # 片名本身带 2 不受影响
        self.assertTrue(BAD_TAGS.search("流浪地球.2019.1080p.HDTC.X264"))

    def test_tv_rules(self):
        from app.filters import match_reason
        n = ["笑傲江湖"]
        self.assertEqual(match_reason("笑傲江湖 全40集 1080p", n, "2001", "tv", 1), "")              # 单季剧不写 S01
        self.assertNotEqual(match_reason("笑傲江湖 全40集 1080p", n, "2001", "tv", 2), "")
        self.assertNotEqual(match_reason("笑傲江湖.2013.E01-E50.1080p", n, "2001", "tv", 1), "")     # 另一个年代的版本
        self.assertEqual(match_reason("笑傲江湖.2001.E01-E40.1080p", n, "2001", "tv", 1), "")
        self.assertEqual(match_reason("Show S01E01-E10 1080p", ["Show"], "2020", "tv", 1), "")
        self.assertEqual(match_reason("Show S01E05 1080p", ["Show"], "2020", "tv", 1), "只有单集")
        self.assertEqual(match_reason("笑傲江湖 合集", n, "2001", "tv", 1), "")                       # 第 1 季：没写季/集数的标题也收，下载后再核对文件

    def test_multi_season_and_bare_titles(self):
        from app.filters import filter_season, match_reason, season_span, verify_season, single_episode_only
        n = ["我的兄弟叫顺溜"]
        # 用户日志里被误拒的几种写法：没写季/没写年份，都应该收（第 1 季）
        for t in ("【高清剧集网发布 www.PTHDTV.com】我的兄弟叫顺溜[高码版][全26集][国语配音+中文字幕]",
                  "[我的兄弟叫顺溜][26集][战争剧][2009][mkv]", "我的兄弟叫顺溜", "[a6a5.com][国产剧][我的兄弟叫顺溜][全26集][国语中字]"):
            self.assertEqual(match_reason(t, n, "2009", "tv", 1), "", t)
        self.assertEqual(match_reason("我的兄弟叫顺溜 第5集", n, "2009", "tv", 1), "只有单集")
        self.assertEqual(match_reason("我的兄弟叫顺溜 EP05", n, "2009", "tv", 1), "只有单集")
        # 多季合集：请求的季在范围内就收
        g = ["权力的游戏"]
        self.assertEqual(season_span("权力的游戏 第一季至第三季"), (1, 3))
        self.assertEqual(season_span("权力的游戏 全8季"), (1, 8))
        self.assertEqual(season_span("Show 第二季 全集"), (2, 2))               # 第二季 + 全集，不是 2 季合集
        for t, s in (("权力的游戏 S01-S08 1080p", 3), ("权力的游戏 第1-8季 全集", 5), ("权力的游戏 全8季", 8)):
            self.assertEqual(match_reason(t, g, "2011", "tv", s), "", t)
        self.assertEqual(match_reason("权力的游戏 S01-S03", g, "2011", "tv", 5), "季不在合集范围内")
        # 第 2 季以后，标题没写季：只收「全集/合集」，不收「全40集」（那是单季集数）
        self.assertEqual(match_reason("某剧 全集", ["某剧"], "2010", "tv", 2), "")
        self.assertNotEqual(match_reason("某剧 全40集", ["某剧"], "2010", "tv", 2), "")
        # 下载后：只留请求的这一季；没法确认属于哪一季就不要
        fl = [{"id": 1, "name": "E01.mkv", "path": "权游/S01/E01.mkv"}, {"id": 2, "name": "E01.mkv", "path": "权游/S02/E01.mkv"},
              {"id": 3, "name": "x.mkv", "path": "权游/第二季/x.mkv"}, {"id": 4, "name": "y.mkv", "path": "y.mkv"}]
        keep, drop = filter_season(fl, 2)
        self.assertEqual(([f["id"] for f in keep], [f["id"] for f in drop]), ([2, 3], [1, 4]))
        self.assertTrue(verify_season(keep, 2, "权力的游戏 全集"))
        self.assertFalse(verify_season([{"id": 9, "name": "E01.mkv", "path": "E01.mkv"}], 2, "某剧 全集"))
        self.assertTrue(verify_season([{"id": 9, "name": "E01.mkv", "path": "E01.mkv"}], 1, "某剧"))

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


class SetupErrorTest(unittest.TestCase):
    def _run(self, drive, rid):
        meta = {"names": ["Dune"], "year": "2024", "episodes": 0}
        raw = [Candidate("magnet", "Dune.2024.1080p.WEB-DL.%d" % i, "magnet:?xt=urn:btih:%040d" % i, size=2 * GB) for i in range(5)]

        async def meta_fn(r): return meta
        async def search_fn(m, t, s): return raw
        async def after(r): pass
        asyncio.run(Pipeline(drive, meta_fn, search_fn, after, poll=0).run(rid))
        return db.get(rid)

    def test_bad_staging_aborts_without_burning_candidates(self):
        class D(FakeDrive):
            async def mkdir(self, parent, name):
                self.calls = getattr(self, "calls", 0) + 1
                raise RuntimeError("115 接口失败: 父目录不存在。")
        d = D()
        rid = db.create("movie", 960, None, "Dune (2024)")
        r = self._run(d, rid)
        self.assertEqual(r["status"], "failed")
        self.assertIn("重新选择暂存目录", r["error"])
        self.assertEqual(d.calls, 1)                 # 不再把 8 个候选挨个试一遍
        self.assertEqual(r["tried"], "[]")           # 候选没有被记为已试，修好目录后可以直接重试

    def test_preflight_catches_unreachable_folder(self):
        class D(FakeDrive):
            async def list_dirs(self, cid): raise RuntimeError("父目录不存在")
        rid = db.create("movie", 961, None, "Dune (2024)")
        r = self._run(D(), rid)
        self.assertEqual(r["status"], "failed"); self.assertIn("下载目录无法访问", r["error"])

    def test_reset_clears_tried(self):
        rid = db.create("movie", 962, None, "X (2020)")
        db.update(rid, status="failed", tried='["a","b"]', error="x")
        self.assertEqual(db.batch([rid], "reset"), [rid])
        r = db.get(rid)
        self.assertEqual((r["status"], r["tried"], r["error"]), ("queued", "[]", ""))

    def test_big_folder_id_stays_exact(self):
        from app import settings
        from app.config import cfg
        big = "3312345678901234567"
        settings.save({"p115_staging_cid": big, "p115_staging_label": "暂存"})
        self.assertEqual(settings.public()["values"]["p115_staging_cid"], big)   # 以字符串返回，浏览器不会丢精度
        self.assertEqual(cfg.P115_STAGING_CID, int(big))


class P115AdapterTest(unittest.TestCase):
    def _drive(self, client):
        from app.drive115 import P115Drive
        d = P115Drive.__new__(P115Drive)       # 绕开真实的 p115client
        d.c, d._dest = client, {}
        return d

    def test_new_method_names_and_folder_polling(self):
        calls = []

        class NewClient:   # 只有新版方法名，没有任务列表接口
            def clouddownload_task_add_url(self, payload): calls.append(payload); return {"state": True}
            def fs_files(self, payload):
                return {"state": True, "data": self.files}
            files = []
        c = NewClient()
        d = self._drive(c)
        mag = "magnet:?xt=urn:btih:" + "a" * 40
        h = asyncio.run(d.add_offline(mag, 123))
        self.assertEqual(h, "a" * 40)
        self.assertEqual(calls[0], {"url": mag, "wp_path_id": 123})
        self.assertEqual(asyncio.run(d.offline_state(h)), "running")        # 目录里还没文件
        c.files = [{"fid": "9", "n": "a.mkv", "s": 2 * GB}]
        self.assertEqual(asyncio.run(d.offline_state(h)), "done")           # 出现文件 = 离线完成

    def test_task_list_api_used_when_present(self):
        class C:
            def clouddownload_task_add_url(self, payload): return {"state": True, "info_hash": "ABC"}
            def clouddownload_task_list(self, payload):
                return {"state": True, "tasks": [{"info_hash": "abc", "status": -1}]}
        d = self._drive(C())
        h = asyncio.run(d.add_offline("magnet:?xt=urn:btih:" + "b" * 40, 1))
        self.assertEqual(h, "abc")
        self.assertEqual(asyncio.run(d.offline_state(h)), "failed")

    def test_missing_method_message_lists_available(self):
        class C:
            def clouddownload_foo(self): pass
        d = self._drive(C())
        with self.assertRaises(AttributeError) as cm:
            asyncio.run(d.add_offline("magnet:?xt=urn:btih:" + "c" * 40, 1))
        self.assertIn("clouddownload_foo", str(cm.exception))

    def test_attribute_error_aborts_without_burning_candidates(self):
        class D(FakeDrive):
            async def add_offline(self, magnet, dest):
                self.n_add = getattr(self, "n_add", 0) + 1
                raise AttributeError("p115client 里没有 clouddownload_task_add_url")
        drive = D()
        meta = {"names": ["Dune"], "year": "2024", "episodes": 0}
        raw = [Candidate("magnet", "Dune.2024.1080p.WEB-DL.%d" % i, "magnet:?xt=urn:btih:%040d" % i, size=2 * GB) for i in range(5)]
        async def meta_fn(r): return meta
        async def search_fn(m, t, s): return raw
        async def after(r): pass
        rid = db.create("movie", 970, None, "Dune (2024)")
        asyncio.run(Pipeline(drive, meta_fn, search_fn, after, poll=0).run(rid))
        r = db.get(rid)
        self.assertEqual((r["status"], drive.n_add, r["tried"]), ("failed", 1, "[]"))
        self.assertIn("版本不匹配", r["error"])


class ClassifyTest(unittest.TestCase):
    def test_regions_and_categories(self):
        from app.classify import classify
        m = lambda lang, cs, g=(): {"lang": lang, "countries": list(cs), "genres": list(g)}
        self.assertEqual(classify("movie", m("zh", ["CN"], [878])), ["电影", "国产"])          # 流浪地球
        self.assertEqual(classify("movie", m("zh", ["HK"], [28])), ["电影", "港台"])
        self.assertEqual(classify("movie", m("cn", ["HK"])), ["电影", "港台"])               # 粤语
        self.assertEqual(classify("movie", m("zh", ["CN", "HK"])), ["电影", "国产"])          # 合拍片
        self.assertEqual(classify("movie", m("ja", ["JP"], [18])), ["电影", "日韩"])
        self.assertEqual(classify("movie", m("en", ["US", "GB"])), ["电影", "欧美"])
        self.assertEqual(classify("tv", m("zh", ["CN"])), ["电视剧", "国产剧"])
        self.assertEqual(classify("tv", m("ko", ["KR"])), ["电视剧", "日韩剧"])
        self.assertEqual(classify("tv", m("en", ["US"])), ["电视剧", "欧美剧"])
        self.assertEqual(classify("tv", m("ja", ["JP"], [16, 10759])), ["动漫"])               # 日本动画剧集
        self.assertEqual(classify("tv", m("zh", ["CN"], [16])), ["动漫"])                     # 国漫
        self.assertEqual(classify("movie", m("ja", ["JP"], [16])), ["动漫"])                  # 日本动画电影
        self.assertEqual(classify("movie", m("en", ["US"], [16, 10751])), ["电影", "欧美"])    # 欧美动画电影不进动漫
        self.assertEqual(classify("tv", m("en", ["US"], [16, 35])), ["电视剧", "欧美剧"])      # 欧美动画剧不进动漫
        self.assertEqual(classify("tv", m("zh", ["CN"], [10764])), ["综艺"])
        self.assertEqual(classify("movie", m("en", ["US"], [99])), ["纪录片"])
        self.assertEqual(classify("movie", {}), ["电影"])                                   # 没有 TMDB 信息

    def test_naming(self):
        from app.classify import episode_of, media_tags, render, target_name, vars_for
        self.assertEqual(episode_of("笑傲江湖.2001.E05.1080p.mkv"), 5)
        self.assertEqual(episode_of("Show.S01E12.1080p.WEB-DL.mkv"), 12)
        self.assertEqual(episode_of("笑傲江湖 第07集 4K.mp4"), 7)
        self.assertEqual(episode_of("[字幕组][09][1080P].mkv"), 9)
        self.assertEqual(episode_of("某剧 - 03 [WebRip 1080p].mkv"), 3)
        self.assertIsNone(episode_of("某剧.1080p.H264.mkv"))                              # 分辨率/编码里的数字不是集数
        self.assertEqual(media_tags("X.2019.2160p.BluRay.REMUX.HEVC"), {"res": "2160p", "source": "Remux", "codec": "HEVC"})
        self.assertEqual(media_tags("X 1080p WEB-DL x264")["codec"], "H264")
        meta = {"names": ["流浪地球"], "year": "2019"}
        mv = {"media_type": "movie", "tmdb_id": 535167, "season": None}
        self.assertEqual(render("{title} ({year}) [tmdbid={tmdb}]", vars_for(mv, meta)), "流浪地球 (2019) [tmdbid=535167]")
        # 取不到的变量会省略，不留空括号/悬空的 -
        self.assertEqual(render("{title} ({year}) [tmdbid={tmdb}] - {res} {source}", {"title": "X", "year": "", "tmdb": ""}), "X")
        self.assertEqual(target_name(mv, meta, "流浪地球.2019.1080p.NF.WEB-DL.H265.mkv", "", set()),
                         "流浪地球 (2019) [tmdbid=535167] - 1080p WEB-DL HEVC.mkv")
        self.assertEqual(target_name(mv, meta, "abc.MKV", "[高清]流浪地球 2019 BluRay x264", set()),
                         "流浪地球 (2019) [tmdbid=535167] - BluRay H264.mkv")                 # 文件名没有，就用资源标题里的
        tv = {"media_type": "tv", "tmdb_id": 99, "season": 1}
        tm = {"names": ["笑傲江湖"], "year": "2001"}
        used = set()
        self.assertEqual(target_name(tv, tm, "E05.1080p.mkv", "", used), "笑傲江湖 (2001) - S01E05 - 1080p.mkv")
        self.assertIsNone(target_name(tv, tm, "E05.2160p.mkv", "", used))                    # 重复集数不改名
        self.assertIsNone(target_name(tv, tm, "无法识别.mkv", "", used))

    def test_keywords(self):
        from app.filters import kw_bonus, kw_reason, parse_terms
        self.assertEqual(parse_terms("国语|国配, 中字"), [["国语", "国配"], ["中字"]])
        t = "流浪地球 2019 国配 1080p 简中字幕"
        self.assertEqual(kw_reason(t, all_s="国语|国配, 中字|简中"), "")
        self.assertIn("缺少「国语」", kw_reason("流浪地球 2019 1080p", all_s="国语|国配"))
        self.assertIn("排除词", kw_reason(t, exclude_s="预告, 简中"))
        self.assertIn("缺少关键词", kw_reason(t, any_s="2160p, 4k"))
        self.assertEqual(kw_reason(t, any_s="2160p, 1080p"), "")
        self.assertEqual(kw_bonus(t, "国语|国配, 中字, 内封"), 16)       # 国配、中字 各 8 分

    def test_keywords_in_build_candidates(self):
        from app.config import cfg
        meta = {"names": ["流浪地球"], "year": "2019", "episodes": 0}
        raw = [Candidate("magnet", "流浪地球 2019 1080p 国语中字", "magnet:?xt=urn:btih:" + "1" * 40, size=2 * GB),
               Candidate("magnet", "流浪地球 2019 1080p 英语", "magnet:?xt=urn:btih:" + "2" * 40, size=2 * GB),
               Candidate("share", "流浪地球 2019", "https://115.com/s/abc?password=x", text="国语 中字 无水印")]   # 正文里有关键词也算
        cfg.KW_ALL = "国语|国配, 中字"
        try:
            rep = {}
            out = build_candidates(raw, meta, "movie", None, R, rep)
            self.assertEqual(len(out), 2)
            self.assertEqual(sum(rep["counts"].values()), 1)
        finally:
            cfg.KW_ALL = ""

    def test_split_year(self):
        from app.tmdb import split_year
        self.assertEqual(split_year("流浪地球 2019"), ("流浪地球", "2019"))
        self.assertEqual(split_year("流浪地球(2019)"), ("流浪地球", "2019"))
        self.assertEqual(split_year("Dune 2021"), ("Dune", "2021"))
        self.assertEqual(split_year("2012"), ("2012", ""))                    # 片名本身是年份
        self.assertEqual(split_year("流浪地球"), ("流浪地球", ""))

    def test_cloudsaver_parse(self):
        from app import cloudsaver
        data = {"code": 0, "data": [{"id": "ch1", "list": [
            {"title": "流浪地球 2019 国语中字", "content": "资源描述 提取码: ab12",
             "cloudLinks": ["https://115.com/s/sw123abc?password=zz99", "https://pan.quark.cn/s/xxxx"]},
            {"title": "流浪地球2", "content": "", "cloudLinks": [{"link": "https://115cdn.com/s/swxyz?password=q1w2", "cloudType": "pan115"}]},
            {"title": "没有 115 链接", "cloudLinks": ["https://www.aliyundrive.com/s/abc"]}]}]}
        r = cloudsaver.parse_results(data)
        self.assertEqual([x["url"].split("?")[0] for x in r], ["https://115.com/s/sw123abc", "https://115cdn.com/s/swxyz"])
        self.assertEqual(r[0]["password"], "zz99")
        self.assertEqual(cloudsaver.find_token({"code": 0, "data": {"token": "T1"}}), "T1")


class LibraryPathTest(unittest.TestCase):
    def test_classified_destination_and_rename(self):
        from app.config import cfg
        cfg.LIBRARY_ROOT_CID = 5000

        class D(FakeDrive):
            def __init__(self):
                super().__init__(); self.tree, self.renames, self.cid = {}, [], 6000
            async def ensure_dir(self, parent, name):
                if (parent, name) not in self.tree:
                    self.cid += 1; self.tree[(parent, name)] = self.cid
                return self.tree[(parent, name)]
            async def list_files(self, cid):
                return [{"id": 1, "name": "流浪地球.2019.1080p.mkv", "size": 3 * GB},
                        {"id": 2, "name": "花絮.mp4", "size": 80 * 1024 ** 2},        # 小于 100MB：删
                        {"id": 3, "name": "readme.txt", "size": 100}]                 # 非视频：删
            async def rename(self, fid, new): self.renames.append((fid, new))
        drive = D()
        meta = {"names": ["流浪地球"], "year": "2019", "episodes": 0, "genres": [878], "lang": "zh", "countries": ["CN"]}
        raw = [Candidate("magnet", "流浪地球 2019 1080p WEB-DL", "magnet:?xt=urn:btih:" + "9" * 40, size=3 * GB)]
        async def meta_fn(r): return meta
        async def search_fn(m, t, s): return raw
        async def after(r): pass
        class D2(D):
            async def offline_state(self, h): return "done"
        drive = D2()
        rid = db.create("movie", 980, None, "流浪地球 (2019)")
        asyncio.run(Pipeline(drive, meta_fn, search_fn, after, poll=0).run(rid))
        r = db.get(rid)
        self.assertEqual(r["status"], "done", r["log"])
        names = [k[1] for k in drive.tree]
        self.assertEqual(names, ["电影", "国产", "流浪地球 (2019) [tmdbid=980]"])              # 影视/电影/国产/片名 [tmdbid=…]
        self.assertEqual(drive.renames, [(1, "流浪地球 (2019) [tmdbid=980] - 1080p WEB-DL.mkv")])
        self.assertEqual(drive.moved[0][0], [1])                                          # 只移动大于 100MB 的视频
        self.assertIn("入库位置：电影 / 国产 / 流浪地球 (2019) [tmdbid=980]", r["log"])
        self.assertEqual(r["category"], "电影-国产")                                        # 联动来源按分类区分
        cfg.LIBRARY_ROOT_CID = 0


class LibraryTvTest(unittest.TestCase):
    def test_tv_destination_and_episode_rename(self):
        from app.config import cfg
        cfg.LIBRARY_ROOT_CID = 5000

        class D(FakeDrive):
            def __init__(self):
                super().__init__(); self.tree, self.renames, self.cid = {}, [], 7000
            async def ensure_dir(self, parent, name):
                if (parent, name) not in self.tree:
                    self.cid += 1; self.tree[(parent, name)] = self.cid
                return self.tree[(parent, name)]
            async def offline_state(self, h): return "done"
            async def list_files(self, cid):
                return [{"id": 11, "name": "笑傲江湖.2001.E01.1080p.mkv", "size": 2 * GB},
                        {"id": 12, "name": "笑傲江湖.2001.E02.1080p.mkv", "size": 2 * GB},
                        {"id": 13, "name": "笑傲江湖.2001.第3集.1080p.mkv", "size": 2 * GB},
                        {"id": 14, "name": "sample.mkv", "size": 30 * 1024 ** 2}]
            async def rename(self, fid, new): self.renames.append((fid, new))
        drive = D()
        meta = {"names": ["笑傲江湖"], "year": "2001", "episodes": 3, "genres": [18], "lang": "zh", "countries": ["CN"]}
        raw = [Candidate("magnet", "笑傲江湖 2001 全3集 1080p", "magnet:?xt=urn:btih:" + "8" * 40, size=6 * GB)]
        async def meta_fn(r): return meta
        async def search_fn(m, t, s): return raw
        async def after(r): pass
        rid = db.create("tv", 981, 1, "笑傲江湖 (2001)")
        asyncio.run(Pipeline(drive, meta_fn, search_fn, after, poll=0).run(rid))
        r = db.get(rid)
        self.assertEqual(r["status"], "done", r["log"])
        self.assertEqual([k[1] for k in drive.tree], ["电视剧", "国产剧", "笑傲江湖 (2001) [tmdbid=981]", "Season 01"])
        self.assertEqual(drive.renames, [(11, "笑傲江湖 (2001) - S01E01 - 1080p.mkv"), (12, "笑傲江湖 (2001) - S01E02 - 1080p.mkv"),
                                         (13, "笑傲江湖 (2001) - S01E03 - 1080p.mkv")])
        self.assertEqual(drive.moved[0][0], [11, 12, 13])
        cfg.LIBRARY_ROOT_CID = 0


class ReplaceTest(unittest.TestCase):
    def _setup(self, tmdb_id):
        from app.config import cfg
        cfg.LIBRARY_ROOT_CID = 5000
        events = []

        class D(FakeDrive):
            def __init__(self):
                super().__init__(); self.tree, self.cid, self.fail_offline, self.fid = {}, 8000, False, 100
            async def ensure_dir(self, parent, name):
                if (parent, name) not in self.tree:
                    self.cid += 1; self.tree[(parent, name)] = self.cid
                return self.tree[(parent, name)]
            async def offline_state(self, h): return "failed" if self.fail_offline else "done"
            async def list_files(self, cid):
                self.fid += 1
                return [{"id": self.fid, "name": "流浪地球.2019.1080p.mkv", "size": 3 * GB}]
            async def rename(self, fid, new): events.append(("rename", fid))
            async def delete(self, ids): events.append(("delete", tuple(ids)))
            async def move(self, ids, dest): events.append(("move", tuple(ids)))
        meta = {"names": ["流浪地球"], "year": "2019", "episodes": 0, "genres": [], "lang": "zh", "countries": ["CN"]}
        raw = [Candidate("magnet", "流浪地球 2019 1080p WEB-DL 国语", "magnet:?xt=urn:btih:" + "1" * 40, size=3 * GB, seeders=9),
               Candidate("magnet", "流浪地球 2019 2160p BluRay 国语", "magnet:?xt=urn:btih:" + "2" * 40, size=4 * GB, seeders=3)]
        async def meta_fn(r): return meta
        async def search_fn(m, t, s): return raw
        async def after(r): pass
        drive = D()
        return drive, events, Pipeline(drive, meta_fn, search_fn, after, poll=0), db.create("movie", tmdb_id, None, "流浪地球 (2019)")

    def test_candidates_saved_and_replace_deletes_old_only_after_success(self):
        import json
        from app.config import cfg
        drive, events, pl, rid = self._setup(990)
        try:
            asyncio.run(pl.run(rid))
            r = db.get(rid)
            self.assertEqual(r["status"], "done")
            cands = json.loads(r["cands"])
            self.assertEqual(len(cands), 2)                                    # 候选已保存，供「候选/替换」使用
            placed = json.loads(r["placed"])
            self.assertEqual(len(placed["files"]), 1)
            old_file = placed["files"][0]
            events.clear()
            other = next(c for c in cands if c["title"] != r["picked"])
            asyncio.run(pl.run_candidate(rid, other["url"], replace=True))
            r = db.get(rid)
            self.assertEqual((r["status"], r["picked"]), ("done", other["title"]))
            kinds = [e[0] for e in events]
            self.assertLess(kinds.index("delete"), kinds.index("move"))        # 先删旧文件，再放新文件（避免同名冲突）
            self.assertIn(("delete", (old_file,)), events)
            self.assertNotEqual(json.loads(r["placed"])["files"], placed["files"])
        finally:
            cfg.LIBRARY_ROOT_CID = 0

    def test_failed_replacement_keeps_old_files(self):
        import json
        from app.config import cfg
        drive, events, pl, rid = self._setup(991)
        try:
            asyncio.run(pl.run(rid))
            cands = json.loads(db.get(rid)["cands"])
            other = next(c for c in cands if c["title"] != db.get(rid)["picked"])
            events.clear()
            drive.fail_offline = True                                           # 新资源下载失败
            asyncio.run(pl.run_candidate(rid, other["url"], replace=True))
            r = db.get(rid)
            self.assertEqual(r["status"], "done")                               # 状态不变，原资源还在
            self.assertIn("原资源保留", r["error"])
            self.assertNotIn("move", [e[0] for e in events])                    # 没有任何新文件被放进库
        finally:
            cfg.LIBRARY_ROOT_CID = 0


class LitePanSourceTest(unittest.TestCase):
    def test_source_per_category(self):
        from app import litepan
        from app.config import cfg
        cfg.LITEPAN_SOURCE = "Emby求片-{category}"
        self.assertEqual(litepan.source_for("电影-国产"), "Emby求片-电影-国产")
        srcs = litepan.all_sources()
        self.assertEqual(len(srcs), 11)                                         # 电影4 + 电视剧4 + 动漫/综艺/纪录片
        self.assertIn("Emby求片-电视剧-国产剧", srcs); self.assertIn("Emby求片-动漫", srcs)
        cfg.LITEPAN_SOURCE = "Emby求片"                                         # 没写 {category}：所有分类同一个来源
        self.assertEqual(litepan.source_for("电影-国产"), "Emby求片")
        self.assertEqual(litepan.all_sources(), ["Emby求片"])
        cfg.LITEPAN_URL = "192.168.1.10:5211/api/open/automation/events"       # 粘贴了完整接口地址、没写 http:// 也能用
        self.assertEqual(litepan.preview("e", "s")["url"], "http://192.168.1.10:5211/api/open/automation/events")
        cfg.LITEPAN_SOURCE = "RequestBridge"


class MultiSeasonGroupTest(unittest.TestCase):
    def _env(self, pack_titles, files_by_pack):
        from app.config import cfg
        cfg.LIBRARY_ROOT_CID = 5000
        searches, adds, moves = [], [], []

        class D(FakeDrive):
            def __init__(self):
                super().__init__(); self.tree, self.cid, self.cur = {}, 9000, None
            async def ensure_dir(self, parent, name):
                if (parent, name) not in self.tree:
                    self.cid += 1; self.tree[(parent, name)] = self.cid
                return self.tree[(parent, name)]
            async def add_offline(self, magnet, dest):
                adds.append(magnet); self.cur = magnet; return magnet[-4:]
            async def offline_state(self, h): return "done"
            async def list_files(self, cid): return files_by_pack[self.cur]
            async def rename(self, fid, new): pass
            async def move(self, ids, dest): moves.append((tuple(ids), dest))
        meta = {"names": ["权力的游戏"], "year": "2011", "episodes": 10, "season_eps": {1: 10, 2: 10, 3: 10},
                "genres": [18], "lang": "en", "countries": ["US"]}
        raw = [Candidate("magnet", t, "magnet:?xt=urn:btih:" + k.ljust(40, "0"), size=sz * GB, seeders=sd)
               for t, k, sz, sd in pack_titles]
        async def meta_fn(r): return meta
        async def search_fn(m, t, seasons): searches.append(seasons); return raw
        done = []
        async def after(r): done.append(r["season"])
        return D(), Pipeline(D(), meta_fn, search_fn, after, poll=0), searches, adds, moves, done, meta_fn, search_fn, after

    def _files(self, seasons, n=10):
        return [{"id": s * 100 + e, "name": f"E{e:02d}.mkv", "size": 2 * GB, "path": f"权游/S{s:02d}/E{e:02d}.mkv"}
                for s in seasons for e in range(1, n + 1)]

    def test_one_search_one_download_for_a_multi_season_pack(self):
        from app.config import cfg
        pack = "magnet:?xt=urn:btih:" + "a".ljust(40, "0")
        drive, _, searches, adds, moves, done, meta_fn, search_fn, after = self._env(
            [("权力的游戏 S01-S03 1080p BluRay", "a", 60, 5), ("权力的游戏 S02 1080p", "b", 20, 50)], {pack: self._files([1, 2, 3])})
        try:
            ids = [db.create("tv", 970, s, "权力的游戏 (2011)") for s in (1, 2, 3)]
            for i in ids:
                db.update(i, grp=ids[0])
            asyncio.run(Pipeline(drive, meta_fn, search_fn, after, poll=0).run(ids[0]))
            self.assertEqual(len(searches), 1)                                   # 只搜一次，不是一季一季单独搜
            self.assertEqual(searches[0], [1, 2, 3])
            self.assertEqual(adds, [pack])                                       # 合集优先，只下载一次
            self.assertEqual([db.get(i)["status"] for i in ids], ["done"] * 3)
            self.assertEqual(sorted(done), [1, 2, 3])
            self.assertEqual([k[1] for k in drive.tree if k[1].startswith("Season")], ["Season 01", "Season 02", "Season 03"])
            self.assertEqual([m[0][0] for m in moves], [101, 201, 301])          # 每一季的文件分进各自的目录
            self.assertEqual({len(m[0]) for m in moves}, {10})
        finally:
            cfg.LIBRARY_ROOT_CID = 0

    def test_partial_pack_then_single_season_fills_the_gap(self):
        from app.config import cfg
        p12 = "magnet:?xt=urn:btih:" + "c".ljust(40, "0")
        p3 = "magnet:?xt=urn:btih:" + "d".ljust(40, "0")
        drive, _, searches, adds, moves, done, meta_fn, search_fn, after = self._env(
            [("权力的游戏 S01-S02 1080p", "c", 40, 5), ("权力的游戏 S03 1080p", "d", 20, 50)],
            {p12: self._files([1, 2]), p3: self._files([3])})
        try:
            ids = [db.create("tv", 971, s, "权力的游戏 (2011)") for s in (1, 2, 3)]
            for i in ids:
                db.update(i, grp=ids[0])
            asyncio.run(Pipeline(drive, meta_fn, search_fn, after, poll=0).run(ids[0]))
            self.assertEqual(adds, [p12, p3])                                    # 覆盖两季的先用，缺的第 3 季再用单季资源补
            self.assertEqual([db.get(i)["status"] for i in ids], ["done"] * 3)
        finally:
            cfg.LIBRARY_ROOT_CID = 0

    def test_unfilled_season_fails_but_others_stay_done(self):
        from app.config import cfg
        p12 = "magnet:?xt=urn:btih:" + "e".ljust(40, "0")
        drive, _, searches, adds, moves, done, meta_fn, search_fn, after = self._env(
            [("权力的游戏 S01-S02 1080p", "e", 40, 5)], {p12: self._files([1, 2])})
        try:
            ids = [db.create("tv", 972, s, "权力的游戏 (2011)") for s in (1, 2, 3)]
            for i in ids:
                db.update(i, grp=ids[0])
            asyncio.run(Pipeline(drive, meta_fn, search_fn, after, poll=0).run(ids[0]))
            self.assertEqual([db.get(i)["status"] for i in ids], ["done", "done", "failed"])
        finally:
            cfg.LIBRARY_ROOT_CID = 0

    def test_assign_seasons(self):
        from app.filters import assign_seasons
        fl = self._files([1, 2])
        self.assertEqual({s: len(v) for s, v in assign_seasons(fl, [1, 2, 3], "x S01-S03").items()}, {1: 10, 2: 10})
        bare = [{"id": 1, "name": "E01.mkv", "path": "E01.mkv"}]
        self.assertEqual(list(assign_seasons(bare, [1], "某剧 全26集")), [1])               # 单季、没标季：整包算第 1 季
        self.assertEqual(assign_seasons(bare, [1, 2], "某剧 全集"), {})                    # 多季请求又没标季：没法分，不要
        self.assertEqual(assign_seasons(bare, [2], "某剧 S01-S03"), {})                    # 多季合集没标季：不乱分


class SearchDepthPriorityTest(unittest.TestCase):
    def test_queries_by_depth_keep_name_whole(self):
        from app.sources import queries
        names = ["唐人街探案", "Detective Chinatown"]
        q1 = queries(names, "2015", "tv", [1, 2], 1)
        q2 = queries(names, "2015", "tv", [1, 2], 2)
        q3 = queries(names, "2015", "tv", [1, 2], 3)
        self.assertLessEqual(len(q1), 8); self.assertGreater(len(q2), len(q1)); self.assertGreater(len(q3), len(q2))
        self.assertIn("唐人街探案 S01", q1)
        self.assertIn("唐人街探案 全集", q2); self.assertIn("唐人街探案 第2季", q2)
        self.assertTrue(all(("唐人街探案" in q or "Detective Chinatown" in q) for q in q3))   # 每个查询词都带完整名称

    def test_priority_order_is_configurable(self):
        from app.config import cfg
        meta = {"names": ["Dune"], "year": "2021", "episodes": 0}
        raw = [Candidate("share", "Dune 2021 1080p", "https://115.com/s/abc?password=x"),
               Candidate("magnet", "Dune 2021 1080p BluRay", "magnet:?xt=urn:btih:" + "7" * 40, size=2 * GB, seeders=50)]
        try:
            cfg.PRIORITY = "year,quality,keywords,size,source,seeders"
            first = build_candidates(raw, meta, "movie", None, R)[0]
            self.assertEqual(first.kind, "magnet")                                  # 体积合适排在「分享优先」前面
            cfg.PRIORITY = "source,year,quality"
            first = build_candidates(raw, meta, "movie", None, R)[0]
            self.assertEqual(first.kind, "share")                                   # 分享优先放最前
            cfg.PRIORITY = "quality,year"
            out = build_candidates([Candidate("magnet", "Dune 2020 720p", "magnet:?xt=urn:btih:" + "8" * 40, size=2 * GB),
                                    Candidate("magnet", "Dune 2021 1080p", "magnet:?xt=urn:btih:" + "9" * 40, size=2 * GB)],
                                   meta, "movie", None, R)
            self.assertEqual([c.title for c in out], ["Dune 2021 1080p", "Dune 2020 720p"])
        finally:
            cfg.PRIORITY = "year,quality,keywords,size,source,seeders"

    def test_tags_and_rejects_are_reported(self):
        meta = {"names": ["Dune"], "year": "2021", "episodes": 0}
        rep = {}
        out = build_candidates([Candidate("magnet", "Dune 2021 1080p", "magnet:?xt=urn:btih:" + "6" * 40, size=2 * GB),
                                Candidate("magnet", "Dune 1984 1080p", "magnet:?xt=urn:btih:" + "5" * 40, size=2 * GB)],
                               meta, "movie", None, R, rep)
        self.assertIn("年份吻合", out[0].tags); self.assertIn("1080p", out[0].tags); self.assertIn("体积合适", out[0].tags)
        self.assertEqual([x["reason"] for x in rep["rejects"]], ["年份不符"])         # 被过滤的也留着，可以强制使用


class PersonAndPurgeTest(unittest.TestCase):
    def test_person_search_and_credits(self):
        import asyncio as aio
        from app import tmdb

        async def fake_get(path, **kw):
            if path == "/search/multi":
                if kw.get("page", 1) > 1:
                    return {"results": []}
                return {"results": [
                    {"media_type": "movie", "id": 1, "title": "唐人街探案", "release_date": "2015-12-31", "popularity": 5},
                    {"media_type": "person", "id": 77, "name": "王宝强", "known_for_department": "Acting", "profile_path": "/p.jpg",
                     "popularity": 30, "known_for": [{"title": "唐人街探案"}, {"name": "士兵突击"}]},
                    {"media_type": "person", "id": 78, "name": "路人", "known_for_department": "Acting", "popularity": 1, "known_for": []}]}
            if path == "/person/77":
                return {"name": "王宝强", "profile_path": "/p.jpg", "known_for_department": "Acting"}
            if path == "/person/77/combined_credits":
                return {"cast": [
                    {"media_type": "movie", "id": 1, "title": "唐人街探案", "release_date": "2015-12-31", "character": "秦风", "popularity": 9, "genre_ids": [35]},
                    {"media_type": "tv", "id": 2, "name": "士兵突击", "first_air_date": "2006-09-01", "character": "许三多", "popularity": 8, "genre_ids": [18]},
                    {"media_type": "tv", "id": 3, "name": "某脱口秀", "first_air_date": "2020-01-01", "character": "Self", "popularity": 3, "genre_ids": [10767]},
                    {"media_type": "movie", "id": 4, "title": "客串", "release_date": "2019-01-01", "character": "本人", "popularity": 2, "genre_ids": [99]}],
                    "crew": [{"media_type": "movie", "id": 1, "title": "唐人街探案", "job": "Director"},
                             {"media_type": "movie", "id": 5, "title": "大闹天竺", "release_date": "2017-01-27", "job": "Director", "popularity": 4},
                             {"media_type": "movie", "id": 6, "title": "编剧作品", "job": "Writer"}]}
            return {}
        orig = tmdb.get
        tmdb.get = fake_get
        try:
            items = aio.run(tmdb.search("王宝强"))
            self.assertEqual([i["type"] for i in items], ["movie", "person", "person"])          # 人物按热度排，影片在前
            self.assertEqual(items[1]["title"], "王宝强"); self.assertIn("士兵突击", items[1]["overview"])
            d = aio.run(tmdb.person_credits(77))
            self.assertEqual(d["person"]["name"], "王宝强")
            self.assertEqual([w["title"] for w in d["works"]], ["大闹天竺", "唐人街探案", "士兵突击"])   # 日期从新到旧；脱口秀/本人客串/编剧都不要
            by = {w["title"]: w for w in d["works"]}
            self.assertEqual(by["唐人街探案"]["job"], "演员/导演"); self.assertEqual(by["唐人街探案"]["role"], "秦风")
            self.assertEqual(by["大闹天竺"]["job"], "导演")
        finally:
            tmdb.get = orig

    def test_purge_deletes_cloud_files_and_empty_folders_only(self):
        import json
        deleted, remaining = [], {"season": 0, "show": 1}      # 季目录删完文件就空了；片名目录里还有别的季，不能删

        class D(FakeDrive):
            async def delete(self, ids): deleted.append(list(ids))
            async def list_files(self, cid):
                return [] if str(cid) == "301" else [{"id": 1, "name": "x.mkv", "size": 1}]
            async def list_dirs(self, cid): return []
        pl = Pipeline(D(), None, None, None, poll=0)
        rid = db.create("tv", 995, 2, "某剧 (2020)")
        db.update(rid, status="done", placed=json.dumps({"dest": "301", "files": ["11", "12"], "chain": ["300", "301"]}))
        n = asyncio.run(pl.purge(db.get(rid)))
        self.assertEqual(n, 2)
        self.assertEqual(deleted, [["11", "12"], ["301"]])                  # 先删文件，再删空的季目录；片名目录（300）里还有文件，保留
        self.assertEqual(db.get(rid)["placed"], "")
        self.assertEqual(asyncio.run(pl.purge(db.get(rid))), 0)             # 没有记录的文件：什么都不删

    def test_source_table_has_scan_paths(self):
        from app import litepan
        from app.config import cfg
        from app import settings
        cfg.LITEPAN_SOURCE = "Emby求片-{category}"
        settings.set_internal(library_root_label="影视")
        t = {x["source"]: x["path"] for x in litepan.source_table()}
        self.assertEqual(t["Emby求片-电影-国产"], "影视/电影/国产")
        self.assertEqual(t["Emby求片-电视剧-国产剧"], "影视/电视剧/国产剧")
        self.assertEqual(t["Emby求片-动漫"], "影视/动漫")
        cfg.LITEPAN_SOURCE = "RequestBridge"


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
        self.assertEqual(len(drive.deleted), 2)   # 先试做种最多的磁力（失败），再试第二个（成功），两次的暂存目录都被清理
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
