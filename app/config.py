import os


def _f(k, d):
    return float(os.environ.get(k, d))


def _i(k, d):
    return int(os.environ.get(k, d))


class cfg:
    TOKEN = os.environ.get("BRIDGE_TOKEN", "change-me")
    DB_PATH = os.environ.get("DB_PATH", "/data/bridge.db")
    TMDB_KEY = os.environ.get("TMDB_API_KEY", "")

    PANSOU_URL = os.environ.get("PANSOU_URL", "http://pansou:8888")
    PROWLARR_URL = os.environ.get("PROWLARR_URL", "http://prowlarr:9696")
    PROWLARR_KEY = os.environ.get("PROWLARR_API_KEY", "")

    P115_COOKIE = os.environ.get("P115_COOKIE", "")
    # 115 文件夹 ID（浏览器打开文件夹，URL 里 cid= 后面的数字）
    P115_DEST_MOVIE_CID = _i("P115_DEST_MOVIE_CID", 0)
    P115_DEST_TV_CID = _i("P115_DEST_TV_CID", 0)
    P115_STAGING_CID = _i("P115_STAGING_CID", 0)  # 暂存目录，务必放在 LitePan 监控目录之外

    EMBY_URL = os.environ.get("EMBY_URL", "")
    EMBY_KEY = os.environ.get("EMBY_API_KEY", "")
    LITEPAN_TRIGGER_URL = os.environ.get("LITEPAN_TRIGGER_URL", "")  # 可选：POST 触发整理/strm
    LITEPAN_WAIT = _i("LITEPAN_WAIT_SECONDS", 120)  # 文件落盘后等 LitePan 生成 strm 的时间

    # 资源要求
    MIN_RES = _i("MIN_RESOLUTION", 720)
    MIN_GB = _f("MIN_FILE_GB", 0.5)
    MAX_GB = _f("MAX_FILE_GB", 5.0)

    MAX_ATTEMPTS = _i("MAX_ATTEMPTS", 8)
    OFFLINE_TIMEOUT = _i("OFFLINE_TIMEOUT_SECONDS", 1800)
    POLL_SECONDS = _i("POLL_SECONDS", 20)
