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


# ---------------------------------------------------------------- 文件命名
def sanitize(s: str) -> str:
    return re.sub(r'[\\/:*?"<>|]+', " ", s).strip()


EP_PATTERNS = [
    re.compile(r"(?i)S\d{1,2}[ ._-]*E(\d{1,3})"),
    re.compile(r"第\s*(\d{1,3})\s*[集话話]"),
    re.compile(r"(?i)(?<![a-z0-9])EP?\s*(\d{1,3})(?!\d)"),
    re.compile(r"[\[【](\d{1,3})(?:v\d)?[\]】]"),
    re.compile(r"\s-\s(\d{1,3})(?:v\d)?(?=[\s.\[(])"),
]


def episode_of(name: str):
    for p in EP_PATTERNS:
        m = p.search(name)
        if m:
            return int(m.group(1))
    return None


def target_name(media_type: str, title: str, year, season, name: str, used: set):
    """返回新文件名；认不出集数（电视剧）或不需要改名时返回 None。"""
    ext = os.path.splitext(name)[1].lower()
    base = f"{sanitize(title)} ({year})" if year else sanitize(title)
    if media_type == "movie":
        return base + ext
    ep = episode_of(name)
    if ep is None or ep in used or not season:
        return None
    used.add(ep)
    return f"{base} - S{int(season):02d}E{ep:02d}{ext}"
