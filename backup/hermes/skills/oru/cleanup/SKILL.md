---
name: cleanup
description: Backpressure / triage flow for stale tasks in oru tasks-hub. Use when Дима asks "что заглохло", "почистить задачи", "что лежит без движения", "разобрать старое", OR sends slash command /cleanup. Tasks-hub at http://localhost:8004 has /stale endpoint that surfaces open tasks 30+ days without updates and no deadline. After listing them this skill walks Дима through keep / defer / drop decisions.
platforms: [darwin, linux]
metadata:
  hermes:
    tags: [tasks, cleanup, backpressure, oru]
---

# Cleanup — weekly stale-task triage (idea 18)

133 task store растёт. Если задача без deadline лежит 30+ дней без апдейтов — кандидат на drop. Skill вытаскивает batch из tasks-hub и предлагает Диме решение по каждой.

## Когда триггерить

- "почистить задачи", "разобрать backlog", "что заглохло"
- "сделай уборку в задачах", "weekly cleanup"
- slash command `/cleanup`
- по дефолту по воскресеньям вечером (когда есть свободное время)

## Step 1 — Получить кандидатов

```
curl -sS 'http://localhost:8004/stale?older_than_days=30&limit=15'
```

Возвращает:
```json
{
  "tasks": [{"id":"...", "text":"...", "updated_at":"..."}],
  "count": N,
  "summary": {"30-60d": X, "60-90d": Y, "90d+": Z, "threshold_days": 30}
}
```

Если `count == 0` — отвечай одной строкой "Заглохших задач нет. Store здоровый." и выходи.

## Step 2 — Показать Диме сводку

```
Заглохшие задачи: X штук (30-60d: A, 60-90d: B, 90d+: C).
По каждой нужно решение: keep / defer / drop.

1. <text 1> — last update 2026-05-15 (45 дней назад)
2. <text 2> — last update 2026-04-20 (62 дня назад)
...
```

Без эмодзи. Список нумерован. Пиши Диме prompt в одну строку:
"Скажи решения: '1 drop, 2 defer 7d, 3 keep' или 'все drop' / 'все defer 30d' / 'выйти'."

## Step 3 — Применить решения

После ответа Димы вызови:

```
curl -sS -X POST http://localhost:8004/stale/triage \
  -H 'Content-Type: application/json' \
  -d '{"items":[
    {"id":"<task1_id>","action":"drop"},
    {"id":"<task2_id>","action":"defer","defer_until":"2026-07-01"},
    {"id":"<task3_id>","action":"keep"}
  ]}'
```

`action`: `keep` (touches updated_at), `drop` (status=dropped), `defer` (status=deferred + defer_until=ISO date).

Получи `{results: [...], summary: {ok, failed}}`. Покажи Диме одной строкой:
"Готово. Drop: X, defer: Y, keep: Z. Failed: 0."

## Failure

- HTTP 4xx → первые 200 chars
- batch с failed > 0 → перечисли неудачные

## Rules

- NO emoji
- Без "хочешь продолжить?" в конце
- Если Дима говорит "не сейчас" / "потом" — сохрани batch ID? нет, оставь как есть, в следующий /cleanup тот же batch вернётся
