import asyncio
import hmac
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import FileResponse

from . import auth as auth_mod, db, emby, settings, tmdb
from .config import cfg
from .drive115 import LazyDrive
from .pipeline import Pipeline, notify_after
from .sources import search_all, tmdb_meta

_tasks: set = set()
_fails: dict = {}  # 登录失败限流：(ip,用户名) -> [时间戳]
_imgs: dict = {}
pipe: Pipeline | None = None


async def _meta(r):
    m = await tmdb_meta(r["media_type"], r["tmdb_id"], r["season"])
    if not m:
        title = re.sub(r"\s*\(\d{4}\)\s*$", "", r["title"])
        y = re.search(r"\((\d{4})\)\s*$", r["title"])
        m = {"names": [title], "year": y.group(1) if y else "", "episodes": 0}
    return m


def spawn(rid):
    t = asyncio.create_task(pipe.run(rid))
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)


@asynccontextmanager
async def lifespan(app):
    global pipe
    pipe = Pipeline(LazyDrive(cfg.P115_COOKIE), _meta, search_all, notify_after)
    for rid in db.unfinished():  # 重启后继续未完成的请求
        spawn(rid)
    yield


app = FastAPI(lifespan=lifespan)


def who(request: Request, x_token: str = "") -> dict:
    """登录的 Emby 用户（会话 Cookie），或管理令牌（供脚本 / emby-manager 调用）。"""
    if x_token and hmac.compare_digest(x_token, cfg.TOKEN):
        return {"uid": "", "name": "token", "admin": True}
    u = auth_mod.verify(request.cookies.get("rb_session", ""))
    if not u:
        raise HTTPException(401, "请先登录")
    return u


def admin(request: Request, x_token: str = "") -> dict:
    u = who(request, x_token)
    if not u["admin"]:
        raise HTTPException(403, "需要管理员权限")
    return u


# ---------- 登录 ----------
@app.post("/api/login")
async def login(request: Request, response: Response):
    b = await request.json()
    name, pw = (b.get("username") or "").strip(), b.get("password") or ""
    key = (request.client.host if request.client else "", name.lower())
    recent = [t for t in _fails.get(key, []) if t > time.time() - 600]
    if len(recent) >= 5:
        raise HTTPException(429, "尝试次数太多，请 10 分钟后再试")
    try:
        u = await auth_mod.emby_login(name, pw)
    except auth_mod.AuthError as e:
        _fails[key] = recent + [time.time()]
        raise HTTPException(401, str(e))
    _fails.pop(key, None)
    response.set_cookie("rb_session", auth_mod.make(u), max_age=7 * 86400, httponly=True, samesite="lax")
    return {"name": u["name"], "admin": u["admin"]}


@app.post("/api/logout")
def logout(response: Response):
    response.delete_cookie("rb_session")
    return {"ok": True}


@app.get("/api/me")
def me(request: Request, x_token: str = Header("")):
    u = who(request, x_token)
    return {"name": u["name"], "admin": u["admin"]}


# ---------- 搜索与求片 ----------
@app.get("/api/search")
async def search(request: Request, q: str = "", x_token: str = Header("")):
    who(request, x_token)
    if not q.strip():
        return []
    try:
        items = await tmdb.search(q.strip())
    except Exception as e:  # noqa
        raise HTTPException(502, f"TMDB 搜索失败：{e}")
    have = await asyncio.gather(*[emby.has_item(i["type"], i["id"]) for i in items])
    for it, h in zip(items, have):
        it["in_library"] = h
        it["requested"] = db.status_of(it["type"], it["id"])
    return items


@app.get("/api/tv/{tv_id}")
async def tv(tv_id: int, request: Request, x_token: str = Header("")):
    who(request, x_token)
    try:
        return await tmdb.tv_seasons(tv_id)
    except Exception as e:  # noqa
        raise HTTPException(502, f"TMDB 查询失败：{e}")


@app.post("/api/request")
async def make_request(request: Request, x_token: str = Header("")):
    u = who(request, x_token)
    b = await request.json()
    mt, tid = b.get("media_type"), int(b.get("tmdb_id") or 0)
    if mt not in ("movie", "tv") or not tid:
        raise HTTPException(400, "参数不对")
    if not u["admin"] and cfg.QUOTA_WEEKLY and db.count_recent(u["name"]) >= cfg.QUOTA_WEEKLY:
        raise HTTPException(429, f"本周求片已达上限（{cfg.QUOTA_WEEKLY} 部）")
    title = f"{b.get('title', '')} ({b.get('year')})" if b.get("year") else b.get("title", "")
    seasons = [None]
    if mt == "tv":
        seasons = [int(s) for s in b.get("seasons", [])]
        if not seasons:
            raise HTTPException(400, "请至少选一季")
    status = "queued" if (u["admin"] or cfg.APPROVAL == "auto") else "pending"
    ids, dup = [], 0
    for s in seasons:
        rid = db.create(mt, tid, s, title, u["name"], status, u["uid"])
        if rid:
            ids.append(rid)
            if status == "queued":
                spawn(rid)
        else:
            dup += 1
    return {"created": ids, "duplicates": dup, "status": status}


@app.get("/api/requests")
def list_requests(request: Request, user: str = "", x_token: str = Header("")):
    u = who(request, x_token)
    return db.list_all(user if u["admin"] else u["name"])


# ---------- 管理 ----------
@app.post("/api/requests/batch")
async def batch(request: Request, x_token: str = Header("")):
    admin(request, x_token)
    body = await request.json()
    action = body.get("action")
    if action not in ("approve", "reject", "retry", "delete"):
        raise HTTPException(400, "bad action")
    done = db.batch(body.get("ids", []), action)
    if action in ("approve", "retry"):
        for rid in done:
            spawn(rid)
    return {"done": done}


@app.post("/api/requests/{rid}/retry")
def retry(rid: int, request: Request, reset: bool = False, x_token: str = Header("")):
    admin(request, x_token)
    if not db.get(rid):
        raise HTTPException(404)
    if reset:
        db.update(rid, tried="[]")
    spawn(rid)
    return {"ok": True}


@app.get("/api/users")
async def users(request: Request, x_token: str = Header("")):
    """按求片人汇总，并带上该 Emby 账号当前是否停用（供管理页或 emby-manager 调用）。"""
    admin(request, x_token)
    stats = db.user_stats()
    try:
        eu = await emby.users()
    except Exception:  # noqa
        eu = {}
    for s in stats:
        e = eu.get(s["requester"].lower())
        s["emby_exists"] = bool(e) if eu else None
        s["emby_disabled"] = e["disabled"] if e else None
    return stats


@app.get("/api/selftest")
async def selftest(request: Request, x_token: str = Header("")):
    admin(request, x_token)
    try:
        files = await pipe.drive.list_files(cfg.P115_STAGING_CID)
        return {"115": "ok", "staging_files": len(files)}
    except Exception as e:  # noqa
        return {"115": f"error: {e!r}"}


@app.get("/api/settings")
def get_settings(request: Request, x_token: str = Header("")):
    admin(request, x_token)
    return settings.public()


@app.put("/api/settings")
async def put_settings(request: Request, x_token: str = Header("")):
    admin(request, x_token)
    settings.save(await request.json())
    return settings.public()


@app.post("/api/settings/test")
async def test_settings(request: Request, x_token: str = Header("")):
    admin(request, x_token)
    return await tmdb.test()


# ---------- 海报代理（浏览器不用直连 TMDB，走上面配置的代理） ----------
@app.get("/img/{size}/{name}")
async def img(size: str, name: str):
    if size not in ("w185", "w342") or not re.fullmatch(r"[\w-]+\.(jpg|png)", name):
        raise HTTPException(400)
    k = f"{size}/{name}"
    if k not in _imgs:
        s = settings.get()
        async with tmdb.client() as c:
            r = await c.get(f"{s['image_host'].rstrip('/')}/t/p/{size}/{name}")
        if r.status_code != 200:
            raise HTTPException(404)
        if len(_imgs) > 300:
            _imgs.clear()
        _imgs[k] = (r.content, r.headers.get("content-type", "image/jpeg"))
    body, ctype = _imgs[k]
    return Response(body, media_type=ctype, headers={"Cache-Control": "public, max-age=604800"})


# ---------- Seerr Webhook（可选，不用 Seerr 可忽略） ----------
@app.post("/webhook/seerr")
async def seerr_webhook(req: Request, token: str = ""):
    if token != cfg.TOKEN:
        raise HTTPException(401, "bad token")
    p = await req.json()
    nt = p.get("notification_type")
    if nt == "MEDIA_PENDING":
        status = "queued" if cfg.APPROVAL == "auto" else "pending"
    elif nt in ("MEDIA_APPROVED", "MEDIA_AUTO_APPROVED"):
        status = "queued"
    else:
        return {"ignored": nt}
    media = p.get("media") or {}
    mt, tid = media.get("media_type"), int(media.get("tmdbId") or 0)
    if mt not in ("movie", "tv") or not tid:
        raise HTTPException(400, "缺少 media_type/tmdbId")
    name = (p.get("request") or {}).get("requestedBy_username", "")
    seasons = [None]
    if mt == "tv":
        val = next((e.get("value", "") for e in p.get("extra", []) if "season" in e.get("name", "").lower()), "")
        seasons = [int(x) for x in re.findall(r"\d+", val)] or [1]
    ok, why, uid = await emby.check(name)
    ids = []
    for s in seasons:
        if not ok:
            rid = db.create(mt, tid, s, p.get("subject", ""), name, "rejected", uid)
            if rid:
                db.update(rid, error=why)
        else:
            rid = db.promote(mt, tid, s) if status == "queued" else None
            rid = rid or db.create(mt, tid, s, p.get("subject", ""), name, status, uid)
            if rid and status == "queued":
                spawn(rid)
        if rid:
            ids.append(rid)
    return {"created": ids, "status": status if ok else "rejected", "reason": why}


@app.get("/")
def index():
    return FileResponse(Path(__file__).parent / "static" / "index.html")
