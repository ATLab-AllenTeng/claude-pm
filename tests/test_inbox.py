"""收件匣單元測試：python3 -m unittest discover tests"""
import io
import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from contextlib import redirect_stdout
from datetime import datetime, timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'inbox'))
import inbox  # noqa: E402

REAL_ENSURE_SERVER = inbox.ensure_server  # Base 會把它換成替身，這裡先留一份真的


def free_port():
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()
    return port


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.project = Path(self.tmp.name) / 'proj'
        self.project.mkdir()
        self.env = mock.patch.dict(os.environ, {'CLAUDE_INBOX_HOME': str(Path(self.tmp.name) / 'home'),
                                                'CLAUDE_CODE_SESSION_ID': 'sess-1'})
        self.env.start()
        self.cwd = os.getcwd()
        os.chdir(self.project)
        self.no_server = mock.patch.object(inbox, 'ensure_server', return_value=True)
        self.no_server.start()

    def tearDown(self):
        self.no_server.stop()
        os.chdir(self.cwd)
        self.env.stop()
        self.tmp.cleanup()

    def ask(self, *argv):
        with redirect_stdout(io.StringIO()) as out:
            inbox.main(['ask'] + list(argv))
        return out.getvalue().split('已放進收件匣：')[1].split()[0]

    def answer(self, qid, choice='A', text=''):
        questions = {q['id']: q for q in inbox.load_questions()}
        events = inbox.validate_answers({'answers': [{'question_id': qid, 'choice': choice, 'text': text}]}, questions)
        with inbox.EventLog() as log:
            log.append(events)
        return events[0]


class AskAndAnswer(Base):
    def test_ask_writes_question_with_session_and_project(self):
        qid = self.ask('--title', '要哪個？', '--option', 'A=甲', '--option', 'B=乙', '--recommend', 'B')
        q = inbox.load_questions()[0]
        self.assertEqual(q['id'], qid)
        self.assertEqual(q['session'], 'sess-1')
        self.assertEqual(q['project'], os.path.realpath(str(self.project)))
        self.assertEqual([o['recommended'] for o in q['options']], [False, True])

    def test_bad_option_and_bad_recommend_rejected(self):
        with self.assertRaises(SystemExit):
            self.ask('--title', 't', '--option', 'A')
        with self.assertRaises(SystemExit):
            self.ask('--title', 't', '--option', 'A=甲', '--recommend', 'Z')

    def test_validate_rejects_unknown_choice_empty_answer_and_long_text(self):
        qid = self.ask('--title', 't', '--option', 'A=甲')
        qs = {q['id']: q for q in inbox.load_questions()}
        for bad in ({'question_id': qid, 'choice': 'Z'}, {'question_id': qid},
                    {'question_id': qid, 'text': 'x' * 4001}, {'question_id': 'nope', 'choice': 'A'}):
            with self.assertRaises(ValueError):
                inbox.validate_answers({'answers': [bad]}, qs)

    def test_info_question_only_accepts_ack(self):
        qid = self.ask('--info', '--title', '知會')
        qs = {q['id']: q for q in inbox.load_questions()}
        self.assertEqual(inbox.validate_answers({'answers': [{'question_id': qid}]}, qs)[0]['choice'], 'ack')
        with self.assertRaises(ValueError):
            inbox.validate_answers({'answers': [{'question_id': qid, 'choice': 'A'}]}, qs)


class Delivery(Base):
    def test_answer_goes_to_asking_session_once(self):
        qid = self.ask('--title', 't', '--option', 'A=甲')
        self.answer(qid, 'A', '補充')
        self.assertEqual(inbox.take('sess-2', str(self.project)), [])  # 別的 session 拿不到
        pairs = inbox.take('sess-1', str(self.project))
        self.assertEqual(len(pairs), 1)
        self.assertIn('使用者選：A 甲', inbox.format_answers(pairs))
        self.assertIn('補充：補充', inbox.format_answers(pairs))
        self.assertEqual(inbox.take('sess-1', str(self.project)), [])  # 只送一次

    def test_asking_session_gets_answer_even_from_another_directory(self):
        sub_dir = self.project / 'a' / 'b'
        sub_dir.mkdir(parents=True)
        os.chdir(sub_dir)  # 非 git 資料夾：出題與 hook 的 cwd 不同，專案根就不同
        qid = self.ask('--title', 't', '--option', 'A=甲')
        self.answer(qid)
        self.assertEqual(len(inbox.take('sess-1', str(self.project))), 1)

    def test_other_project_cannot_take_over_even_old_answers_or_empty_sessions(self):
        qid = self.ask('--title', 't', '--option', 'A=甲')
        event = self.answer(qid)
        other = Path(self.tmp.name) / 'other'
        other.mkdir()
        later = datetime.fromisoformat(event['at']).timestamp() + inbox.TAKEOVER_HOURS * 3600 + 1
        with mock.patch.object(inbox.time, 'time', return_value=later):
            self.assertEqual(inbox.take('sess-2', str(other)), [])  # 出題的對話再久沒動靜也只限同專案
        with mock.patch.dict(os.environ, {'CLAUDE_CODE_SESSION_ID': ''}):
            anon = self.ask('--title', '匿名', '--option', 'A=甲')
        self.answer(anon)
        self.assertEqual(inbox.take('', str(other)), [])  # 空 session 對空 session 不算同一個

    def test_other_project_never_gets_it(self):
        qid = self.ask('--title', 't', '--option', 'A=甲')
        self.answer(qid)
        other = Path(self.tmp.name) / 'other'
        other.mkdir()
        self.assertEqual(inbox.take('sess-2', str(other)), [])  # 別的專案、別的 session

    def test_answer_goes_to_same_project_when_asker_has_been_silent_for_a_day(self):
        qid = self.ask('--title', 't', '--option', 'A=甲')
        event = self.answer(qid)
        self.assertEqual(inbox.take('sess-2', str(self.project)), [])
        later = datetime.fromisoformat(event['at']).timestamp() + inbox.TAKEOVER_HOURS * 3600 + 1
        with mock.patch.object(inbox.time, 'time', return_value=later):
            self.assertEqual(len(inbox.take('sess-2', str(self.project))), 1)

    def test_asker_that_is_still_around_keeps_its_answer(self):
        qid = self.ask('--title', 't', '--option', 'A=甲')
        event = self.answer(qid)
        later = datetime.fromisoformat(event['at']).timestamp() + inbox.TAKEOVER_HOURS * 3600 + 1
        with mock.patch.object(inbox.time, 'time', return_value=later - 60):
            inbox.touch_session('sess-1')  # 出題的對話一分鐘前還在（例如正在 inbox wait）
        with mock.patch.object(inbox.time, 'time', return_value=later):
            self.assertEqual(inbox.take('sess-2', str(self.project)), [])  # 別的對話接不走
            self.assertEqual(len(inbox.take('sess-1', str(self.project))), 1)

    def hook(self, event, session='sess-1', cwd=None):
        stdin = io.StringIO(json.dumps({'hook_event_name': event, 'session_id': session,
                                        'cwd': cwd or str(self.project)}))
        with mock.patch.object(sys, 'stdin', stdin), redirect_stdout(io.StringIO()) as out:
            inbox.main(['hook'])
        return out.getvalue()

    def test_idle_asker_is_not_taken_over_before_the_fallback_time(self):
        qid = self.ask('--title', 't', '--option', 'A=甲')
        event = self.answer(qid)
        hours_later = datetime.fromisoformat(event['at']).timestamp() + 5 * 3600  # 遠超過舊版的 30 分鐘
        with mock.patch.object(inbox.time, 'time', return_value=hours_later):
            self.assertEqual(inbox.take('sess-2', str(self.project)), [])
            self.assertEqual(len(inbox.take('sess-1', str(self.project))), 1)  # 回到原對話還拿得到

    def test_session_end_hands_answer_to_same_project_at_once_and_takes_nothing_itself(self):
        qid = self.ask('--title', '問題標題', '--option', 'A=甲')
        self.answer(qid)
        self.assertEqual(self.hook('SessionEnd'), '')  # 正在關掉的對話不拿回答
        self.assertFalse(inbox.build_state()['questions'][0]['answers'][0]['delivered'])
        self.assertTrue(inbox.session_ended('sess-1'))
        other = Path(self.tmp.name) / 'other'
        other.mkdir()
        self.assertEqual(self.hook('UserPromptSubmit', 'sess-3', str(other)), '')  # 別的專案還是拿不到
        self.assertIn('問題標題', self.hook('UserPromptSubmit', 'sess-2'))  # 同專案馬上接手，不用等

    def test_resumed_session_gets_its_answer_back(self):
        qid = self.ask('--title', '問題標題', '--option', 'A=甲')
        self.hook('SessionEnd')
        self.answer(qid)
        self.assertIn('問題標題', self.hook('SessionStart'))  # 同一個對話被 resume：自己先拿到
        self.assertFalse(inbox.session_ended('sess-1'))
        self.assertEqual(self.hook('UserPromptSubmit', 'sess-2'), '')

    def test_leftover_wait_stops_and_does_not_undo_session_end(self):
        qid = self.ask('--title', 't', '--option', 'A=甲')
        self.answer(qid)
        self.hook('SessionEnd')
        inbox.touch_session('sess-1')  # 殘留的背景行程碰一下，不能把「已結束」改回來
        self.assertTrue(inbox.session_ended('sess-1'))
        with redirect_stdout(io.StringIO()) as out, self.assertRaises(SystemExit) as stop:
            inbox.main(['wait', '--timeout', '5'])
        self.assertEqual(stop.exception.code, 3)
        self.assertIn('已經結束', out.getvalue())
        self.assertFalse(inbox.build_state()['questions'][0]['answers'][0]['delivered'])

    def test_takeover_hours_zero_means_only_session_end_counts(self):
        (inbox.home() / 'config.json').write_text(json.dumps({'takeover_hours': 0}))
        qid = self.ask('--title', 't', '--option', 'A=甲')
        event = self.answer(qid)
        much_later = datetime.fromisoformat(event['at']).timestamp() + 30 * 86400
        with mock.patch.object(inbox.time, 'time', return_value=much_later):
            self.assertEqual(inbox.take('sess-2', str(self.project)), [])
            self.hook('SessionEnd')
            self.assertEqual(len(inbox.take('sess-2', str(self.project))), 1)
        (inbox.home() / 'config.json').write_text(json.dumps({'takeover_hours': 'oops'}))
        self.assertEqual(inbox.takeover_seconds(), inbox.TAKEOVER_HOURS * 3600)  # 看不懂就用預設

    def test_question_without_seen_record_counts_silence_from_when_it_was_asked(self):
        qid = self.ask('--title', '舊版出的題', '--option', 'A=甲')
        inbox.session_file('sess-1').unlink()  # 舊版沒有動靜記錄
        event = self.answer(qid)
        asked = datetime.fromisoformat(event['at']).timestamp()
        with mock.patch.object(inbox.time, 'time', return_value=asked + 3600):
            self.assertEqual(inbox.take('sess-2', str(self.project)), [])
        with mock.patch.object(inbox.time, 'time', return_value=asked + inbox.TAKEOVER_HOURS * 3600 + 5):
            self.assertEqual(len(inbox.take('sess-2', str(self.project))), 1)

    def test_session_seen_file_is_private_and_missing_one_counts_as_gone(self):
        self.assertEqual(inbox.last_seen('never-seen'), 0.0)
        inbox.touch_session('')  # 沒有 session 代號時什麼都不記
        inbox.touch_session('sess-9')
        folder = Path(os.environ['CLAUDE_INBOX_HOME']) / 'sessions'
        self.assertEqual(len(list(folder.iterdir())), 1)
        self.assertEqual(oct(folder.stat().st_mode & 0o777), '0o700')
        self.assertGreater(inbox.last_seen('sess-9'), 0)

    def test_hook_reads_stdin_and_prints_answers(self):
        qid = self.ask('--title', '問題標題', '--option', 'A=甲')
        self.answer(qid)
        stdin = io.StringIO(json.dumps({'session_id': 'sess-1', 'cwd': str(self.project)}))
        with mock.patch.object(sys, 'stdin', stdin), redirect_stdout(io.StringIO()) as out:
            inbox.main(['hook'])
        self.assertIn('問題標題', out.getvalue())
        with mock.patch.object(sys, 'stdin', io.StringIO('{}')), redirect_stdout(io.StringIO()) as out:
            inbox.main(['hook'])
        self.assertEqual(out.getvalue(), '')  # 沒有新回覆時什麼都不印

    def test_answer_is_not_marked_delivered_when_output_fails(self):
        qid = self.ask('--title', 't', '--option', 'A=甲')
        self.answer(qid)

        def broken(pairs):
            raise BrokenPipeError('讀的那一端不見了')

        with self.assertRaises(BrokenPipeError):
            inbox.take('sess-1', str(self.project), emit=broken)
        self.assertFalse(inbox.build_state()['questions'][0]['answers'][0]['delivered'])
        seen = []
        self.assertEqual(len(inbox.take('sess-1', str(self.project), emit=seen.extend)), 1)  # 下次還拿得到
        self.assertEqual(len(seen), 1)
        self.assertTrue(inbox.build_state()['questions'][0]['answers'][0]['delivered'])

    def test_take_command_prints_answers_then_reports_none(self):
        qid = self.ask('--title', '問題標題', '--option', 'A=甲')
        self.answer(qid)
        with redirect_stdout(io.StringIO()) as out:
            inbox.main(['take'])
        self.assertIn('問題標題', out.getvalue())
        with redirect_stdout(io.StringIO()) as out:
            inbox.main(['take'])
        self.assertIn('沒有新的回覆', out.getvalue())

    def test_wait_asks_git_for_the_project_only_once(self):
        qid = self.ask('--title', 't', '--option', 'A=甲')
        self.answer(qid)
        with mock.patch.object(inbox, 'project_of', wraps=inbox.project_of) as spy, \
                redirect_stdout(io.StringIO()) as out:
            inbox.main(['wait', '--timeout', '5'])
        self.assertEqual(spy.call_count, 1)
        self.assertIn('使用者選：A 甲', out.getvalue())

    def test_close_hides_question_from_list(self):
        qid = self.ask('--title', '要收的題')
        with redirect_stdout(io.StringIO()):
            inbox.main(['close', qid])
        with redirect_stdout(io.StringIO()) as out:
            inbox.main(['list'])
        self.assertNotIn('要收的題', out.getvalue())
        self.assertTrue(inbox.build_state()['questions'][0]['closed'])


class Encoding(Base):
    def run_cli(self, *argv, stdin=b'', codepage='cp950'):
        """用真的子行程跑指令，並把 stdio 的預設編碼設成 Windows 繁中的碼頁，看輸出是不是仍然是 UTF-8。"""
        import subprocess
        env = dict(os.environ, PYTHONIOENCODING=codepage, PYTHONUTF8='0')
        return subprocess.run([sys.executable, str(Path(inbox.__file__).resolve()), *argv],
                              input=stdin, capture_output=True, env=env, cwd=str(self.project), timeout=60)

    def test_hook_output_is_utf8_even_when_console_codepage_is_not(self):
        qid = self.ask('--title', '要不要上線？🚀', '--option', 'A=上線')  # 表情符號不在 CP950 裡
        self.answer(qid, 'A', '補充說明')
        payload = json.dumps({'hook_event_name': 'UserPromptSubmit', 'session_id': 'sess-1',
                              'cwd': str(self.project)}).encode('utf-8')
        done = self.run_cli('hook', stdin=payload)
        self.assertEqual(done.returncode, 0, done.stderr.decode('utf-8', 'replace'))
        text = done.stdout.decode('utf-8')  # 不是 UTF-8 的話這裡就會丟例外
        self.assertIn('要不要上線？🚀', text)
        self.assertIn('補充：補充說明', text)

    def test_hook_accepts_stdin_that_starts_with_a_bom(self):
        qid = self.ask('--title', '問題標題', '--option', 'A=甲')
        self.answer(qid)
        payload = json.dumps({'session_id': 'sess-1', 'cwd': str(self.project)}).encode('utf-8')
        done = self.run_cli('hook', stdin=b'\xef\xbb\xbf' + payload)
        self.assertIn('問題標題', done.stdout.decode('utf-8'))

    def test_list_prints_non_ascii_titles_as_utf8(self):
        self.ask('--title', '標題 ✓')
        done = self.run_cli('list')
        self.assertEqual(done.returncode, 0, done.stderr.decode('utf-8', 'replace'))
        self.assertIn('標題 ✓', done.stdout.decode('utf-8'))


class Locking(Base):
    def test_lock_keeps_two_processes_from_interleaving(self):
        import subprocess
        lock = inbox.home() / 'probe.lock'
        log = inbox.home() / 'probe.log'
        code = (
            "import sys, time; sys.path.insert(0, sys.argv[1]); import inbox\n"
            "for _ in range(20):\n"
            "    with inbox.FileLock(sys.argv[2]):\n"
            "        with open(sys.argv[3], 'a') as fh: fh.write('begin ' + sys.argv[4] + '\\n')\n"
            "        time.sleep(0.002)\n"
            "        with open(sys.argv[3], 'a') as fh: fh.write('end ' + sys.argv[4] + '\\n')\n"
        )
        src = str(Path(inbox.__file__).resolve().parent)
        workers = [subprocess.Popen([sys.executable, '-c', code, src, str(lock), str(log), name])
                   for name in ('a', 'b', 'c')]
        for w in workers:
            self.assertEqual(w.wait(timeout=60), 0)
        lines = log.read_text().split()
        pairs = list(zip(lines[0::2], lines[1::2]))
        self.assertEqual(len(pairs), 120)  # 3 個行程 x 20 次 x（begin + end 各兩個字）
        for i in range(0, len(lines), 4):  # 每個 begin 後面緊跟同一個行程的 end：沒有人插進來
            self.assertEqual((lines[i], lines[i + 2]), ('begin', 'end'))
            self.assertEqual(lines[i + 1], lines[i + 3])

    def test_event_log_releases_lock_when_opening_fails(self):
        with mock.patch.object(inbox.os, 'open', side_effect=OSError('開不了')):
            with self.assertRaises(OSError):
                inbox.EventLog().__enter__()
        with inbox.EventLog() as log:  # 鎖有放掉，下一個人拿得到
            self.assertEqual(log.read(), [])

    def test_events_file_uses_lf_line_endings_everywhere(self):
        qid = self.ask('--title', 't', '--option', 'A=甲')
        self.answer(qid)
        self.assertNotIn(b'\r', inbox.events_path().read_bytes())


class Prune(Base):
    def close_at(self, qid, days_ago):
        when = (datetime.now().astimezone() - timedelta(days=days_ago)).isoformat(timespec='seconds')
        with inbox.EventLog() as log:
            log.append([{'event_id': 'c-' + qid, 'kind': 'closed', 'question_id': qid, 'at': when}])

    def test_prune_removes_only_questions_closed_long_ago(self):
        old = self.ask('--title', '很久以前收起的', '--option', 'A=甲')
        recent = self.ask('--title', '最近收起的', '--option', 'A=甲')
        waiting = self.ask('--title', '還沒回答的', '--option', 'A=甲')
        answered = self.ask('--title', '回答了還沒收起的', '--option', 'A=甲')
        for qid in (old, recent, answered):
            self.answer(qid)
        self.assertEqual(len(inbox.take('sess-1', str(self.project))), 3)
        self.close_at(old, 40)
        self.close_at(recent, 3)
        stale = inbox.session_file('long-gone')
        stale.touch()
        os.utime(stale, (time.time() - 40 * 86400,) * 2)
        with redirect_stdout(io.StringIO()) as out:
            inbox.main(['prune', '--days', '30'])
        self.assertIn('已清掉 1 題', out.getvalue())
        self.assertEqual({q['id'] for q in inbox.load_questions()}, {recent, waiting, answered})
        events = inbox.read_events()
        self.assertFalse([e for e in events if e.get('question_id') == old])
        self.assertEqual(len([e for e in events if e['kind'] == 'delivered']), 2)  # 被刪那題的送達紀錄也清掉
        state = {q['id']: q for q in inbox.build_state()['questions']}
        self.assertTrue(state[recent]['closed'])
        self.assertTrue(state[answered]['answers'][0]['delivered'])
        self.assertFalse(stale.exists())
        self.assertGreater(inbox.last_seen('sess-1'), 0)  # 還有動靜的對話記錄留著
        with redirect_stdout(io.StringIO()) as out:
            inbox.main(['prune'])
        self.assertIn('已清掉 0 題', out.getvalue())

    def run_server_until_idle_exit(self, **settings):
        import threading as th
        config = dict({'host': '127.0.0.1', 'port': free_port(), 'token': 't', 'idle_hours': 0.0001}, **settings)
        with mock.patch.object(inbox, 'IDLE_CHECK_SECONDS', 0.1), redirect_stdout(io.StringIO()) as out:
            worker = th.Thread(target=inbox.serve, args=(config,), daemon=True)
            worker.start()
            worker.join(timeout=10)
        self.assertFalse(worker.is_alive())
        return out.getvalue()

    def test_server_prunes_old_closed_questions_when_it_starts(self):
        old = self.ask('--title', '很久以前收起的')
        recent = self.ask('--title', '最近收起的')
        self.close_at(old, 40)
        self.close_at(recent, 3)
        log = self.run_server_until_idle_exit(keep_days=30)
        self.assertIn('自動清掉 1 題', log)
        self.assertEqual({q['id'] for q in inbox.load_questions()}, {recent})

    def test_keep_days_zero_or_unreadable_turns_auto_prune_off(self):
        old = self.ask('--title', '很久以前收起的')
        self.close_at(old, 40)
        for off in (0, 'oops', None, -5):
            self.assertEqual(inbox.keep_days_of({'keep_days': off}), 0)
            self.assertNotIn('自動清掉', self.run_server_until_idle_exit(keep_days=off))
            self.assertEqual(len(inbox.load_questions()), 1)  # 沒被刪
        self.assertEqual(inbox.keep_days_of({}), 30)
        self.assertEqual(inbox.keep_days_of({'keep_days': 7}), 7)

    def test_manual_prune_uses_configured_days_unless_told_otherwise(self):
        qid = self.ask('--title', '十天前收起的')
        self.close_at(qid, 10)
        (inbox.home() / 'config.json').write_text(json.dumps({'keep_days': 0}))  # 不自動清時，手動用預設 30 天
        with redirect_stdout(io.StringIO()) as out:
            inbox.main(['prune'])
        self.assertIn('已清掉 0 題（收起超過 30 天的）', out.getvalue())
        (inbox.home() / 'config.json').write_text(json.dumps({'keep_days': 7}))
        with redirect_stdout(io.StringIO()) as out:
            inbox.main(['prune'])
        self.assertIn('已清掉 1 題（收起超過 7 天的）', out.getvalue())

    def test_events_can_still_be_appended_after_prune_rewrites_the_file(self):
        old = self.ask('--title', '舊題')
        self.close_at(old, 40)
        self.assertEqual(inbox.prune(30), 1)
        qid = self.ask('--title', '新題', '--option', 'A=甲')
        self.answer(qid)
        self.assertEqual(len(inbox.take('sess-1', str(self.project))), 1)
        self.assertEqual(oct(inbox.events_path().stat().st_mode & 0o777), '0o600')


class Server(Base):
    def start(self, host='127.0.0.1', token=''):
        port = free_port()
        inbox.Handler.config = {'host': host, 'port': port, 'token': token}
        httpd = ThreadingHTTPServer((host, port), inbox.Handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.shutdown)
        return port

    def req(self, port, path, data=None, headers=None):
        h = {'Content-Type': 'application/json'}
        h.update(headers or {})
        r = urllib.request.Request('http://127.0.0.1:%d%s' % (port, path),
                                   data=json.dumps(data).encode() if data is not None else None, headers=h)
        try:
            with urllib.request.urlopen(r, timeout=5) as resp:
                return resp.status, resp.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()

    def test_answer_round_trip(self):
        qid = self.ask('--title', 't', '--option', 'A=甲')
        port = self.start()
        self.assertEqual(self.req(port, '/')[0], 200)
        code, body = self.req(port, '/api/answer', {'answers': [{'question_id': qid, 'choice': 'A'}]})
        self.assertEqual((code, json.loads(body)), (200, {'stored': 1}))
        state = json.loads(self.req(port, '/api/state')[1])
        self.assertEqual(state['questions'][0]['answers'][0]['choice'], 'A')

    def test_bad_host_origin_and_invalid_answer_rejected(self):
        qid = self.ask('--title', 't', '--option', 'A=甲')
        port = self.start()
        self.assertEqual(self.req(port, '/api/state', headers={'Host': 'evil.example:80'})[0], 403)
        self.assertEqual(self.req(port, '/api/answer', {'answers': [{'question_id': qid, 'choice': 'A'}]},
                                  headers={'Origin': 'http://evil.example'})[0], 403)
        self.assertEqual(self.req(port, '/api/answer', {'answers': [{'question_id': qid, 'choice': 'Z'}]})[0], 400)
        self.assertEqual(inbox.read_events(), [])

    def test_second_answer_and_answer_to_closed_question_rejected(self):
        qid = self.ask('--title', 't', '--option', 'A=甲')
        port = self.start()
        once = {'answers': [{'question_id': qid, 'choice': 'A'}]}
        twice = {'answers': [{'question_id': qid, 'choice': 'A'}, {'question_id': qid, 'choice': 'A'}]}
        self.assertEqual(self.req(port, '/api/answer', twice)[0], 400)  # 同一包裡重複
        self.assertEqual(inbox.read_events(), [])
        self.assertEqual(self.req(port, '/api/answer', once)[0], 200)
        code, body = self.req(port, '/api/answer', once)  # 連點兩下
        self.assertEqual(code, 400)
        self.assertIn('已經回答過', body)
        self.assertEqual(len([e for e in inbox.read_events() if e['kind'] == 'answer']), 1)
        closed = self.ask('--title', '收起的題', '--option', 'A=甲')
        with redirect_stdout(io.StringIO()):
            inbox.main(['close', closed])
        code, body = self.req(port, '/api/answer', {'answers': [{'question_id': closed, 'choice': 'A'}]})
        self.assertEqual(code, 400)
        self.assertIn('已經收起來', body)

    def test_malformed_requests_get_an_error_response_instead_of_a_dropped_connection(self):
        import http.client
        qid = self.ask('--title', 't', '--option', 'A=甲')
        port = self.start(token='secret-token')
        good = {'X-Inbox-Token': 'secret-token'}
        self.assertEqual(self.req(port, '/api/state', headers={'X-Inbox-Token': 'tök\u00e9n'})[0], 401)
        for bad_id in (['x'], {'a': 1}, 5, None):
            code, _ = self.req(port, '/api/answer', {'answers': [{'question_id': bad_id, 'choice': 'A'}]}, headers=good)
            self.assertEqual(code, 400)
        conn = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
        self.addCleanup(conn.close)
        conn.putrequest('POST', '/api/answer')
        conn.putheader('X-Inbox-Token', 'secret-token')
        conn.putheader('Content-Type', 'application/json')
        conn.putheader('Content-Length', 'abc')
        conn.endheaders()
        self.assertEqual(conn.getresponse().status, 400)
        self.assertEqual(inbox.read_events(), [])
        self.assertEqual(self.req(port, '/api/answer', {'answers': [{'question_id': qid, 'choice': 'A'}]},
                                  headers=good)[0], 200)  # 服務還活著

    def test_page_is_served_with_csp_that_only_allows_its_own_script(self):
        port = self.start()
        seen = []
        for _ in range(2):
            with urllib.request.urlopen('http://127.0.0.1:%d/' % port, timeout=5) as resp:
                csp = resp.headers['Content-Security-Policy']
                html = resp.read().decode()
                self.assertEqual(resp.headers['X-Frame-Options'], 'DENY')
                self.assertEqual(resp.headers['Referrer-Policy'], 'no-referrer')
            nonce = csp.split("script-src 'nonce-")[1].split("'")[0]
            self.assertIn('<script nonce="%s">' % nonce, html)
            self.assertNotIn('<script>', html)  # 頁面上沒有不帶 nonce 的 script
            self.assertNotIn('unsafe-inline', csp.split('script-src')[1].split(';')[0])
            self.assertIn("default-src 'none'", csp)
            self.assertIn("connect-src 'self'", csp)
            seen.append(nonce)
        self.assertNotEqual(seen[0], seen[1])  # 每次回應換一個

    def test_token_required_when_configured(self):
        port = self.start(token='secret-token')
        self.assertEqual(self.req(port, '/api/state')[0], 401)
        self.assertEqual(self.req(port, '/api/state', headers={'X-Inbox-Token': 'secret-token'})[0], 200)

    def test_other_program_returning_200_is_not_taken_for_inbox(self):
        from http.server import BaseHTTPRequestHandler

        class Fake(BaseHTTPRequestHandler):
            body = b'<html>spa fallback</html>'

            def do_GET(self):
                body = Fake.body
                self.send_response(200)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        port = free_port()
        httpd = ThreadingHTTPServer(('127.0.0.1', port), Fake)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.shutdown)
        self.assertFalse(inbox.server_alive({'host': '127.0.0.1', 'port': port, 'token': ''}))
        for body in (b'{}', b'["questions"]', b'"questions"'):
            Fake.body = body
            self.assertFalse(inbox.server_alive({'host': '127.0.0.1', 'port': port, 'token': ''}))
        Fake.body = b'<html>spa fallback</html>'
        real = self.start()
        self.assertTrue(inbox.server_alive({'host': '127.0.0.1', 'port': real, 'token': ''}))

    def test_background_server_log_never_contains_token(self):
        import signal
        port = free_port()
        inbox.home()
        (Path(os.environ['CLAUDE_INBOX_HOME']) / 'config.json').write_text(json.dumps({'port': port}))
        config = inbox.load_config()
        pid_file = Path(os.environ['CLAUDE_INBOX_HOME']) / 'server.pid'

        try:
            with mock.patch.object(inbox, 'ensure_server', REAL_ENSURE_SERVER):
                self.assertTrue(inbox.ensure_server(config))
            log = (Path(os.environ['CLAUDE_INBOX_HOME']) / 'server.log').read_text()
            self.assertIn('收件匣服務已啟動', log)
            self.assertNotIn(config['token'], log)
        finally:  # 必須在測試本體清：tearDown 會先刪掉暫存資料夾與 PID 檔
            if pid_file.exists():
                try:
                    os.kill(int(pid_file.read_text()), signal.SIGTERM)
                except (ProcessLookupError, ValueError):
                    pass

    def test_stop_leaves_unrelated_process_alone_when_pid_file_is_stale(self):
        import subprocess
        bystander = subprocess.Popen(['sleep', '30'])
        self.addCleanup(bystander.kill)
        pid_file = inbox.home() / 'server.pid'
        pid_file.write_text(str(bystander.pid))  # 舊 PID 檔，號碼已經是別的程式的
        with redirect_stdout(io.StringIO()) as out:
            inbox.main(['stop'])
        self.assertIn('本來就沒在跑', out.getvalue())
        time.sleep(0.2)
        self.assertIsNone(bystander.poll())  # 沒被誤殺
        self.assertFalse(pid_file.exists())

    def test_stop_ends_real_server_and_server_removes_its_pid_file(self):
        port = free_port()
        inbox.home()
        (Path(os.environ['CLAUDE_INBOX_HOME']) / 'config.json').write_text(json.dumps({'port': port}))
        config = inbox.load_config()
        pid_file = Path(os.environ['CLAUDE_INBOX_HOME']) / 'server.pid'
        with mock.patch.object(inbox, 'ensure_server', REAL_ENSURE_SERVER):
            self.assertTrue(inbox.ensure_server(config))
        pid = int(pid_file.read_text())
        try:
            self.assertTrue(inbox.is_inbox_server(pid))
            with redirect_stdout(io.StringIO()) as out:
                inbox.main(['stop'])
            self.assertIn('已停止', out.getvalue())
            for _ in range(25):
                if not inbox.server_alive(config):
                    break
                time.sleep(0.2)
            self.assertFalse(inbox.server_alive(config))
        finally:
            try:
                os.kill(pid, 15)
            except ProcessLookupError:
                pass

    def test_idle_rule_needs_no_waiting_question_and_a_long_quiet_period(self):
        hour = 3600
        self.assertTrue(inbox.idle_too_long(0, 13 * hour, 12 * hour, waiting=0))
        self.assertFalse(inbox.idle_too_long(0, 11 * hour, 12 * hour, waiting=0))  # 還不夠久
        self.assertFalse(inbox.idle_too_long(0, 99 * hour, 12 * hour, waiting=1))  # 還有題目等你回
        self.assertFalse(inbox.idle_too_long(0, 99 * hour, 0, waiting=0))  # idle_hours 設 0：不自動結束
        self.assertEqual(inbox.read_config(Path('/nonexistent'))['idle_hours'], 12)

    def test_server_exits_by_itself_when_idle_and_nothing_is_waiting(self):
        import threading as th
        qid = self.ask('--title', '已經回答的題', '--option', 'A=甲')
        self.answer(qid)  # 回答過的不算「等你回」
        config = {'host': '127.0.0.1', 'port': free_port(), 'token': 't', 'idle_hours': 0.00005}  # 0.18 秒
        pid_file = Path(os.environ['CLAUDE_INBOX_HOME']) / 'server.pid'
        with mock.patch.object(inbox, 'IDLE_CHECK_SECONDS', 0.1), redirect_stdout(io.StringIO()) as out:
            worker = th.Thread(target=inbox.serve, args=(config,), daemon=True)
            worker.start()
            worker.join(timeout=10)
        self.assertFalse(worker.is_alive())
        self.assertIn('自動結束', out.getvalue())
        self.assertFalse(pid_file.exists())
        self.assertFalse(inbox.server_alive(config))

    def test_loopback_default_also_gets_private_token_and_folder(self):
        old = os.umask(0o022)  # 在寬鬆的 umask 下測，權限被放寬時才會變紅
        self.addCleanup(os.umask, old)
        config = inbox.load_config()
        path = Path(os.environ['CLAUDE_INBOX_HOME']) / 'config.json'
        self.assertTrue(config['token'])
        self.assertEqual(oct(path.stat().st_mode & 0o777), '0o600')
        self.assertEqual(oct(path.parent.stat().st_mode & 0o777), '0o700')
        self.assertEqual(inbox.load_config()['token'], config['token'])  # 第二次不重產
        qid = self.ask('--title', 't')
        self.answer(qid, choice='', text='x')
        self.assertEqual(oct(inbox.events_path().stat().st_mode & 0o777), '0o600')
        self.assertEqual(oct((path.parent / 'questions').stat().st_mode & 0o777), '0o700')

    def test_plain_http_warning_only_outside_loopback_and_tailscale(self):
        from contextlib import redirect_stderr
        for quiet in ('127.0.0.1', 'localhost', '100.64.0.1', '100.101.102.103'):
            self.assertEqual(inbox.exposure_warning({'host': quiet}), '')
        for loud in ('192.168.1.20', '10.0.0.5', '0.0.0.0', 'my-laptop.local'):
            self.assertIn('沒加密', inbox.exposure_warning({'host': loud}))
        path = Path(os.environ['CLAUDE_INBOX_HOME']) / 'config.json'
        inbox.home()
        path.write_text(json.dumps({'host': '192.168.1.20'}))
        with redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()) as err:
            inbox.main(['url'])
        self.assertIn('http://192.168.1.20:', out.getvalue())
        self.assertNotIn('注意', out.getvalue())  # 提醒走 stderr，不混進網址那一行
        self.assertIn('192.168.1.20', err.getvalue())
        path.write_text(json.dumps({'host': '127.0.0.1'}))
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as err:
            inbox.main(['url'])
        self.assertEqual(err.getvalue(), '')

    def test_non_loopback_host_gets_token_generated(self):
        path = Path(os.environ['CLAUDE_INBOX_HOME']) / 'config.json'
        inbox.home()
        path.write_text(json.dumps({'host': '100.64.0.1'}))
        config = inbox.load_config()
        self.assertTrue(config['token'])
        self.assertEqual(oct(path.stat().st_mode & 0o777), '0o600')
        self.assertIn('?t=' + config['token'], inbox.page_url(config))


if __name__ == '__main__':
    unittest.main()
