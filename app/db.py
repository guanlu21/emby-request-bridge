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
        _conn.commit()
    return _conn


def create(media_type, tmdb_id, season, title):
    with _lock:
        c = conn()
        dup = c.execute("SELECT id FROM requests WHERE media_type=? AND tmdb_id=? AND IFNULL(season,0)=IFNULL(?,0)"
                        " AND status!='failed'", (media_type, tmdb_id, season)).fetchone()
        if dup:
            return None
        cur = c.execute("INSERT INTO requests(media_type,tmdb_id,season,title,status,created,updated)"
                        " VALUES(?,?,?,?,?,?,?)",
                        (media_type, tmdb_id, season, title, "queued", time.time(), time.time()))
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
    lg.append(f"{time.strftime('%H:%M:%S')} {msg}")
    update(rid, log=json.dumps(lg[-60:], ensure_ascii=False))


def list_all():
    with _lock:
        return [dict(r) for r in conn().execute("SELECT * FROM requests ORDER BY id DESC LIMIT 200")]


def unfinished():
    with _lock:
        return [r["id"] for r in conn().execute(
            "SELECT id FROM requests WHERE status IN ('queued','searching','downloading')")]
