"""可在网页里修改的设置（代理 / TMDB），保存在数据目录的 settings.json。"""
import json
import os
from urllib.parse import quote, urlsplit

from .config import cfg

DEFAULTS = {"proxy_enabled": False, "proxy_url": "", "proxy_user": "", "proxy_pass": "",
            "tmdb_key": "", "language": "zh-CN",
            "api_host": "https://api.themoviedb.org", "image_host": "https://image.tmdb.org"}
SECRET = ("tmdb_key", "proxy_pass")
_cache = None


def _path():
    return os.path.join(os.path.dirname(cfg.DB_PATH) or ".", "settings.json")


def get() -> dict:
    global _cache
    if _cache is None:
        d = dict(DEFAULTS)
        d["tmdb_key"] = cfg.TMDB_KEY
        try:
            with open(_path(), encoding="utf-8") as f:
                d.update(json.load(f))
        except Exception:  # noqa
            pass
        _cache = d
    return _cache


def save(new: dict) -> dict:
    cur = dict(get())
    for k in DEFAULTS:
        if k not in new:
            continue
        v = new[k]
        if k in SECRET and not v:  # 密钥类字段留空 = 不修改
            continue
        cur[k] = bool(v) if k == "proxy_enabled" else (v.strip() if isinstance(v, str) else v)
    os.makedirs(os.path.dirname(_path()) or ".", exist_ok=True)
    with open(_path(), "w", encoding="utf-8") as f:
        json.dump(cur, f, ensure_ascii=False, indent=2)
    globals()["_cache"] = cur
    return cur


def public() -> dict:
    """给前端看的版本：密钥不回传，只告诉有没有配置。"""
    d = dict(get())
    for k in SECRET:
        d["has_" + k] = bool(d.pop(k, ""))
    return d


def proxy():
    s = get()
    if not (s["proxy_enabled"] and s["proxy_url"]):
        return None
    u = urlsplit(s["proxy_url"] if "://" in s["proxy_url"] else "http://" + s["proxy_url"])
    auth = f"{quote(s['proxy_user'], safe='')}:{quote(s['proxy_pass'], safe='')}@" if s["proxy_user"] else ""
    return f"{u.scheme}://{auth}{u.netloc}"
