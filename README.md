# minds.games referral bot

Бот на Python (`asyncio` + `aiohttp`) для регистрации email'ов на [minds.games](https://minds.games) по реферальным ссылкам. Проходит тот же путь, что и обычный посетитель: открывает страницу по `?ref=CODE`, решает Cloudflare Turnstile через [CapSolver](https://www.capsolver.com/) и отправляет форму регистрации. Для каждого аккаунта используется свой прокси.

> ⚠️ Используйте на свой риск. Автоматическая массовая регистрация может нарушать правила сайта и приводить к блокировке аккаунтов и реф-баллов.

## Как это работает

Для каждого email бот делает:

1. `GET /?ref=CODE` — заходит по реф-ссылке через прокси, получает куки (так делает и сайт).
2. `GET /api/flags` — запрашивает флаги фазы кампании (как сайт).
3. Решает Turnstile через CapSolver (`AntiTurnstileTaskProxyLess`), ждёт до ~120 с.
4. `POST /api/signup` с телом `{email, website:"", consent:true, turnstile, ref}`.
5. При успехе записывает в `state.json`: `seat`, свой `referral_code`, `session`, куки и `referred` (засчитан ли реф).

### Логика запуска

- **Email'ы** берутся из `emails.txt`. Те, что уже помечены `"ok": true` в `state.json`, пропускаются — прокси на них не тратятся. Неудачные остаются в очереди и попробуются при следующем запуске.
- **Прокси** берутся из `Proxy.txt` сверху вниз. Взятый прокси сразу **удаляется из файла**, поэтому один прокси никогда не используется для двух аккаунтов (даже если регистрация не удалась). Пустой `Proxy.txt` → бот останавливается, без прокси не работает, чтобы не светить ваш IP.
- **Реф-коды** берутся из `Reff.txt` (ссылка `https://minds.games/?ref=CODE` или просто код).
  Если их несколько, бот идёт по ним **по порядку**: на каждый реф делает случайное число успешных регистраций от `LIMIT_MIN` до `LIMIT_MAX` (по умолчанию 130–250, константы в `main.py`), затем переходит к следующему, пока не закончатся рефы, email'ы или прокси.
- **Лимит** считает только успешные регистрации.
- Прогресс по рефам между запусками не хранится: новый запуск начинает с первого рефа. Уже зарегистрированные email'ы при этом не повторяются.

## Установка

Нужен Python 3.10+.

```bash
git clone https://github.com/<your-name>/minds-games-bot.git
cd minds-games-bot

python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # Linux / macOS

pip install -r requirements.txt
```

## Настройка

Скопируйте примеры и замените содержимое своими данными:

| Пример | Копия | Что внутри |
|---|---|---|
| `config.py.example` | `config.py` | ключ `CAPSOLVER_API_KEY` |
| `emails.txt.example` | `emails.txt` | email'ы, по одному на строку |
| `Proxy.txt.example` | `Proxy.txt` | прокси `user:pass@host:port`, по одному на строку |
| `Reff.txt.example` | `Reff.txt` | реф-ссылки или коды, по одному на строку |

```bash
cp config.py.example config.py
cp emails.txt.example emails.txt
cp Proxy.txt.example Proxy.txt
cp Reff.txt.example Reff.txt
```

(в Windows: `copy config.py.example config.py` и т. д.)

Все эти файлы, а также `state.json`, добавлены в `.gitignore` и не попадут в репозиторий.

## Запуск

```bash
python main.py                              # все email'ы из emails.txt
python main.py --email me@example.com       # только один адрес
python main.py --ref ABCD1234               # один реф вместо Reff.txt
python main.py --proxy user:pass@host:1234  # свой прокси; Proxy.txt не трогается
```

Пример вывода:

```
[12:00:00] Email: всего 34, уже готово 1, к регистрации 33
[12:00:00] ══ Реф 1/2: ABCD1234 — лимит регистраций 187
[12:00:00] ── [1/33] example.user1@gmail.com | ref ABCD1234 | proxy proxy.example.com:10001
[12:00:01] GET https://minds.games/?ref=ABCD1234 → 200
[12:00:02] CapSolver taskId=..., решаем...
[12:00:15] Turnstile решён за ~12s
[12:00:16] ✅ Готово: example.user1@gmail.com | seat #15972 | реф засчитан (referred=true) | свой ref-код: 1A2B3C4D
```

Код возврата: `0` — хотя бы одна регистрация прошла, `1` — ни одной.

## state.json

```json
{
  "example.user1@gmail.com": {
    "ok": true,
    "ref_used": "ABCD1234",
    "referred": true,
    "seat": 15972,
    "referral_code": "1A2B3C4D",
    "session": "...",
    "cookies": { "mg_me": "..." },
    "registered_at": "2026-01-01T12:00:00"
  }
}
```

Чтобы перерегистрировать адрес, удалите его запись из `state.json`.

## Структура

```
main.py               # весь код бота
requirements.txt      # aiohttp
config.py.example     # шаблон конфига (ключ CapSolver)
emails.txt.example    # пример списка email'ов
Proxy.txt.example     # пример списка прокси
Reff.txt.example      # пример реф-ссылок
```

## Настройки в коде

В начале `main.py`:

- `LIMIT_MIN`, `LIMIT_MAX` — диапазон случайного числа регистраций на один реф.
- `TURNSTILE_SITEKEY`, `BASE_URL` — параметры сайта.
