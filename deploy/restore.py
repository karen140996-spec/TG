"""Import a checked SQLite snapshot from stdin into an EMPTY state directory."""
import os
from pathlib import Path
import sqlite3
import sys
import tempfile


def restore(stream, state_dir):
    state = Path(state_dir)
    state.mkdir(parents=True, exist_ok=True)
    target = state / 'publisher.sqlite3'
    if target.exists():
        raise ValueError('База уже существует; автоматическая замена запрещена.')
    fd, tmp = tempfile.mkstemp(prefix='restore-', dir=state)
    try:
        with os.fdopen(fd, 'wb') as out:
            while chunk := stream.read(65536):
                out.write(chunk)
        with sqlite3.connect(tmp) as db:
            if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise ValueError('Некорректная база.')
            db.execute('SELECT id,status,message_id FROM posts LIMIT 1')
            db.execute("INSERT INTO settings VALUES ('paused','1') ON CONFLICT(key) DO UPDATE SET value='1'")
        # link refuses to overwrite a target created since the initial check.
        os.link(tmp, target)
    finally:
        Path(tmp).unlink(missing_ok=True)


if __name__ == '__main__':
    restore(sys.stdin.buffer, os.getenv('STATE_DIR', '.state'))
    print('База восстановлена. Публикации на паузе. Проверьте статусы перед запуском.')
