import datetime as dt
import json
import io
from pathlib import Path
import tempfile
import unittest

from app import APIError, Bot, acquire_worker_lock, backup_database, connect, put, seed
from deploy.restore import restore


class FakeAPI:
    def __init__(self):
        self.sent = []
        self.markups = []
        self.fail = False

    def send(self, chat_id, text, **kwargs):
        if self.fail and chat_id == '@channel':
            raise APIError('test')
        self.sent.append((chat_id, text))
        self.markups.append(kwargs.get('reply_markup'))
        return {'message_id': len(self.sent)}

    def call(self, method, **kwargs):
        return True


class FlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = connect(Path(self.tmp.name) / 'test.sqlite3')
        seed(self.db, Path(__file__).parents[1] / 'content/week1.json')
        self.api = FakeAPI()
        self.bot = Bot(self.db, self.api, 42, '@channel')
        self.now = dt.datetime(2026, 10, 23, 7, 15, tzinfo=dt.timezone.utc)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def state(self):
        return self.db.execute('SELECT * FROM posts WHERE id=1').fetchone()

    def message(self, text, user=42):
        self.bot.handle({'message': {'from': {'id': user}, 'chat': {'id': user}, 'text': text}})

    def approve(self, user=42):
        self.bot.handle({'callback_query': {'id': 'q', 'from': {'id': user}, 'message': {'chat': {'id': user}}, 'data': 'approve:1'}})

    def test_approval_requires_verification_and_owner(self):
        self.approve()
        self.assertEqual(self.state()['status'], 'draft')
        self.message('/verify 1', user=77)
        self.assertEqual(self.state()['verified'], 0)
        self.message('/verify 1')
        self.approve(user=77)
        self.assertEqual(self.state()['status'], 'draft')
        self.approve()
        self.assertEqual(self.state()['status'], 'approved')

    def test_edit_resets_both_gates_and_seed_preserves_edit(self):
        self.message('/verify 1')
        self.approve()
        self.message('/edit 1\nИсправленный текст')
        seed(self.db, Path(__file__).parents[1] / 'content/week1.json')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM posts').fetchone()[0], 7)
        self.assertEqual(self.state()['body'], 'Исправленный текст')
        self.assertEqual(self.state()['verified'], 0)
        self.assertEqual(self.state()['status'], 'draft')

    def test_expired_callback_ack_does_not_block_approval(self):
        self.message('/verify 1')
        def expired(method, **kwargs):
            raise APIError('expired callback')
        self.api.call = expired
        self.approve()
        self.assertEqual(self.state()['status'], 'approved')

    def test_verify_button_is_separate_from_approval(self):
        self.bot.handle({'callback_query': {'id': 'v', 'from': {'id': 42},
                         'message': {'chat': {'id': 42}}, 'data': 'verify:1'}})
        self.assertEqual(self.state()['verified'], 1)
        self.assertEqual(self.state()['status'], 'draft')
        self.assertEqual(self.api.markups[-1]['inline_keyboard'][0][0],
                         {'text': 'Одобрить', 'callback_data': 'approve:1'})
        self.approve()
        self.assertEqual(self.state()['status'], 'approved')

    def test_verify_command_returns_approval_button(self):
        self.message('/verify 1')
        self.assertEqual(self.api.markups[-1]['inline_keyboard'][0][0]['callback_data'], 'approve:1')
        self.assertEqual(self.state()['status'], 'draft')

    def test_pause_window_and_daily_limit(self):
        self.db.execute("UPDATE posts SET status='approved', verified=1")
        self.db.commit()
        self.bot.tick(self.now)
        self.assertFalse(self.api.sent)
        self.message('/resume')
        self.bot.tick(self.now.replace(hour=6))
        self.assertFalse(any(chat == '@channel' for chat, _ in self.api.sent))
        self.bot.tick(self.now)
        self.bot.tick(self.now)
        self.assertEqual(sum(chat == '@channel' for chat, _ in self.api.sent), 1)
        self.bot.tick(self.now + dt.timedelta(days=1))
        self.assertEqual(sum(chat == '@channel' for chat, _ in self.api.sent), 2)

    def test_uncertain_delivery_blocks_queue_and_restart(self):
        self.db.execute("UPDATE posts SET status='approved', verified=1")
        self.db.commit()
        put(self.db, 'paused', '0')
        self.api.fail = True
        self.bot.tick(self.now)
        self.assertEqual(self.state()['status'], 'uncertain')
        self.api.fail = False
        bot = Bot(self.db, self.api, 42, '@channel')
        bot.tick(self.now + dt.timedelta(days=1))
        self.assertFalse(any(chat == '@channel' for chat, _ in self.api.sent))

    def test_crash_recovery_and_published_immutable(self):
        self.db.execute("UPDATE posts SET status='publishing' WHERE id=1")
        self.db.commit()
        Bot(self.db, self.api, 42, '@channel')
        self.assertEqual(self.state()['status'], 'uncertain')
        self.db.execute("UPDATE posts SET status='published' WHERE id=1")
        self.db.commit()
        old = self.state()['body']
        self.message('/edit 1\nДругой текст')
        self.approve()
        self.assertEqual(self.state()['body'], old)
        self.assertEqual(self.state()['status'], 'published')

    def test_backup_restores_published_state_and_refuses_overwrite(self):
        self.db.execute("UPDATE posts SET status='published',message_id=2 WHERE id=1")
        self.db.commit()
        target = Path(self.tmp.name) / 'backup.sqlite3'
        backup_database(self.db, target)
        with self.assertRaises(FileExistsError):
            backup_database(self.db, target)
        restored = connect(target)
        try:
            row = restored.execute('SELECT status,message_id FROM posts WHERE id=1').fetchone()
            self.assertEqual(tuple(row), ('published', 2))
            bot = Bot(restored, self.api, 42, '@channel')
            put(restored, 'paused', '0')
            bot.tick(self.now)
            self.assertFalse(any(chat == '@channel' for chat, _ in self.api.sent))
        finally:
            restored.close()

    def test_second_worker_cannot_lock_same_state(self):
        handle = acquire_worker_lock(self.tmp.name)
        try:
            with self.assertRaises(ValueError):
                acquire_worker_lock(self.tmp.name)
        finally:
            handle.close()
        acquire_worker_lock(self.tmp.name).close()

    def test_queue_private_to_reviewer(self):
        self.message('/queue', user=77)
        self.assertFalse(self.api.sent)
        self.message('/queue')
        self.assertIn('#1', self.api.sent[-1][1])
        self.assertIn('черновик', self.api.sent[-1][1])

    def test_restore_pauses_and_keeps_publication_history(self):
        self.db.execute("UPDATE posts SET status='published',message_id=2 WHERE id=1")
        self.db.commit()
        put(self.db, 'paused', '0')
        source = Path(self.tmp.name) / 'snapshot.sqlite3'
        backup_database(self.db, source)
        destination = Path(self.tmp.name) / 'restored'
        restore(io.BytesIO(source.read_bytes()), destination)
        restored = connect(destination / 'publisher.sqlite3')
        try:
            self.assertEqual(restored.execute("SELECT value FROM settings WHERE key='paused'").fetchone()[0], '1')
            self.assertEqual(restored.execute('SELECT message_id FROM posts WHERE id=1').fetchone()[0], 2)
        finally:
            restored.close()
        with self.assertRaises(ValueError):
            restore(io.BytesIO(source.read_bytes()), destination)

    def test_invalid_snapshot_never_installs_a_database(self):
        destination = Path(self.tmp.name) / 'bad-restore'
        with self.assertRaises(Exception):
            restore(io.BytesIO(b'not a sqlite database'), destination)
        self.assertFalse((destination / 'publisher.sqlite3').exists())


if __name__ == '__main__':
    unittest.main()
