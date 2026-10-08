import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import app_updates as updates


class UpdateTests(unittest.TestCase):
    def release(self, tag='v1.0.1', **extra):
        return {'tag_name': tag, 'assets': [{'name': updates.ASSET_NAME},
                 {'name': updates.ASSET_NAME + '.sha256'}], **extra}

    def stage(self, folder, release, corrupt=False):
        binary = b'MZverified executable'
        calls = []
        def download(url, path, limit):
            calls.append(url)
            Path(path).write_bytes(b'0' * 64 if corrupt and url.endswith('.sha256') else
                hashlib.sha256(binary).hexdigest().encode() if url.endswith('.sha256') else binary)
        with patch.object(updates.urllib.request, 'urlopen', return_value=io.BytesIO(json.dumps(release).encode())), \
             patch.object(updates, 'download', side_effect=download):
            return updates.stage_latest(Path(folder) / 'NewsMonitor.exe'), calls

    def test_new_release_is_verified_and_staged_without_replacing_exe(self):
        with tempfile.TemporaryDirectory() as folder:
            exe = Path(folder) / 'NewsMonitor.exe'
            exe.write_bytes(b'old exe')
            tag, calls = self.stage(folder, self.release())
            self.assertEqual(tag, 'v1.0.1')
            self.assertEqual(exe.read_bytes(), b'old exe')
            metadata = json.loads(exe.with_name(exe.name + '.pending.json').read_text())
            self.assertEqual(updates.digest(Path(folder) / metadata['file']), metadata['sha256'])
            self.assertTrue(all(url.startswith(f'https://github.com/{updates.REPOSITORY}/releases/download/') for url in calls))

    def test_checksum_failure_leaves_no_pending_update(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(ValueError, 'SHA-256'):
                self.stage(folder, self.release(), corrupt=True)
            self.assertEqual(list(Path(folder).iterdir()), [])

    def test_old_equal_and_prerelease_are_ignored(self):
        for release in [self.release('v0.9.0'), self.release('v1.0.0'), self.release(prerelease=True)]:
            with self.subTest(release=release), tempfile.TemporaryDirectory() as folder:
                tag, calls = self.stage(folder, release)
                self.assertIsNone(tag)
                self.assertEqual(calls, [])

    def test_versions_are_numeric_and_reject_invalid_tags(self):
        self.assertGreater(updates.version_tuple('v1.10.0'), updates.version_tuple('1.9.0'))
        for tag in ['latest', 'v1.0.1-beta', 'v1.2', 'v1.2.3/../../x']:
            with self.assertRaises(ValueError):
                updates.version_tuple(tag)

    def test_frozen_data_directory_is_beside_executable(self):
        with patch.object(updates.sys, 'frozen', True, create=True), \
             patch.object(updates.sys, 'executable', str(Path('portable/NewsMonitor.exe').resolve())):
            self.assertEqual(updates.app_directory(), Path('portable').resolve())

    def test_source_launch_never_applies_update(self):
        with patch.object(updates.sys, 'frozen', False, create=True):
            self.assertFalse(updates.apply_pending())

    @unittest.skipUnless(updates.os.name == 'nt', 'Windows helper')
    def test_pending_update_starts_hidden_helper_with_quoted_arguments(self):
        with tempfile.TemporaryDirectory() as folder:
            exe = Path(folder) / "News Monitor's.exe"
            exe.write_bytes(b'old exe')
            self.stage(folder, self.release())
            source_pending = Path(folder) / 'NewsMonitor.exe.pending.json'
            source_pending.rename(exe.with_name(exe.name + '.pending.json'))
            with patch.object(updates.sys, 'frozen', True, create=True), \
                 patch.object(updates.subprocess, 'Popen') as launch:
                self.assertTrue(updates.apply_pending(exe, ['--data-dir', r'C:\data with spaces']))
            self.assertEqual(launch.call_args.kwargs['creationflags'], updates.subprocess.CREATE_NO_WINDOW)
            script = next(Path(folder).glob('*.ps1')).read_text(encoding='utf-8-sig')
            self.assertIn("News Monitor''s.exe", script)
            self.assertIn('PYINSTALLER_RESET_ENVIRONMENT', script)
            self.assertIn('Get-FileHash', script)
            self.assertIn('Move-Item -LiteralPath $backup -Destination $target', script)

    @unittest.skipUnless(updates.os.name == 'nt', 'Windows helper')
    def test_tampered_pending_file_is_removed_and_not_launched(self):
        with tempfile.TemporaryDirectory() as folder:
            self.stage(folder, self.release())
            staged = next(Path(folder).glob('.news-update-*.exe'))
            staged.write_bytes(b'tampered')
            with patch.object(updates.sys, 'frozen', True, create=True), \
                 patch.object(updates.subprocess, 'Popen') as launch:
                with self.assertRaisesRegex(ValueError, '검증'):
                    updates.apply_pending(Path(folder) / 'NewsMonitor.exe')
            launch.assert_not_called()
            self.assertFalse((Path(folder) / 'NewsMonitor.exe.pending.json').exists())
