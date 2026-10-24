"""Approval-first Telegram publisher. Python 3.12+, standard library only."""
import argparse
import datetime as dt
import fcntl
import json
import logging
import os
from pathlib import Path
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

LOG = logging.getLogger('publisher')


def acquire_worker_lock(state_dir):
    Path(state_dir).mkdir(parents=True, exist_ok=True)
    handle = (Path(state_dir) / 'publisher.lock').open('a')
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        raise ValueError('Издатель уже запущен для этой базы.') from None
    return handle  # Keep open for the entire worker lifetime.


def bot_token():
    path = os.getenv('TELEGRAM_BOT_TOKEN_FILE')
    token = Path(path).read_text().strip() if path else os.getenv('TELEGRAM_BOT_TOKEN', '')
    if not token:
        raise ValueError('Не задан токен Telegram.')
    return token


def backup_database(db, output):
    destination = Path(output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Refuse to replace any existing file, especially the live database.
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
    with sqlite3.connect(destination) as copy:
        db.backup(copy)
        if copy.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ValueError('Резервная копия не прошла проверку целостности.')


def connect(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.executescript('''
    CREATE TABLE IF NOT EXISTS posts (
      id INTEGER PRIMARY KEY, slug TEXT UNIQUE NOT NULL, title TEXT NOT NULL,
      body TEXT NOT NULL, sources TEXT NOT NULL, checklist TEXT NOT NULL,
      verified INTEGER NOT NULL DEFAULT 0,
      status TEXT NOT NULL DEFAULT 'draft', message_id INTEGER,
      error TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
    CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    ''')
    db.commit()
    return db


def setting(db, key, default=None):
    row = db.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
    return row[0] if row else default


def put(db, key, value):
    db.execute('INSERT INTO settings VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, str(value)))
    db.commit()


def seed(db, path):
    items = json.loads(Path(path).read_text())
    for item in items:
        validate_body(item['body'])
        db.execute('INSERT OR IGNORE INTO posts (slug,title,body,sources,checklist) VALUES (?,?,?,?,?)',
                   (item['slug'], item['title'], item['body'], json.dumps(item['sources'], ensure_ascii=False), item['checklist']))
    db.commit()


def validate_body(body):
    # Telegram counts UTF-16 code units; leave margin for Unicode edge cases.
    if not body.strip() or len(body.encode('utf-16-le')) // 2 > 3900:
        raise ValueError('Текст должен содержать от 1 до 3900 UTF-16 единиц.')


class APIError(Exception):
    pass


class Telegram:
    def __init__(self, token):
        self.base = 'https://api.telegram.org/bot' + token + '/'

    def call(self, method, **payload):
        request = urllib.request.Request(self.base + method, json.dumps(payload).encode(), {'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(request, timeout=40) as response:
                result = json.load(response)
        except urllib.error.HTTPError as exc:
            # Do not expose URLs or exception strings: they contain the token.
            raise APIError(f'Telegram HTTP {exc.code}; проверьте настройки и права.') from None
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            raise APIError('Сбой соединения или некорректный ответ Telegram.') from None
        if not result.get('ok'):
            raise APIError('Telegram отклонил запрос.')
        return result['result']

    def send(self, chat_id, text, **kwargs):
        return self.call('sendMessage', chat_id=chat_id, text=text, link_preview_options={'is_disabled': True}, **kwargs)


class Bot:
    def __init__(self, db, api, reviewer, channel, timezone='Europe/Moscow', hour=10):
        self.db, self.api = db, api
        self.reviewer, self.channel = int(reviewer), channel
        self.tz, self.hour = ZoneInfo(timezone), int(hour)
        if not 0 <= self.hour <= 23:
            raise ValueError('PUBLISH_HOUR должен быть от 0 до 23.')
        # A crash between send and acknowledgement is ambiguous: never resend blindly.
        self.db.execute("UPDATE posts SET status='uncertain', error='Перезапуск во время отправки: проверьте канал вручную.' WHERE status='publishing'")
        self.db.commit()

    def tell(self, text, **kwargs):
        return self.api.send(self.reviewer, text, **kwargs)

    def verification_confirmed(self, pid):
        self.tell(
            f'Проверка #{pid} подтверждена вами. Одобрите пост кнопкой ниже. '
            'Это добавит его в очередь; состояние паузы не изменится.',
            reply_markup={'inline_keyboard': [[
                {'text': 'Одобрить', 'callback_data': f'approve:{pid}'}]]})

    def preview(self, row):
        self.tell(f"Черновик #{row['id']} · {row['title']}\n\n{row['body']}")
        sources = '\n'.join(json.loads(row['sources'])) or 'Авторский учебный пример; внешних утверждений нет.'
        self.tell(f"#{row['id']} · проверка: {'подтверждена вами' if row['verified'] else 'НЕ выполнена'}\n"
                  f"{row['checklist']}\n\nИсточники:\n{sources}\n\n"
                  'После выполнения чек-листа нажмите «Проверка выполнена», затем «Одобрить». Изменить: /edit ID и новый текст со следующей строки.',
                  reply_markup={'inline_keyboard': [[
                      {'text': 'Проверка выполнена', 'callback_data': f"verify:{row['id']}"}], [
                      {'text': 'Одобрить', 'callback_data': f"approve:{row['id']}"},
                      {'text': 'На доработку', 'callback_data': f"revise:{row['id']}"},
                      {'text': 'Отклонить', 'callback_data': f"reject:{row['id']}"}]]})

    def handle(self, update):
        callback = update.get('callback_query')
        msg = callback.get('message', {}) if callback else update.get('message', {})
        actor = callback.get('from', {}) if callback else msg.get('from', {})
        if actor.get('id') != self.reviewer or msg.get('chat', {}).get('id') != self.reviewer:
            if callback:
                self.api.call('answerCallbackQuery', callback_query_id=callback['id'], text='Нет доступа.')
            return
        if callback:
            try:
                self.api.call('answerCallbackQuery', callback_query_id=callback['id'])
            except APIError:
                # Telegram expires callback acknowledgements; this must not
                # prevent handling the action or block the entire update queue.
                LOG.warning('Не удалось подтвердить кнопку; продолжаем обработку действия.')
            try:
                action, raw = callback.get('data', '').split(':', 1)
                pid = int(raw)
            except ValueError:
                return
            states = {'approve': 'approved', 'revise': 'needs_revision', 'reject': 'rejected', 'verify': 'draft'}
            if action not in states:
                return
            row = self.db.execute('SELECT * FROM posts WHERE id=?', (pid,)).fetchone()
            if not row or row['status'] not in ('draft', 'needs_revision', 'approved'):
                self.tell('Пост уже обработан или недоступен.')
                return
            if action == 'verify':
                self.db.execute('UPDATE posts SET verified=1 WHERE id=?', (pid,))
                self.db.commit()
                self.verification_confirmed(pid)
                return
            if action == 'approve' and not row['verified']:
                self.tell(f'Сначала выполните проверку, затем /verify {pid} и нажмите «Одобрить».')
                return
            self.db.execute('UPDATE posts SET status=? WHERE id=?', (states[action], pid))
            self.db.commit()
            self.tell(f'#{pid}: {states[action]}. Для доработки: /edit ID и текст со следующей строки.'
                      if action == 'revise' else f'#{pid}: {states[action]}.')
            return
        text = msg.get('text', '')
        command = text.split(maxsplit=1)[0] if text.strip() else ''
        if command in ('/start', '/help'):
            self.tell('/next — следующий черновик\n/show ID — показать пост\n/verify ID — подтвердить практическую проверку\n/edit ID\nновый текст — сохранить правку и сбросить одобрение\n/status — сводка\n/queue — список постов\n/pause — остановить публикации\n/resume — включить публикации\nНепроверенные и неодобренные посты не выходят. Доработка пока ручная.')
        elif command == '/next':
            row = self.db.execute("SELECT * FROM posts WHERE status IN ('draft','needs_revision') ORDER BY id LIMIT 1").fetchone()
            self.preview(row) if row else self.tell('Нет черновиков.')
        elif command in ('/verify', '/show', '/edit'):
            try:
                pid = int(text.split('\n', 1)[0].split()[1])
            except (ValueError, IndexError):
                self.tell('Укажите числовой ID поста.')
                return
            row = self.db.execute('SELECT * FROM posts WHERE id=?', (pid,)).fetchone()
            if not row:
                self.tell('Пост не найден.')
            elif command == '/show':
                self.preview(row)
            elif row['status'] not in ('draft', 'needs_revision', 'approved'):
                self.tell('Этот пост нельзя изменять или подтверждать.')
            elif command == '/verify':
                self.db.execute('UPDATE posts SET verified=1 WHERE id=?', (pid,))
                self.db.commit()
                self.verification_confirmed(pid)
            else:
                body = text.partition('\n')[2].strip()
                try:
                    validate_body(body)
                except ValueError as exc:
                    self.tell(str(exc))
                    return
                self.db.execute("UPDATE posts SET body=?, verified=0, status='draft' WHERE id=?", (body, pid))
                self.db.commit()
                self.tell('Правка сохранена. Проверка и одобрение сброшены.')
        elif command in ('/pause', '/resume'):
            put(self.db, 'paused', '1' if command == '/pause' else '0')
            self.tell('Публикации остановлены.' if command == '/pause' else 'Публикации включены: не более одного поста в день.')
        elif command == '/status':
            rows = self.db.execute('SELECT status, COUNT(*) AS n FROM posts GROUP BY status').fetchall()
            self.tell('Пауза: ' + setting(self.db, 'paused', '1') + '\n' + '\n'.join(f"{r['status']}: {r['n']}" for r in rows) + f'\nОкно: {self.hour:02}:00–{self.hour:02}:59 {self.tz.key}.')
        elif command == '/queue':
            labels = {'draft': 'черновик', 'needs_revision': 'доработка', 'approved': 'одобрен',
                      'published': 'опубликован', 'rejected': 'отклонён',
                      'uncertain': 'отправка не подтверждена', 'publishing': 'отправляется'}
            rows = self.db.execute('SELECT id,title,status FROM posts ORDER BY id DESC LIMIT 20').fetchall()
            self.tell('\n'.join(f"#{r['id']} · {labels.get(r['status'], r['status'])} · {r['title'][:80]}" for r in reversed(rows)) or 'Очередь пуста.')

    def tick(self, now=None):
        now = (now or dt.datetime.now(dt.timezone.utc)).astimezone(self.tz)
        day = now.date().isoformat()
        if setting(self.db, 'paused', '1') == '1' or now.hour != self.hour or setting(self.db, 'last_slot') == day:
            return
        if self.db.execute("SELECT 1 FROM posts WHERE status='uncertain' LIMIT 1").fetchone():
            return
        row = self.db.execute("SELECT * FROM posts WHERE status='approved' AND verified=1 ORDER BY id LIMIT 1").fetchone()
        if not row:
            return
        self.db.execute("UPDATE posts SET status='publishing' WHERE id=? AND status='approved'", (row['id'],))
        self.db.commit()
        # Reserve the daily slot before sending; any ambiguous delivery requires manual inspection.
        put(self.db, 'last_slot', day)
        try:
            sent = self.api.send(self.channel, row['body'])
        except APIError:
            self.db.execute("UPDATE posts SET status='uncertain', error='Отправка не подтверждена: проверьте канал.' WHERE id=?", (row['id'],))
            self.db.commit()
            self.tell(f"Отправка #{row['id']} не подтверждена. Автопубликация заблокирована. Проверьте канал; восстановление описано в README.")
            return
        self.db.execute("UPDATE posts SET status='published', message_id=? WHERE id=?", (sent['message_id'], row['id']))
        self.db.commit()
        self.tell(f"Опубликован пост #{row['id']}.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['seed', 'status', 'run', 'resolve', 'identity', 'backup'])
    parser.add_argument('--id', type=int)
    parser.add_argument('--outcome', choices=['published', 'draft'])
    parser.add_argument('--message-id', type=int)
    parser.add_argument('--output')
    args = parser.parse_args()
    db = connect(Path(os.getenv('STATE_DIR', '.state')) / 'publisher.sqlite3')
    if args.command == 'identity':
        api = Telegram(bot_token())
        for update in api.call('getUpdates', timeout=0):
            message = update.get('message', {})
            if message.get('chat', {}).get('type') == 'private':
                print('Private sender ID:', message.get('from', {}).get('id'))
        print('Выберите свой ID. Запускайте identity только при остановленном издателе.')
    elif args.command == 'backup':
        if not args.output:
            parser.error('Укажите --output с новым путём резервной копии.')
        backup_database(db, args.output)
        print('Резервная копия создана и проверена:', args.output)
    elif args.command == 'seed':
        seed(db, Path(__file__).parent / 'content/week1.json')
        print('Черновиков в базе:', db.execute('SELECT COUNT(*) FROM posts').fetchone()[0])
    elif args.command == 'status':
        for row in db.execute('SELECT id,title,status,verified FROM posts ORDER BY id'):
            print(dict(row))
    elif args.command == 'resolve':
        if not args.id or not args.outcome or (args.outcome == 'published' and not args.message_id):
            parser.error('Нужны --id, --outcome и --message-id для опубликованного поста.')
        db.execute("UPDATE posts SET status=?, message_id=?, verified=0, error=NULL WHERE id=? AND status='uncertain'", (args.outcome, args.message_id, args.id))
        db.commit()
        print('Обновлено:', db.execute('SELECT changes()').fetchone()[0])
    else:
        required = ['TELEGRAM_CHANNEL_ID', 'TELEGRAM_REVIEWER_ID']
        missing = [name for name in required if not os.getenv(name)]
        if missing:
            parser.error('Не заданы: ' + ', '.join(missing))
        if int(os.environ['TELEGRAM_REVIEWER_ID']) <= 0:
            parser.error('Укажите настоящий положительный TELEGRAM_REVIEWER_ID вместо заглушки.')
        try:
            worker_lock = acquire_worker_lock(os.getenv('STATE_DIR', '.state'))
            api = Telegram(bot_token())
        except (ValueError, OSError) as exc:
            parser.error(str(exc))
        bot = Bot(db, api, os.environ['TELEGRAM_REVIEWER_ID'], os.environ['TELEGRAM_CHANNEL_ID'], os.getenv('PUBLISH_TIMEZONE', 'Europe/Moscow'), os.getenv('PUBLISH_HOUR', '10'))
        # Long polling cannot coexist with a webhook. Never remove an existing webhook silently.
        if api.call('getWebhookInfo').get('url'):
            parser.error('У бота настроен webhook. Используйте отдельного бота или отключите webhook самостоятельно.')
        me = api.call('getMe')
        member = api.call('getChatMember', chat_id=bot.channel, user_id=me['id'])
        if member.get('status') != 'creator' and not member.get('can_post_messages'):
            parser.error('Боту требуется право публикации в канале.')
        bot.tell('Издатель запущен. /help — команды. При первом запуске публикации на паузе.')
        # One process per DB; flock is documented in README.
        while True:
            try:
                updates = api.call('getUpdates', offset=int(setting(db, 'offset', '0')), timeout=20, allowed_updates=['message', 'callback_query'])
                for update in updates:
                    bot.handle(update)
                    put(db, 'offset', update['update_id'] + 1)
                bot.tick()
            except APIError:
                LOG.warning('Ошибка Telegram; повтор опроса через 5 секунд. Статусы отправки сохранены.')
                time.sleep(5)


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    main()
