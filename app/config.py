"""配置入口。

_Env：环境变量里的初始值（可选）。之后以网页「设置」里保存的为准（见 settings.py）。
cfg：读取时先看网页设置，没有再回退到环境变量，业务代码统一用 cfg.XXX。
"""
import os


def _f(k, d):
    return float(os.environ.get(k, d))


def _i(k, d):
    return int(os.environ.get(k, d))


class _Env:
    TOKEN = os.environ.get("BRIDGE_TOKEN", "")
    DB_PATH = os.environ.get("DB_PATH", "/data/bridge.db")
    APPROVAL = os.environ.get("BRIDGE_APPROVAL", "manual")
    QUOTA_WEEKLY = _i("USER_QUOTA_WEEKLY", 0)
    ADMINS = os.environ.get("BRIDGE_ADMINS", "")

    TMDB_KEY = os.environ.get("TMDB_API_KEY", "")
    PANSOU_URL = os.environ.get("PANSOU_URL", "")
    PROWLARR_URL = os.environ.get("PROWLARR_URL", "")
    PROWLARR_KEY = os.environ.get("PROWLARR_API_KEY", "")

    KITE_URL = os.environ.get("KITE_URL", "")
    KITE_TOKEN = os.environ.get("KITE_TOKEN", "")

    P115_APP_ID = os.environ.get("P115_APP_ID", "")
    P115_COOKIE = os.environ.get("P115_COOKIE", "")
    P115_DEST_MOVIE_CID = _i("P115_DEST_MOVIE_CID", 0)
    P115_DEST_TV_CID = _i("P115_DEST_TV_CID", 0)
    P115_STAGING_CID = _i("P115_STAGING_CID", 0)

    EMBY_URL = os.environ.get("EMBY_URL", "")
    EMBY_KEY = os.environ.get("EMBY_API_KEY", "")
    LITEPAN_URL = os.environ.get("LITEPAN_URL", "")
    LITEPAN_KEY = os.environ.get("LITEPAN_API_KEY", "")
    LITEPAN_EVENT = os.environ.get("LITEPAN_EVENT", "download_completed")
    LITEPAN_SOURCE = os.environ.get("LITEPAN_SOURCE", "RequestBridge")
    LITEPAN_DELAY = _i("LITEPAN_DELAY_SECONDS", 20)
    LITEPAN_WAIT = _i("LITEPAN_WAIT_SECONDS", 120)

    MIN_RES = _i("MIN_RESOLUTION", 720)
    MIN_GB = _f("MIN_FILE_GB", 0.5)
    MAX_GB = _f("MAX_FILE_GB", 5.0)
    PREFER_MIN_GB = _f("PREFER_MIN_GB", 1.0)
    PREFER_MAX_GB = _f("PREFER_MAX_GB", 3.0)
    MAX_ATTEMPTS = _i("MAX_ATTEMPTS", 8)
    OFFLINE_TIMEOUT = _i("OFFLINE_TIMEOUT_SECONDS", 600)
    POLL_SECONDS = _i("POLL_SECONDS", 20)


class _Cfg:
    def __getattr__(self, name):  # 仅在实例上没有该属性时才会走到这里
        if name.startswith("__"):
            raise AttributeError(name)
        from . import settings
        s = settings.get()
        key = name.lower()
        return s[key] if key in s else getattr(_Env, name)


cfg = _Cfg()
