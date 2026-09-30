"""资源筛选与打分：分辨率、体积、视频类型、标题/季匹配。纯函数，便于测试。"""
import re
from dataclasses import dataclass

GB = 1024 ** 3
VIDEO_EXT = {".mkv", ".mp4", ".ts", ".m2ts", ".avi", ".mov", ".wmv", ".flv", ".rmvb", ".webm"}
BAD_TAGS = re.compile(r"(?i)(?<![a-z])(cam|hdcam|hdts|telesync|telecine|screener|tc|ts)(?![a-z])|枪版")
CN_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


@dataclass
class Rules:
    min_res: int = 720
    min_size: float = 0.5 * GB
    max_size: float = 5 * GB


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
    if not is_video(name):
        return False
    if not (rules.min_size <= size <= rules.max_size):
        return False
    res = parse_resolution(name)
    return res == 0 or res >= rules.min_res  # 文件名没写分辨率时，靠体积区间兜底


def select_files(files: list[dict], rules: Rules):
    """files: [{id,name,size}]  ->  (保留, 丢弃)"""
    keep, drop = [], []
    for f in files:
        (keep if file_ok(f["name"], f["size"], rules) else drop).append(f)
    return keep, drop


def norm(s: str) -> str:
    return re.sub(r"[\W_]+", "", s.lower())


def parse_season(title: str):
    """返回 (季号|None, 是否整季包, 是否多季)"""
    t = title
    if re.search(r"(?i)S\d{1,2}\s*[-~]\s*S?\d{1,2}(?!\d)|第\s*\d+\s*[-~]\s*\d+\s*季", t):
        return None, False, True
    m = re.search(r"(?i)(?<![a-z])S(\d{1,2})(?:\s*E(\d{1,3}))?(?![a-z0-9])", t)
    if m:
        return int(m.group(1)), m.group(2) is None, False
    m = re.search(r"第\s*(\d{1,2}|[一二三四五六七八九十])\s*季", t)
    if m:
        v = m.group(1)
        return (int(v) if v.isdigit() else CN_NUM[v]), not re.search(r"第\s*\d+\s*集|E\d+", t, re.I), False
    return None, False, False


def title_match(result_title: str, names: list[str], year, media_type: str, season):
    rt = norm(result_title)
    if not any(norm(n) and norm(n) in rt for n in names):
        return False
    if media_type == "movie":
        return not year or str(year) in result_title
    s, pack, multi = parse_season(result_title)
    return (not multi) and s == season and pack


def score(title: str, seeders: int = 0, is_share: bool = False) -> int:
    t = title.lower()
    sc = {1080: 50, 2160: 48, 720: 20}.get(parse_resolution(title), 0)
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
