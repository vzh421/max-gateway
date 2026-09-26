# Этап 0: сравнение PyMax (maxapi-python 2.4.1) и maxion 0.2.1 по исходникам

Дата: 2026-09-26. Метод: `pip download --no-deps --no-binary :all:` (обе библиотеки есть в sdist), распаковка,
чтение кода. Код библиотек **не запускался** (ни клиенты, ни тесты), к серверам MAX не подключались.
Метаданные релизов взяты из `https://pypi.org/pypi/<pkg>/json`.

Исходники: скачанные sdist с PyPI (`pip download --no-deps --no-binary :all: maxapi-python==2.4.1 maxion==0.2.1`).
Ниже пути сокращены:
- **P/** = `libs/maxapi_python-2.4.1/src/pymax/`
- **M/** = `libs/maxion-0.2.1/maxion/`

---

## 1. Краткий итог

**Рекомендация: брать PyMax (`maxapi-python==2.4.1`) для `PyMaxTransport`.** maxion не брать даже для бота.

Почему:
1. У PyMax есть явный колбэк разрыва соединения `on_disconnect(exc, reconnect, delay)` и обработчик ошибок
   `on_error` (P/base.py:334-340, P/dispatch/dispatcher.py:338-359). У maxion сигнала о разрыве или слёте
   сессии нет: переподключение идёт бесконечно и молча (M/raw/client.py:506-536). Для правила 9 (мониторинг)
   это критично.
2. SMS-код и пароль 2FA в PyMax вводятся через подключаемые провайдеры (`SmsCodeProvider`,
   `PasswordProvider`, P/auth/providers.py:8-47). В высокоуровневом `maxion.Client` код читается только
   через `input()`: параметра для кода нет (M/client.py:119, M/raw/client.py:310-314).
3. В maxion по коду видны дефекты, которые ломают наш сценарий S1 (разобраны в разделе 4.2):
   - у входящего сообщения `chat_id` берётся из вложенного `message`, хотя сервер кладёт `chatId` уровнем
     выше. В собственном тесте библиотеки payload именно такой (tests/test_client.py:140), так что
     `message.chat` и фильтр `filters.private` не сработают;
   - признак «редактирование» определяется по наличию `prevMessageId` (M/raw/events.py:77-79).
4. PyMax зрелее: 37 релизов с 2025-10-09, pydantic-модели, `py.typed`, CI на ruff и pytest под
   Python 3.10–3.14, 154 теста. У maxion 9 релизов, все выпущены за ~4 часа 29–30.08.2026, после этого
   обновлений нет; 108 тестов, `py.typed` нет.
5. Bot API в maxion указан с базовым URL `https://botapi.max.ru` (M/bot/client.py:35). В CLAUDE.md этот
   домен помечен как устаревший. Поля `payload` у `BotStarted` нет, его можно достать только через `raw`.

**Обязательные настройки PyMax для нашего случая** (обоснование в разделах 3 и 7):
- `ExtraConfig(telemetry=False)`: по умолчанию телеметрия **включена** (P/config.py:267);
- свой `AuthFlow` для демона, который бросает исключение вместо запроса SMS;
- `log_level` не выше `INFO`: на DEBUG библиотека пишет в лог полные payload, включая токен и тексты
  (P/app.py:300, 384);
- права 600 на файл сессии выставлять самим: библиотека их не ставит (в P/ нет `chmod`).

---

## 2. Сравнительная таблица

| Критерий | PyMax (maxapi-python 2.4.1) | maxion 0.2.1 |
|---|---|---|
| Лицензия | MIT (LICENSE:1-3; pyproject.toml:8) | MIT (LICENSE:1-3; pyproject.toml:11) |
| Класс userbot | `pymax.Client` (TCP, SMS), `pymax.WebClient` (WS, только QR) (P/client.py:26; P/client_web.py) | `maxion.Client` (высокий уровень) поверх `maxion.raw.MaxClient` (M/client.py:38; M/raw/client.py:49) |
| Ввод SMS-кода | `SmsCodeProvider.get_code(phone)`, по умолчанию консоль (P/auth/providers.py:8-36) | `raw.start(code=str\|callable)`; в `Client.start` только `input()` (M/raw/client.py:277-317; M/client.py:119) |
| 2FA-пароль | Да, `PasswordProvider`, лимит `password_max_attempts` (P/auth/sms.py:81-86, 113-153) | Да, `password=` или `input()` (M/raw/client.py:321-328) |
| Сессия | SQLite `work_dir/session_name` (по умолчанию `./session.db`), таблица `sessions` (P/session/store.py:81-113) | JSON `workdir/<name>.session`, атомарная запись, chmod 600 (M/raw/session.py:54-74; M/client.py:85-86) |
| Транспорт по умолчанию | TCP+TLS `api2.oneme.ru:443`, свой root CA (P/config.py:157, 248-249; P/transport/tcp.py:21-24) | `Client`: TCP+TLS `api.oneme.ru:443`; `MaxClient`: WS `wss://ws-api.oneme.ru/websocket` (M/raw/const.py:7-11; M/client.py:61) |
| Прокси | Да, SOCKS/HTTP через python-socks (P/transport/tcp.py:38-41) | Только для WS (M/raw/transport/ws.py:28-62); у TCP параметра нет |
| Сигнал о разрыве | `on_disconnect(exc, reconnect, delay)`, `on_error`, `is_connected` | Нет. Есть только `is_connected` и событие `reconnect` на серверный опкод RECONNECT |
| Переподключение | Да, фиксированная пауза `reconnect_delay=1.0` (P/base.py:220-242) | Да, 1/2/5/10/15/30 с, затем бесконечно раз в 30 с (M/raw/const.py:44; M/raw/client.py:506-536) |
| Подписка на входящие | `@client.on_message(*filters)`, handler `(message, client)` (P/base.py:271-276) | `@app.on_message(filters)`, handler `(client, message)` (M/client.py:175) |
| Признак «своё сообщение» | Встроенного нет: сравнивать `message.sender == client.me.contact.id` | `message.outgoing` (M/raw/types/message.py:164-170) |
| Тип чата в событии | Нет. Искать в `client.chats` или `get_chat()`, поле `Chat.type` (DIALOG/CHAT/CHANNEL) | Нет. `filters.private` берёт чат из кеша (M/filters.py:101-106; M/types.py:226-231) |
| Телефон отправителя в событии | Нет (в модели `Message` нет такого поля) | Нет |
| Профиль по id | `get_user(id)` / `fetch_users(ids)` → `User` с полем `phone: int\|None` (P/infra/user.py:33-53; P/types/domain/user.py:71) | `get_users(ids)` → `User.phone: str\|None` (M/client.py:436-444; M/raw/types/user.py:24-27) |
| Отправка | `send_message(chat_id, text=None, reply_to=None, attachments=None, *, notify=True, send_at=None)` (P/infra/message.py:19-28) | `send_message(chat_id, text, *, parse_mode, entities, reply_to_message_id, disable_notification, **kw)` (M/client.py:224-234) |
| Авто-«прочитано» | Нет. `read_message` вызывается только явно из `Message.read()` (P/types/domain/message.py:416-429) | Нет. Только явно: `Message.read()`, `Chat.read_all()`, `read_chat_history()` |
| Список контактов | Только `client.contacts`, заполняется при login; метода CONTACT_LIST нет (P/app.py:199-206) | `get_contacts()` → CONTACT_LIST постранично (M/client.py:446-447; M/raw/methods/contacts.py:111-138) |
| Фоновые действия | PING каждые 30 с с `interactive=True`; **телеметрия (Opcode.LOG) включена по умолчанию** | PING каждые 30 с с `interactive=False`, но только после входа по SMS или после reconnect |
| Сторонние хосты | `hashes.pymax.org` только при `VersionCatalog(remote=True)`, по умолчанию выключено (P/versions/catalog.py:244-267) | Нет (кроме Bot API и модуля звонков `*.okcdn.ru`) |
| Официальный Bot API | Нет (есть только `RequestInitData` мини-приложений, P/api/bots/service.py:25-29) | Есть `maxion.bot.Bot`, `BASE_URL="https://botapi.max.ru"` |
| Типизация | pydantic v2, `py.typed`, конфиги pyright и mypy (pyproject.toml:83-120) | Обёртки над dict со свойствами, `py.typed` нет |
| Тесты / CI | 154 `def test_`, CI с ruff и pytest на 3.10–3.14 (.github/workflows/tests.yml) | 108 `def test_`, CI в sdist нет |
| Релизы (PyPI) | 37 версий: 1.0.1 от 2025-10-09 … 2.4.0 от 2026-08-04, 2.4.1 от 2026-08-24 | 9 версий: 0.1.0 от 2026-08-29 22:42 … 0.2.1 от 2026-08-30 02:56 |
| Зависимости | aiofiles, aiohttp, aiosqlite, msgpack, pydantic, python-socks, qrcode, websockets, zstandard (pyproject.toml:25-35) | websockets, aiohttp, msgpack, lz4; опционально aiortc, mitmproxy, androguard (pyproject.toml:28-45) |

---

## 3. PyMax (maxapi-python 2.4.1): подробно

### 3.1 Лицензия, структура, клиент
- MIT, «Copyright (c) 2025 ink-developer» (LICENSE). Пакет `pymax` в `src/pymax`: api/, auth/, connection/,
  dispatch/, protocol/{tcp,ws}/, session/, telemetry/, transport/, types/, versions/. Python ≥3.10
  (pyproject.toml:6).
- Клиент для личного аккаунта по телефону: `pymax.Client`, TCP (P/client.py:26). Конструктор
  (P/client.py:54-65):
  ```python
  Client(phone: str, session_name: str = "session.db", work_dir: str = ".",
         extra_config: ExtraConfig | None = None, auth_flow: AuthFlow | None = None,
         sms_code_provider: SmsCodeProvider | None = None,
         password_provider: PasswordProvider | None = None,
         app_version: str = VersionCatalog.RECOMMENDED_APP_VERSION,  # "26.25.0"
         catalog: VersionCatalog | None = None)
  ```
- `WebClient` работает по WebSocket и входит **только по QR** (P/client_web.py:20-37). Для нас не подходит.
- Запуск: `await client.start()` — вечный цикл с переподключением (P/base.py:193-248). Второй вариант —
  `await client.connect()`: одно подключение без переподключения (P/base.py:165-191). Есть также
  `close()`, `stop()` и `async with`.

### 3.2 Авторизация
- Сценарий: `SmsAuthFlow.authenticate` (P/auth/sms.py:55-111) вызывает `request_code(phone)`, затем
  `code_provider.get_code(phone)`, затем `send_code(token, code)`. Если сервер вернул
  `password_challenge`, идёт цикл `password_provider.get_password(hint)` → `check_password` (sms.py:113-153).
  Если вернул `register_token`, вызывается регистрация через `RegistrationConfig` (sms.py:87-97).
- Провайдеры по умолчанию читают из консоли: `ConsoleSmsCodeProvider` через `input()` в отдельном потоке,
  `ConsolePasswordProvider` через `getpass` (P/auth/providers.py:20-47).
- Лимит попыток 2FA: `ExtraConfig.password_max_attempts`. При `None` лимита нет (P/config.py:257). Когда
  лимит исчерпан, бросается `PasswordAttemptsExceededError` (P/auth/exceptions.py).
- Вход по готовому токену: `ExtraConfig.token` (P/app.py:118-127).
- **Сессия:** `SessionStore(work_dir, db_name)` создаёт каталог и SQLite-файл `work_dir/session_name`
  (P/session/store.py:81-84). Таблица `sessions` содержит поля `token` (PK), `device_id`, `phone`,
  `mt_instance_id`, `chats_sync`, `contacts_sync`, `drafts_sync`, `presence_sync`, `config_hash` и
  `user_agent` (JSON профиля устройства) (store.py:100-112). Загружается первая строка
  (`LIMIT 1`, store.py:187-208). Путь задаётся через `work_dir` и `session_name`. Можно передать своё
  хранилище `ExtraConfig.store` или отключить сохранение `persist_session=False` (P/app.py:41-45).
  Права на файл библиотека **не выставляет**.
- **Отзыв токена.** Если login вернул `FAIL_LOGIN_TOKEN` или `FAIL_LOGOUT_ALL` (P/app.py:220-228) и
  `relogin=True` (по умолчанию, P/config.py:256), `start()` удаляет сессию из БД и снова запускает
  auth_flow (P/base.py:212-219, 346-369). `SmsAuthFlow` **сначала запрашивает SMS** и только потом
  спрашивает код (sms.py:73-75). В демоне это приведёт к автоматической отправке SMS на номер управляющего
  и к зависанию на `input()`.
  Если поставить `relogin=False`, то по чтению кода `start()` зациклится: после этой ошибки соединение не
  закрывается, и следующий виток цикла сразу повторяет handshake и login без паузы (base.py:199-219,
  app.py:173-175). **Это не проверено запуском.**
  **Вывод:** в демоне передавать свой `AuthFlow`, который сразу бросает исключение («сессия слетела»).
  Тогда `start()` пробросит его наружу (base.py:243-245), и можно уведомить управляющего. Для команды
  `python -m gateway login` использовать `SmsAuthFlow` с консольными провайдерами.

### 3.3 Транспорт
- TCP+TLS к `api2.oneme.ru:443` (P/config.py:157, 248-249). Используется встроенный корневой
  сертификат `_data/rootca_ssl_rsa2022.crt` (P/transport/tcp.py:21-24). Протокол версии 10: msgpack,
  сжатие LZ4 и zstd (P/protocol/tcp/protocol.py:16-29).
  В WebClient адрес `wss://api.oneme.ru/websocket` с Origin `https://web.max.ru`
  (P/config.py:250; P/transport/websocket.py:17-28).
- Профиль устройства: случайный Android-телефон из списка `ANDROID_DEVICES` и случайная часовая зона из
  `LOCALE_TIMEZONES` (P/config.py:59-108, 273-299). `app_version` по умолчанию 26.25.0, fingerprint
  берётся из встроенного `_data/apk_fingerprints.json` (P/versions/catalog.py:243-249).
  `device_id` — 16 hex-символов (P/config.py:116-117). После первого входа `device_id`,
  `mt_instance_id` и `user_agent` сохраняются в сессии и восстанавливаются при следующих запусках
  (P/app.py:79-95, 101-105; P/session/models.py:24-37).
- Keepalive: `PING` каждые 30 с с `{"interactive": config.interactive}`, по умолчанию `True`
  (P/app.py:317-330; P/config.py:168). Если пинг не прошёл, соединение помечается упавшим (app.py:328-330).
  `client.set_presence(online=False)` переключает `interactive` (P/api/self/service.py:156-159;
  P/infra/self.py:139).
- **Переподключение и сигналы:**
  - сетевые ошибки `ConnectionError`, `EOFError`, `OSError`, `TimeoutError` вызывают
    `emit_disconnect(exc, reconnect, delay)`, затем пауза `reconnect_delay` (1.0 с) и новый runtime
    (P/base.py:220-242);
  - обработчик регистрируется так: `@client.on_disconnect()`, сигнатура `handler(exception, reconnect, delay)`
    (P/dispatch/router.py:159-170; dispatcher.py:338-359);
  - `@client.on_error(scope)` ловит ошибки в обработчиках и при старте (P/base.py:334-336;
    dispatcher.py:304-336);
  - свойство `client.is_connected` (P/base.py:82-84);
  - исключения: `PyMaxError`, `ApiError(opcode, error, message, localized_message, title, payload)`,
    `UploadError` (P/exceptions.py). Отзыв токена приходит как `ApiError` с `error` `FAIL_LOGIN_TOKEN`
    или `FAIL_LOGOUT_ALL`.

### 3.4 Входящие сообщения
- Подписка: `@client.on_message(*filters)`, фильтры — callable от события, могут быть async. Handler
  вызывается как `callback(event, client)` (P/base.py:271-276; P/dispatch/dispatcher.py:276-302). Есть
  также `on_message_edit`, `on_message_delete`, `on_message_read`, `on_chat_update`, `on_raw`, `on_start`
  (P/base.py:267-340).
- Источник: опкод `NOTIF_MESSAGE`. Статус `EDITED` превращается в `MESSAGE_EDIT`, статус `REMOVED` — в
  `MESSAGE_DELETE`, всё остальное считается `MESSAGE_NEW` (P/dispatch/resolvers.py:61-76). Payload
  валидируется в `Message` (P/dispatch/mapping.py:43-51). Валидатор раскрывает обёртку
  `{chatId, message:{...}, prevMessageId, ttl, unread, mark}` в плоский объект
  (P/types/domain/message.py:489-514).
- **Поля `Message`** (P/types/domain/message.py:223-241; модель допускает лишние поля, `extra="allow"`,
  P/types/domain/base.py):
  - `id: int`, `chat_id: int|None`, `sender: int|None` (id отправителя);
  - `text: str`, `time: int` (Unix time; единица по коду не установлена, в примере на строке 142
    значение в мс), `type: str`, `cid`;
  - `attaches: list[Attachment]` — фото, видео, файл, контакт, стикер, аудио, control, inline-клавиатура,
    share, звонок, опрос или `UnknownAttachment`;
  - `stats`, `status`, `reaction_info`, `options`, `prev_message_id`, `ttl`, `unread`, `mark`,
    `elements`, `delayed_attributes`, `link` (ReplyLink или ForwardLink).
- **Телефона отправителя в событии нет.** Способы получить профиль:
  - `await client.get_user(user_id)` берёт из кеша или делает запрос `CONTACT_INFO`;
  - `await client.fetch_users([ids])` всегда делает запрос `CONTACT_INFO` (P/infra/user.py:33-53;
    P/api/users/service.py:58-75).
  Возвращается `User` с полями `id`, `names[]` (name, first_name, last_name, type), `phone: int|None`
  («если возвращен API»), `options: list[str]`, `link`, `description`, `account_status` и др.
  (P/types/domain/user.py:61-77).
  Обратный поиск: `search_by_phone(phone)` → `User` через `CONTACT_INFO_BY_PHONE`
  (P/api/users/service.py:77-90).
- **Своё или чужое:** встроенного флага нет, в пакете нет ни `outgoing`, ни `incoming`. Нужно сравнивать
  `message.sender` с `client.me.contact.id`; `me: Profile{contact: User}` (P/types/domain/profile.py).
- **Тип чата:** в `Message` его нет. Есть `Chat.type: ChatType | str`, значения `DIALOG`, `CHAT`,
  `CHANNEL` (P/types/domain/enums.py:4-9; P/types/domain/chat.py:101-102). Источники:
  - `client.chats` — список, полученный при login;
  - `await client.get_chat(chat_id)` (P/infra/chat.py:205-217).
  Id личного чата библиотека вычисляет как `my_id ^ user_id` (P/api/users/service.py:138-139). Это
  годится как дополнительная проверка, но на реальных данных не проверено.
  **Бот:** в `Chat` есть `has_bots` (chat.py:126). Признака «пользователь — бот» в `User` нет; возможно,
  он лежит в `User.options` (у maxion ищут `"BOT"` в options, M/raw/types/user.py:81-83). Не проверено.

### 3.5 Отправка и «прочитано»
- Сигнатура (P/infra/message.py:19-28):
  ```python
  send_message(chat_id: int, text: str | None = None, reply_to: int | None = None,
               attachments=None, *, notify: bool = True, send_at=None) -> Message
  ```
  Текст всегда проходит через `Formatter.format_markdown` (P/api/messages/service.py:209-212). Символы
  markdown в шаблоне сообщения будут интерпретированы как разметка. Есть также `message.answer()` и
  `message.reply()`.
- «Прочитано»: `read_message(message_id, chat_id)` отправляет `CHAT_MARK` с типом `READ_MESSAGE`
  (P/api/messages/service.py:554-567). Вызывается только из `Message.read()`
  (P/types/domain/message.py:416-429) и из публичного `client.read_message` (P/infra/message.py:339).
  **В приёме и диспетчеризации входящих автоматических вызовов нет.** Проверено grep по
  `read_message|CHAT_MARK` и чтением P/connection/connection.py:204-238 и P/dispatch/dispatcher.py:236-274.
  Ответ-подтверждение на входящие `NOTIF_*` клиент тоже не отправляет.

### 3.6 Адресная книга
- `client.contacts: list[User | None]` заполняется при login из `login_response.contacts` или из LOGIN2
  (P/app.py:199-206; P/base.py:72-75). Отдельного метода «получить список контактов» (опкод
  `CONTACT_LIST=36` есть в P/protocol/enums.py:40) в API **нет**.
- Маркеры синхронизации (`contacts_sync` и др.) сохраняются в сессии после каждого login
  (P/api/auth/service.py:272-285). Поэтому при последующих запусках сервер, вероятно, отдаёт только
  изменения. Полный список для белого списка надёжнее снять при первом входе или через
  `ExtraConfig(sync=SyncOverrides(contacts_sync=...))` (P/types/domain/sync.py). Какое значение даёт полную
  выгрузку, **не проверено**.
- Прочие методы: `add_contact`, `remove_contact`, `import_contacts(list[ContactInfo(phone, first_name, last_name)])`
  (P/infra/user.py:75-106). `import_contacts` изменяет адресную книгу, нам не нужен.

### 3.7 Что библиотека отправляет сама
- `PING` каждые 30 с (см. 3.3).
- **Телеметрия включена по умолчанию** (`ExtraConfig.telemetry=True`, P/config.py:267; передаётся в
  P/base.py:108). `TelemetryService` запускается после старта (P/app.py:62, 217-218). Через 15–90 с и
  затем каждые 15–45 мин она шлёт на сервер MAX (`Opcode.LOG`) **имитацию действий пользователя**:
  переходы по экранам и «open_chat» со случайным чатом из `app.chats` (P/telemetry/service.py:32-37,
  82-99, 130-158, 180-196). По коду это не `CHAT_MARK`. Влияет ли это на статус прочтения или на
  уведомления на телефоне, **не проверено**. Рекомендация — `telemetry=False`.
- URL в коде (grep `https?|wss?://`):
  - `wss://api.oneme.ru/websocket` (P/config.py:250);
  - Origin `https://web.max.ru` (P/transport/websocket.py:21, 27);
  - `https://hashes.pymax.org/versions.json`, только при `VersionCatalog(remote=True)`, по умолчанию
    `False` (P/versions/catalog.py:244-267);
  - пример ссылки в строке-комментарии (P/types/domain/message.py:149).
  Хосты: `api2.oneme.ru` (P/config.py:157, 248). Загрузка файлов идёт по URL, которые выдаёт сервер.
- Автоответов нет.

### 3.8 Качество
- 37 релизов на PyPI (1.0.1 от 2025-10-09 … 2.4.1 от 2026-08-24). Мажорная 2.0.0 вышла 2026-05-23: API
  менялся, версию нужно зафиксировать.
- Модели на pydantic v2, `py.typed`, настроены pyright и mypy (pyproject.toml:83-120).
- CI: ruff format/check и pytest на Python 3.10–3.14 (.github/workflows/tests.yml:34-63). 154 теста
  (сумма `def test_` в tests/). В sdist есть документация docs/*.rst.

---

## 4. maxion 0.2.1: подробно

### 4.1 Лицензия, структура, клиент
- MIT, «Copyright (c) 2026 PureAholy». Пакет `maxion`: client.py, filters.py, types.py (высокий уровень),
  raw/ (протокол, 153 опкода, транспорты, методы), bot/ (Bot API), calls/ (звонки).
  В `__init__.py:43` указано `__version__ = "0.1.0"` при версии пакета 0.2.1 — небрежность в релизах.
- Конструктор высокоуровневого клиента (M/client.py:54-67):
  ```python
  Client(name: str = "my_account", *, phone_number: str | None = None, password: str | None = None,
         workdir: str | os.PathLike = ".", transport: str = "tcp", device_model: str | None = None,
         app_version: str | None = None, parse_mode: ParseMode = ParseMode.DEFAULT,
         device: Device | str | None = None, **kwargs)   # kwargs уходят в MaxClient
  ```
  Низкоуровневый клиент (M/raw/client.py:59-72):
  ```python
  MaxClient(session=None, *, transport="ws", device=None, device_id=None, user_agent=None,
            auto_reconnect=True, ping_interval=30.0, request_timeout=30.0, router=None, **transport_kwargs)
  ```

### 4.2 Авторизация
- `MaxClient.start(phone=None, *, code=None, password=None)` (M/raw/client.py:277-332):
  - если в сессии есть токен, вызывается `login_by_token()`;
  - при **любом** `RpcError` или `SessionExpiredError` токен стирается из файла (`session.clear()`, с
    записью на диск) (M/raw/client.py:292-299; M/raw/session.py:76-80);
  - затем `request_code(phone or session.phone)`: SMS уходит автоматически, а телефон сохранён в сессии
    с прошлого входа (M/raw/methods/auth.py:110, 419-427);
  - затем `input()` или `code`.
  В демоне это означает автоматический запрос SMS на номер управляющего при любой ошибке логина.
- `code` может быть строкой или callable, в том числе async. В `Client.start` его передать нельзя: только
  `phone` и `password` (M/client.py:119).
- 2FA: `sign_in` бросает `TwoFactorRequired`, затем `login_check_password(password, trackId)`
  (M/raw/methods/auth.py:135-137, 251-263). Если сервер не вернул токен, ошибки нет, а профиль всё равно
  присваивается (auth.py:259-263; client.py:330).
- Вход по номеру возможен только с android/ios/desktop-профилем: web его не поддерживает
  (M/raw/device.py:201-204; auth.py:80-109).
- **Сессия:** JSON `workdir/<name>.session`, по умолчанию `./my_account.session` (M/client.py:85-86).
  Поля: `token`, `device_id` (uuid4), `phone`, `user_id`, `name`, `extra`. Запись атомарная, права 0600
  (M/raw/session.py:22-27, 54-74).
- После входа **по токену** keepalive-пинг не запускается: `return` на M/raw/client.py:296 стоит раньше
  `_start_ping()` на строке 331. Пинг появится только после reconnect (client.py:531). Как это скажется
  на практике, не проверено.

### 4.3 Транспорт
- Константы (M/raw/const.py):
  - `WS_URL="wss://ws-api.oneme.ru/websocket"`, `TCP_HOST="api.oneme.ru"`, `TCP_PORT=443`,
    `WEB_ORIGIN="https://web.max.ru"`;
  - `RPC_VERSION_TCP=10`, `APP_VERSION_ANDROID="26.28.0"`;
  - `PING_INTERVAL=30`, `RECONNECT_DELAYS=(1,2,5,10,15,30)`.
  Хост TCP **отличается** от PyMax (`api2.oneme.ru`). Какой из них правильный, не проверено.
- Профиль Android фиксированный: «Xiaomi Redmi Note 12», Android 13, 26.29.1, build 6808
  (M/raw/device.py:45-47, 105-135). `clientSessionId` и `mt_instanceid` генерируются заново **при каждом
  подключении** и в сессии не сохраняются (device.py:233-244).
- Сигналы о разрыве:
  - ошибка приёма `TransportError` запускает `_reconnect()` (M/raw/client.py:403-408);
  - неудачный пинг тоже (client.py:496-500);
  - переподключение бесконечное, каждая неудача только пишется в лог warning (client.py:517-536);
  - отдельного события или колбэка о разрыве либо слёте токена при переподключении **нет**;
  - `run_until_disconnected()` возвращает управление только при `auto_reconnect=False` или по сигналу
    ОС (client.py:376-393, 506-509);
  - есть событие `reconnect`, но только для серверного опкода `RECONNECT` (M/raw/events.py:337-344);
  - исключения: `MaxError` → `TransportError`/`NotConnectedError`, `TimeoutError_`,
    `AuthError`/`NotAuthorizedError`/`SessionExpiredError`/`TwoFactorRequired`,
    `RpcError(opcode, payload)` → `FloodWaitError` (M/raw/errors.py).

### 4.4 Входящие сообщения
- Подписка: `@app.on_message(filters=None, group=0)`, handler `(client, message)`. Есть готовые фильтры
  `filters.private`, `incoming`, `outgoing`, `command` и др. (M/client.py:175-217; M/filters.py:92-108).
- Событие `NewMessage` (M/raw/events.py:54-91): `message` = `Message(payload["message"])`,
  `chat_id` берётся из `payload["chatId"]`, `is_edit` = `bool(prevMessageId or edited)`.
- Поля `raw.Message`, все это property над dict (M/raw/types/message.py:58-170):
  - `id: str`, `chat_id` (из `message.chatId`), `sender_id` (`sender|senderId|userId`), `cid`;
  - `text`, `elements`, `attaches`, `type`, `status`, `is_deleted`, `is_edited`, `is_system`;
  - `time: datetime` (мс в UTC), `link`, `reply_to`, `forwarded_from`, `reactions`, `views`;
  - `sender` (из кеша контактов), `outgoing` (`sender_id == session.user_id`).
  Высокоуровневый `maxion.Message` (M/types.py:200-300) добавляет `chat`, `from_user`, `date`, `media` и др.
- **Дефект 1, по коду.** `Message.chat_id` читает `chatId` из вложенного `message`. Собственный тест
  библиотеки подаёт `{"chatId": -1, "message": {"id": "1", "text": "пинг!", "sender": 5}}`, где `chatId`
  лежит только снаружи (tests/test_client.py:140). В этом случае `message.chat` → `None`
  (M/types.py:226-229), `filters.private` → `False` (M/filters.py:101-106), `message.reply()` уйдёт с
  `chat_id=None`. В другом тесте `chatId` положен в оба уровня (tests/test_maxion.py:57-58), и это
  маскирует проблему. На реальном сервере не проверено.
- **Дефект 2, по коду.** `is_edit = bool(payload.get("prevMessageId") ...)` (M/raw/events.py:77-79), а
  высокоуровневый диспетчер превращает такие события в `edited_message` (M/client.py:193-196). В PyMax
  `prevMessageId` описан как «ID предыдущего сообщения» (P/types/domain/message.py:205) и ожидается в
  обычных новых сообщениях (message.py:510). Если сервер присылает его в новых сообщениях,
  `on_message` в maxion не сработает. Не проверено.
- `filters.private` работает только если чат есть в `chats_cache`: иначе создаётся
  `RawChat({"id": chat_id})` без `type` (M/types.py:230-231).
- Телефона в событии нет. `app.get_users(ids)` → `User`, а `User.phone` берётся из `raw.contact.phone`
  или `raw.phone` (M/client.py:436-444; M/raw/types/user.py:24-27). Есть `User.is_bot` (`"BOT"` в
  options, user.py:81-83) и `resolve_phone(phone)` (M/client.py:455-458).
- Тип чата: `ChatType.PRIVATE="DIALOG"`, `GROUP="CHAT"`, `CHANNEL="CHANNEL"` (M/enums.py:20-26);
  `Chat.is_dialog`, `is_group`, `is_channel` (M/raw/types/chat.py:63-73).

### 4.5 Отправка и «прочитано»
- `send_message(chat_id, text, *, parse_mode=None, entities=None, reply_to_message_id=None,
  disable_notification=False, **kwargs)` (M/client.py:224-245) вызывает низкоуровневый
  `raw.send_message(chat_id, text="", *, attaches, elements, reply_to, forward_from, notify=True,
  markdown=False, user_id, post_id, cid, **extra)` через `MSG_SEND` (M/raw/methods/messages.py:20-65).
- «Прочитано»: `mark_chat` / `read_message` через `CHAT_MARK` (M/raw/methods/chats.py:153-182). Вызовы
  только явные: `Client.read_chat_history` (M/client.py:304-305), `Message.read`
  (M/raw/types/message.py:202-203), `Chat.read_all` (M/raw/types/chat.py:196-200), `NewMessage.read`.
  **Автоматических вызовов при приёме нет** (M/raw/client.py:417-435).

### 4.6 Адресная книга
- `app.get_contacts()` → список `User`, постранично через `CONTACT_LIST`
  (`{"status","from","count"}`), по 200 (M/client.py:446-447; M/raw/methods/contacts.py:111-138).
  Телефон — в `User.phone`, если сервер его вернул.
- Также `contacts_cache` заполняется из LOGIN (M/raw/client.py:334-350).

### 4.7 Что библиотека отправляет сама
- `PING` с `interactive=False` (M/raw/methods/misc.py:15-17), но только после SMS-входа или reconnect
  (см. 4.2).
- LOGIN с `interactive=True` (M/raw/methods/auth.py:149, 174).
- Телеметрии нет: `send_log` (`Opcode.LOG`, misc.py:25) сам нигде не вызывается.
- URL в коде: `wss://ws-api.oneme.ru/websocket`, `https://web.max.ru`, `https://botapi.max.ru`
  (только Bot), `wss://videowebrtc.okcdn.ru/ws2` и `https://calls.okcdn.ru` (модуль звонков, только в
  docstring), `https://max.ru` (пример в docstring, M/raw/utils.py:86). Сторонних хостов телеметрии нет.
- Автоответов нет.

### 4.8 Bot API (только в maxion)
- `maxion.bot.Bot(token, *, base_url="https://botapi.max.ru", timeout=40.0)`
  (M/bot/client.py:35, 69-83). Токен передаётся и в заголовке `Authorization`, и в query `access_token`
  (client.py:93-113).
- Long polling через `GET /updates` с параметрами `limit`, `timeout`, `marker`, `types`
  (client.py:423-443). Webhook через `POST /subscriptions` (client.py:340) и `run_webhook` (client.py:358-406).
- `bot_started` разбирается в `BotStarted` (M/bot/updates.py:152-168). Свойства: `chat_id`, `user`,
  `answer()`. **Свойства `payload` нет**: payload из deep-link доступен только как
  `update.raw.get("payload")`. Фильтр `filters.payload` рассчитан на payload кнопок callback
  (M/bot/filters.py:78-86; updates.py:101-103).
- Базовый URL `botapi.max.ru` по CLAUDE.md устаревший. Актуальный домен нужно сверить на dev.max.ru.
  Для бота лучше собственный тонкий HTTP-клиент или официальный SDK, а не maxion.

### 4.9 Качество
- 9 релизов 0.1.0…0.2.1, все выпущены за 29.08.2026 22:42 – 30.08.2026 02:56, с тех пор новых нет
  (PyPI JSON).
- 108 тестов. CI-конфигов в sdist нет. Модели — «обёртка над dict» (M/raw/types/base.py), `py.typed`
  нет. Зависимости минимальные.

---

## 5. Рекомендация для PersonalAccountTransport

**Выбор: PyMax `maxapi-python==2.4.1`** (зафиксировать `==`). Реализация `PyMaxTransport`:

1. **Команда `python -m gateway login`:** `Client(phone, work_dir=<volume>, session_name=...,
   sms_code_provider=ConsoleSmsCodeProvider(), password_provider=ConsolePasswordProvider(),
   extra_config=ExtraConfig(telemetry=False, log_level="INFO"))`, затем `connect()` и `close()`.
   После этого сделать `chmod 600` на файл сессии.
2. **Демон:** тот же `work_dir` и `session_name`, свой `auth_flow`, который **бросает исключение**
   (никогда не запрашивает SMS), `telemetry=False`. Обработать исключение из `start()`: уведомить
   управляющего и остановить отправки.
3. **Мониторинг:**
   - `@client.on_disconnect()` — уведомить и поставить отправки на паузу до следующего `on_start`;
   - `@client.on_error()` — ошибки в обработчиках;
   - периодически проверять `client.is_connected`;
   - переподключаться библиотека будет сама раз в 1 с. Возможно, стоит увеличить `reconnect_delay`.
4. **Фильтр S1:**
   - `message.sender != client.me.contact.id`;
   - тип чата через кеш `client.chats` или `get_chat(chat_id)`, пропускать только `ChatType.DIALOG`;
   - собеседник-бот: проверить по `User.options`, это выяснить при запуске;
   - телефон через `get_user(sender)` → `User.phone`. Если `None`, считать отправителя неизвестным.
5. **«Прочитано»:** не вызывать `message.read()` / `client.read_message()`. Библиотека сама этого не
   делает. Добавить тест-страж (мок), что `read_message` не вызывается.
6. **Отправка:** `client.send_message(chat_id, text)`. Учесть, что текст разбирается как markdown.
7. **Белый список:** `client.contacts` после первого входа. Проверить на реальном запуске, что список
   полный.

maxion в `GreenApiTransport`/резерв не годится: это другой тип провайдера. Держать его вторым
userbot-адаптером тоже не рекомендуется из-за дефектов 4.4 и отсутствия сигналов о разрыве.

---

## 6. Вопросы, которые закрываются только реальным запуском

1. Реальная структура payload `NOTIF_MESSAGE`: где лежит `chatId` (снаружи или ещё и внутри `message`),
   есть ли `prevMessageId` в новых сообщениях, какие значения у `message.type`, в каких единицах `time`.
   Нужен прототип, который печатает `on_raw` и `on_message`.
2. Возвращает ли `CONTACT_INFO` (`get_user`/`fetch_users`) поле `phone`:
   - для отправителя, которого **нет** в контактах управляющего;
   - для отправителя из контактов;
   - как на это влияют настройки приватности отправителя.
3. Формат `User.phone` (int вида 7XXXXXXXXXX или что-то иное).
4. Можно ли по событию надёжно отличить бота и канал (`Chat.type`, `User.options`) и совпадает ли id
   личного чата с `my_id ^ user_id`.
5. Приходят ли в `on_message` собственные исходящие сообщения, отправленные с телефона управляющего.
6. Полнота `client.contacts` при первом и повторном login (маркер `contacts_sync`). Какое значение
   `SyncOverrides.contacts_sync` даёт полную выгрузку.
7. Влияет ли `interactive=True` в PING и LOGIN на push-уведомления на телефоне управляющего и на его
   статус «онлайн». Нужно ли `set_presence(online=False)`.
8. Как ведёт себя сервер при одновременной работе мобильного приложения управляющего и шлюза: не
   выбивает ли сессии и нет ли `FAIL_LOGOUT_ALL`.
9. Какой TCP-хост актуален: `api2.oneme.ru` (PyMax) или `api.oneme.ru` (maxion). Принимает ли сервер
   `app_version` 26.25.0 из встроенного каталога PyMax.
10. Как выглядит на практике слёт сессии: какой `ApiError.error` приходит и в какой момент.
11. Приводит ли отправка PING, LOGIN и `send_message` к отметке прочтения входящих в этом чате на
    стороне сервера (косвенный эффект).
12. Актуальный домен Bot API (`platform-api.max.ru` / `botapi.max.ru` / `platform-api2.max.ru`) и точное
    имя и формат поля `payload` в `bot_started`. Это проверяется на dev.max.ru, не по maxion.

## 7. Что не проверено в рамках этапа 0

- Тесты обеих библиотек не запускались: для запуска нужна установка зависимостей и выполнение кода
  библиотек.
- Бесконечный цикл `start()` при `relogin=False` в PyMax выведен из чтения кода (P/base.py:199-219,
  P/app.py:113, 173-175). Запуском не подтверждён.
- Дефекты maxion 1 и 2 (раздел 4.4) выведены из кода и тестов библиотеки. На реальных payload не
  проверены.
- Полный аудит всех 153 опкодов maxion и всех методов PyMax не проводился. Проверены пути приёма,
  отправки, авторизации, keepalive, телеметрии и «прочитано».
