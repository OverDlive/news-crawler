import json
from pathlib import Path
import sqlite3
from contextlib import closing
import tempfile
import unittest
from unittest.mock import patch

from app_storage import migrate_data, resolve_data_directory


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'old' / 'data'
        self.source.mkdir(parents=True)
        self.settings = {'telegram': {'token': 'test-only', 'chat_id': '123'},
                         'keywords': [{'query': '한국전력 AND 보안'}]}
        (self.source / 'settings.json').write_text(json.dumps(self.settings), encoding='utf-8')
        self.target = self.root / 'user' / 'data'

    def test_backup_preserves_settings_auxiliary_files_and_committed_wal(self):
        (self.source / 'bot.json').write_text('{"test": true}')
        with closing(sqlite3.connect(self.source / 'news.sqlite3')) as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('CREATE TABLE news (title TEXT)')
            db.execute("INSERT INTO news VALUES ('보존 테스트')")
            db.commit()
            migrate_data(self.source, self.target)
        for folder in [self.source, self.target, next((self.target.parent / 'backups').iterdir())]:
            self.assertEqual(json.loads((folder / 'settings.json').read_text()), self.settings)
            self.assertTrue((folder / 'bot.json').exists())
            with closing(sqlite3.connect(folder / 'news.sqlite3')) as db:
                self.assertEqual(db.execute('SELECT title FROM news').fetchone()[0], '보존 테스트')

    def test_existing_data_is_never_overwritten(self):
        self.target.mkdir(parents=True)
        (self.target / 'settings.json').write_text('new data')
        migrate_data(self.source, self.target)
        self.assertEqual((self.target / 'settings.json').read_text(), 'new data')
        self.assertFalse((self.target.parent / 'backups').exists())

    def test_failed_copy_does_not_publish_partial_data(self):
        with patch('app_storage.shutil.copytree', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                migrate_data(self.source, self.target)
        self.assertFalse(self.target.exists())
        self.assertTrue((self.source / 'settings.json').exists())
        self.assertEqual(len(list((self.target.parent / 'backups').iterdir())), 1)
        migrate_data(self.source, self.target)
        self.assertTrue(self.target.exists())

    def test_bad_settings_abort_without_creating_defaults(self):
        (self.source / 'settings.json').write_text('broken')
        with self.assertRaises(ValueError):
            migrate_data(self.source, self.target)
        self.assertFalse(self.target.exists())

    def test_installer_marker_and_source_demo_explicit_isolation(self):
        app = self.root / 'installed'
        app.mkdir()
        (app / 'migration-source.txt').write_text(str(self.source), encoding='utf-8')
        with patch('app_storage.user_directory', return_value=self.target.parent):
            self.assertEqual(resolve_data_directory(app, frozen=True), self.target)
            self.assertEqual(resolve_data_directory(app, frozen=True), self.target)
            self.assertEqual(resolve_data_directory(app, demo=True, frozen=True), self.target.parent / 'demo_data')
            self.assertEqual(resolve_data_directory(app), app / 'data')
            self.assertEqual(resolve_data_directory(app, explicit=self.root), self.root)

    def test_missing_explicit_legacy_path_blocks_startup(self):
        app = self.root / 'installed'
        app.mkdir()
        (app / 'migration-source.txt').write_text(str(self.root / 'missing'), encoding='utf-8')
        with patch('app_storage.user_directory', return_value=self.target.parent):
            with self.assertRaises(ValueError):
                resolve_data_directory(app, frozen=True)


if __name__ == '__main__':
    unittest.main()
