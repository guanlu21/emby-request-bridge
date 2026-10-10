import asyncio
import hmac
import json
import re
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import auth as auth_mod, cloudsaver, db, drive115, dyg, emby, kite, litepan, quark, rules, settings, tmdb
from .config import cfg
from .pipeline import Pipeline, notify_after
from .sources import search_all, tmdb_meta

_tasks: set = set()
_fails: dict = {}  # 登录失败限流：(ip,用户名) -> [时间戳]
_imgs: dict = {}
pipe: Pipeline | None = None


async def _meta(r):
    m = await tmdb_meta(r["media_type"], r["tmdb_id"], r.get("seasons") or r["season"])
    if not m:
        title = re.sub(r"\s*\(\d{4}\)\s*$", "", r["title"])
        y = re.search(r"\((\d{4})\)\s*$", r["title"])
        m = {"names": [title], "year": y.group(1) if y else "", "episodes": 0}
    return m


def spawn(rid, append: bool = False, depth: int = 0):
    if append:
        t = asyncio.create_task(pipe.run_append(rid, depth))
    else:
        t = asyncio.create_task(pipe.run(rid, depth))
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)


@asynccontextmanager
async def lifespan(app):
    global pipe
    pipe = Pipeline(drive115.CompositeDrive(), _meta, search_all, notify_after, quark=quark.QuarkDrive())
    for rid in db.leaders(db.unfinished()):  # 重启后继续未完成的请求（电视剧一次求多季的算一组）
        spawn(rid)
    yield


app = FastAPI(lifespan=lifespan, title="Emby求片")
app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")


def who(request: Request, x_token: str = "") -> dict:
    """登录的 Emby 用户（会话 Cookie），或管理令牌（供脚本 / emby-manager 调用）。"""
    if x_token and hmac.compare_digest(x_token, cfg.TOKEN):
        return {"uid": "", "name": "token", "admin": True}
    u = auth_mod.verify(request.cookies.get("rb_session", ""))
    if not u:
        raise HTTPException(401, "请先登录")
    return {**u, "admin": u["name"].lower() in settings.admins()}


def admin(request: Request, x_token: str = "") -> dict:
    u = who(request, x_token)
    if not u["admin"]:
        raise HTTPException(403, "需要管理员权限")
    return u


# ---------- 登录 ----------
@app.get("/api/status")
def status():
    """登录页用：Emby 地址是否已配置、是否已有管理员。"""
    return {"configured": bool(cfg.EMBY_URL), "initialized": bool(settings.admins())}


@app.post("/api/login")
async def login(request: Request, response: Response):
    b = await request.json()
    name, pw = (b.get("username") or "").strip(), b.get("password") or ""
    key = (request.client.host if request.client else "", name.lower())
    recent = [t for t in _fails.get(key, []) if t > time.time() - 600]
    if len(recent) >= 5:
        raise HTTPException(429, "尝试次数太多，请 10 分钟后再试")
    try:
        if not cfg.EMBY_URL:  # 首次设置：登录的同时填 Emby 地址
            url = auth_mod.normalize_url(b.get("emby_url", ""))
            if not url:
                raise auth_mod.AuthError("请填写 Emby 地址")
            if not auth_mod.is_private_host(url):
                raise auth_mod.AuthError("首次设置请填局域网地址（如 http://192.168.1.10:8096），之后可在设置里改成域名")
            u = await auth_mod.emby_login(name, pw, base=url, setup=True)
            if not u["emby_admin"]:
                raise auth_mod.AuthError("首次设置请用 Emby 管理员账号登录")
            settings.save({"emby_url": url, "emby_key": u["api_key"]})
            settings.set_internal(admins=u["name"])
        else:
            u = await auth_mod.emby_login(name, pw)
            if not settings.admins():  # 已有 Emby 地址但还没有管理员（比如用环境变量配置的）
                if not u["emby_admin"]:
                    raise auth_mod.AuthError("系统还没初始化，请先让 Emby 管理员登录一次")
                settings.set_internal(admins=u["name"])
    except auth_mod.AuthError as e:
        _fails[key] = recent + [time.time()]
        raise HTTPException(401, str(e))
    _fails.pop(key, None)
    response.set_cookie("rb_session", auth_mod.make({"uid": u["uid"], "name": u["name"]}), max_age=7 * 86400,
                        httponly=True, samesite="lax")
    return {"name": u["name"], "admin": u["name"].lower() in settings.admins()}


@app.post("/api/logout")
def logout(response: Response):
    response.delete_cookie("rb_session")
    return {"ok": True}


@app.get("/api/me")
def me(request: Request, x_token: str = Header("")):
    u = who(request, x_token)
    return {"name": u["name"], "admin": u["admin"]}


# ---------- 搜索与求片 ----------
async def annotate(items: list[dict], limit: int = 80):
    """给影片加上「已入库 / 已有人求片」状态（人物跳过）。Emby 查询并发 8 个，只查前 limit 个。"""
    sem = asyncio.Semaphore(8)

    async def one(it):
        if it["type"] == "person":
            return
        async with sem:
            it["in_library"] = await emby.has_item(it["type"], it["id"])
        it["requested"] = db.status_of(it["type"], it["id"])

    await asyncio.gather(*[one(it) for it in items[:limit]])
    for it in items[limit:]:
        if it["type"] != "person":
            it["in_library"], it["requested"] = False, db.status_of(it["type"], it["id"])


@app.get("/api/search")
async def search(request: Request, q: str = "", x_token: str = Header("")):
    who(request, x_token)
    if not q.strip():
        return []
    try:
        items = await tmdb.search(q.strip())
    except Exception as e:  # noqa
        raise HTTPException(502, f"TMDB 搜索失败：{e}")
    await annotate(items)
    return items


@app.get("/api/person/{pid}")
async def person(pid: int, request: Request, x_token: str = Header("")):
    """人物页：他参演/导演的全部影视作品（带入库、求片状态），点作品就能求片。"""
    who(request, x_token)
    try:
        d = await tmdb.person_credits(pid)
    except Exception as e:  # noqa
        raise HTTPException(502, f"TMDB 查询失败：{e}")
    await annotate(d["works"], 100)
    return d


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
        else:
            dup += 1
    if mt == "tv" and len(ids) > 1:  # 一次求多季：同一组，一起搜索、优先找覆盖多季的合集
        for i in ids:
            db.update(i, grp=ids[0])
    if status == "queued":
        for rid in db.leaders(ids):
            spawn(rid)
    return {"created": ids, "duplicates": dup, "status": status}


@app.get("/api/requests")
def list_requests(request: Request, user: str = "", x_token: str = Header("")):
    u = who(request, x_token)
    return db.list_all(user if u["admin"] else u["name"])


# ---------- 管理 ----------
@app.post("/api/requests/batch")
async def batch(request: Request, x_token: str = Header("")):
    u = admin(request, x_token)
    body = await request.json()
    action = body.get("action")
    if action not in ("approve", "reject", "retry", "reset", "delete"):
        raise HTTPException(400, "bad action")
    purged = 0
    if action == "delete" and body.get("purge"):
        # 同时删除网盘里这些请求入库的文件：先删网盘，成功了才删记录；网盘出错就整体停下，记录原样保留，可以重试
        for rid in body.get("ids", []):
            r = db.get(rid)
            if not r or r["status"] in ("queued", "searching", "downloading"):
                continue
            try:
                purged += await pipe.purge(r)
            except Exception as e:  # noqa
                raise HTTPException(502, f"删除网盘文件失败（记录没有删除）：{e}")
    done = db.batch(body.get("ids", []), action, by=u["name"])
    if action in ("approve", "retry", "reset"):
        for rid in db.leaders(done):
            spawn(rid)
    return {"done": done, "purged": purged}


@app.post("/api/requests/{rid}/retry")
def retry(rid: int, request: Request, reset: bool = False, x_token: str = Header("")):
    admin(request, x_token)
    if not db.get(rid):
        raise HTTPException(404)
    if reset:
        db.update(rid, tried="[]")
    spawn(rid)
    return {"ok": True}


@app.post("/api/requests/{rid}/append")
def append(rid: int, request: Request, depth: int = 0, x_token: str = Header("")):
    """追加集数：连载剧已入库后，重新搜索并只把库中没有的新集补进同一个 Season 目录。"""
    admin(request, x_token)
    if not db.get(rid):
        raise HTTPException(404)
    spawn(rid, append=True, depth=depth)
    return {"ok": True}


@app.get("/api/requests/{rid}/candidates")
def candidates(rid: int, request: Request, x_token: str = Header("")):
    """这条请求筛选出的候选资源（含满足的要求、已试过/当前使用的状态）和被过滤掉的资源（可强制使用）。"""
    admin(request, x_token)
    r = db.get(rid)
    if not r:
        raise HTTPException(404)
    tried = set(json.loads(r["tried"] or "[]"))
    out = []
    for c in json.loads(r["cands"] or "[]"):
        c["state"] = "当前使用" if (r["status"] == "done" and c["title"] == r["picked"]) else ("已试过" if c["url"] in tried else "")
        c.pop("password", None)
        out.append(c)
    rej = []
    for c in json.loads(r["rejects"] or "[]"):
        c.pop("password", None)
        rej.append(c)
    return {"title": r["title"], "season": r["season"], "status": r["status"], "picked": r["picked"],
            "candidates": out, "rejects": rej, "ver": r["cands_at"], "busy": rid in pipe._active}


@app.post("/api/requests/{rid}/research")
async def research(rid: int, request: Request, x_token: str = Header("")):
    """用更深的搜索（更多关键词、更多条数、不用缓存）重新找一遍，只刷新候选列表，不下载。"""
    admin(request, x_token)
    r = db.get(rid)
    if not r:
        raise HTTPException(404)
    if rid in pipe._active or r["status"] in ("searching", "downloading"):
        raise HTTPException(400, "这条请求正在处理中")
    depth = int((await request.json()).get("depth") or 3)
    t = asyncio.create_task(pipe.search_only(rid, max(1, min(3, depth))))
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)
    return {"ok": True, "ver": r["cands_at"]}


@app.post("/api/requests/{rid}/replace")
async def replace(rid: int, request: Request, x_token: str = Header("")):
    """改用另一个候选资源。新资源下载并通过筛选后，才会删除之前入库的文件。"""
    admin(request, x_token)
    r = db.get(rid)
    if not r:
        raise HTTPException(404)
    if r["status"] not in ("done", "failed") or rid in pipe._active:
        raise HTTPException(400, "这条请求还在处理中，等它结束后再替换")
    url = ((await request.json()).get("url") or "").strip()
    if not url:
        raise HTTPException(400, "缺少资源")
    t = asyncio.create_task(pipe.run_candidate(rid, url, replace=r["status"] == "done"))
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)
    return {"ok": True}


@app.post("/api/requests/{rid}/manual")
async def manual(rid: int, request: Request, x_token: str = Header("")):
    """给搜不到的片手动指定 115/夸克 分享链接或磁力链接；已完成的请求按替换处理，append=True 按追加集数处理。"""
    admin(request, x_token)
    if not db.get(rid):
        raise HTTPException(404)
    body = await request.json()
    url = (body.get("url") or "").strip()
    if not url:
        raise HTTPException(400, "请粘贴链接")
    t = asyncio.create_task(pipe.run_manual(rid, url, append=bool(body.get("append"))))
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)
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


@app.post("/api/settings/kite/test")
async def test_kite(request: Request, x_token: str = Header("")):
    admin(request, x_token)
    return await kite.test()


@app.post("/api/settings/cloudsaver/test")
async def test_cloudsaver(request: Request, x_token: str = Header("")):
    admin(request, x_token)
    return await cloudsaver.test()


@app.post("/api/settings/litepan/test")
async def test_litepan(request: Request, x_token: str = Header("")):
    """发一条名为 bridge_test 的通知：验证地址和秘钥。联动里没有这个名称，所以不会真的执行任何任务。"""
    admin(request, x_token)
    return await litepan.send_detail("bridge_test", "RequestBridge 连接测试")


@app.post("/api/settings/litepan/trigger")
async def trigger_litepan(request: Request, x_token: str = Header("")):
    """发一条真实的联动通知（事件名用设置里的值，来源按所选分类生成），用来验证 LitePan 里的联动会不会被触发。"""
    admin(request, x_token)
    cat = ((await request.json()).get("category") or "").strip()
    return await litepan.send_detail(cfg.LITEPAN_EVENT, "RequestBridge 手动触发", litepan.source_for(cat))


@app.get("/api/settings/litepan/sources")
def litepan_sources(request: Request, x_token: str = Header("")):
    admin(request, x_token)
    return {"event": cfg.LITEPAN_EVENT, "sources": litepan.all_sources(), "table": litepan.source_table()}


@app.get("/api/settings/token")
def get_token(request: Request, x_token: str = Header("")):
    """API 令牌：给脚本或 emby-manager 调用管理接口用（X-Token 请求头）。"""
    admin(request, x_token)
    return {"token": cfg.TOKEN}


@app.post("/api/settings/token/reset")
def reset_token(request: Request, x_token: str = Header("")):
    admin(request, x_token)
    settings.set_internal(token=secrets.token_urlsafe(24))
    return {"token": cfg.TOKEN}  # 旧的登录会话会一并失效


# ---------- 115 开放平台授权 / 目录选择 ----------
def _qr_svg(text: str) -> str:
    try:
        import io
        import qrcode
        import qrcode.image.svg
        buf = io.BytesIO()
        qrcode.make(text, image_factory=qrcode.image.svg.SvgPathImage, box_size=8).save(buf)
        return buf.getvalue().decode()
    except Exception:  # noqa
        return ""


@app.get("/api/p115/status")
def p115_status(request: Request, x_token: str = Header("")):
    admin(request, x_token)
    return drive115.auth_status()


@app.post("/api/p115/test")
async def p115_test(request: Request, x_token: str = Header("")):
    admin(request, x_token)
    try:
        mode = pipe.drive.mode()
        dirs = await pipe.drive.list_dirs(0)
        return {"ok": True, "mode": mode, "root_dirs": len(dirs)}
    except Exception as e:  # noqa
        return {"ok": False, "error": str(e)[:200]}


@app.get("/api/quark/folders")
async def quark_folders(request: Request, fid: str = "0", x_token: str = Header("")):
    admin(request, x_token)
    try:
        return [{"id": str(d["id"]), "name": d["name"]} for d in await pipe.quark.list_dirs(fid)]
    except Exception as e:  # noqa
        raise HTTPException(400, str(e))


@app.post("/api/quark/test")
async def quark_test(request: Request, x_token: str = Header("")):
    admin(request, x_token)
    return await quark.test()


@app.post("/api/settings/rules/test")
async def test_rules(request: Request, x_token: str = Header("")):
    admin(request, x_token)
    return await rules.test()


@app.post("/api/settings/dyg/test")
async def test_dyg(request: Request, x_token: str = Header("")):
    admin(request, x_token)
    return await dyg.test()


@app.post("/api/p115/auth/start")
async def p115_start(request: Request, x_token: str = Header("")):
    admin(request, x_token)
    try:
        r = await drive115.auth_start(cfg.P115_APP_ID)
    except Exception as e:  # noqa
        raise HTTPException(400, str(e))
    return {"qrcode": r["qrcode"], "svg": _qr_svg(r["qrcode"])}


@app.get("/api/p115/auth/poll")
async def p115_poll(request: Request, x_token: str = Header("")):
    admin(request, x_token)
    try:
        return await drive115.auth_poll()
    except Exception as e:  # noqa
        raise HTTPException(400, str(e))


@app.post("/api/p115/logout")
def p115_logout(request: Request, x_token: str = Header("")):
    admin(request, x_token)
    drive115.auth_clear()
    return {"ok": True}


@app.get("/api/p115/folders")
async def p115_folders(request: Request, cid: int = 0, x_token: str = Header("")):
    admin(request, x_token)
    try:
        return [{"id": str(d["id"]), "name": d["name"]} for d in await pipe.drive.list_dirs(cid)]  # 字符串，避免浏览器丢精度
    except Exception as e:  # noqa
        raise HTTPException(400, str(e))


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
        if rid:
            ids.append(rid)
    if ok and mt == "tv" and len(ids) > 1:
        for i in ids:
            db.update(i, grp=ids[0])
    if ok and status == "queued":
        for rid in db.leaders(ids):
            spawn(rid)
    return {"created": ids, "status": status if ok else "rejected", "reason": why}


@app.get("/manifest.webmanifest")
def manifest():
    """让手机浏览器可以「添加到主屏幕」，像个独立的 App。"""
    return JSONResponse({"name": "Emby求片", "short_name": "Emby求片", "start_url": "/", "display": "standalone",
                         "background_color": "#10141c", "theme_color": "#182030",
                         "icons": [{"src": "/static/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
                                   {"src": "/static/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"}]},
                        media_type="application/manifest+json")


@app.get("/favicon.ico")
def favicon():
    return FileResponse(Path(__file__).parent / "static" / "icon-64.png", media_type="image/png")


@app.get("/")
def index():
    return FileResponse(Path(__file__).parent / "static" / "index.html")
