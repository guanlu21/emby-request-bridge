"""按 TMDB 信息决定影片放进哪个分类目录，并给文件起 Emby 认得的名字。

目录结构（目录名可在设置里改）：
  影视根目录/
    电影/{国产,港台,日韩,欧美}/片名 (年份)/片名 (年份).mkv
    电视剧/{国产剧,港台剧,日韩剧,欧美剧}/剧名 (年份)/Season 01/剧名 (年份) - S01E01.mkv
    动漫/ 综艺/ 纪录片/
"""
import os
import re

from .config import cfg

GENRE_DOC, GENRE_ANIM, GENRE_REALITY, GENRE_TALK = 99, 16, 10764, 10767
ASIA_ANIME_LANG = {"ja", "zh", "cn", "ko"}
ASIA_ANIME_COUNTRY = {"JP", "CN", "KR", "HK", "TW"}


def region_names() -> list[str]:
    names = [x.strip() for x in str(cfg.REGION_NAMES).replace("，", ",").split(",") if x.strip()]
    return (names + ["国产", "港台", "日韩", "欧美"][len(names):])[:4]


def region_index(lang: str, countries: list[str]) -> int:
    """0 国产  1 港台  2 日韩  3 欧美"""
    cs = [c.upper() for c in countries]
    if lang in ("ja", "ko"):
        return 2
    if lang == "cn":          # TMDB 里 cn 是粤语
        return 1
    if lang == "zh":
        if "CN" in cs:
            return 0
        return 1 if any(c in cs for c in ("HK", "TW", "MO")) else 0
    first = cs[0] if cs else ""
    if first == "CN":
        return 0
    if first in ("HK", "TW", "MO"):
        return 1
    if first in ("JP", "KR"):
        return 2
    return 3


def classify(media_type: str, meta: dict) -> list[str]:
    """返回影视根目录下的分类路径（不含片名目录）。"""
    g, lang = set(meta.get("genres") or []), meta.get("lang") or ""
    cs = [c.upper() for c in meta.get("countries") or []]
    if GENRE_DOC in g:
        return [cfg.DIR_DOC]
    if media_type == "tv" and (GENRE_REALITY in g or GENRE_TALK in g):
        return [cfg.DIR_VARIETY]
    if GENRE_ANIM in g:
        if media_type == "tv" and (lang in ASIA_ANIME_LANG or set(cs) & ASIA_ANIME_COUNTRY):
            return [cfg.DIR_ANIME]
        if media_type == "movie" and lang == "ja":
            return [cfg.DIR_ANIME]
    if not lang and not cs:    # 没有 TMDB 信息，只能放到大类下面
        return [cfg.DIR_MOVIE if media_type == "movie" else cfg.DIR_TV]
    region = region_names()[region_index(lang, cs)]
    return [cfg.DIR_MOVIE, region] if media_type == "movie" else [cfg.DIR_TV, region + cfg.TV_SUFFIX]


# ---------------------------------------------------------------- 命名
def sanitize(s: str) -> str:
    return re.sub(r'[\\/:*?"<>|]+', " ", str(s)).strip()


SRC_PATTERNS = [(re.compile(r"(?i)remux"), "Remux"), (re.compile(r"(?i)blu-?ray|bdrip|bd-?rip"), "BluRay"),
                (re.compile(r"(?i)web-?dl"), "WEB-DL"), (re.compile(r"(?i)webrip"), "WEBRip"), (re.compile(r"(?i)hdtv"), "HDTV")]
CODEC_PATTERNS = [(re.compile(r"(?i)x265|h\.?265|hevc"), "HEVC"), (re.compile(r"(?i)x264|h\.?264|avc"), "H264"),
                  (re.compile(r"(?i)(?<![a-z])av1(?![a-z0-9])"), "AV1")]


def media_tags(text: str) -> dict:
    """从文件名/资源标题里提取分辨率、片源、编码，用于命名模板。"""
    from .filters import parse_resolution
    res = parse_resolution(text or "")
    out = {"res": f"{res}p" if res else "", "source": "", "codec": ""}
    for pat, name in SRC_PATTERNS:
        if pat.search(text or ""):
            out["source"] = name
            break
    for pat, name in CODEC_PATTERNS:
        if pat.search(text or ""):
            out["codec"] = name
            break
    return out


def vars_for(r: dict, meta: dict) -> dict:
    s = r.get("season") or 0
    return {"title": sanitize(meta["names"][0]), "year": str(meta.get("year") or ""), "tmdb": str(r.get("tmdb_id") or ""),
            "season": str(s) if s else "", "season2": f"{s:02d}" if s else ""}


def render(tpl: str, v: dict) -> str:
    """套用命名模板：取不到的变量省略，并清理由此留下的空括号、悬空的 - 和多余空格。"""
    s = re.sub(r"\{(\w+)\}", lambda m: sanitize(v.get(m.group(1), "")), tpl)
    s = re.sub(r"\(\s*\)|\[\s*\]|\[[^\]\[=]*=\s*\]", "", s)
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"(?:\s*-\s*)+$", "", s)
    s = re.sub(r"\s-(?:\s-)+\s", " - ", s)
    return s.strip()


EP_PATTERNS = [
    re.compile(r"(?i)S\d{1,2}[ ._-]*E(\d{1,3})"),
    re.compile(r"第\s*(\d{1,3})\s*[集话話]"),
    re.compile(r"(?i)(?<![a-z0-9])EP?\s*(\d{1,3})(?!\d)"),
    re.compile(r"[\[【](\d{1,3})(?:v\d)?[\]】]"),
    re.compile(r"\s-\s(\d{1,3})(?:v\d)?(?=[\s.(\[])"),
    # 裸数字集数：12.mp4 这种文件名只有集数（上面的 EP? 模式要求先有字母 E，匹配不到）
    re.compile(r"(?i)^(\d{1,3})\.(?:mp4|mkv|avi|ts|mov|wmv|mpg|mpeg|m2ts|iso|m4v|rmvb|flv)$"),  # 12.mp4
    re.compile(r"(?i)\s(\d{1,3})(?=\.\w{1,4}$)"),  # xxx 07.mkv，空格后面的数字直接接扩展名
]


def episode_of(name: str):
    for p in EP_PATTERNS:
        m = p.search(name)
        if m:
            return int(m.group(1))
    return None


def target_name(r: dict, meta: dict, file_name: str, hint: str, used: set):
    """返回新文件名；电视剧认不出集数/集数重复，或模板渲染为空时返回 None（保留原名）。"""
    from .config import cfg
    ext = os.path.splitext(file_name)[1].lower()
    v = vars_for(r, meta)
    tags = media_tags(file_name)
    hint_tags = media_tags(hint)
    v.update({k: tags[k] or hint_tags[k] for k in tags})
    if r["media_type"] == "movie":
        base = render(cfg.NAME_MOVIE_FILE, v)
    else:
        ep = episode_of(file_name)
        if ep is None or ep in used or not r.get("season"):
            return None
        used.add(ep)
        v.update({"ep": str(ep), "ep2": f"{ep:02d}"})
        base = render(cfg.NAME_TV_FILE, v)
    return base + ext if base else None
