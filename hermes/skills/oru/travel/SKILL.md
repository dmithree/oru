---
name: travel
description: "Unified travel command for Dima. Auto-dispatches by current trip phase: idle (2-sentence capabilities blurb), pre_trip (preflight pack), active (morning or evening by trip-local hour), post_trip (recap). Container at http://localhost:8003."
platforms:
  - darwin
  - linux
---

# Travel — unified dispatcher

Один skill закрывает весь travel-flow. Контейнер `oru-travel-continuity` на `http://localhost:8003` хранит фазу поездки.

## Step 0 — Source of truth is THE FILESYSTEM

ALWAYS first, before ANY container call:
```
find ~/Documents/GitHub/personal-agent/everything/personal/travel/2026 -name "*.md" -type f | sort
```

If recent plans exist on disk (check modification times), read them to find the actual trip destination and dates. Many times the user mentions a trip but hasn't registered it with the container yet.

## Step 1 — Sync and read phase

Once you know what's on disk (or confirm nothing), sync with container and read phase:
```
curl -sS http://localhost:8003/status
```

Returns JSON with `phase` field: `idle | scheduled_distant | pre_trip | active | post_trip | past`.

## Step 2 — Dispatch by phase

### phase = "idle"

NO container call. Respond to Дима РОВНО ЭТИМИ ДВУМЯ ФРАЗАМИ (без эмодзи, без добавлений, без упоминания HTTP/curl/endpoint'ов — это внутренняя кухня):

```
Активной поездки нет. Скажи направление и даты — за неделю до старта соберу packing, проверю паспорта, погоду и golden hour, найду Muji и прогулочные маршруты; во время поездки буду давать утренний план и вечерний check-in в местном времени; после возвращения соберу recap из дневника.
```

### phase = "scheduled_distant"

Reply (without container call), две фразы — без HTTP/endpoint'ов:
```
Поездка в {trip.destination} через {days_until} дн. За неделю до старта соберу packing, проверю паспорта, дам погоду и golden hour, найду Muji и прогулочные маршруты; во время поездки — утренний план и вечерний check-in в местном времени; после возвращения — recap.
```

### phase = "past"

Reply (without container call), без HTTP/endpoint'ов:
```
Прошлая поездка ({trip.destination}) закрыта. Скажи направление и даты следующей — pre-trip pack за неделю, утро и вечер в местном tz во время поездки, recap после возвращения.
```

### phase = "pre_trip"

Run:
```
curl -sS -X POST 'http://localhost:8003/preflight?notify=false'
```

Return `result.summary` verbatim. Prefix one line:
`Pre-trip: {trip.destination} ({trip.start_date} → {trip.end_date}, {days_until} дн до старта).`

### phase = "active"

Determine local hour in trip timezone (you can compute it from current UTC and trip.timezone). If local hour ∈ [06, 16) → morning. If [16, 23] → evening. Else just status line.

Morning:
```
curl -sS -X POST 'http://localhost:8003/run-morning?notify=false'
```
Return `result.summary` verbatim.

Evening:
```
curl -sS -X POST 'http://localhost:8003/run-evening?notify=false'
```
Returns `result.question`. Show it to Диме with one prefix line: `Вечерний check-in. Ответ свободной формой одним сообщением — сохраню в дневник поездки.`

Status fallback (если час не утренний и не вечерний):
`В поездке: {trip.destination}, день {phase.day_of_trip}/{phase.total_days}.`

### phase = "post_trip"

Run:
```
curl -sS -X POST 'http://localhost:8003/recap?notify=false'
```

Return `result.summary` verbatim. Prefix:
`Recap: {trip.destination} ({trip.start_date} — {trip.end_date}). Файл: {result.recap_file}.`

Recap action also auto-deactivates trip (active_trip → null).

## Rules

- NO emoji anywhere
- NO trailing follow-up questions
- NO advice "береги себя"
- Return container summaries VERBATIM, не перефразируй

## Full trip planning + Vercel publish (NEW)

Кроме фазовых endpoint'ов выше, контейнер умеет генерировать **полный план поездки** с three-tier рекомендациями, golden hour, walking-first маршрутами, packing list, passport check, Muji, и публиковать его на Vercel через posmotri репо.

### POST /plan — сгенерировать полный план (без публикации)

Триггеры от Димы: "сделай план в [city] на [даты]", "составь маршрут", "нужен полный план поездки".

```
curl -sS -X POST 'http://localhost:8003/plan' \
  -H 'Content-Type: application/json' \
  -d '{"destination": "<city>", "start_date": "YYYY-MM-DD", "end_date": "YYYY-MM-DD", "pace": "moderate|relaxed|packed", "interests": ["photography", ...], "country_hint": "<UK|France|...>"}'
```

Опциональные поля: `budget`, `purpose`, `must_see`. Markdown сохраняется в `everything/personal/travel/YYYY/<slug>.md` (host доступ).

Ответ содержит `result.plan_file`, `result.slug`, `result.markdown`. Верни Диме краткую сводку: путь к файлу + флаг + одну строку "Готово, X дней, Y секций". Полный markdown НЕ показывай в чате — он большой, он на диске и (если опубликовано) на URL.

**ВАЖНО: /plan генерирует план через LLM и занимает 60-120 секунд на первый раз (обычно 90с). Это нормально. Если timeout 60с — запрос в фоне через `terminal(background=true, notify_on_complete=true)` с `timeout=300`. Контейнер ответит когда готов.**

### POST /publish — опубликовать существующий план на Vercel

Триггеры: "выложи план", "опубликуй", "/share поездку".

```
curl -sS -X POST 'http://localhost:8003/publish' \
  -H 'Content-Type: application/json' \
  -d '{"destination": "<city>", "start_date": "YYYY-MM-DD", "end_date": "YYYY-MM-DD"}'
```

Контейнер находит `<slug>.md` в `/opt/data/travel/YYYY/`, пушит в `dmithree/posmotri`, Vercel автодеплоит. Ответ: `{ok, url, slug}` или `{ok: false, error}`.

Если ошибка про "no plan file" — сначала вызови `/plan`. Если ошибка про push (auth/clone) — скажи Диме что нужен `GITHUB_TOKEN_POSMOTRI` в `oru/secrets/travel-continuity.env` (fine-grained PAT с scope contents:write на dmithree/posmotri).

### POST /plan-and-publish — за один заход

```
curl -sS -X POST 'http://localhost:8003/plan-and-publish' \
  -H 'Content-Type: application/json' \
  -d '{"destination": "<city>", "start_date": "...", "end_date": "...", "set_as_active": false, "notify": false}'
```

Возвращает `{ok, plan_file, slug, url}`. Если `set_as_active: true` — записывает `active_trip` в personal-context.

### POST /three-recs — три уровня по одному месту

Триггеры: "что в [place]", "что в Tate Modern", "рекомендации по [place]".

```
curl -sS -X POST 'http://localhost:8003/three-recs' \
  -H 'Content-Type: application/json' \
  -d '{"place": "Tate Modern", "city": "London"}'
```

Возврат: `result.recommendations` — три bullet'а (Obvious / Non-obvious / Very non-obvious). Верни verbatim, без эмодзи.

## When to use

AUTOMATICALLY trigger this skill (no slash needed) when Дима mentions ANY of:
- поездка, путешествие, travel, trip, дата поездки, когда ездим
- конкретное направление ("в Лондон", "на Бали", "в Таиланд")
- "что с [city]", "план [place]", "что там"
- hotel, booking, flight, погода, packing, чемодан, виза, паспорт в контексте поездки
- slash command `/travel`

**CRITICAL:** Do NOT wait for explicit permission. If the conversation context mentions travel/trip/destination in any way, load this skill immediately, check the filesystem first, then sync with container. This is part of your automatic context awareness, like with /health.

Additional triggers (old list):
- "что с поездкой", "статус travel"
- "pre-trip", "что взять в [city]"
- "сделай recap", "сводку по поездке"
- Plan generation: "сделай план в [city] на [даты]" — POST /plan
- Publish: "опубликуй план", "выложи", "/share поездку" — POST /publish
- "план в [city] и сразу выложи" — POST /plan-and-publish
- Recommendations: "что в [place]" — POST /three-recs

## Failure handling

- connection refused → "travel-continuity контейнер не отвечает. Проверь: `docker compose ps` в `/Users/dmitry/Documents/GitHub/oru/`."
- HTTP 500 → first 200 chars of body
- empty summary → forward as-is
