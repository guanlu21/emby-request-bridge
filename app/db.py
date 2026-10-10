import json
import os
import sqlite3
import threading
import time

from .config import cfg

_lock = threading.Lock()
_conn = None


def conn():
    global _conn
    if _conn is None:
        d = os.path.dirname(cfg.DB_PATH)
        try:
            if d:
                os.makedirs(d, exist_ok=True)
            _conn = sqlite3.connect(cfg.DB_PATH, check_same_thread=False)
        except (OSError, sqlite3.OperationalError) as e:
            raise RuntimeError(f"无法打开数据库 {cfg.DB_PATH}：{e}。请给 {d or '.'} 挂载一个可写目录，"
                               f"或设置环境变量 DB_PATH=/tmp/bridge.db（重启会丢数据）") from e
        _conn.row_factory = sqlite3.Row
        _conn.execute("""CREATE TABLE IF NOT EXISTS requests(
            id INTEGER PRIMARY KEY AUTOINCREMENT, media_type TEXT, tmdb_id INTEGER, season INTEGER,
            title TEXT, status TEXT, picked TEXT DEFAULT '', error TEXT DEFAULT '',
            tried TEXT DEFAULT '[]', log TEXT DEFAULT '[]', created REAL, updated REAL)""")
        cols = [r[1] for r in _conn.execute("PRAGMA table_info(requests)")]
        for col, ddl in (("cands", "TEXT DEFAULT '[]'"), ("placed", "TEXT DEFAULT ''"), ("category", "TEXT DEFAULT ''"),
                         ("grp", "INTEGER DEFAULT 0"), ("rejects", "TEXT DEFAULT '[]'"), ("cands_at", "REAL DEFAULT 0"),
                         ("approved_at", "REAL DEFAULT 0"), ("approved_by", "TEXT DEFAULT ''"), ("done_at", "REAL DEFAULT 0")):
            if col not in cols:
                _conn.execute(f"ALTER TABLE requests ADD COLUMN {col} {ddl}")
        if "emby_user_id" not in cols:
            _conn.execute("ALTER TABLE requests ADD COLUMN emby_user_id TEXT DEFAULT ''")
        if "requester" not in cols:
            _conn.execute("ALTER TABLE requests ADD COLUMN requester TEXT DEFAULT ''")
        _conn.commit()
    return _conn


def create(media_type, tmdb_id, season, title, requester="", status="queued", emby_user_id=""):
    with _lock:
        c = conn()
        dup = c.execute("SELECT id FROM requests WHERE media_type=? AND tmdb_id=? AND IFNULL(season,0)=IFNULL(?,0)"
                        " AND status NOT IN ('failed','rejected')", (media_type, tmdb_id, season)).fetchone()
        if dup:
            return None
        now = time.time()
        auto = status == "queued"  # 不需要人工审批，创建的同时就算批准了
        cur = c.execute("INSERT INTO requests(media_type,tmdb_id,season,title,status,requester,emby_user_id,created,updated,approved_at,approved_by)"
                        " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (media_type, tmdb_id, season, title, status, requester, emby_user_id, now, now, now if auto else 0, "自动批准" if auto else ""))
        c.commit()
        return cur.lastrowid


def get(rid):
    with _lock:
        r = conn().execute("SELECT * FROM requests WHERE id=?", (rid,)).fetchone()
        return dict(r) if r else None


def update(rid, **kw):
    kw["updated"] = time.time()
    with _lock:
        conn().execute(f"UPDATE requests SET {','.join(k + '=?' for k in kw)} WHERE id=?", (*kw.values(), rid))
        conn().commit()


def log(rid, msg):
    r = get(rid)
    lg = json.loads(r["log"])
    try:
        from datetime import datetime
        from zoneinfo import ZoneInfo
        import os
        ts = datetime.now(ZoneInfo(os.environ.get("TZ") or "Asia/Shanghai")).strftime("%H:%M:%S")  # 容器默认 UTC，按本地时区显示
    except Exception:
        ts = time.strftime("%H:%M:%S")
    lg.append(f"{ts} {msg}")
    update(rid, log=json.dumps(lg[-60:], ensure_ascii=False))


def list_all(user=""):
    with _lock:
        if user:
            q = conn().execute("SELECT * FROM requests WHERE requester=? COLLATE NOCASE ORDER BY id DESC LIMIT 200", (user,))
        else:
            q = conn().execute("SELECT * FROM requests ORDER BY id DESC LIMIT 200")
        return [dict(r) for r in q]


def count_recent(requester, days=7):
    """近 N 天该用户的求片数（被拒绝的不算）。"""
    with _lock:
        return conn().execute("SELECT COUNT(*) FROM requests WHERE requester=? COLLATE NOCASE AND created>? AND status!='rejected'",
                              (requester, time.time() - days * 86400)).fetchone()[0]


def user_stats():
    with _lock:
        rows = conn().execute("""SELECT requester, MAX(emby_user_id) emby_user_id, COUNT(*) total,
            SUM(status='done') done, SUM(status='failed') failed, SUM(status='pending') pending,
            SUM(status='rejected') rejected, MAX(created) last FROM requests
            WHERE requester!='' GROUP BY requester COLLATE NOCASE ORDER BY last DESC""")
        return [dict(r) for r in rows]


def unfinished():
    with _lock:
        return [r["id"] for r in conn().execute(
            "SELECT id FROM requests WHERE status IN ('queued','searching','downloading')")]


def promote(media_type, tmdb_id, season):
    """把同一部片的"待审批"记录转成排队；没有则返回 None。"""
    with _lock:
        c = conn()
        r = c.execute("SELECT id FROM requests WHERE media_type=? AND tmdb_id=? AND IFNULL(season,0)=IFNULL(?,0)"
                      " AND status='pending'", (media_type, tmdb_id, season)).fetchone()
        if not r:
            return None
        c.execute("UPDATE requests SET status='queued', updated=?, approved_at=?, approved_by=? WHERE id=?",
                  (time.time(), time.time(), "Seerr", r["id"]))
        c.commit()
        return r["id"]


def batch(ids, action, by=""):
    """approve/reject 只作用于待审批；retry 只作用于失败/已拒绝；delete 作用于非进行中的记录。返回受影响的 id。"""
    rules = {"approve": ("pending", "queued"), "reject": ("pending", "rejected"),
             "retry": ("failed,rejected", "queued"), "reset": ("failed,rejected", "queued")}
    done = []
    with _lock:
        c = conn()
        for i in ids:
            r = c.execute("SELECT status FROM requests WHERE id=?", (i,)).fetchone()
            if not r:
                continue
            if action == "delete":
                if r["status"] in ("queued", "searching", "downloading"):
                    continue
                c.execute("DELETE FROM requests WHERE id=?", (i,))
            else:
                src, dst = rules[action]
                if r["status"] not in src.split(","):
                    continue
                c.execute("UPDATE requests SET status=?, updated=? WHERE id=?", (dst, time.time(), i))
                if action == "approve" or (r["status"] == "rejected" and dst == "queued"):  # 批准（含把已拒绝的重新放行）
                    c.execute("UPDATE requests SET approved_at=?, approved_by=? WHERE id=?", (time.time(), by, i))
                if action == "reset":
                    c.execute("UPDATE requests SET tried='[]', error='' WHERE id=?", (i,))
            done.append(i)
        c.commit()
    return done


def status_of(media_type, tmdb_id):
    """这部片最近一条请求的状态；没人求过返回空串。"""
    with _lock:
        r = conn().execute("SELECT status FROM requests WHERE media_type=? AND tmdb_id=? ORDER BY id DESC LIMIT 1",
                           (media_type, tmdb_id)).fetchone()
        return r["status"] if r else ""


def group_rows(grp: int):
    """同一批求片的各季（电视剧一次求多季时共用 grp = 第一条的 id）。"""
    with _lock:
        return [dict(r) for r in conn().execute("SELECT * FROM requests WHERE grp=? ORDER BY season", (grp,))]


def leaders(ids):
    """一批 id 里每个分组只留一个（同组的各季由同一次流程处理）。"""
    seen, out = set(), []
    for i in ids:
        r = get(i)
        if not r:
            continue
        key = r["grp"] or i
        if key not in seen:
            seen.add(key)
            out.append(i)
    return out
