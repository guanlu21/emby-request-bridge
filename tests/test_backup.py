import json
import os
import tempfile
import unittest
from pathlib import Path

os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "bridge.db")

from app import backup, db, settings  # noqa: E402
from app.config import cfg  # noqa: E402


class BackupTests(unittest.TestCase):
    def setUp(self):
        # Each test starts with a clean database and a fresh settings cache.
        with db._lock:
            if db._conn is not None:
                db._conn.close()
                db._conn = None
            for suffix in ("", "-wal", "-shm"):
                Path(cfg.DB_PATH + suffix).unlink(missing_ok=True)
            Path(settings._path()).unlink(missing_ok=True)
            (Path(os.path.dirname(cfg.DB_PATH)) / "rules.json").unlink(missing_ok=True)
            settings._cache = None
        db.conn()

    def test_archive_restores_database_settings_and_custom_rules(self):
        rules_path = Path(os.path.dirname(cfg.DB_PATH)) / "rules.json"
        rid = db.create("movie", 1001, None, "Backup fixture")
        settings.set_internal(token="token-from-backup", admins="alice")
        expected_rules = {"regulars": [{"engineName": "fixture"}]}
        rules_path.write_text(json.dumps(expected_rules), encoding="utf-8")
        archive = backup.create_backup()

        db.create("movie", 1002, None, "Should be removed")
        settings.set_internal(token="newer-token", admins="bob")
        rules_path.write_text(json.dumps({"regulars": []}), encoding="utf-8")
        result = backup.restore_backup(archive)

        self.assertTrue(result["ok"])
        self.assertEqual([x["id"] for x in db.list_all()], [rid])
        self.assertEqual(settings.get()["token"], "token-from-backup")
        self.assertEqual(settings.get()["admins"], "alice")
        self.assertEqual(json.loads(rules_path.read_text(encoding="utf-8")), expected_rules)

    def test_backup_without_custom_rules_removes_current_override(self):
        rules_path = Path(os.path.dirname(cfg.DB_PATH)) / "rules.json"
        rules_path.unlink(missing_ok=True)
        archive = backup.create_backup()
        rules_path.write_text('{"regulars": []}', encoding="utf-8")
        backup.restore_backup(archive)
        self.assertFalse(rules_path.exists())

    def test_invalid_archive_is_rejected_without_touching_data(self):
        rid = db.create("movie", 1003, None, "Keep this")
        with self.assertRaises(backup.BackupError):
            backup.restore_backup(b"not a zip archive")
        self.assertEqual(db.get(rid)["title"], "Keep this")


if __name__ == "__main__":
    unittest.main()
