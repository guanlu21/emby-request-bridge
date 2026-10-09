"""网页「设置」里保存的配置（/data/settings.json）。字段定义即前端表单的 schema。"""
import json
import os
import secrets
from urllib.parse import quote, urlsplit

from .config import _Env


def F(key, label, type="text", help="", options=None, default=""):
    return {"key": key, "label": label, "type": type, "help": help, "options": options or [], "default": default}


SCHEMA = [
    {"group": "Emby", "fields": [
        F("emby_url", "Emby 地址", help="例如 http://192.168.1.10:8096；容器里不要写 127.0.0.1"),
        F("emby_key", "Emby API 密钥", "password", "首次登录时会自动生成；失败的话到 Emby 控制台 → 高级 → API 密钥手动创建"),
        F("admins", "管理员", help="Emby 用户名，多个用逗号分隔。第一个登录的 Emby 管理员会自动加入"),
    ]},
    {"group": "115 网盘", "fields": [
        F("p115_cookie", "115 Cookie", "password", "没有 AppID 时用它登录 115：浏览器登录 115.com 后复制包含 UID、CID、SEID 的完整 Cookie。Cookie 会失效，失效后重新复制即可；已扫码授权开放平台时，它只用于转存分享链接"),
        F("p115_app_id", "115 开放平台 AppID（可选）", help="有 AppID 就扫码授权，更稳定、不会过期；没有就先只填 Cookie。建议给本服务单独申请，别和 LitePan 共用"),
        F("p115_staging_cid", "下载目录（云下载）", "folder", "磁力先离线到这里，过滤合格后才移走；建议直接选 115 的「云下载」目录。不要选 LitePan 要生成 STRM 的目录"),
        F("p115_dest_movie_cid", "电影目录（旧，可选）", "folder", "只有没设「影视根目录」时才用"),
        F("p115_dest_tv_cid", "剧集目录（旧，可选）", "folder", "只有没设「影视根目录」时才用"),
    ]},
    {"group": "入库分类", "fields": [
        F("library_root_cid", "影视根目录", "folder", "例如「影视」。下载完成后按 TMDB 信息自动放进下面的分类子目录（不存在会自动创建）"),
        F("dir_movie", "电影目录名", default="电影"),
        F("dir_tv", "电视剧目录名", default="电视剧"),
        F("dir_anime", "动漫目录名", help="电视剧类动画（日本、国产、韩国）和日本动画电影放这里", default="动漫"),
        F("dir_variety", "综艺目录名", default="综艺"),
        F("dir_doc", "纪录片目录名", default="纪录片"),
        F("region_names", "地区目录名", help="依次是：国产、港台、日韩、欧美，用逗号分隔", default="国产,港台,日韩,欧美"),
        F("tv_suffix", "电视剧地区目录后缀", help="电视剧地区目录 = 地区名 + 后缀，如 国产剧", default="剧"),
        F("keep_min_mb", "下载后保留的最小文件（MB）", "number", "只保留视频文件，小于它的（样片、花絮）直接删除", default=100),
    ]},
    {"group": "夸克网盘", "fields": [
        F("quark_cookie", "夸克 Cookie", "password", "浏览器登录 pan.quark.cn 后，在开发者工具里复制请求的 Cookie。夸克只能转存分享链接（没有磁力离线下载）；Cookie 会失效，失效后重新复制"),
        F("quark_staging_fid", "夸克下载目录", "qfolder", "分享先转存到这里，过滤合格才移走；不要选要生成 STRM 的目录"),
        F("quark_library_fid", "夸克影视根目录", "qfolder", "按分类（电影/国产…）放进这里；不设就不会使用夸克"),
    ]},
    {"group": "资源搜索", "fields": [
        F("pansou_url", "PanSou 地址", help="例如 http://192.168.1.10:8888；留空则不搜分享链接"),
        F("cloudsaver_url", "CloudSaver 地址", help="例如 http://192.168.1.10:8008；它从 Telegram 等频道搜 115 分享链接。留空则不用"),
        F("cloudsaver_user", "CloudSaver 用户名"),
        F("cloudsaver_pass", "CloudSaver 密码", "password"),
        F("kite_url", "纸鸢磁力 MCP 地址", help="例如 https://magnet.kiteyuan.info/mcp，以纸鸢磁力「MCP」页面客户端配置里的 url 为准。它会聚合你在纸鸢里配置的国内磁力站（包括自定义规则的站点）"),
        F("dyg_on", "启用电影港（dyg7.com）", "toggle", "从电影港的影片页取磁力、夸克、115 链接。页面里常写明「国语中字无水印」，匹配度比较高", default=True),
        F("dyg_url", "电影港地址", help="网站换域名时在这里改", default="https://www.dyg7.com"),
        F("rule_engines", "磁力站（按纸鸢磁力的规则直接搜）", help="引擎名，逗号分隔，默认 磁力帝,BitSearch。可用的名字点「测试磁力站」查看；把纸鸢磁力导出的规则文件放到数据目录 /data/rules.json 可追加或覆盖（成人类引擎不会使用）", default="磁力帝,BitSearch"),
        F("rules_proxy", "磁力站走上面配置的代理", "toggle", "这些站点常需要代理才能访问：开启后，请求会用「代理与 TMDB」里配置的代理（没配代理就是直连）", default=True),
        F("kite_token", "纸鸢磁力 MCP Token", "password", "在纸鸢磁力「MCP」页面生成（mcp__ 开头）"),
        F("kite_exclude", "纸鸢磁力：排除的搜索引擎", help="逗号分隔；结果里带有引擎/来源信息时，这些引擎的结果会被丢弃（默认排除综合匹配、快速搜索）", default="综合匹配,快速搜索"),
        F("kite_engine", "纸鸢磁力：指定搜索引擎（可选）", help="如 磁力帝；仅当 magnet_search 工具支持选择引擎时才会传过去，点「测试纸鸢磁力」可以看到工具有哪些参数"),
    ]},
    {"group": "LitePan 联动", "fields": [
        F("litepan_url", "LitePan 地址", help="例如 http://192.168.1.10:5211；留空则不联动，改由本服务等一会儿后通知 Emby 刷新"),
        F("litepan_key", "LitePan API 秘钥", "password", "LitePan → 系统设置 → API 秘钥，新建「任务执行」型（lpk_api_ 开头）；STRM Key 和只读 Key 不能用"),
        F("litepan_event", "联动通知名称", help="要和 LitePan 自动联动里「第三方通知」填的名称一致", default="download_completed"),
        F("litepan_source", "联动来源（可选）", help="LitePan 联动里填了来源才需要一致；不限来源就留空", default="RequestBridge"),
        F("litepan_delay", "合并等待（秒）", "number", "文件进入正式目录后等这么久再通知；这段时间内的多次求片合并成一次通知", default=20),
        F("litepan_wait", "未联动时等待（秒）", "number", "没配 LitePan 时，文件落盘后等这么久再通知 Emby 刷新媒体库", default=120),
    ]},
    {"group": "审批与限额", "fields": [
        F("approval", "新请求的处理方式", "select", "管理员自己的请求总是直接处理",
          [["manual", "先进待审批，管理员批准后处理"], ["auto", "直接处理"]], "manual"),
        F("quota_weekly", "每人每 7 天最多求几部", "number", "0 表示不限；管理员不受限制", default=0),
    ]},
    {"group": "选源规则", "fields": [
        F("min_res", "最低分辨率（p）", "number", "标题里明确低于它的会丢弃", default=720),
        F("min_gb", "单文件最小（GB）", "number", "太小的（样片等）直接过滤", default=0.5),
        F("max_gb", "单文件最大（GB）", "number", default=5),
        F("prefer_min_gb", "偏好区间下限（GB）", "number", "落在偏好区间内的排名更靠前", default=1),
        F("prefer_max_gb", "偏好区间上限（GB）", "number", default=3),
        F("search_depth", "搜索深度", "select", "越深，用的关键词越多、各搜索源返回的条数越多（也更慢）；某条请求搜不到想要的，还可以在「候选/替换」里单独做一次最深的搜索",
          [["1", "1 普通"], ["2", "2 深入（默认）"], ["3", "3 最深"]], "2"),
        F("drive_prefer", "优先使用的网盘", "select", "两个网盘都配置了、又都有资源时，优先用哪个（只是优先，另一个照样会用来补缺）",
          [["any", "不限"], ["115", "115"], ["quark", "夸克"]], "any"),
        F("priority", "候选优先级顺序", help="从前到后逐项比较，靠前的更重要。可用项：chinese 中文名称（标题里有中文片名的优先）、year 年份吻合、quality 画质（1080p 最优）、drive 优先的网盘、keywords 优先关键词、size 体积合适、source 分享链接优先、seeders 做种数；电视剧始终先看覆盖了几季", default="chinese,year,quality,drive,keywords,size,source,seeders"),
        F("season_ratio", "整季最少集数占比（%）", "number", "电视剧下载下来的某一季，视频数量要达到「已播出集数」的这个比例才算整季；连载中的剧按已播出的集数算。0 表示不检查", default=50),
        F("keep_failed", "保留没通过检查的下载", "toggle", "开启后，下载成功但没通过检查（太小、集数不够…）的内容不再删除，留在下载目录里改名为「未入库-…」，方便你自己看；默认关闭（会自动删除）", default=False),
        F("max_attempts", "每个请求最多尝试几个资源", "number", default=8),
        F("offline_timeout", "单个资源离线等待上限（秒）", "number", "磁力在 115 上一直下不完（死种）时，等这么久就换下一个；热门资源通常几分钟内完成", default=600),
    ]},
    {"group": "命名格式", "fields": [
        F("name_movie_dir", "电影文件夹", help="可用变量：{title} 片名、{year} 年份、{tmdb} TMDB 编号。Emby 认 [tmdbid=编号]", default="{title} ({year}) [tmdbid={tmdb}]"),
        F("name_movie_file", "电影文件", help="另可用 {res} 分辨率、{source} 片源（WEB-DL/BluRay…）、{codec} 编码（HEVC/H264…）。取不到的变量会自动省略", default="{title} ({year}) [tmdbid={tmdb}] - {res} {source} {codec}"),
        F("name_tv_dir", "电视剧文件夹", default="{title} ({year}) [tmdbid={tmdb}]"),
        F("name_season_dir", "季文件夹", help="可用 {season} 季号、{season2} 两位季号", default="Season {season2}"),
        F("name_tv_file", "电视剧文件", help="可用 {ep} 集号、{ep2} 两位集号，以及上面的 {res} {source} {codec}。认不出集号的文件保留原名", default="{title} ({year}) - S{season2}E{ep2} - {res} {source} {codec}"),
    ]},
    {"group": "关键词筛选", "fields": [
        F("kw_all", "必须全部包含", help="逗号分隔，每一项都必须出现在资源标题里。一项里可以用 | 写同义词，如：国语|国配|普通话, 中字|简中|中英字幕"),
        F("kw_any", "至少包含其中一个", help="逗号分隔，命中任意一项即可，如：1080p, 2160p"),
        F("kw_exclude", "不能包含", help="逗号分隔，命中任何一项就丢弃，如：枪版, 预告, 韩语, 日语"),
        F("kw_prefer", "优先包含（加分）", help="逗号分隔，命中的越多排名越靠前，如：国语, 中字, 内封, 无水印"),
    ]},
    {"group": "代理与 TMDB", "fields": [
        F("proxy_enabled", "启用代理", "toggle", default=False),
        F("proxy_url", "代理地址", help="例如 http://192.168.1.10:7891；不要写 127.0.0.1"),
        F("proxy_user", "代理用户名", help="可选"),
        F("proxy_pass", "代理密码", "password", "可选"),
        F("tmdb_key", "TMDB API Key", "password"),
        F("language", "TMDB 语言（影响搜索和命名）", "select", "", [["zh-CN", "简体中文"], ["zh-TW", "繁體中文"], ["en-US", "English"], ["ja-JP", "日本語"]], "zh-CN"),
        F("api_host", "TMDB API 主机名", default="https://api.themoviedb.org"),
        F("image_host", "TMDB 图片主机名", default="https://image.tmdb.org"),
    ]},
]
FIELDS = {f["key"]: f for g in SCHEMA for f in g["fields"]}
SECRET = {k for k, f in FIELDS.items() if f["type"] == "password"}
_cache = None


def _path():
    return os.path.join(os.path.dirname(_Env.DB_PATH) or ".", "settings.json")


def _defaults() -> dict:
    d = {}
    for k, f in FIELDS.items():
        d[k] = getattr(_Env, k.upper(), f["default"])
        if f["type"] in ("folder", "qfolder"):
            d[k.replace("_cid", "_label").replace("_fid", "_label")] = ""
    d.update(p115_access="", p115_refresh="", p115_expires=0, token="")
    return d


def _write(cur: dict):
    os.makedirs(os.path.dirname(_path()) or ".", exist_ok=True)
    with open(_path(), "w", encoding="utf-8") as f:
        json.dump(cur, f, ensure_ascii=False, indent=2)


def get() -> dict:
    global _cache
    if _cache is None:
        d = _defaults()
        try:
            with open(_path(), encoding="utf-8") as f:
                d.update(json.load(f))
        except Exception:  # noqa
            pass
        if not d.get("token"):  # API 令牌：环境变量里给了就用，否则自动生成
            d["token"] = _Env.TOKEN or secrets.token_urlsafe(24)
            _write(d)
        _cache = d
    return _cache


def _num(v):
    f = float(v)
    return int(f) if f == int(f) else f


def save(new: dict) -> dict:
    cur = dict(get())
    for k, f in FIELDS.items():
        if k not in new:
            continue
        v, t = new[k], f["type"]
        if k in SECRET and not v:  # 密钥类字段留空 = 不修改
            continue
        if t == "number":
            try:
                cur[k] = _num(v)
            except (TypeError, ValueError):
                pass
        elif t == "toggle":
            cur[k] = bool(v)
        elif t == "folder":
            cur[k] = int(v or 0)
            cur[k.replace("_cid", "_label")] = str(new.get(k.replace("_cid", "_label"), "") or "")
        elif t == "qfolder":
            lk = k.replace("_fid", "_label")
            cur[k] = str(v or "").strip()
            cur[lk] = str(new.get(lk, "") or "")
        else:
            cur[k] = v.strip() if isinstance(v, str) else v
            if k == "emby_url":
                cur[k] = cur[k].rstrip("/")
    _write(cur)
    globals()["_cache"] = cur
    return cur


def set_internal(**kw):
    """程序内部写入（令牌、自动识别的管理员等），不经过表单校验。"""
    cur = dict(get())
    cur.update(kw)
    _write(cur)
    globals()["_cache"] = cur


def public() -> dict:
    """给前端：schema + 当前值；密钥不回传，只告诉有没有配置。"""
    cur = get()
    # 115 目录 ID 有 19 位，超出浏览器 JSON 数字的精度（约 16 位），必须以字符串返回
    values = {k: ("" if k in SECRET else (str(cur[k]) if FIELDS[k]["type"] == "folder" else cur[k])) for k in FIELDS}
    for k, f in FIELDS.items():
        if f["type"] in ("folder", "qfolder"):
            lk = k.replace("_cid", "_label").replace("_fid", "_label")
            values[lk] = cur.get(lk, "")
    return {"schema": SCHEMA, "values": values, "secret_set": {k: bool(cur[k]) for k in SECRET}}


def proxy():
    s = get()
    if not (s["proxy_enabled"] and s["proxy_url"]):
        return None
    u = urlsplit(s["proxy_url"] if "://" in s["proxy_url"] else "http://" + s["proxy_url"])
    auth = f"{quote(s['proxy_user'], safe='')}:{quote(s['proxy_pass'], safe='')}@" if s["proxy_user"] else ""
    return f"{u.scheme}://{auth}{u.netloc}"


def admins() -> set:
    return {a.strip().lower() for a in str(get().get("admins", "")).split(",") if a.strip()}
