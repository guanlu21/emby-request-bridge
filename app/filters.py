"""资源筛选与打分：分辨率、体积、视频类型、标题/季匹配。纯函数，便于测试。"""
import re
from dataclasses import dataclass

GB = 1024 ** 3
VIDEO_EXT = {".mkv", ".mp4", ".ts", ".m2ts", ".avi", ".mov", ".wmv", ".flv", ".rmvb", ".webm"}
SUB_EXT = {".srt", ".ass", ".ssa", ".sup", ".sub", ".vtt"}  # 字幕跟着视频一起入库，不能丢
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


def is_sub(name: str) -> bool:
    n = name.lower()
    return any(n.endswith(e) for e in SUB_EXT)


def pair_subs(files: list[dict], keep: list[dict]):
    """把字幕文件配给同集数的保留视频。返回 ({视频id: [字幕,...]}, 没配上视频的字幕)。
    按文件名里的集号配对（20.mp4 ↔ 20.chs.srt、S01E20.mkv ↔ S01E20.ass 都能配上）；
    同一集有多个字幕（如简体+繁体）都保留。"""
    subs = [f for f in files if is_sub(f["name"])]
    if not subs:
        return {}, []
    eps = {}
    for f in keep:
        e = classify.episode_of(f["name"])
        if e is not None:
            eps.setdefault(e, []).append(f)
    out, orphan = {}, []
    for s in subs:
        e = classify.episode_of(s["name"])
        match = eps.get(e) if e is not None else None
        if match and len(match) == 1:  # 一集多个同名视频版本时不乱配，留作未配
            out.setdefault(match[0]["id"], []).append(s)
        else:
            orphan.append(s)
    return out, orphan


def file_ok(name: str, size: float, rules: Rules) -> bool:
    if not is_video(name) or has_watermark(name):
        return False
    if not (rules.keep_min <= size <= rules.max_size):
        return False
    res = parse_resolution(name)
    return res == 0 or res >= rules.min_res  # 文件名没写分辨率时，靠体积区间兜底


def file_reason(name: str, size: float, rules: Rules) -> str:
    """文件为什么不要（要的返回空串）：非视频 / 带水印 / 太小 / 太大 / 分辨率不足。"""
    if not is_video(name):
        return "非视频"
    if has_watermark(name):
        return "带水印"
    if size < rules.keep_min:
        return "太小"
    if size > rules.max_size:
        return "太大"
    res = parse_resolution(name)
    if res and res < rules.min_res:
        return "分辨率不足"
    return ""


def explain_files(files: list[dict], rules: Rules) -> str:
    """'共 12 个文件：太小 10、非视频 2'。"""
    from collections import Counter
    c = Counter(file_reason(f["name"], f["size"], rules) or "合格" for f in files)
    return f"共 {len(files)} 个文件：" + "、".join(f"{k} {v}" for k, v in c.most_common())


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


def parse_terms(s: str) -> list[list[str]]:
    """'国语|国配, 中字' → [['国语','国配'], ['中字']]（小写；一项里的 | 表示同义词）"""
    return [[a.strip().lower() for a in t.split("|") if a.strip()]
            for t in re.split(r"[,，;；\n]+", s or "") if t.strip()]


def kw_reason(text: str, all_s: str = "", any_s: str = "", exclude_s: str = "") -> str:
    """不满足自定义关键词条件时返回原因，满足返回空串。"""
    t = (text or "").lower()
    hit = lambda grp: any(a in t for a in grp)
    for g in parse_terms(exclude_s):
        if g and hit(g):
            return f"含排除词「{g[0]}」"
    for g in parse_terms(all_s):
        if g and not hit(g):
            return f"缺少「{g[0]}」"
    anys = [g for g in parse_terms(any_s) if g]
    if anys and not any(hit(g) for g in anys):
        return "缺少关键词（" + "/".join(g[0] for g in anys) + "）"
    return ""


def kw_bonus(text: str, prefer_s: str = "") -> int:
    t = (text or "").lower()
    return min(sum(1 for g in parse_terms(prefer_s) if g and any(a in t for a in g)) * 8, 32)


def norm(s: str) -> str:
    return re.sub(r"[\W_]+", "", s.lower())


PACK_STRICT = re.compile(r"全\s*\d+\s*集|\d+\s*集全|完结|(?i:E|EP)\d{1,3}\s*[-~]\s*(?i:E|EP)?\d{1,3}")
PACK_ALL = re.compile(r"全集|合集|(?i:complete)")
PACK_LOOSE = re.compile(r"全集|合集|(?i:complete)|" + PACK_STRICT.pattern)
STOP = {"the", "a", "an", "of", "and"}


def _cn_int(v: str) -> int:
    """'3'、'三'、'十'、'十二'、'二十' → int"""
    if v.isdigit():
        return int(v)
    if "十" in v:
        head, _, tail = v.partition("十")
        return (CN_NUM.get(head, 1) if head else 1) * 10 + (CN_NUM.get(tail, 0) if tail else 0)
    return CN_NUM.get(v, 0)


_NUM = r"\d{1,2}|[一二三四五六七八九十]{1,3}"


def season_span(title: str):
    """标题里写明的季范围 (起, 止)；单季返回 (n, n)；没写返回 None。
    支持 S01-S03、第1-3季、第一季至第五季、全3季、3季全、单独的 S02 / 第二季。"""
    t = title
    m = re.search(r"(?i)(?<![a-z])S(\d{1,2})\s*[-~至到]\s*S?(\d{1,2})(?!\d)", t)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = re.search(rf"第\s*({_NUM})\s*季?\s*[-~至到]\s*第?\s*({_NUM})\s*季", t)
    if m:
        return _cn_int(m.group(1)), _cn_int(m.group(2))
    m = re.search(rf"[全共]\s*({_NUM})\s*季|(?<![第\d])({_NUM})\s*季\s*全(?!集)", t)
    if m:
        return 1, _cn_int(m.group(1) or m.group(2))
    m = re.search(r"(?i)(?<![a-z])S(\d{1,2})(?:\s*E\d{1,3})?(?![a-z0-9])", t)
    if m:
        return int(m.group(1)), int(m.group(1))
    m = re.search(rf"第\s*({_NUM})\s*季", t)
    if m:
        return _cn_int(m.group(1)), _cn_int(m.group(1))
    return None


def parse_season(title: str):
    """返回 (季号|None, 是否整季包, 是否多季)。多季时季号为 None。"""
    span = season_span(title)
    if span and span[0] != span[1]:
        return None, False, True
    if not span:
        return None, False, False
    s = span[0]
    m = re.search(r"(?i)S\d{1,2}\s*E(\d{1,3})", title)
    if m:
        return s, bool(PACK_LOOSE.search(title)), False
    single = re.search(r"第\s*\d+\s*[集话話]|(?i:(?<![a-z0-9])EP?\d{1,3}(?!\d))", title) and not PACK_LOOSE.search(title)
    return s, not single, False


SINGLE_EP = re.compile(r"第\s*\d{1,3}\s*[集话話](?!\s*[-~至到])|(?i:(?<![a-z0-9])EP?\s*\d{1,3}(?![\d-]))")


def single_episode_only(title: str) -> bool:
    return bool(SINGLE_EP.search(title)) and not PACK_LOOSE.search(title) and not re.search(r"(?<![第\d])\d+\s*集", title)


_SEASON_PATS = [re.compile(r"(?i)S(\d{1,2})[ ._-]*E\d"), re.compile(r"(?i)(?<![a-z])Season[ ._-]*(\d{1,2})"),
                re.compile(rf"第\s*({_NUM})\s*季"), re.compile(r"(?i)(?<![a-z0-9])S(\d{1,2})(?![a-z0-9])")]


def season_of_path(path: str):
    """文件所属的季：先看文件名，再往上看各级文件夹名；认不出返回 None。"""
    for comp in reversed(re.split(r"[\\/]", path)):
        for p in _SEASON_PATS:
            m = p.search(comp)
            if m:
                return _cn_int(m.group(1))
    return None


def assign_seasons(files: list[dict], wanted: list[int], title: str) -> dict:
    """把下载到的视频分给请求的各季 → {季: [文件]}。
    文件/文件夹名里识别出季的，按识别结果分；整包都没标季时：标题不是多季合集、且只请求一季，才整包算这一季。"""
    seasons = {f["id"]: season_of_path(f.get("path") or f["name"]) for f in files}
    out = {s: [f for f in files if seasons[f["id"]] == s] for s in wanted}
    if any(seasons.values()):
        return {s: fl for s, fl in out.items() if fl}
    span = season_span(title)
    if len(wanted) == 1 and not (span and span[0] != span[1]):
        s = wanted[0]
        if s <= 1 or span == (s, s):
            return {s: list(files)}
    return {}


def verify_season(files: list[dict], season: int, title: str) -> bool:
    """第 2 季及以后：标题明确写了就是这一季，或文件/文件夹名里能识别出这一季，才算确认。"""
    if season <= 1:
        return True
    if season_span(title) == (season, season):
        return True
    return any(season_of_path(f.get("path") or f["name"]) == season for f in files)


def filter_season(files: list[dict], season: int):
    """合集（多季）下载完成后，只留请求的这一季。(保留, 丢弃)
    规则：识别出的季里有别的季，才会过滤；整包都没标季、或都是这一季，原样保留。"""
    seasons = {f["id"]: season_of_path(f.get("path") or f["name"]) for f in files}
    detected = [s for s in seasons.values() if s]
    if not detected or all(s == season for s in detected):
        return files, []
    return [f for f in files if seasons[f["id"]] == season], [f for f in files if seasons[f["id"]] != season]


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
    """名称必须完整出现在资源标题里：不拆词、不乱序、不去副标题（任何文字都一样）；
    只忽略大小写、空格和标点（Dune.Part.Two = Dune: Part Two）。片名后面紧跟 1-2 位数字的当续集排除。"""
    rt = norm(result_title)
    spaced = re.sub(r"[\W_]+", " ", result_title.lower()).strip()
    for n in names:
        k = norm(n)
        if k and k in rt and not _is_sequel(spaced, n):
            return True
    return False


def has_cjk(s: str) -> bool:
    return bool(re.search(r"[\u4e00-\u9fff\u3400-\u4dbf]", s or ""))


def chinese_rank(title: str, names: list[str]) -> int:
    """2 = 标题里有完整的中文片名；1 = 标题里有中文（如「国语中字」）；0 = 全是外文。"""
    zh = next((n for n in names if has_cjk(n)), "")
    if zh and norm(zh) in norm(title):
        return 2
    return 1 if has_cjk(title) else 0


def similarity(title: str, names: list[str]) -> float:
    """标题和片名有多像（按相邻两字的重合度，0~1）：被过滤的资源里，把「差一点就匹配」的排到前面。"""
    def bigrams(s):
        k = norm(s)
        return {k[i:i + 2] for i in range(len(k) - 1)} or set(k)
    t = bigrams(title)
    best = 0.0
    for n in names:
        b = bigrams(n)
        if b:
            best = max(best, len(b & t) / len(b))
    return best


def year_rank(title: str, names: list[str], year) -> int:
    """年份吻合度：2 = 同年；1 = 差一年；0 = 标题里没写年份（也可以考虑，只是排在后面）。"""
    try:
        ty = int(year)
    except (TypeError, ValueError):
        return 0
    joined = " ".join(names)
    ys = [int(y) for y in _years(title) if y not in joined]
    if not ys:
        return 0
    if any(y == ty for y in ys):
        return 2
    return 1 if any(abs(y - ty) <= 1 for y in ys) else 0


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
    span = season_span(result_title)
    multi = bool(span and span[0] != span[1])
    if multi:                                   # 多季合集：请求的这一季在范围内就收（下载后只留这一季）
        if not (span[0] <= season <= span[1]):
            return "季不在合集范围内"
    elif span:
        if span[0] != season:
            return "季不符"
        s, pack, _ = parse_season(result_title)
        if not pack:
            return "只有单集"
    else:                                       # 标题没写季：第 1 季直接收；其它季只收「全集/合集」这类整包
        if single_episode_only(result_title):
            return "只有单集"
        if season != 1 and not PACK_ALL.search(result_title):
            return "没写季信息（且不是第 1 季）"
    if ys and ty:
        ok = any(abs(int(y) - ty) <= 1 for y in ys) if (season == 1 and not multi) else any(int(y) >= ty - 1 for y in ys)
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


def magnet_size_ok(size: float, media_type: str, episodes: int, rules: Rules, multi: bool = False) -> bool:
    """磁力只知道总大小：电影看总大小；电视剧按每集平均大小（下限用「下载后保留的最小文件」，不是 500MB）；
    多季合集或不知道集数时，只要求不是明显太小。"""
    if media_type == "movie":
        return rules.min_size <= size <= rules.max_size
    if multi or episodes <= 0:
        return size >= rules.keep_min
    return rules.keep_min <= size / episodes <= rules.max_size


PRIORITY_DEFAULT = "chinese,year,quality,drive,keywords,size,source,seeders"
PRIORITY_LABELS = {"chinese": "中文名称", "year": "年份吻合", "quality": "画质", "drive": "优先的网盘", "keywords": "优先关键词", "size": "体积合适", "source": "分享优先", "seeders": "做种数"}


def priority_tuple(order: str, facts: dict) -> tuple:
    """按 order（逗号分隔，靠前的优先）依次比较各项；facts 里没有的项按 0 算。越大越优先。"""
    keys = [k.strip() for k in (order or PRIORITY_DEFAULT).split(",") if k.strip() in PRIORITY_LABELS]
    if "chinese" not in keys:  # 以前保存的顺序里没有「中文名称」：默认它排第一（想改位置/去掉，在设置里重新排）
        keys.insert(0, "chinese")
    keys += [k for k in PRIORITY_DEFAULT.split(",") if k not in keys]
    return tuple(facts.get(k, 0) for k in keys)
