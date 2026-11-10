#!/usr/bin/env python3
"""合進 main 之後，把 .claude-plugin/plugin.json 的 patch 版號加一。

Claude Code 用 version 判斷要不要更新已安裝的 plugin：版號沒變，使用者就拿不到新的 commit。
這支指令由 .github/workflows/bump-version.yml 呼叫，用法：

    bump_version.py <這次推送之前的 commit>

這次推送已經自己改過版號（例如 PR 裡手動進 minor）就不動；否則 X.Y.Z 變成 X.Y.(Z+1)。
結果寫進 $GITHUB_OUTPUT（bumped、version），也印在畫面上。
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

MANIFEST = Path('.claude-plugin/plugin.json')
VERSION_LINE = re.compile(r'("version"\s*:\s*")([^"]*)(")')


def version_at(commit):
    """那個 commit 當時的版號；查不到（新分支的第一次推送、檔案當時不存在）回 None。"""
    if not commit or set(commit) == {'0'}:
        return None
    out = subprocess.run(['git', 'show', '%s:%s' % (commit, MANIFEST.as_posix())],
                         capture_output=True, text=True)
    if out.returncode != 0:
        return None
    try:
        return json.loads(out.stdout).get('version')
    except ValueError:
        return None


def next_patch(version):
    parts = version.split('.')
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        raise ValueError('版號 "%s" 不是 X.Y.Z 的格式，沒辦法自動加一' % version)
    return '%s.%s.%d' % (parts[0], parts[1], int(parts[2]) + 1)


def report(bumped, version):
    target = os.environ.get('GITHUB_OUTPUT')
    if target:
        with open(target, 'a', encoding='utf-8') as fh:
            fh.write('bumped=%s\nversion=%s\n' % ('true' if bumped else 'false', version))


def main(argv):
    before = argv[1] if len(argv) > 1 else ''
    text = MANIFEST.read_text(encoding='utf-8')
    current = json.loads(text).get('version')
    if not current:
        print('plugin.json 沒有 version：Claude Code 會直接跟著 commit 走，不需要推進。')
        return report(False, '')
    previous = version_at(before)
    if previous is None:
        print('查不到推送前的版號，這次不動（目前 %s）。' % current)
        return report(False, current)
    if previous != current:
        print('這次推送已經把版號從 %s 改成 %s，不再自動加。' % (previous, current))
        return report(False, current)
    try:
        bumped = next_patch(current)
    except ValueError as exc:
        sys.exit(str(exc))
    MANIFEST.write_text(VERSION_LINE.sub(lambda m: m.group(1) + bumped + m.group(3), text, count=1),
                        encoding='utf-8')
    print('版號 %s → %s' % (current, bumped))
    report(True, bumped)


if __name__ == '__main__':
    main(sys.argv)
