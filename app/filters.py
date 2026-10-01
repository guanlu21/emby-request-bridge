"""资源筛选与打分：分辨率、体积、视频类型、标题/季匹配。纯函数，便于测试。"""
import re
from dataclasses import dataclass

GB = 1024 ** 3
VIDEO_EXT = {".mkv", ".mp4", ".ts", ".m2ts", ".avi", ".mov", ".wmv", ".flv", ".rmvb", ".webm"}
BAD_TAGS = re.compile(r"(?i)(?<![a-z])(cam|hdcam|hdts|hdtc|telesync|telecine|screener|tc|ts)(?![a-z])|枪版")
CN_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


WM_BAD = re.compile(r"(?<![无去免])水印|watermark|[带有]logo", re.I)
WM_OK = re.compile(r"[无去免]水印|no[ ._-]?watermark|watermark[ ._-]?free", re.I)


def has_watermark(text: str) -> bool:
    return bool(WM_BAD.search(text)) and not WM_OK.search(text)


@dataclass
class Rules:
    min_res: int = 720
    min_size: float = 0.5 * GB
    max_size: float = 5 * GB
    pref_min: float = 1 * GB   # 偏好区间，只影响排序
    pref_max: float = 3 * GB
    keep_min: float = 100 * 1024 ** 2  # 下载完成后保留文件的最小体积（小于它的当样片/花絮删掉）


def parse_resolution(title: str) -> int:
    t = title.lower()
    if re.search(r"2160[pi]|(?<![a-z0-9])(4k|uhd)(?![a-z0-9])", t):
        return 2160
    m = re.search(r"(?<!\d)(1080|720|576|480)[pi]", t)
    return int(m.group(1)) if m else 0


def is_video(name: str) -> bool:
    n = name.lower()
    return any(n.endswith(e) for e in VIDEO_EXT)


def file_ok(name: str, size: float, rules: Rules) -> bool:
    if not is_video(name) or has_watermark(name):
        return False
    if not (rules.keep_min <= size <= rules.max_size):
        return False
    res = parse_resolution(name)
    return res == 0 or res >= rules.min_res  # 文件名没写分辨率时，靠体积区间兜底


def select_files(files: list[dict], rules: Rules):
    """files: [{id,name,size}]  ->  (保留, 丢弃)"""
    keep, drop = [], []
    for f in files:
        (keep if file_ok(f["name"], f["size"], rules) else drop).append(f)
    return keep, drop


def pick_best_file(files: list[dict], rules: Rules) -> dict:
    """电影分享里常带多个版本，只留一个：优先 1080p，其次落在偏好体积区间，再取较大的。"""
    def key(f):
        return (parse_resolution(f["name"]) == 1080, rules.pref_min <= f["size"] <= rules.pref_max,
                bool(WM_OK.search(f["name"])), f["size"])
    return max(files, key=key)


def norm(s: str) -> str:
    return re.sub(r"[\W_]+", "", s.lower())


PACK_STRICT = re.compile(r"全\s*\d+\s*集|\d+\s*集全|完结|(?i:E|EP)\d{1,3}\s*[-~]\s*(?i:E|EP)?\d{1,3}")
PACK_LOOSE = re.compile(r"全集|合集|(?i:complete)|" + PACK_STRICT.pattern)
STOP = {"the", "a", "an", "of", "and"}


def parse_season(title: str):
    """返回 (季号|None, 是否整季包, 是否多季)"""
    t = title
    if re.search(r"(?i)S\d{1,2}\s*[-~]\s*S?\d{1,2}(?!\d)|第\s*\d+\s*[-~]\s*\d+\s*季", t):
        return None, False, True
    m = re.search(r"(?i)(?<![a-z])S(\d{1,2})(?:\s*E(\d{1,3}))?(?![a-z0-9])", t)
    if m:
        return int(m.group(1)), m.group(2) is None or bool(PACK_LOOSE.search(t)), False
    m = re.search(r"第\s*(\d{1,2}|[一二三四五六七八九十])\s*季", t)
    if m:
        v = m.group(1)
        single = re.search(r"第\s*\d+\s*集|E\d+", t, re.I) and not PACK_LOOSE.search(t)
        return (int(v) if v.isdigit() else CN_NUM[v]), not single, False
    return None, False, False


def _years(text: str) -> list[str]:
    return re.findall(r"(?<!\d)((?:19|20)\d{2})(?!\d)", text)


def _is_sequel(spaced_title: str, name: str) -> bool:
    """片名后面紧跟 1-2 位数字或 Ⅱ/Ⅲ，而 TMDB 片名本身不带数字：多半是续集（流浪地球2 ≠ 流浪地球）。年份是 4 位数，不受影响。"""
    ns = re.sub(r"[\W_]+", " ", name.lower()).strip()
    i = spaced_title.find(ns) if ns else -1
    if i < 0 or re.search(r"\d$", ns):
        return False
    return bool(re.match(r"\s*(?:\d{1,2}|[ⅱⅲⅳ])(?:\s|$)", spaced_title[i + len(ns):], re.I))


def name_hit(result_title: str, names: list[str]) -> bool:
    """名字命中：整名包含，或去掉副标题后包含，或英文名的关键词都出现（不要求相邻、不在乎标点）。"""
    rt = norm(result_title)
    toks = set(re.findall(r"[a-z0-9]+", result_title.lower()))
    cands = []
    for n in names:
        cands.append(n)
        head = re.split(r"[:：]", n, maxsplit=1)[0].strip()
        if head and head != n and len(norm(head)) >= 2:
            cands.append(head)
    spaced = re.sub(r"[\W_]+", " ", result_title.lower()).strip()
    for n in cands:
        k = norm(n)
        if k and k in rt:
            if not _is_sequel(spaced, n):
                return True
            continue
        ts = [t for t in re.findall(r"[a-z0-9]+", n.lower()) if t not in STOP]
        if len(ts) >= 2 and all(t in toks for t in ts):
            return True
    return False


def match_reason(result_title: str, names: list[str], year, media_type: str, season) -> str:
    """不匹配时返回原因，匹配返回空串。年份允许 ±1；标题里没写年份不算错。"""
    if not name_hit(result_title, names):
        return "标题不匹配"
    joined = " ".join(names)
    ys = [y for y in _years(result_title) if y not in joined]  # 片名本身带数字（如 1917）不算年份
    try:
        ty = int(year)
    except (TypeError, ValueError):
        ty = None
    if media_type == "movie":
        if ys and ty and not any(abs(int(y) - ty) <= 1 for y in ys):
            return "年份不符"
        return ""
    s, pack, multi = parse_season(result_title)
    if multi:
        return "多季合集"
    if s is not None:
        if s != season:
            return "季不符"
        if not pack:
            return "只有单集"
    elif season != 1 or not PACK_STRICT.search(result_title):
        return "没写季/整季信息"
    if ys and ty:
        ok = any(abs(int(y) - ty) <= 1 for y in ys) if season == 1 else any(int(y) >= ty - 1 for y in ys)
        if not ok:
            return "年份不符"
    return ""


def title_match(result_title: str, names: list[str], year, media_type: str, season):
    return not match_reason(result_title, names, year, media_type, season)


def score(title: str, seeders: int = 0, is_share: bool = False, per_file: float = 0, rules: Rules = None) -> int:
    t = title.lower()
    sc = {1080: 50, 2160: 35, 720: 15}.get(parse_resolution(title), 0)
    if rules and per_file and rules.pref_min <= per_file <= rules.pref_max:
        sc += 25
    if WM_OK.search(title):
        sc += 10
    if re.search(r"blu-?ray|bdrip", t):
        sc += 15
    elif re.search(r"web-?dl|webrip", t):
        sc += 12
    elif "hdtv" in t:
        sc += 5
    if re.search(r"x265|hevc|h\.?265", t):
        sc += 8
    if re.search(r"中字|简中|简体|双语|内封|内嵌|chs|国语|国配|中文字幕", t):
        sc += 20
    sc += int(min(seeders, 50) * 0.5)
    if is_share:
        sc += 15
    return sc


def magnet_size_ok(size: float, media_type: str, episodes: int, rules: Rules) -> bool:
    """磁力只知道总大小：电影看总大小，整季包按平均每集大小折算。"""
    per = size if media_type == "movie" else size / max(episodes, 1)
    return rules.min_size <= per <= rules.max_size
