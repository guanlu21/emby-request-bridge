import asyncio
import json
import re
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse

from . import db, emby
from .config import cfg
from .drive115 import LazyDrive
from .pipeline import Pipeline, notify_after
from .sources import tmdb_meta, search_all

_tasks: set = set()
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


def auth(token):
    if token != cfg.TOKEN:
        raise HTTPException(401, "bad token")


@app.post("/webhook/seerr")
async def seerr_webhook(req: Request, token: str = ""):
    auth(token)
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
        raise HTTPException(400, "缺少 media_type/tmdbId，请检查 Seerr Webhook 的 JSON payload")
    who = (p.get("request") or {}).get("requestedBy_username", "")
    seasons = [None]
    if mt == "tv":
        val = next((e.get("value", "") for e in p.get("extra", []) if "season" in e.get("name", "").lower()), "")
        seasons = [int(x) for x in re.findall(r"\d+", val)] or [1]
    ok, why, uid = await emby.check(who)
    if ok and cfg.QUOTA_WEEKLY and who and db.count_recent(who) >= cfg.QUOTA_WEEKLY:
        ok, why = False, f"本周求片已达上限（{cfg.QUOTA_WEEKLY} 部）"
    ids = []
    for s in seasons:
        if not ok:
            rid = db.create(mt, tid, s, p.get("subject", ""), who, "rejected", uid)
            if rid:
                db.update(rid, error=why)
        else:
            rid = db.promote(mt, tid, s) if status == "queued" else None
            rid = rid or db.create(mt, tid, s, p.get("subject", ""), who, status, uid)
            if rid and status == "queued":
                spawn(rid)
        if rid:
            ids.append(rid)
    return {"created": ids, "status": status if ok else "rejected", "reason": why}


@app.post("/api/requests/batch")
async def batch(req: Request, x_token: str = Header("")):
    auth(x_token)
    body = await req.json()
    action = body.get("action")
    if action not in ("approve", "reject", "retry", "delete"):
        raise HTTPException(400, "bad action")
    done = db.batch(body.get("ids", []), action)
    if action in ("approve", "retry"):
        for rid in done:
            spawn(rid)
    return {"done": done}


@app.get("/api/requests")
def list_requests(user: str = "", x_token: str = Header("")):
    auth(x_token)
    return db.list_all(user)


@app.get("/api/users")
async def users(x_token: str = Header("")):
    """按求片人汇总，并带上该 Emby 账号当前是否停用（供 emby-manager 或管理页调用）。"""
    auth(x_token)
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


@app.post("/api/requests/{rid}/retry")
def retry(rid: int, reset: bool = False, x_token: str = Header("")):
    auth(x_token)
    if not db.get(rid):
        raise HTTPException(404)
    if reset:
        db.update(rid, tried="[]")
    spawn(rid)
    return {"ok": True}


@app.get("/api/selftest")
async def selftest(x_token: str = Header("")):
    auth(x_token)
    try:
        files = await pipe.drive.list_files(cfg.P115_STAGING_CID)
        return {"115": "ok", "staging_files": len(files)}
    except Exception as e:  # noqa
        return {"115": f"error: {e!r}"}


@app.get("/")
def index():
    return FileResponse(Path(__file__).parent / "static" / "index.html")
