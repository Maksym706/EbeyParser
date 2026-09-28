# EbeyParser

Круглосуточный охотник за выгодными объявлениями на **Kleinanzeigen** и **eBay.de**.

Программа сама мониторит нужные тебе регионы и категории. Для каждого нового объявления она:

1. **считает рыночную цену** по реально проданным товарам на eBay и похожим объявлениям на Kleinanzeigen (или по твоему справочнику цен);
2. **показывает фото и описание локальной ИИ-модели** (Ollama, у тебя на компьютере, бесплатно). Модель определяет, что это за товар, совпадают ли фото с описанием, нет ли скрытых дефектов, признаков развода или стоковых картинок;
3. **считает чистую прибыль при перепродаже** и **максимальную цену**, до которой стоит торговаться или делать ставку;
4. **присылает на почту или в Telegram** только то, что правда стоит купить, с объяснением почему.

Есть два режима: **перепродажа** (купить дёшево → продать по рынку) и **для себя** (например, собрать AI-сервер из б/у железа по цене ниже рынка).

> Вся обработка идёт **локально**: парсер и ИИ-модель работают на твоём компьютере. Облачная модель (Claude) — только необязательное «второе мнение» для лучших находок, по умолчанию выключено.

---

## Как это работает

```
 Kleinanzeigen (HTML)  ─┐
 eBay (официальный API) ─┼─► новые объявления ─► фильтр (Suche/Tausch/defekt, цена, слова)
                        │        │
                        │        ▼
                        │   полная страница объявления (текст + все фото)
                        │        │
                        │        ▼
 проданные на eBay  ────┴─► оценка рыночной цены (медиана, без выбросов)
 похожие объявления              │
 твой справочник цен             ▼
                          локальная vision-модель (Ollama): товар, состояние, фото ↔ описание, риски
                                 │
                                 ▼
                          прибыль, ROI, «выгодно до X €», оценка 0–100, вердикт
                                 │
                   ┌─────────────┼──────────────┐
                   ▼             ▼              ▼
               веб-панель      e-mail        Telegram
```

- Повторно объявления не присылаются; при **снижении цены на 10 %+** объявление оценивается заново.
- Kleinanzeigen опрашивается вежливо: случайные паузы 3–7 с между запросами. При блокировке проверка останавливается, чтобы тебя не забанили.

---

## Быстрый старт

Нужен **Python 3.10+** и (для ИИ) **[Ollama](https://ollama.com)**.

```bash
git clone https://github.com/Maksym706/EbeyParser.git
cd EbeyParser
python -m venv .venv
# Windows:  .venv\Scripts\activate      Linux/Mac:  source .venv/bin/activate
pip install -e .

python -m ebeyparser init        # создаст config.yaml и .env
```

Открой `config.yaml` и настрой поиски: что искать, где, в каком радиусе, до какой цены и для чего (перепродажа или для себя). Файл подробно прокомментирован.

### Локальная ИИ-модель

```bash
# установи Ollama с https://ollama.com, затем:
ollama pull qwen2.5vl:7b
python -m ebeyparser ai-check    # проверит, что модель доступна
```

| Твоё железо | Модель | Примерно памяти |
|---|---|---|
| Видеокарта 10–12 ГБ | `qwen2.5vl:7b` (по умолчанию, лучшее качество, понимает немецкий) | 8–10 ГБ; на 8 ГБ поставь `max_images: 2` |
| Видеокарта 6–8 ГБ | `gemma3:4b` (хороший немецкий и русский) или `minicpm-v` (хорошо читает текст на фото) | 5–8 ГБ |
| Слабый ПК / без видеокарты | `qwen2.5vl:3b` | 4–5 ГБ; на процессоре 1–3 минуты на объявление, поставь `timeout_seconds: 400` |
| Сервер 16–24 ГБ (например, RTX 3090) | `gemma3:12b` или `qwen2.5vl:32b` | 11–24 ГБ |

`llama3.2-vision` в Ollama принимает только одно фото (`max_images: 1`) и слабее по-русски.

### LM Studio вместо Ollama

Если удобнее программа с интерфейсом, подойдёт **[LM Studio](https://lmstudio.ai)**:

1. Во вкладке **Discover** скачай vision-модель, например `Qwen2.5-VL-7B-Instruct` (для слабого ПК — версия 3B или `Gemma 3 4B`).
2. Загрузи её и поставь **Context Length не меньше 8192** (три фото занимают много токенов).
3. Вкладка **Developer → Start Server** (или `lms server start`). В настройках включи автозапуск сервера при входе в систему, чтобы всё работало 24/7.
4. В `config.yaml`:
   ```yaml
   ai:
     enabled: true
     provider: openai
     base_url: http://localhost:1234/v1
     model: qwen2.5-vl-7b-instruct
   ```
5. `python -m ebeyparser ai-check` покажет, видит ли программа модель, и выведет точные идентификаторы моделей.

Так же работают **llama.cpp server** и **vLLM**: `provider: openai` и их адрес.

### Запуск

```bash
python -m ebeyparser run         # веб-панель + мониторинг в фоне → http://localhost:8000
```

Хочешь сначала посмотреть интерфейс без настройки?

```bash
python -m ebeyparser demo        # заполнит базу демо-сделками
python -m ebeyparser run --no-monitor
```

---

## Команды

| Команда | Что делает |
|---|---|
| `python -m ebeyparser run` | веб-панель + мониторинг 24/7 (основной режим) |
| `python -m ebeyparser monitor` | только мониторинг, без сайта (для сервера) |
| `python -m ebeyparser once` | одна проверка всех поисков и список лучших находок в консоли |
| `python -m ebeyparser check <ссылка>` | «брать или нет?» — оценить одно объявление Kleinanzeigen или eBay |
| `python -m ebeyparser check <ссылка> --purpose personal --target 300` | то же, но для себя с бюджетом |
| `python -m ebeyparser test-notify` | отправить тестовое уведомление на почту / в Telegram |
| `python -m ebeyparser ai-check [--fix]` | проверить локальную модель (`--fix` сам впишет найденную vision-модель в конфиг) |
| `python -m ebeyparser debug-search` | показать, что парсер видит на странице поиска Kleinanzeigen (если находит 0 объявлений) |
| `python -m ebeyparser ebay-limits` | сколько запросов к eBay API осталось на твоём ключе и сколько их тратят твои настройки |
| `python -m ebeyparser demo` | демо-данные для просмотра интерфейса |

Общие опции: `-c путь/к/config.yaml`, `-v` (подробный лог).

---

## Настройка поисков

**Kleinanzeigen, самый надёжный способ:** настрой поиск прямо на kleinanzeigen.de (регион, радиус, категория, цена, «Angebote»), скопируй адрес страницы и вставь в `url`:

```yaml
searches:
  - name: "MacBook рядом"
    source: kleinanzeigen
    url: "https://www.kleinanzeigen.de/s-berlin/macbook/k0l3331r20"
    purpose: resale
    exclude_keywords: [icloud, gesperrt, defekt]
```

**Или по параметрам:** `query`, `location` (город или индекс), `radius_km`, `category_id` (число из адреса категории, `.../c225` → `225`), `min_price` / `max_price`.

**Для себя** (например, железо под AI-сервер):

```yaml
  - name: "RTX 3090 для AI-сервера"
    query: "rtx 3090"
    location: "Berlin"
    radius_km: 50
    purpose: personal
    target_price: 550     # сколько готов заплатить
```

**eBay** (нужны ключи API, см. ниже):

```yaml
  - name: "Аукционы видеокарт, заканчиваются скоро"
    source: ebay
    query: "rtx 3080"
    buying_options: [AUCTION]
    ending_within_hours: 3      # незамеченные аукционы — классический способ купить дёшево
    max_price: 400
```

Для точной оценки добавь в `pricing.reference_prices` товары, цены на которые ты знаешь.

---

## eBay API

1. Зарегистрируйся на **[developer.ebay.com](https://developer.ebay.com)** (бесплатно) → *Application Keys* → создай ключи **Production**.
2. Впиши в `.env`:
   ```
   EBAY_CLIENT_ID=<App ID (Client ID)>
   EBAY_CLIENT_SECRET=<Cert ID (Client Secret)>
   ```
   Программа сама получает и обновляет токен. Лимиты ключа: `python -m ebeyparser ebay-limits`; обычно Browse API даёт 5 000 запросов в день, больше можно запросить у eBay через Application Growth Check. Разовый токен `v^1.1#...` (`EBAY_OAUTH_TOKEN`) тоже работает, но живёт всего около двух часов, поэтому для 24/7 нужны ключи.

> Никогда не коммить `.env` и не публикуй токены. `.env` уже добавлен в `.gitignore`.

---

## Уведомления

**Telegram** (удобнее всего, приходит с фото и кнопкой «Открыть»):
1. Напиши [@BotFather](https://t.me/BotFather) → `/newbot` → получишь токен.
2. Напиши своему боту любое сообщение, затем узнай свой `chat_id` у [@userinfobot](https://t.me/userinfobot).
3. В `.env`: `TELEGRAM_BOT_TOKEN=...`, `TELEGRAM_CHAT_ID=...`; в `config.yaml`: `notifications.telegram.enabled: true`.

**Почта (Gmail):**
1. Включи двухэтапную аутентификацию в аккаунте Google.
2. [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords) → создай «пароль приложения».
3. В `.env`: `SMTP_USER=you@gmail.com`, `SMTP_PASSWORD=<пароль приложения>`, `NOTIFY_EMAIL=you@gmail.com`; в `config.yaml`: `notifications.email.enabled: true`.

Проверка: `python -m ebeyparser test-notify`.

Порог задаётся в `notifications.min_score` (0–100) и `notifications.verdicts` (`[buy]` или `[buy, maybe]`).

---

## Работа 24/7

**Docker (Linux / Windows / Mac, вместе с Ollama):**
```bash
cp config.example.yaml config.yaml   # и настрой
cp .env.example .env                 # и заполни
docker compose up -d
docker compose exec ollama ollama pull qwen2.5vl:7b
```
Панель: http://localhost:8000. Если есть видеокарта NVIDIA, раскомментируй блок `deploy` в `docker-compose.yml`.

**Linux (systemd):** см. [`deploy/ebeyparser.service`](deploy/ebeyparser.service), там инструкция в комментариях.

**Windows:** [`deploy/start-windows.bat`](deploy/start-windows.bat). Сам ставит зависимости и перезапускается при сбое. Для автозапуска положи ярлык в `shell:startup`.

Чтобы смотреть панель с телефона в домашней сети, поставь `web.host: 0.0.0.0` и открой `http://<IP-компьютера>:8000`.

---

## Необязательно: Claude как «второе мнение»

Если хочется перепроверять самые лучшие находки более сильной моделью:

```bash
pip install -e ".[claude]"
```

```yaml
ai:
  second_opinion:
    enabled: true
    model: claude-opus-5
    api_key: ${ANTHROPIC_API_KEY}
    min_score: 70      # только то, что локальная модель уже оценила высоко
    max_per_run: 5     # лимит платных запросов за проверку
```

Локальная модель по-прежнему оценивает всё. Claude подключается только к топовым кандидатам, и итоговый вердикт учитывает его мнение. Можно и полностью заменить локальную модель на Claude (`ai.provider: anthropic`), но это платно.

---

## Советы по перепродаже

- Лучшие сделки уходят за минуты: держи `interval_minutes` 10–15 и включи Telegram.
- **VB** (Verhandlungsbasis) означает, что продавец готов торговаться. Программа подскажет, до какой цены торговаться выгодно.
- Смотри на `⚠` в карточке: «Nur Tausch», «Vorkasse», iCloud-блокировка, «defekt» — почти всегда пропуск.
- Перед покупкой дорогих вещей проверяй товар при встрече (видеокарта под нагрузкой, аккумулятор ноутбука, iCloud на iPhone).
- Частные продавцы на eBay.de и Kleinanzeigen сейчас, как правило, не платят комиссию за продажу (условия меняются, проверь актуальные и поправь `pricing.selling_fee_percent`). Если продаёшь много и регулярно, это уже предпринимательство (Gewerbe), и налоги нужно учитывать.

## Важно

Инструмент для личного использования. Kleinanzeigen не любит автоматические запросы, поэтому не ставь слишком частые проверки и много страниц. Для eBay используется официальный API.

## Для разработчиков

```bash
pip install -e ".[dev]"
python -m pytest -q
```

Структура: `ebeyparser/scraper` (Kleinanzeigen HTML, eBay API, проданные на eBay), `pricing` (оценка цены, прибыль, фильтры), `ai` (локальная vision-модель, опционально Claude), `notify` (почта, Telegram), `web` (FastAPI-панель), `monitor.py` (конвейер), `cli.py`.
