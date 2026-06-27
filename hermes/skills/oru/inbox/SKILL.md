---
name: inbox
description: Triage flow for unprocessed thoughts in oru tasks-hub. Items captured by voice (yumiru), meeting/therapy action items (Fireflies), or any other source land as status=inbox so they don't auto-pollute the brief. This skill lists them and walks Дима through open / dropped decisions. Use when Дима says 'inbox', 'разобрать новое', 'что в inbox', 'триаж', OR sends slash command /inbox. Container at http://localhost:8004.
platforms:
  - darwin
  - linux
---

# Inbox triage — process captured thoughts (idea 6)

Голосовые из yumiru, action items с встреч и любой capture попадают
в store со `status=inbox`. Они не появляются в brief'е и не считаются
открытыми задачами пока Дима их явно не триажит — `open` (берёшь в
работу) или `dropped` (не надо).

## Когда триггерить

- "что в inbox", "разобрать новое", "новые мысли", "триаж"
- "что записал голосом", "что с Fireflies"
- slash `/inbox`

## Step 1 — list

```
curl -sS 'http://localhost:8004/inbox?limit=30'
```

Возвращает `{tasks: [{id, text, source, raw, updated_at, ...}], count}`.

Если `count == 0` — отвечай одной строкой "Inbox пуст." и выходи.

## Step 2 — show

Отформатируй Диме:

```
Inbox: N штук.

1. <text 1>  [<source>]
2. <text 2>  [<source>]
...
```

Source примеры: `thoughts:voice`, `thoughts:meeting`, `thoughts:therapy`.
Без эмодзи. Список нумерован. Под списком одной строкой:

"Решения: `1 open, 2 drop, 3 open` или `все open` / `все drop` / `выйти`."

## Step 3 — apply

После ответа Димы:

```
curl -sS -X POST http://localhost:8004/inbox/triage \
  -H 'Content-Type: application/json' \
  -d '{"items":[
    {"id":"<id1>","decision":"open"},
    {"id":"<id2>","decision":"dropped"}
  ]}'
```

`decision`: `open` (статус становится open и пойдёт в brief) или
`dropped` (тихо забываем).

Получи `{results, summary: {ok, failed}}`. Покажи одной строкой:
"Готово. Open: X, drop: Y. Failed: 0."

## Failure handling

- connection refused → "tasks-hub контейнер не отвечает. `docker compose ps` в `/Users/dmitry/Documents/GitHub/oru/`."
- HTTP 4xx/5xx → первые 200 chars body
- если Дима говорит "потом" / "не сейчас" — оставь как есть, при следующем `/inbox` тот же batch вернётся

## Rules

- NO emoji
- Без trailing "хочешь что-то ещё?"
- Если task имеет `due_at` или `context_tags` в raw — упомяни в скобках для контекста
