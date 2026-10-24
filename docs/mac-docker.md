# macOS + Docker Desktop: выбранный способ запуска

Ничего покупать и ставить Python не нужно. Откройте Docker Desktop и дождитесь запуска Engine. Далее — Terminal macOS.

## 1. Получить проект

Код ещё не загружен в GitHub. После разрешения владельца он будет доступен в отдельной ветке `codex/telegram-mvp` репозитория `karen140996-spec/TG`; main не меняется. До подтверждения загрузки не выполняйте clone в расчёте на наличие этой ветки.

После подтверждения:

```sh
git clone --branch codex/telegram-mvp https://github.com/karen140996-spec/TG.git
cd TG
cp deploy/runtime.env.example .env
mkdir -p .secrets backups
chmod 700 .secrets backups
nano .secrets/telegram_bot_token
```

Введите только токен. В nano: Ctrl+O, Enter, Ctrl+X. Затем:

```sh
chmod 444 .secrets/telegram_bot_token
docker compose build
```

`.secrets` остаётся 700: на хосте каталог доступен только вашему пользователю. Токен не передаётся в аргументах и не попадает в образ/Git.

## 2. Согласовать момент переключения

Пока облачный издатель работает, не запускайте локальный `run` и не включайте `/resume`. Для переключения старый процесс останавливается, создаётся свежая копия очереди и передаётся вам отдельно. История не публикуется в GitHub. Сохраните файл как `backups/publisher-transfer.sqlite3`.

```sh
docker compose run --rm -T publisher python deploy/restore.py < backups/publisher-transfer.sqlite3
docker compose run --rm publisher python app.py status
```

У поста #1 должен быть статус `published`. Если база в Docker volume уже существует — не удаляйте её: остановитесь для проверки.

## 3. Запустить и проверить

Только после остановки облачного экземпляра:

```sh
docker compose up -d
docker compose ps
docker compose logs --tail 20
```

Напишите `/queue` и `/status` боту. Пауза должна быть включена. Перезапустите контейнер и повторите проверку:

```sh
docker compose restart publisher
```

Это проверяет сохранение данных. Расписание включается отдельно вашим `/resume`, когда одобрены проверенные посты. Один пост в день, окно 10:00–10:59 по Москве. На других часовых поясах время локального показа будет отличаться.

Ноутбук должен быть включён, с открытой крышкой, Docker Desktop запущен. Для предотвращения простоя сна на время работы откройте отдельную вкладку Terminal и выполните `caffeinate -i` (остановка Ctrl+C). Закрытие крышки всё равно может остановить работу. Если ноутбук пропустил окно отправки, пост ждёт следующего дня. Автозапуск Docker Desktop после входа включается в его настройках.

Остановить бота: `docker compose stop`. Возобновить процесс: `docker compose up -d`. Не использовать `down -v`: это удаляет очередь.

Резервные копии и дальнейший перенос на VPS: docs/deployment.md. Сейчас резервные копии создаются командой, не автоматически.
