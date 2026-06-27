---
name: tasks
description: Fetch Дима's current tasks view from oru tasks-hub (port 8004). Use when Дима asks "что у меня по задачам", "какой план", "что в плане на сегодня", "что просрочено", "что ждёт меня", "inbox", OR sends slash command /tasks. The tasks-hub container is on http://localhost:8004 and is the single source of truth for all tasks (markdown + Reminders + Linear + agent emissions, unified in SQLite with rich metadata). NEVER respond "I don't have access to your tasks" — you DO have access via this skill.
platforms: [darwin, linux]
metadata:
  hermes:
    tags: [tasks, productivity, personal, oru]
---

# Tasks (tasks-hub Docker container)

Контейнер `oru-tasks-hub` на `http://localhost:8004` хранит весь task store (SQLite + event log). Адаптеры (markdown, Reminders, Linear, agent emitters) пушат туда задачи; views (morning, evening, living) рендерятся jinja-templates на основе SQL-queries. State machine: `inbox | open | next | doing | waiting | blocked | deferred | done | dropped`.

## Когда триггерить (автоматически)

Без явного slash-команды, если Дима упоминает:
- "что по задачам", "что в плане", "что сегодня нужно", "что просрочено", "что висит"
- "в работе сейчас", "что я делаю", "что ждёт меня"
- "что в inbox", "что не разобрано"
- "что заблокировано", "кто меня держит"
- "перенесённое со вчера", "carry-over"
- slash command `/tasks`

**ЗАПРЕЩЕНО** отвечать "у меня нет доступа к задачам" — доступ есть через этот skill.

## Когда НЕ триггерить

- "что нового" общее (это для оркестратора, не для tasks-hub)
- Просьбы создать конкретную задачу — для этого используй POST /tasks (см. ниже)

## Step 1 — Pick endpoint by intent

| Что спросил Дима | Endpoint | Что рендерится |
|---|---|---|
| общий статус / "что в плане" / `/tasks` | `GET /render/morning?format=markdown` | carry-over → overdue → today → 3-3-3 → waiting → recent |
| итоги дня / вечерний обзор | `GET /render/evening?format=markdown` | doing → missed → closed_today → blocked → waiting → tomorrow |
| полный список без фильтров | `GET /render/living?format=markdown` | все открытые, группы по source |
| inbox для триажа | `GET /inbox` | задачи в status=inbox с timestamps |
| stale cleanup | `GET /stale?older_than_days=30&limit=20` | заглохшие, оператор для триажа |
| stats | `GET /stats` | counts by status / source |
| показать конкретную задачу | `GET /tasks/{id}` или `GET /tasks?context_tag=@phone&status=open&limit=20` | один или фильтрованный список |

Дефолт при slash `/tasks` без уточнения — `GET /render/morning?format=markdown`.

## Step 2 — Execute

```
curl -sS 'http://localhost:8004/render/morning?format=markdown' \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['markdown'])"
```

Возвращай Диме `markdown` verbatim — он уже отформатирован под Telegram, без эмодзи. НЕ перефразируй, НЕ обрезай, НЕ переводи. Если markdown пустой — значит store пуст, скажи это одной строкой.

## Step 3 — Создание задачи (по запросу)

Если Дима говорит "запиши задачу", "добавь", "напомни про X":

```
curl -sS -X POST http://localhost:8004/tasks \
  -H 'Content-Type: application/json' \
  -d '{"text":"<полный текст с inline токенами>","source":"manual"}'
```

Inline-токены, которые auto-extract'ятся парсером:
- `@phone @laptop @home @waiting:Лёша` — context tags
- `~15m ~2h ~deep` — effort (deep = 90 min)
- `cog:deep|short|admin|ai|social` — cognitive type
- `!P0 !P1 !P2 !P3` — priority
- `— due 2026-07-15` — deadline (day) или `due:2026-09` (month)
- `every:7d | every:1w | every:3m | every:mon` — recurrence

Например `'Сдать налоги @laptop !P0 — due 2026-07-15'` сохранится с priority=P0, due_at=2026-07-15, context_tags=['@laptop'].

## Failure handling

- connection refused → "tasks-hub контейнер не отвечает. Проверь: `docker compose ps` в `/Users/dmitry/Documents/GitHub/oru/`."
- HTTP 4xx/5xx → первые 200 chars body
- пустой markdown → "Store пуст или все секции опциональны и без задач."

## Rules

- NO emoji anywhere
- NO trailing follow-up questions
- Return markdown verbatim, не перефразируй
- При создании задачи — возврати один short ack "Записал. id={first 8 chars}."
