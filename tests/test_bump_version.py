"""自動推進版號的測試：python3 -m unittest discover tests"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / '.github' / 'scripts' / 'bump_version.py'


class BumpVersion(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name)
        self.manifest = self.repo / '.claude-plugin' / 'plugin.json'
        self.manifest.parent.mkdir()
        self.git('init', '-q')
        self.first = self.commit_manifest('0.2.0')

    def git(self, *args):
        out = subprocess.run(['git', '-c', 'user.name=t', '-c', 'user.email=t@example.invalid',
                              '-c', 'commit.gpgsign=false', *args],
                             cwd=self.repo, capture_output=True, encoding='utf-8', errors='replace', check=True)
        return out.stdout.strip()

    def commit_manifest(self, version=None, raw=None):
        body = raw if raw is not None else json.dumps({'name': 'inbox', 'version': version}, indent=2) + '\n'
        self.manifest.write_text(body, encoding='utf-8')
        return self.commit('manifest')

    def commit(self, message):
        self.git('add', '-A')
        self.git('commit', '-q', '--allow-empty', '-m', message)
        return self.git('rev-parse', 'HEAD')

    def run_script(self, before):
        output = self.repo / 'github-output.txt'
        output.write_text('', encoding='utf-8')
        env = dict(os.environ, GITHUB_OUTPUT=str(output))
        done = subprocess.run([sys.executable, str(SCRIPT), before], cwd=self.repo, env=env,
                              capture_output=True, encoding='utf-8', errors='replace')
        return done, dict(line.split('=', 1) for line in output.read_text(encoding='utf-8').splitlines())

    def version(self):
        return json.loads(self.manifest.read_text(encoding='utf-8')).get('version')

    def test_merge_without_version_change_gets_next_patch(self):
        (self.repo / 'README.md').write_text('改了別的檔', encoding='utf-8')
        self.commit('合進一個沒改版號的 PR')
        done, out = self.run_script(self.first)
        self.assertEqual(done.returncode, 0)
        self.assertEqual(self.version(), '0.2.1')
        self.assertEqual(out, {'bumped': 'true', 'version': '0.2.1'})
        self.assertEqual(json.loads(self.manifest.read_text())['name'], 'inbox')  # 其他欄位沒被動到

    def test_version_changed_in_the_push_is_left_alone(self):
        self.commit_manifest('0.3.0')  # PR 裡手動進了 minor
        done, out = self.run_script(self.first)
        self.assertEqual(done.returncode, 0)
        self.assertEqual(self.version(), '0.3.0')
        self.assertEqual(out['bumped'], 'false')

    def test_unknown_previous_commit_changes_nothing(self):
        for before in ('0' * 40, '', 'deadbeef' * 5):
            done, out = self.run_script(before)
            self.assertEqual(done.returncode, 0)
            self.assertEqual(self.version(), '0.2.0')
            self.assertEqual(out['bumped'], 'false')

    def test_manifest_without_version_is_left_alone(self):
        before = self.commit_manifest(raw='{ "name": "inbox" }\n')
        self.commit('之後的 commit')
        done, out = self.run_script(before)
        self.assertEqual(done.returncode, 0)
        self.assertIsNone(self.version())
        self.assertEqual(out['bumped'], 'false')

    def test_version_that_is_not_three_numbers_fails_loudly(self):
        before = self.commit_manifest('1.0.0-beta')
        self.commit('之後的 commit')
        done, _ = self.run_script(before)
        self.assertNotEqual(done.returncode, 0)
        self.assertIn('不是 X.Y.Z', done.stderr)
        self.assertEqual(self.version(), '1.0.0-beta')

    def test_layout_of_the_real_manifest_survives_a_bump(self):
        real = (SCRIPT.parent.parent.parent / '.claude-plugin' / 'plugin.json').read_text(encoding='utf-8')
        before = self.commit_manifest(raw=real)
        self.commit('之後的 commit')
        was = json.loads(real)['version']
        self.run_script(before)
        now = self.manifest.read_text(encoding='utf-8')
        self.assertEqual(len(now.splitlines()), len(real.splitlines()))  # 只換了版號那幾個字
        major, minor, patch = was.split('.')
        self.assertEqual(json.loads(now)['version'], '%s.%s.%d' % (major, minor, int(patch) + 1))


if __name__ == '__main__':
    unittest.main()
