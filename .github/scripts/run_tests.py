#!/usr/bin/env python3
"""在 CI 上跑測試，並把每個失敗寫成 GitHub 的錯誤標註。

標註會出現在 PR 的檢查結果上，不用翻完整的執行紀錄就看得到是哪個測試、在哪個平台、為什麼失敗。
本機直接用 python3 -m unittest discover tests 就好，不需要這支。
"""
import platform
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
MAX_ANNOTATIONS = 9  # GitHub 每個步驟最多顯示 10 則錯誤，留一則給總結
MAX_CHARS = 3500


def escape(text, is_property=False):
    text = text.replace('%', '%25').replace('\r', '%0D').replace('\n', '%0A')
    if is_property:
        text = text.replace(':', '%3A').replace(',', '%2C')
    return text


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='replace')
    suite = unittest.TestLoader().discover(str(ROOT / 'tests'))
    result = unittest.TextTestRunner(stream=sys.stdout, verbosity=2).run(suite)
    where = '%s / Python %s' % (platform.system(), platform.python_version())
    problems = [('FAIL', t, tb) for t, tb in result.failures] + [('ERROR', t, tb) for t, tb in result.errors]
    for kind, test, traceback_text in problems[:MAX_ANNOTATIONS]:
        title = '%s %s (%s)' % (kind, test.id(), where)
        print('::error title=%s::%s' % (escape(title, True), escape(traceback_text[-MAX_CHARS:])))
    if problems:
        names = ', '.join(test.id().rsplit('.', 1)[-1] for _, test, _ in problems)
        print('::error title=%s::%s' % (escape('%d 個測試沒過 (%s)' % (len(problems), where), True),
                                        escape(names[:MAX_CHARS])))
    summary = '跑了 %d 個，失敗 %d，錯誤 %d，略過 %d' % (
        result.testsRun, len(result.failures), len(result.errors), len(result.skipped))
    skipped = '；'.join('%s（%s）' % (test.id().rsplit('.', 1)[-1], reason) for test, reason in result.skipped)
    # 略過了哪些也寫出來：某個平台專屬的測試如果悄悄被略過，綠燈就不代表它有被驗到
    print('::notice title=%s::%s' % (escape('測試結果 (%s)' % where, True),
                                     escape(summary + ('。略過：' + skipped if skipped else ''))))
    print('%s：%s' % (where, summary))
    sys.exit(0 if result.wasSuccessful() else 1)


if __name__ == '__main__':
    main()
