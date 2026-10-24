"""Laptop launcher: local secret entry, no shell evaluation of settings."""
import argparse
import getpass
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / '.state/runtime.json'
TOKEN = ROOT / '.secrets/telegram_bot_token'


def configure():
    if CONFIG.exists() or TOKEN.exists():
        raise ValueError('Локальные настройки уже существуют. Они не будут перезаписаны.')
    channel = input('Канал [@kontent_po_delu_ai]: ').strip() or '@kontent_po_delu_ai'
    reviewer = input('Ваш числовой ID [258034602]: ').strip() or '258034602'
    if int(reviewer) <= 0:
        raise ValueError('Нужен положительный числовой ID.')
    token = getpass.getpass('Токен BotFather (ввод скрыт): ').strip()
    if not token or ':' not in token or any(c.isspace() for c in token):
        raise ValueError('Проверьте формат токена.')
    TOKEN.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    TOKEN.parent.chmod(0o700)
    with os.fdopen(os.open(TOKEN, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), 'w') as f:
        f.write(token)
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    with CONFIG.open('x') as f:
        json.dump({'TELEGRAM_CHANNEL_ID': channel, 'TELEGRAM_REVIEWER_ID': reviewer,
                   'PUBLISH_TIMEZONE': 'Europe/Moscow', 'PUBLISH_HOUR': '10'}, f, indent=2)
    print('Настройки сохранены локально. Токен не выводился и не входит в Git.')


def environment():
    settings = json.loads(CONFIG.read_text())
    allowed = {'TELEGRAM_CHANNEL_ID', 'TELEGRAM_REVIEWER_ID', 'PUBLISH_TIMEZONE', 'PUBLISH_HOUR'}
    if set(settings) - allowed:
        raise ValueError('В runtime.json есть неизвестные ключи.')
    env = os.environ.copy()
    env.update({key: str(value) for key, value in settings.items()})
    env['TELEGRAM_BOT_TOKEN_FILE'] = str(TOKEN)
    env['STATE_DIR'] = str(ROOT / '.state')
    return env


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['configure', 'run', 'status', 'backup', 'restore'])
    parser.add_argument('--file')
    args = parser.parse_args()
    os.chdir(ROOT)
    if args.command == 'configure':
        configure()
    elif args.command == 'restore':
        if not args.file:
            parser.error('Нужен --file с резервной копией из облачной среды.')
        from deploy.restore import restore
        with open(args.file, 'rb') as f:
            restore(f, ROOT / '.state')
        print('Очередь восстановлена на паузе. Проверьте status.')
    else:
        command = [sys.executable, 'app.py', args.command]
        if args.command == 'backup':
            if not args.file:
                parser.error('Нужен --file с новым именем резервной копии.')
            command += ['--output', args.file]
        raise SystemExit(subprocess.call(command, env=environment(), cwd=ROOT))


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        print('Ошибка локальной настройки:', str(exc), file=sys.stderr)
        raise SystemExit(1)
