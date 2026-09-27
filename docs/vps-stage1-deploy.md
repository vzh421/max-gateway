# Задание для сессии, работающей с VPS: развернуть max-gateway (этапы 1–2)

Это задание подготовила сессия разработки max-gateway. Прочитай его целиком, прежде чем
что-либо делать. Отвечай пользователю на русском.

## Контекст

max-gateway — шлюз между личным MAX арбитражного управляющего Жалсанова В.В. и
официальным ботом. Сервис подключается к личному MAX, узнаёт должников по телефону и
перенаправляет их в бот. Режим `DRY_RUN=true`: **ничего не отправляется**, решения только
пишутся в журнал. Тексты — плейсхолдеры `[НЕ УТВЕРЖДЕНО]`: даже в боевом режиме такой
текст не уйдёт.

Подробности: `docs/stage1.md`, `docs/stage2.md`, контракт с ботом — `docs/bot-integration.md`.

## Жёсткие ограничения

1. **Это основной личный номер управляющего.** `DRY_RUN` не менять, `KILL_SWITCH` не
   выключать без прямого указания пользователя. Код не править.
2. **Вход по SMS (`login`) выполняет управляющий лично** в своём SSH-терминале. Ты SMS-код
   не спрашиваешь и в чат его не принимаешь.
3. **Не трогать ai4au и всё, что уже работает на сервере**: его контейнеры, compose-проекты,
   nginx, базы, порты. max-gateway — отдельный compose-проект в отдельном каталоге.
4. **Том `gateway-data` (файл сессии MAX) = полный доступ к личному MAX.** Не выводить
   содержимое, не копировать, не удалять без вопроса пользователю.
5. **Одновременно к MAX должен быть подключён только один процесс шлюза.** Если с этапа 0
   ещё работает прототип (`stage0_probe.py listen`) — остановить его до запуска сервиса.
6. `.env` в git не коммитить. Секреты пользователю в чат не выводить.

## Шаги

### 1. Код

```bash
mkdir -p /opt/max-gateway && cd /opt/max-gateway
git clone -b claude/zealous-bohr-9ocscv https://github.com/vzh421/max-gateway.git .
```

Если каталог уже есть — `git pull`. Если нет доступа к репозиторию — не обходить,
а сказать пользователю.

Проверить, что прототип этапа 0 не запущен:
```bash
pgrep -af stage0_probe || echo "прототип не запущен"
```
Если запущен — сказать пользователю и остановить его только с его согласия.

### 2. Конфигурация

```bash
cp .env.example .env
chmod 600 .env
```

Заполнить в `.env`:
- `MAX_PHONE` — номер спросить у пользователя (или пусть впишет сам);
- `POSTGRES_PASSWORD` — сгенерировать: `openssl rand -hex 24`;
- `ADMIN_TOKEN` — сгенерировать: `openssl rand -hex 24`;
- `GATEWAY_API_PORT` — проверить, что порт свободен (`ss -ltn | grep 8090`); если занят —
  взять другой свободный.

Связь с ботом (бот ведёт другая сессия; значения согласовать с пользователем):
- `BOT_USERNAME` — ник бота;
- `BOT_API_KEY` — сгенерировать `openssl rand -hex 24` и передать сессии бота;
- `BOT_NOTIFY_URL`, `BOT_NOTIFY_KEY` — эндпоинт уведомлений бота и ключ (от сессии бота).
  Пока их нет — оставить пустыми, уведомления пойдут в лог.
- Если бот в Docker на этом же сервере — общая сеть: `docker network create max-net` и
  запуск с `-f docker-compose.yml -f docker-compose.botnet.yml` (см. `docs/bot-integration.md`, раздел 4).

Остальное **не менять**: `DRY_RUN=true`, `KILL_SWITCH=false`, шаблоны
`REDIRECT_*_TEMPLATE` с меткой `[НЕ УТВЕРЖДЕНО]`.

### 3. Сборка и БД

```bash
docker compose build
docker compose up -d db
docker compose run --rm gateway db upgrade
```

### 4. Вход в MAX (делает управляющий сам)

Выдай пользователю команду для его SSH-терминала:

```bash
cd /opt/max-gateway && docker compose run --rm gateway login
```

Придёт SMS, код вводится в консоли (при включённой 2FA спросит и пароль). После входа
команда напечатает, сколько контактов вернул MAX, и заполнит белый список из адресной книги.

Попроси пользователя передать в сессию разработки строки вывода: «Контактов получено: N,
с телефоном: M» и «Белый список из адресной книги: …».

Если контактов 0 — это не ошибка входа. Попроси пользователя выполнить:
```bash
docker compose run --rm gateway contacts-sync
```
и тоже передать вывод.

### 4а. Контакты телефона и список должников (по `docs/contacts.md`)

1. Список должников — файл `case-map.json` (карта дел; пользователь передаст его или
   укажет, где он лежит на сервере). Загрузить:
   ```bash
   docker compose run --rm -v "$PWD/case-map.json:/tmp/case-map.json:ro" gateway import-debtors /tmp/case-map.json
   ```
   Для сессии разработки: пришли, если можешь, схему API ai4au (`/openapi.json`, раздел
   `GET /api/v1/cases/`: параметры пагинации, формат ответа, способ авторизации) — нужно
   для сверки списка с ai4au.
2. **После** списка должников (п. 1) — контакты телефона: пользователь выгружает их из contacts.google.com в формате «Google CSV»
   и передаёт файл на сервер. Загрузить и сразу удалить файл:
   ```bash
   docker compose run --rm -v "$PWD/contacts.csv:/tmp/contacts.csv:ro" gateway contacts import /tmp/contacts.csv
   shred -u contacts.csv 2>/dev/null || rm -f contacts.csv
   docker compose run --rm gateway contacts stats
   ```
   Передай пользователю итоговую строку. Содержимое файла не выводить. Файл спорных
   (`contacts review-export`) содержит полные номера и имена — отдать только пользователю.

### 4б. Выгрузка переписки с должниками (по запросу пользователя; `docs/history-export.md`)

Только чтение, отправок нет. Сервис на время выгрузки остановить: к MAX подключается один процесс.
```bash
mkdir -p out && chmod 700 out
docker compose stop gateway
docker compose run --rm -v "$PWD/out:/out" gateway history-export /out/history.jsonl \
    --manager-name "Жалсанов Виталий Викторович"
docker compose start gateway
```
Передай пользователю итоговую строку. Файл `out/history.jsonl` содержит персональные данные —
не выводить, отдать только пользователю (он передаст его в сессию разработки для анализа),
затем удалить с сервера. Попроси пользователя проверить на телефоне, не пропали ли непрочитанные.

### 5. Запуск

```bash
docker compose up -d gateway
sleep 20
docker compose ps
curl -s 127.0.0.1:${GATEWAY_API_PORT:-8090}/health
```

Ожидается: `"connected": true`, `"dry_run": true`, `"sends_blocked": false`.

Если `sends_blocked: true` — посмотри причину (например, сервис запускали до `login`,
или был реальный разрыв):
```bash
docker compose exec gateway python -m gateway sends status
```
Если причина — «Файла сессии нет» и она старше, чем `login`, скажи пользователю и сними
блокировку только с его согласия:
```bash
docker compose exec gateway python -m gateway sends resume
```

### 6. Проверка приёма

Попроси пользователя написать себе в личный MAX с другого номера. Затем:

```bash
docker compose logs --since 5m gateway | grep -E "Чат |ERROR|УВЕДОМЛЕНИЕ" | tail -20
docker compose exec db psql -U gateway -d gateway -c \
  "select direction, count(*) from message_log group by direction;"
```

Ожидается: строка «Чат …: <решение>» на каждое входящее, записи `in` в `message_log`.
Записи `out` возможны только со статусом `dry_run` (решение «отправил бы»), `sent` быть не должно:
```bash
docker compose exec db psql -U gateway -d gateway -c \
  "select status, dry_run, count(*) from message_log where direction='out' group by 1,2;"
```
В логах не должно быть полных номеров телефонов:
```bash
docker compose logs gateway | grep -E '(\+?7|8)[0-9]{10}' | head
```
(должно быть пусто; если нет — сообщить пользователю).

### 7. Результат для сессии разработки

Передай пользователю короткую сводку:
- вывод `login` (и `contacts-sync`, если запускали) — числа контактов;
- ответ `/health`;
- число записей в `message_log` по направлениям;
- все строки `ERROR` и `УВЕДОМЛЕНИЕ` из логов (без токенов);
- что пользователь заметил на телефоне: не разлогинило ли MAX, приходят ли уведомления.

## Если что-то пошло не так

- Контейнер `gateway` не стартует → `docker compose logs gateway | tail -50`, показать
  пользователю, ничего не править в коде.
- В логах `session_lost` → сессия MAX отозвана. Повторный `login` — только по решению
  пользователя.
- Нужно срочно остановить любые отправки (на этапе 1 их нет, но на всякий случай):
  `docker compose exec gateway python -m gateway kill on`.
- Любые сомнения — остановиться и спросить пользователя. Ничего не придумывать.
