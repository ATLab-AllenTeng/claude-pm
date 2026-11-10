#!/usr/bin/env python3
"""收件匣：讓 Claude Code 的 agent 把要你決定的事集中到一個網頁，你回答後答案自動回到 agent 手上。

只用 Python 標準庫（3.9 以上）。資料放在 $CLAUDE_INBOX_HOME（預設 ~/.claude-inbox）。
"""
import argparse
import fcntl
import hashlib
import ipaddress
import json
import os
import secrets
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
PAGE = HERE / 'page.html'
DEFAULT_PORT = 7799
MAX_BODY = 64 * 1024
MAX_TEXT = 4000
ORPHAN_SECONDS = 30 * 60  # 回答放了 30 分鐘、出題的 session 也 30 分鐘沒動靜，同專案其他 session 才可以拿


# ---------- 路徑與設定 ----------

def home():
    root = Path(os.environ.get('CLAUDE_INBOX_HOME') or Path.home() / '.claude-inbox')
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    (root / 'questions').mkdir(mode=0o700, exist_ok=True)
    return root


def read_config(path):
    config = {'host': '127.0.0.1', 'port': DEFAULT_PORT, 'token': ''}
    if path.exists():
        config.update(json.loads(path.read_text(encoding='utf-8')))
    return config


def load_config():
    path = home() / 'config.json'
    config = read_config(path)
    if not config.get('token'):
        with open(home() / 'config.lock', 'a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            config = read_config(path)  # 搶到鎖後重讀，別人可能已經產生過
            if not config.get('token'):
                config['token'] = secrets.token_urlsafe(24)
                write_json(path, config)
    return config


def is_loopback(host):
    if host == 'localhost':
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def write_json(path, data):
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + '.')  # 預設權限 600
    with os.fdopen(fd, 'w', encoding='utf-8') as fh:
        fh.write(json.dumps(data, ensure_ascii=False, indent=2))
    os.replace(tmp, path)


def now_iso():
    return datetime.now().astimezone().isoformat(timespec='seconds')


def project_of(cwd):
    try:
        out = subprocess.run(['git', '-C', str(cwd), 'rev-parse', '--show-toplevel'],
                             capture_output=True, text=True, timeout=5)
        if out.returncode == 0 and out.stdout.strip():
            return os.path.realpath(out.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        pass
    return os.path.realpath(str(cwd))


def base_url(config):
    host = 'localhost' if config['host'] == '127.0.0.1' else config['host']
    return 'http://%s:%s/' % (host, config['port'])


def page_url(config):
    return base_url(config) + ('?t=' + config['token'] if config.get('token') else '')


# ---------- session 的動靜 ----------

def session_file(session_id):
    folder = home() / 'sessions'
    folder.mkdir(mode=0o700, exist_ok=True)
    return folder / hashlib.sha256(session_id.encode('utf-8')).hexdigest()[:32]


def touch_session(session_id):
    """記下這個 session 剛剛還在：出題、來拿回答、在 wait 裡等，都算。"""
    if not session_id:
        return
    now = time.time()
    try:
        path = session_file(session_id)
        path.touch(mode=0o600)
        os.utime(path, (now, now))
    except OSError:
        pass  # 記不下來不該擋住出題或拿回答


def last_seen(session_id):
    try:
        return session_file(session_id).stat().st_mtime
    except OSError:
        return 0.0  # 沒記錄過（例如舊版出的題）就當它不在了


# ---------- 題目與事件 ----------

def load_questions():
    items = []
    for path in sorted((home() / 'questions').glob('*.json')):
        try:
            items.append(json.loads(path.read_text(encoding='utf-8')))
        except (OSError, ValueError):
            continue
    return items


def events_path():
    return home() / 'events.jsonl'


class EventLog:
    """events.jsonl 的讀寫，全部包在同一把檔案鎖裡。"""

    def __enter__(self):
        fd = os.open(events_path(), os.O_RDWR | os.O_APPEND | os.O_CREAT, 0o600)
        self.fh = os.fdopen(fd, 'a+', encoding='utf-8')
        fcntl.flock(self.fh, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        fcntl.flock(self.fh, fcntl.LOCK_UN)
        self.fh.close()

    def read(self):
        self.fh.seek(0)
        out = []
        for line in self.fh:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
        return out

    def append(self, events):
        for event in events:
            self.fh.write(json.dumps(event, ensure_ascii=False) + '\n')
        self.fh.flush()
        os.fsync(self.fh.fileno())


def read_events():
    if not events_path().exists():
        return []
    with EventLog() as log:
        return log.read()


def build_state(events=None):
    questions = {q['id']: dict(q, answers=[], closed=False) for q in load_questions()}
    delivered = set()
    if events is None:
        events = read_events()
    for e in events:
        if e.get('kind') == 'delivered':
            delivered.add(e.get('ref'))
    for e in events:
        q = questions.get(e.get('question_id'))
        if not q:
            continue
        if e.get('kind') == 'answer':
            q['answers'].append(dict(e, delivered=e['event_id'] in delivered))
        elif e.get('kind') == 'closed':
            q['closed'] = True
    items = sorted(questions.values(), key=lambda q: q['created'])
    return {'questions': items}


def deliverable(session_id, cwd, events=None):
    """這個 session 該拿到、還沒拿過的回答。"""
    project = project_of(cwd)
    now = time.time()
    out = []
    for q in build_state(events)['questions']:
        asker = q.get('session')
        own = bool(asker) and asker == session_id  # 出題的就是這個 session：不管現在在哪個資料夾
        if not own and q.get('project') != project:
            continue
        # 出題的 session 還有動靜（剛出題、剛來拿過、正在 wait）就不讓別人接手
        asker_gone = not asker or now - last_seen(asker) > ORPHAN_SECONDS
        for a in q['answers']:
            if a['delivered']:
                continue
            age = now - datetime.fromisoformat(a['at']).timestamp()
            if own or not asker or (age > ORPHAN_SECONDS and asker_gone):
                out.append((q, a))
    return out


def format_answers(pairs):
    lines = ['收件匣：使用者回覆了 %d 則。' % len(pairs)]
    for q, a in pairs:
        lines.append('')
        lines.append('【%s】%s' % (q['id'], q['title']))
        if a.get('choice') == 'ack':
            lines.append('使用者：我知道了')
        elif a.get('choice'):
            lines.append('使用者選：%s %s' % (a['choice'], a.get('choice_label', '')))
        if a.get('text'):
            lines.append('補充：' + a['text'])
    lines.append('')
    lines.append('照回覆繼續做；這件處理完可跑 inbox close <題號> 把它收起來。')
    return '\n'.join(lines)


def emit_answers(pairs):
    """把回答印到 stdout 並確實送出；印不出去會丟例外，呼叫端就不會把它標成已送達。"""
    sys.stdout.write(format_answers(pairs) + '\n')
    sys.stdout.flush()


def take(session_id, cwd, emit=None):
    """取出回答並標成已送達；同一把鎖裡做，兩個 session 不會拿到同一則。

    有給 emit 時，先把回答交出去、成功了才寫「已送達」。中途被砍掉（例如 hook 逾時）
    的話這則會留到下次再送：寧可重送一次，也不要網頁顯示已收到、agent 卻沒拿到。
    """
    touch_session(session_id)
    with EventLog() as log:
        pairs = deliverable(session_id, cwd, log.read())
        if pairs and emit:
            emit(pairs)
        log.append([{'event_id': str(uuid.uuid4()), 'kind': 'delivered', 'ref': a['event_id'],
                     'session': session_id, 'at': now_iso()} for _, a in pairs])
    return pairs


def validate_answers(payload, questions):
    answers = payload.get('answers') if isinstance(payload, dict) else None
    if not isinstance(answers, list) or not answers:
        raise ValueError('answers 必須是非空陣列')
    events = []
    seen = set()
    for item in answers:
        qid = item.get('question_id') if isinstance(item, dict) else None
        q = questions.get(qid) if isinstance(qid, str) else None
        if not q:
            raise ValueError('找不到題目')
        if q.get('closed'):
            raise ValueError('這題已經收起來了')
        if q.get('answers') or q['id'] in seen:
            raise ValueError('這題已經回答過了')
        seen.add(q['id'])
        choice = item.get('choice') or ''
        text = item.get('text') or ''
        if not isinstance(choice, str) or not isinstance(text, str) or len(text) > MAX_TEXT:
            raise ValueError('欄位格式不對')
        labels = {o['key']: o['label'] for o in q.get('options', [])}
        if q.get('kind') == 'info':
            if choice not in ('', 'ack'):
                raise ValueError('知會題只能按「我知道了」')
            choice = 'ack'
        elif choice and choice not in labels:
            raise ValueError('選項不存在')
        if not choice and not text.strip():
            raise ValueError('沒有選擇也沒有補充')
        events.append({'event_id': str(uuid.uuid4()), 'kind': 'answer', 'question_id': q['id'],
                       'choice': choice, 'choice_label': labels.get(choice, ''),
                       'text': text.strip(), 'at': now_iso()})
    return events


# ---------- 網頁服務 ----------

class Handler(BaseHTTPRequestHandler):
    config = None

    def log_message(self, fmt, *args):
        pass

    def allowed_hosts(self):
        port = self.config['port']
        hosts = {'%s:%s' % (self.config['host'], port)}
        if is_loopback(self.config['host']):
            hosts |= {'localhost:%s' % port, '127.0.0.1:%s' % port}
        return hosts

    def check(self, api):
        host = self.headers.get('Host', '')
        if host not in self.allowed_hosts():
            return self.send_text(403, 'Host 不符')
        origin = self.headers.get('Origin')
        if origin and origin != 'http://' + host:
            return self.send_text(403, 'Origin 不符')
        token = self.config.get('token')
        given = self.headers.get('X-Inbox-Token', '')
        # 轉成 bytes 再比：compare_digest 遇到非 ASCII 的字串會丟例外
        if api and token and not secrets.compare_digest(given.encode('utf-8'), token.encode('utf-8')):
            return self.send_text(401, '需要 token')
        return True

    def send_text(self, code, text, ctype='text/plain; charset=utf-8'):
        body = text.encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()
        self.wfile.write(body)
        return False

    def do_GET(self):
        path = self.path.split('?', 1)[0]
        if path == '/':
            if self.check(api=False):
                self.send_text(200, PAGE.read_text(encoding='utf-8'), 'text/html; charset=utf-8')
        elif path == '/api/state':
            if self.check(api=True):
                self.send_text(200, json.dumps(build_state(), ensure_ascii=False),
                               'application/json; charset=utf-8')
        else:
            self.send_text(404, 'not found')

    def do_POST(self):
        if self.path != '/api/answer':
            return self.send_text(404, 'not found')
        if not self.check(api=True):
            return
        if not self.headers.get('Content-Type', '').startswith('application/json'):
            return self.send_text(415, '只收 JSON')
        try:
            length = int(self.headers.get('Content-Length') or 0)
        except ValueError:
            return self.send_text(400, 'Content-Length 不是數字')
        if length <= 0 or length > MAX_BODY:
            return self.send_text(413, '內容太大或是空的')
        try:
            payload = json.loads(self.rfile.read(length).decode('utf-8'))
            with EventLog() as log:  # 檢查和寫入在同一把鎖裡：連點兩下也只會存一則
                questions = {q['id']: q for q in build_state(log.read())['questions']}
                events = validate_answers(payload, questions)
                log.append(events)
        except ValueError as exc:
            return self.send_text(400, str(exc))
        self.send_text(200, json.dumps({'stored': len(events)}), 'application/json')


def serve(config):
    Handler.config = config
    server = ThreadingHTTPServer((config['host'], config['port']), Handler)
    pidfile = home() / 'server.pid'
    pidfile.write_text(str(os.getpid()))  # 綁埠成功才寫：搶輸的不會蓋掉活著的那個
    print('收件匣服務已啟動：' + base_url(config), flush=True)  # 服務的 log 不放 token

    def on_term(signum, frame):
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, on_term)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        try:  # 結束時收掉自己的 PID 檔；已經被別的服務換掉就不動
            if pidfile.read_text().strip() == str(os.getpid()):
                pidfile.unlink()
        except OSError:
            pass


def server_alive(config):
    import urllib.request
    host = 'localhost' if is_loopback(config['host']) else config['host']
    req = urllib.request.Request('http://%s:%s/api/state' % (host, config['port']),
                                 headers={'X-Inbox-Token': config.get('token', '')})
    try:
        with urllib.request.urlopen(req, timeout=2) as resp:
            body = json.loads(resp.read().decode('utf-8'))
            return resp.status == 200 and isinstance(body, dict) and 'questions' in body
    except Exception:
        return False


def ensure_server(config):
    if server_alive(config):
        return True
    log = open(home() / 'server.log', 'a')
    subprocess.Popen([sys.executable, str(Path(__file__).resolve()), 'serve'],
                            stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                            start_new_session=True)
    for _ in range(20):
        time.sleep(0.2)
        if server_alive(config):
            return True
    return False


# ---------- 指令 ----------

def cmd_ask(args):
    options = []
    for raw in args.option or []:
        key, sep, label = raw.partition('=')
        if not sep or not key.strip() or not label.strip():
            sys.exit('選項格式是 A=說明文字：' + raw)
        options.append({'key': key.strip(), 'label': label.strip(),
                        'recommended': key.strip() == args.recommend})
    if args.recommend and not any(o['recommended'] for o in options):
        sys.exit('--recommend 指的選項不存在')
    body = Path(args.body_file).read_text(encoding='utf-8') if args.body_file else (args.body or '')
    stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    q = {'id': 'q-%s-%s' % (stamp, secrets.token_hex(2)), 'kind': 'info' if args.info else 'ask',
         'title': args.title, 'body': body, 'options': options, 'deadline': args.deadline or '',
         'from': args.source or os.path.basename(project_of(os.getcwd())),
         'project': project_of(os.getcwd()), 'session': os.environ.get('CLAUDE_CODE_SESSION_ID', ''),
         'created': now_iso()}
    write_json(home() / 'questions' / (q['id'] + '.json'), q)
    touch_session(q['session'])
    config = load_config()
    up = ensure_server(config)
    print('已放進收件匣：%s' % q['id'])
    print('網址：' + page_url(config) if up else '服務沒起來，請看 %s' % (home() / 'server.log'))


def cmd_close(args):
    ids = {q['id'] for q in load_questions()}
    missing = [i for i in args.ids if i not in ids]
    if missing:
        sys.exit('找不到：' + ' '.join(missing))
    with EventLog() as log:
        log.append([{'event_id': str(uuid.uuid4()), 'kind': 'closed', 'question_id': i,
                     'at': now_iso()} for i in args.ids])
    print('已收起 %d 題' % len(args.ids))


def cmd_take(args):
    if not take(os.environ.get('CLAUDE_CODE_SESSION_ID', ''), os.getcwd(), emit=emit_answers):
        print('收件匣：沒有新的回覆。')


def cmd_wait(args):
    deadline = time.time() + args.timeout
    session = os.environ.get('CLAUDE_CODE_SESSION_ID', '')
    while time.time() < deadline:
        touch_session(session)  # 還在等：別讓同專案的其他對話把回答接走
        if deliverable(session, os.getcwd()) and take(session, os.getcwd(), emit=emit_answers):
            return
        time.sleep(3)
    print('收件匣：等了 %d 秒沒有新回覆。' % args.timeout)
    sys.exit(2)


def cmd_hook(args):
    """給 Claude Code hook 用：stdin 是 hook 的 JSON，stdout 會進 agent 的上下文。"""
    try:
        data = json.loads(sys.stdin.read() or '{}')
    except ValueError:
        data = {}
    take(data.get('session_id', ''), data.get('cwd') or os.getcwd(), emit=emit_answers)


def cmd_list(args):
    for q in build_state()['questions']:
        if q['closed'] and not args.all:
            continue
        status = '已收起' if q['closed'] else ('已回答' if q['answers'] else '等回答')
        print('%s  %s  %s  （%s）' % (q['id'], status, q['title'], q['from']))


def is_inbox_server(pid):
    """這個 PID 是不是收件匣服務。PID 檔可能是舊的（例如重開機後號碼被別的程式拿去用）。"""
    try:
        out = subprocess.run(['ps', '-p', str(pid), '-o', 'command='],
                             capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return False
    words = out.stdout.split()
    return out.returncode == 0 and 'serve' in words and any(w.endswith('inbox.py') for w in words)


def cmd_stop(args):
    pidfile = home() / 'server.pid'
    if not pidfile.exists():
        return print('沒有記錄中的服務')
    try:
        pid = int(pidfile.read_text())
        if is_inbox_server(pid):
            os.kill(pid, signal.SIGTERM)
            print('已停止')
        else:
            print('服務本來就沒在跑')  # 不是我們的服務就不碰它
    except (ProcessLookupError, ValueError):
        print('服務本來就沒在跑')
    except PermissionError:
        print('沒有權限停止 PID 檔記的那個程式，沒動它')
    pidfile.unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(prog='inbox', description='Claude Code 收件匣')
    sub = parser.add_subparsers(dest='cmd', required=True)
    p = sub.add_parser('ask', help='放一題進收件匣')
    p.add_argument('--title', required=True)
    p.add_argument('--body')
    p.add_argument('--body-file')
    p.add_argument('--option', action='append', help='A=說明，可重複')
    p.add_argument('--recommend', help='推薦的選項代號')
    p.add_argument('--info', action='store_true', help='只是知會，使用者按「我知道了」')
    p.add_argument('--deadline')
    p.add_argument('--from', dest='source', help='顯示的來源名稱，預設是專案資料夾名')
    p.set_defaults(func=cmd_ask)
    p = sub.add_parser('take', help='取出這個 session 的新回覆')
    p.set_defaults(func=cmd_take)
    p = sub.add_parser('wait', help='等到有新回覆才結束')
    p.add_argument('--timeout', type=int, default=1800)
    p.set_defaults(func=cmd_wait)
    p = sub.add_parser('close', help='把處理完的題目收起來')
    p.add_argument('ids', nargs='+')
    p.set_defaults(func=cmd_close)
    p = sub.add_parser('list', help='列出題目')
    p.add_argument('--all', action='store_true')
    p.set_defaults(func=cmd_list)
    sub.add_parser('hook', help='給 Claude Code hook 用').set_defaults(func=cmd_hook)
    sub.add_parser('serve', help='前景執行網頁服務').set_defaults(func=lambda a: serve(load_config()))
    sub.add_parser('start', help='背景啟動網頁服務').set_defaults(
        func=lambda a: print(page_url(load_config()) if ensure_server(load_config()) else '啟動失敗'))
    sub.add_parser('stop', help='停止背景服務').set_defaults(func=cmd_stop)
    sub.add_parser('url', help='印出網址').set_defaults(func=lambda a: print(page_url(load_config())))
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == '__main__':
    main()
