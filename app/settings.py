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
        F("p115_dest_movie_cid", "电影目录", "folder", "整理前的正式目录，LitePan 监控它"),
        F("p115_dest_tv_cid", "剧集目录", "folder", "同上，放剧集"),
        F("p115_staging_cid", "暂存目录", "folder", "下载先放这里，合格才移走；必须在 LitePan 监控范围之外"),
    ]},
    {"group": "资源搜索", "fields": [
        F("pansou_url", "PanSou 地址", help="例如 http://192.168.1.10:8888；留空则不搜分享链接"),
        F("prowlarr_url", "Prowlarr 地址", help="例如 http://192.168.1.10:9696；留空则不搜磁力"),
        F("prowlarr_key", "Prowlarr API Key", "password"),
        F("kite_url", "纸鸢磁力 MCP 地址", help="例如 https://magnet.kiteyuan.info/mcp，以纸鸢磁力「MCP」页面客户端配置里的 url 为准。它会聚合你在纸鸢里配置的国内磁力站（包括自定义规则的站点）"),
        F("kite_token", "纸鸢磁力 MCP Token", "password", "在纸鸢磁力「MCP」页面生成（mcp__ 开头）"),
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
        F("max_attempts", "每个请求最多尝试几个资源", "number", default=8),
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
        if f["type"] == "folder":
            d[k.replace("_cid", "_label")] = ""
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
        if f["type"] == "folder":
            lk = k.replace("_cid", "_label")
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
