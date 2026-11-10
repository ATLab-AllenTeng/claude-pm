#!/usr/bin/env python3
"""收件匣：讓 Claude Code 的 agent 把要你決定的事集中到一個網頁，你回答後答案自動回到 agent 手上。

只用 Python 標準庫（3.9 以上）。資料放在 $CLAUDE_INBOX_HOME（預設 ~/.claude-inbox）。
"""
import argparse
import hashlib
import ipaddress
import json
import os
import secrets
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

try:
    import fcntl
except ImportError:  # 原生 Windows 沒有 fcntl
    fcntl = None
    import msvcrt

IS_WINDOWS = os.name == 'nt'

HERE = Path(__file__).resolve().parent
PAGE = HERE / 'page.html'
DEFAULT_PORT = 7799
MAX_BODY = 64 * 1024
MAX_TEXT = 4000
IDLE_CHECK_SECONDS = 60
KEEP_DAYS = 30  # 收起的題目留幾天（config 的 keep_days 可改，0 是不自動清）
PRUNE_EVERY_SECONDS = 24 * 3600
TAILSCALE_NET = ipaddress.ip_network('100.64.0.0/10')  # Tailscale 的位址範圍，連線本身有加密
TAKEOVER_HOURS = 24  # 出題的對話沒說自己結束、但這麼久沒動靜，就當它不在了（config 的 takeover_hours 可改）


# ---------- 文字編碼 ----------

def use_utf8_stdio():
    """stdin／stdout／stderr 一律用 UTF-8。

    Windows 預設是系統碼頁（繁中是 CP950）：hook 印出的中文進到 agent 會變亂碼，
    碼頁裡沒有的字還會讓整個指令丟例外；hook 從 stdin 收到的 JSON 也會解錯。
    """
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, 'reconfigure'):
            try:
                stream.reconfigure(encoding='utf-8')
            except (ValueError, OSError):
                pass


# ---------- 檔案鎖 ----------

class FileLock:
    """跨平台的獨佔檔案鎖：POSIX 用 flock，Windows 用 msvcrt.locking 鎖第一個位元組。

    鎖的是獨立的 .lock 檔，不是資料檔本身：Windows 的鎖是強制的，鎖住資料檔會讓別的行程連讀都讀不了。
    """

    def __init__(self, path):
        self.path = path

    def __enter__(self):
        self.fh = open(self.path, 'a+b')
        if fcntl:
            fcntl.flock(self.fh, fcntl.LOCK_EX)
        else:
            # msvcrt 的 LK_LOCK 只重試約 10 秒就丟 OSError，所以自己用 LK_NBLCK 輪詢到拿到為止
            while True:
                self.fh.seek(0)
                try:
                    msvcrt.locking(self.fh.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    time.sleep(0.05)
        return self

    def __exit__(self, *exc):
        if fcntl:
            fcntl.flock(self.fh, fcntl.LOCK_UN)
        else:
            self.fh.seek(0)
            msvcrt.locking(self.fh.fileno(), msvcrt.LK_UNLCK, 1)
        self.fh.close()


# ---------- 路徑與設定 ----------

def home():
    root = Path(os.environ.get('CLAUDE_INBOX_HOME') or Path.home() / '.claude-inbox')
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    (root / 'questions').mkdir(mode=0o700, exist_ok=True)
    return root


def read_config(path):
    config = {'host': '127.0.0.1', 'port': DEFAULT_PORT, 'token': '', 'idle_hours': 12,
              'takeover_hours': TAKEOVER_HOURS, 'keep_days': KEEP_DAYS}
    if path.exists():
        config.update(json.loads(path.read_text(encoding='utf-8')))
    return config


def load_config():
    path = home() / 'config.json'
    config = read_config(path)
    if not config.get('token'):
        with FileLock(home() / 'config.lock'):
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


def exposure_warning(config):
    """服務開在本機和 Tailscale 以外的位址時，回一段提醒；其他情況回空字串。"""
    host = str(config.get('host', ''))
    if is_loopback(host):
        return ''
    try:
        if ipaddress.ip_address(host) in TAILSCALE_NET:
            return ''
    except ValueError:
        pass
    return ('注意：收件匣開在 %s，走的是沒加密的 HTTP。同一個網路上的人看得到網址裡的 token，'
            '拿到 token 就能用你的名義回答，等於直接對 agent 下指令。'
            '只在你信任的網路上這樣用，或改成 Tailscale 位址。' % host)


def warn_exposure(config):
    warning = exposure_warning(config)
    if warning:
        print(warning, file=sys.stderr)


def write_json(path, data):
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + '.')  # 預設權限 600
    with os.fdopen(fd, 'w', encoding='utf-8') as fh:
        fh.write(json.dumps(data, ensure_ascii=False, indent=2))
    replace_file(tmp, path)


def replace_file(tmp, path, attempts=80):
    """把寫好的暫存檔換上去。Windows 上別的行程正開著目標檔時換不掉（PermissionError），
    那只會持續讀一個小檔的時間，所以等一下再試；其他平台一次就成功。"""
    for left in range(attempts - 1, -1, -1):
        try:
            return os.replace(tmp, path)
        except PermissionError:
            if not IS_WINDOWS or not left:
                try:
                    os.unlink(tmp)  # 換不上去就不要留下暫存檔
                except OSError:
                    pass
                raise
            time.sleep(0.025)


def now_iso():
    return datetime.now().astimezone().isoformat(timespec='seconds')


def project_of(cwd):
    try:
        out = subprocess.run(['git', '-C', str(cwd), 'rev-parse', '--show-toplevel'],
                             capture_output=True, timeout=5,
                             encoding='utf-8', errors='replace')  # git 印的是 UTF-8，不是系統碼頁
        if out.returncode == 0 and out.stdout.strip():
            return os.path.realpath(out.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        pass
    return os.path.realpath(str(cwd))


def same_project(a, b):
    """兩個專案路徑是不是同一個。Windows 的路徑不分大小寫，磁碟代號也可能一個大寫一個小寫。"""
    return bool(a) and bool(b) and os.path.normcase(a) == os.path.normcase(b)


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
    """記下這個 session 剛剛還有動靜。不會把「已結束」改回來：背景殘留的 wait 也會走到這裡。"""
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
        return 0.0  # 沒記錄過（例如舊版出的題）


def ended_file(session_id):
    path = session_file(session_id)
    return path.with_name(path.name + '.ended')


def mark_ended(session_id):
    """Claude Code 說這個 session 結束了（SessionEnd hook）：它的回答可以馬上交給同專案的其他對話。"""
    if not session_id:
        return
    try:
        ended_file(session_id).touch(mode=0o600)
    except OSError:
        pass


def session_ended(session_id):
    return bool(session_id) and ended_file(session_id).exists()


def revive_session(session_id):
    """這個 session 確定還在用（出題、送出 prompt、被 resume、自己來拿回答）：記下動靜並取消「已結束」。"""
    if not session_id:
        return
    try:
        ended_file(session_id).unlink(missing_ok=True)
    except OSError:
        pass
    touch_session(session_id)


def takeover_seconds():
    """出題的對話多久沒動靜才讓別人接手。0 表示只認 SessionEnd，不靠時間猜。"""
    try:
        hours = float(read_config(home() / 'config.json').get('takeover_hours', TAKEOVER_HOURS))
    except (OSError, TypeError, ValueError):
        hours = TAKEOVER_HOURS
    return max(hours, 0) * 3600


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
        self.lock = FileLock(home() / 'events.lock').__enter__()
        try:
            fd = os.open(events_path(), os.O_RDWR | os.O_APPEND | os.O_CREAT, 0o600)
            self.fh = os.fdopen(fd, 'a+', encoding='utf-8', newline='\n')  # 各平台都寫 LF
            if fcntl:  # 舊版只鎖資料檔本身；升級途中舊版的服務可能還在跑，這把鎖也拿，兩邊才互斥
                fcntl.flock(self.fh, fcntl.LOCK_EX)
        except BaseException:
            self.lock.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, *exc):
        if fcntl:
            fcntl.flock(self.fh, fcntl.LOCK_UN)
        self.fh.close()
        self.lock.__exit__(*exc)

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

    def rewrite(self, events):
        """整份換掉（清理用）。直接改寫原檔：Windows 上別的行程開著檔案時換不掉，改寫原檔各平台都行。"""
        self.fh.seek(0)
        self.fh.truncate()
        self.append(events)


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


def asker_gone(q, now, limit):
    """出題的 session 是不是不在了。

    明確結束（SessionEnd）就算；沒有明確結束的（當機、被強制關掉）看它多久沒動靜。
    只是閒置、Claude Code 還開著的對話不算不在。
    """
    asker = q.get('session')
    if session_ended(asker):
        return True
    if limit <= 0:
        return False
    seen = last_seen(asker)
    if not seen:  # 沒有動靜記錄（舊版出的題）：從出題時間起算
        try:
            seen = datetime.fromisoformat(q['created']).timestamp()
        except (KeyError, TypeError, ValueError):
            seen = 0.0
    return now - seen > limit


def deliverable(session_id, cwd, events=None, project=None):
    """這個 session 該拿到、還沒拿過的回答。project 可以先算好傳進來，省掉每次問 git。"""
    project = project or project_of(cwd)
    now = time.time()
    limit = takeover_seconds()
    out = []
    for q in build_state(events)['questions']:
        asker = q.get('session')
        own = bool(asker) and asker == session_id  # 出題的就是這個 session：不管現在在哪個資料夾
        if not own and not same_project(q.get('project'), project):
            continue
        if not own and asker and not asker_gone(q, now, limit):
            continue  # 出題的對話還在：只有它自己能拿
        out.extend((q, a) for a in q['answers'] if not a['delivered'])
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


def take(session_id, cwd, emit=None, project=None):
    """取出回答並標成已送達；同一把鎖裡做，兩個 session 不會拿到同一則。

    有給 emit 時，先把回答交出去、成功了才寫「已送達」。中途被砍掉（例如 hook 逾時）
    的話這則會留到下次再送：寧可重送一次，也不要網頁顯示已收到、agent 卻沒拿到。
    """
    with EventLog() as log:
        pairs = deliverable(session_id, cwd, log.read(), project)
        if pairs and emit:
            emit(pairs)
        log.append([{'event_id': str(uuid.uuid4()), 'kind': 'delivered', 'ref': a['event_id'],
                     'session': session_id, 'at': now_iso()} for _, a in pairs])
    return pairs


def prune(days):
    """刪掉收起超過 days 天的題目和它們的事件，以及同樣久沒動靜的 session 記錄。回傳刪掉幾題。

    只動已經收起的題目；還沒回答、或回答了還沒收起的，放再久也不刪。
    """
    cutoff = time.time() - days * 86400
    with EventLog() as log:
        events = log.read()
        closed_at = {}
        for e in events:
            if e.get('kind') != 'closed':
                continue
            try:
                when = datetime.fromisoformat(e['at']).timestamp()
            except (KeyError, TypeError, ValueError):
                continue
            closed_at[e.get('question_id')] = max(when, closed_at.get(e.get('question_id'), 0))
        gone = {qid for qid, when in closed_at.items() if when < cutoff}
        removed = 0
        if gone:
            answers = {e.get('event_id') for e in events
                       if e.get('kind') == 'answer' and e.get('question_id') in gone}
            log.rewrite([e for e in events
                         if e.get('question_id') not in gone and e.get('ref') not in answers])
            for path in (home() / 'questions').glob('*.json'):
                if path.stem in gone:
                    path.unlink(missing_ok=True)
                    removed += 1
    sessions = home() / 'sessions'
    if sessions.is_dir():
        for path in sessions.iterdir():
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
            except OSError:
                continue
    return removed


def keep_days_of(config):
    """收起的題目留幾天。回 0 表示不自動清：設成 0，或設定值看不懂時（寧可不刪）。"""
    try:
        days = float(config.get('keep_days', KEEP_DAYS))
    except (TypeError, ValueError):
        return 0
    return days if days > 0 else 0


def auto_prune(days):
    """服務啟動時清一次，之後每天清一次。"""
    while True:
        try:
            removed = prune(days)
            if removed:
                print('自動清掉 %d 題（收起超過 %g 天的）' % (removed, days), flush=True)
        except Exception:
            pass  # 清不了不該影響服務，下一輪再試
        time.sleep(PRUNE_EVERY_SECONDS)


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
    last_request = 0.0  # 最後一次有人連進來的時間，閒置自動結束用

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

    def send_text(self, code, text, ctype='text/plain; charset=utf-8', extra=None):
        body = text.encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        for name, value in (extra or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)
        return False

    def send_page(self):
        """送出頁面。CSP 只放行這一次回應帶 nonce 的那段 script，也不准頁面連到外面：
        就算題目文字的跳脫哪天漏了，塞進來的 script 不會跑，資料也送不出去。"""
        nonce = secrets.token_urlsafe(16)
        html = PAGE.read_text(encoding='utf-8').replace('<script>', '<script nonce="%s">' % nonce, 1)
        csp = "; ".join([
            "default-src 'none'", "script-src 'nonce-%s'" % nonce, "style-src 'unsafe-inline'",
            "connect-src 'self'", "base-uri 'none'", "form-action 'none'", "frame-ancestors 'none'"])
        self.send_text(200, html, 'text/html; charset=utf-8',
                       extra={'Content-Security-Policy': csp, 'X-Frame-Options': 'DENY'})

    def do_GET(self):
        Handler.last_request = time.time()
        path = self.path.split('?', 1)[0]
        if path == '/':
            if self.check(api=False):
                self.send_page()
        elif path == '/api/state':
            if self.check(api=True):
                self.send_text(200, json.dumps(build_state(), ensure_ascii=False),
                               'application/json; charset=utf-8')
        else:
            self.send_text(404, 'not found')

    def do_POST(self):
        Handler.last_request = time.time()
        try:
            length = int(self.headers.get('Content-Length') or 0)
        except ValueError:
            length = None
        # 先把請求本文讀完再回錯誤：Windows 關掉還有未讀資料的連線會送 RST，
        # 對方收不到 403／404，只看到連線被重設（WinError 10053／10054）。
        raw = self.rfile.read(length) if length and 0 < length <= MAX_BODY else None
        if self.path != '/api/answer':
            return self.send_text(404, 'not found')
        if not self.check(api=True):
            return
        if not self.headers.get('Content-Type', '').startswith('application/json'):
            return self.send_text(415, '只收 JSON')
        if length is None:
            return self.send_text(400, 'Content-Length 不是數字')
        if raw is None:
            return self.send_text(413, '內容太大或是空的')
        try:
            payload = json.loads(raw.decode('utf-8'))
            with EventLog() as log:  # 檢查和寫入在同一把鎖裡：連點兩下也只會存一則
                questions = {q['id']: q for q in build_state(log.read())['questions']}
                events = validate_answers(payload, questions)
                log.append(events)
        except ValueError as exc:
            return self.send_text(400, str(exc))
        self.send_text(200, json.dumps({'stored': len(events)}), 'application/json')


class InboxServer(ThreadingHTTPServer):
    """Windows 上不能沿用預設的 SO_REUSEADDR：那裡它允許兩個行程同時綁同一個埠，
    兩個服務會一起活著、輪流接請求。改成獨佔，搶輸的那個照樣綁不到就結束。"""
    allow_reuse_address = not IS_WINDOWS

    def server_bind(self):
        if IS_WINDOWS:
            import socket
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def idle_too_long(last_request, now, idle_seconds, waiting):
    """沒有等使用者回答的題目，而且很久沒人連進來：服務可以收工了。"""
    return idle_seconds > 0 and waiting == 0 and now - last_request > idle_seconds


def watch_idle(server, idle_seconds):
    while True:
        time.sleep(IDLE_CHECK_SECONDS)
        try:
            waiting = sum(1 for q in build_state()['questions'] if not q['closed'] and not q['answers'])
        except Exception:
            continue  # 讀不到狀態就先不停，下一輪再看
        if idle_too_long(Handler.last_request, time.time(), idle_seconds, waiting):
            print('沒有待回答的題目，也很久沒人連線，服務自動結束；下次出題會自動再啟動。', flush=True)
            server.shutdown()
            return


def serve(config):
    Handler.config = config
    Handler.last_request = time.time()
    try:
        server = InboxServer((config['host'], config['port']), Handler)
    except PermissionError:
        if IS_WINDOWS:  # WinError 10013：埠落在 Hyper-V／WSL／Docker 保留的範圍，沒人在用也綁不到
            print('綁不到埠 %s：可能在 Windows 保留的範圍裡（用 netsh interface ipv4 show '
                  'excludedportrange protocol=tcp 查）。請在 %s 換一個 port。'
                  % (config['port'], home() / 'config.json'), flush=True)
        raise
    pidfile = home() / 'server.pid'
    pidfile.write_text(str(os.getpid()))  # 綁埠成功才寫：搶輸的不會蓋掉活著的那個
    print('收件匣服務已啟動：' + base_url(config), flush=True)  # 服務的 log 不放 token

    def on_term(signum, frame):
        raise SystemExit(0)

    if threading.current_thread() is threading.main_thread():  # 訊號只能在主執行緒設
        signal.signal(signal.SIGTERM, on_term)
    try:
        idle_seconds = float(config.get('idle_hours') or 0) * 3600
    except (TypeError, ValueError):
        idle_seconds = 0  # 設定值看不懂就當成不自動結束
    if idle_seconds > 0:
        threading.Thread(target=watch_idle, args=(server, idle_seconds), daemon=True).start()
    keep_days = keep_days_of(config)
    if keep_days:
        threading.Thread(target=auto_prune, args=(keep_days,), daemon=True).start()
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
    command = [sys.executable, str(Path(__file__).resolve()), 'serve']
    # 工作目錄放在資料夾裡，不要留在出題當下的專案：Windows 上行程的工作目錄會被佔住，
    # 服務活著的期間那個專案資料夾刪不掉也改不了名。
    where = str(home())
    # 子行程拿到自己的那份 handle；這邊用完就關，否則 Windows 上 server.log 會一直被佔著、刪不掉
    with open(home() / 'server.log', 'a', encoding='utf-8') as log:
        if IS_WINDOWS:
            # 脫離目前的主控台：不跳視窗，hook 或終端機結束時也不會被一起帶走
            flags = (subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
                     | subprocess.CREATE_NO_WINDOW)
            subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log, stderr=log, cwd=where,
                             creationflags=flags, close_fds=True)
        else:
            subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log, stderr=log, cwd=where,
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
    revive_session(q['session'])
    config = load_config()
    up = ensure_server(config)
    print('已放進收件匣：%s' % q['id'])
    print('網址：' + page_url(config) if up else '服務沒起來，請看 %s' % (home() / 'server.log'))
    warn_exposure(config)


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
    session = os.environ.get('CLAUDE_CODE_SESSION_ID', '')
    revive_session(session)
    if not take(session, os.getcwd(), emit=emit_answers):
        print('收件匣：沒有新的回覆。')


def cmd_wait(args):
    deadline = time.time() + args.timeout
    session = os.environ.get('CLAUDE_CODE_SESSION_ID', '')
    cwd = os.getcwd()
    project = project_of(cwd)  # 只問 git 一次，不要每 3 秒起一個行程
    while time.time() < deadline:
        if session_ended(session):  # 對話關掉後殘留的 wait：別把回答拿去印給沒人看的地方
            print('收件匣：這個對話已經結束，不再等回覆。')
            sys.exit(3)
        touch_session(session)
        if deliverable(session, cwd, project=project) and take(session, cwd, emit=emit_answers, project=project):
            return
        time.sleep(3)
    print('收件匣：等了 %d 秒沒有新回覆。' % args.timeout)
    sys.exit(2)


def cmd_hook(args):
    """給 Claude Code hook 用：stdin 是 hook 的 JSON，stdout 會進 agent 的上下文。"""
    try:
        # Windows PowerShell 5.1 寫 stdin 會先送一個 BOM，json.loads 遇到會失敗
        data = json.loads(sys.stdin.read().lstrip('\ufeff') or '{}')
    except ValueError:
        data = {}
    session = data.get('session_id', '')
    if data.get('hook_event_name') == 'SessionEnd':
        mark_ended(session)  # 只記下它結束了；這時印出來的東西 agent 已經看不到，不能把回答交給它
        return
    revive_session(session)
    take(session, data.get('cwd') or os.getcwd(), emit=emit_answers)


def cmd_list(args):
    for q in build_state()['questions']:
        if q['closed'] and not args.all:
            continue
        status = '已收起' if q['closed'] else ('已回答' if q['answers'] else '等回答')
        print('%s  %s  %s  （%s）' % (q['id'], status, q['title'], q['from']))


def is_inbox_server(pid):
    """這個 PID 是不是收件匣服務。PID 檔可能是舊的（例如重開機後號碼被別的程式拿去用）。"""
    words = command_line_of(pid).replace('"', ' ').split()
    return 'serve' in words and any(w.endswith('inbox.py') for w in words)


def command_line_of(pid):
    """那個 PID 的完整命令列；查不到（行程不在了、沒權限、工具不存在）回空字串。"""
    if IS_WINDOWS:  # Windows 沒有 ps；tasklist 只給程式名，要命令列得問 CIM
        query = "(Get-CimInstance Win32_Process -Filter 'ProcessId=%d').CommandLine" % int(pid)
        command = ['powershell', '-NoProfile', '-NonInteractive', '-Command', query]
    else:
        command = ['ps', '-p', str(int(pid)), '-o', 'command=']
    try:
        out = subprocess.run(command, capture_output=True, timeout=20, stdin=subprocess.DEVNULL,
                             encoding='utf-8', errors='replace')
    except (OSError, subprocess.SubprocessError):
        return ''
    return out.stdout.strip() if out.returncode == 0 else ''


def cmd_prune(args):
    days = args.days
    if days is None:  # 沒指定就用設定的天數；設定是「不自動清」時，手動清用預設天數
        try:
            days = keep_days_of(read_config(home() / 'config.json')) or KEEP_DAYS
        except (OSError, ValueError):
            days = KEEP_DAYS
    if days < 1:
        sys.exit('--days 至少是 1')
    print('已清掉 %d 題（收起超過 %g 天的）' % (prune(days), days))


def cmd_start(args):
    config = load_config()
    print(page_url(config) if ensure_server(config) else '啟動失敗')
    warn_exposure(config)


def cmd_url(args):
    config = load_config()
    print(page_url(config))
    warn_exposure(config)


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
    except PermissionError:
        print('沒有權限停止 PID 檔記的那個程式，沒動它')
    except (OSError, ValueError):  # 行程剛好不在了：POSIX 是 ProcessLookupError，Windows 是一般的 OSError
        print('服務本來就沒在跑')
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
    p = sub.add_parser('prune', help='刪掉收起很久的題目，讓資料不會一直變大')
    p.add_argument('--days', type=int, help='收起超過幾天才刪，預設用 config 的 keep_days（30）')
    p.set_defaults(func=cmd_prune)
    sub.add_parser('hook', help='給 Claude Code hook 用').set_defaults(func=cmd_hook)
    sub.add_parser('serve', help='前景執行網頁服務').set_defaults(func=lambda a: serve(load_config()))
    sub.add_parser('start', help='背景啟動網頁服務').set_defaults(func=cmd_start)
    sub.add_parser('stop', help='停止背景服務').set_defaults(func=cmd_stop)
    sub.add_parser('url', help='印出網址').set_defaults(func=cmd_url)
    use_utf8_stdio()
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == '__main__':
    main()
