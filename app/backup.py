"""Downloadable backups for the app's persistent data directory."""
from __future__ import annotations

import io
import json
import os
import sqlite3
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from . import db, settings
from .config import cfg

FORMAT = "emby-request-bridge-backup"
VERSION = 1
MAX_ARCHIVE_BYTES = 256 * 1024 * 1024
MAX_TOTAL_UNCOMPRESSED = 256 * 1024 * 1024
REQUIRED_FILES = {"bridge.db", "settings.json"}
OPTIONAL_FILES = {"rules.json"}


class BackupError(ValueError):
    """A backup archive is invalid or cannot be safely restored."""


def _data_dir() -> Path:
    return Path(os.path.dirname(cfg.DB_PATH) or ".").resolve()


def _db_snapshot() -> bytes:
    # SQLite's backup API includes committed WAL data and creates a consistent snapshot.
    with db._lock:
        source = db.conn()
        target = sqlite3.connect(":memory:")
        try:
            source.backup(target)
            return target.serialize()
        finally:
            target.close()


def create_backup() -> bytes:
    """Return a ZIP containing the database, settings, and optional custom rules."""
    settings.get()  # Ensure settings.json and the initial API token exist.
    base = _data_dir()
    settings_path = Path(settings._path())
    db_bytes = _db_snapshot()
    try:
        settings_bytes = settings_path.read_bytes()
    except OSError as exc:
        raise BackupError(f"无法读取设置文件：{exc}") from exc

    files = {"bridge.db": db_bytes, "settings.json": settings_bytes}
    custom_rules = base / "rules.json"
    if custom_rules.is_file():
        files["rules.json"] = custom_rules.read_bytes()
    if sum(len(content) for content in files.values()) > MAX_TOTAL_UNCOMPRESSED:
        raise BackupError("数据总量超过 256 MB 备份限制")
    manifest = {
        "format": FORMAT,
        "version": VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "files": sorted(files),
    }
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        for name, content in files.items():
            archive.writestr(name, content)
    result = out.getvalue()
    if len(result) > MAX_ARCHIVE_BYTES:
        raise BackupError("生成的备份超过 256 MB 限制")
    return result


def validate_backup(raw: bytes) -> tuple[dict, dict[str, bytes]]:
    """Validate archive structure, JSON files, and SQLite integrity before touching live data."""
    if not raw or len(raw) > MAX_ARCHIVE_BYTES:
        raise BackupError("备份文件为空或超过 256 MB 限制")
    try:
        archive = zipfile.ZipFile(io.BytesIO(raw))
    except (zipfile.BadZipFile, OSError) as exc:
        raise BackupError("文件不是有效的 ZIP 备份") from exc

    with archive:
        infos = archive.infolist()
        names = [i.filename for i in infos]
        if len(names) != len(set(names)):
            raise BackupError("备份中包含重复文件名")
        if any(i.is_dir() for i in infos):
            raise BackupError("备份格式无效：不应包含目录条目")
        if "manifest.json" not in names:
            raise BackupError("备份缺少 manifest.json")
        allowed = REQUIRED_FILES | OPTIONAL_FILES | {"manifest.json"}
        if set(names) - allowed:
            raise BackupError("备份包含不支持的文件")
        if not REQUIRED_FILES.issubset(names):
            raise BackupError("备份缺少数据库或设置文件")
        if sum(i.file_size for i in infos) > MAX_TOTAL_UNCOMPRESSED:
            raise BackupError("备份解压后超过 256 MB 限制")
        if any(i.file_size > MAX_TOTAL_UNCOMPRESSED for i in infos):
            raise BackupError("备份中的单个文件超过 256 MB 限制")
        try:
            contents = {name: archive.read(name) for name in names}
        except (zipfile.BadZipFile, RuntimeError, OSError) as exc:
            raise BackupError("备份内容损坏或无法解压") from exc

    try:
        manifest = json.loads(contents["manifest.json"])
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BackupError("备份清单不是有效 JSON") from exc
    if (not isinstance(manifest, dict) or manifest.get("format") != FORMAT
            or manifest.get("version") != VERSION):
        raise BackupError("不支持此备份格式或版本")
    try:
        saved_settings = json.loads(contents["settings.json"])
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BackupError("设置文件不是有效 JSON") from exc
    if not isinstance(saved_settings, dict):
        raise BackupError("设置文件格式无效")

    if "rules.json" in contents:
        try:
            custom_rules = json.loads(contents["rules.json"])
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BackupError("自定义规则文件不是有效 JSON") from exc
        if not isinstance(custom_rules, dict) or not isinstance(custom_rules.get("regulars", []), list):
            raise BackupError("自定义规则文件格式无效")

    try:
        check = sqlite3.connect(":memory:")
        try:
            check.deserialize(contents["bridge.db"])
            result = check.execute("PRAGMA integrity_check").fetchone()
            tables = {r[0] for r in check.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not result or result[0] != "ok" or "requests" not in tables:
                raise BackupError("数据库完整性检查失败或缺少请求记录表")
        finally:
            check.close()
    except (sqlite3.DatabaseError, ValueError) as exc:
        if isinstance(exc, BackupError):
            raise
        raise BackupError("数据库文件无效或已损坏") from exc
    return manifest, contents


def _stage(target: Path, content: bytes) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".restore", dir=target.parent)
    path = Path(name)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return path


def restore_backup(raw: bytes) -> dict:
    """Restore a validated archive, replacing all current persistent app data."""
    manifest, contents = validate_backup(raw)
    base = _data_dir()
    db_path = Path(cfg.DB_PATH).resolve()
    settings_path = Path(settings._path()).resolve()
    rules_path = base / "rules.json"
    staged: list[Path] = []
    try:
        staged_db = _stage(db_path, contents["bridge.db"])
        staged.append(staged_db)
        replacements = [(staged_db, db_path)]
        staged_settings = _stage(settings_path, contents["settings.json"])
        staged.append(staged_settings)
        replacements.append((staged_settings, settings_path))
        if "rules.json" in contents:
            staged_rules = _stage(rules_path, contents["rules.json"])
            staged.append(staged_rules)
            replacements.append((staged_rules, rules_path))

        # db uses the same lock for every operation; close its connection before the atomic swap.
        with db._lock:
            if db._conn is not None:
                db._conn.close()
                db._conn = None
            for source, target in replacements:
                os.replace(source, target)
            # Match the archive exactly: a backup without custom rules removes any current override.
            if "rules.json" not in contents:
                rules_path.unlink(missing_ok=True)
        settings.reload()
    except Exception as exc:
        raise BackupError(f"恢复失败：{exc}") from exc
    finally:
        for path in staged:
            path.unlink(missing_ok=True)
    return {"ok": True, "created_at": manifest.get("created_at"), "files": manifest.get("files", [])}
