import asyncio
import json
import re
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse

from . import db
from .config import cfg
from .drive115 import P115Drive
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
    pipe = Pipeline(P115Drive(cfg.P115_COOKIE), _meta, search_all, notify_after)
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
    if p.get("notification_type") not in ("MEDIA_APPROVED", "MEDIA_AUTO_APPROVED"):
        return {"ignored": p.get("notification_type")}
    media = p.get("media") or {}
    mt, tid = media.get("media_type"), int(media.get("tmdbId") or 0)
    if mt not in ("movie", "tv") or not tid:
        raise HTTPException(400, "缺少 media_type/tmdbId，请检查 Seerr Webhook 的 JSON payload")
    seasons = [None]
    if mt == "tv":
        val = next((e.get("value", "") for e in p.get("extra", []) if "season" in e.get("name", "").lower()), "")
        seasons = [int(x) for x in re.findall(r"\d+", val)] or [1]
    ids = []
    for s in seasons:
        rid = db.create(mt, tid, s, p.get("subject", ""))
        if rid:
            ids.append(rid)
            spawn(rid)
    return {"created": ids}


@app.get("/api/requests")
def list_requests(x_token: str = Header("")):
    auth(x_token)
    return db.list_all()


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
