---
name: debrief
description: "Evening debrief flow: Дима's freeform end-of-day text becomes structured events (completed/deferred/blocked/created) that mutate the tasks-hub store. Two-step state machine via daily-briefing container at http://localhost:8002 (pending flag + capture next message), which then forwards to tasks-hub /debrief/ingest where the LLM matches against open tasks and applies events. Use when Дима sends /debrief, asks for evening reflection, or says 'подведу итоги дня'."
platforms:
  - darwin
  - linux
---

# Evening debrief — event ingestion (state-machine)

Two-step flow:
1. `daily-briefing` container (`http://localhost:8002`) holds the pending-state flag and the Telegram dialog.
2. Once Дима sends his freeform reply, the container forwards it to `tasks-hub` (`http://localhost:8004/debrief/ingest`) where the LLM matches statements against today's open tasks and emits **events that actually mutate the store**: completed / deferred / blocked / waiting / created. The debrief file ends up at `~/Library/Application Support/oru-host-state/debriefs/YYYY-MM-DD-debrief.md`.

This replaces the old pattern where parse_evening_debrief just wrote a JSON file no consumer read.

## Step 1 — Check pending state

ALWAYS first:
```
curl -sS http://localhost:8002/pending-debrief
```

Returns `{"pending": true|false, ...}`.

## Step 2A — If pending=false (start new debrief)

Run:
```
curl -sS -X POST 'http://localhost:8002/run-evening?notify=false'
```

Get `question` from response. The container's `question` references a specific task from today's brief — use it. Send to Диме РОВНО в таком формате:

```
Как прошёл день? <question text — про одну задачу из брифа>

Если сделал что-то ещё или узнал полезное, расскажи — запомню.
```

NO header "Вечерний debrief". NO emoji. Start straight with "Как прошёл день?". Stop. Wait for Дима's next message.

## Step 2B — If pending=true (save user response)

The user's free-text message IS the debrief. POST it to daily-briefing (which forwards to tasks-hub):

```
curl -sS -X POST 'http://localhost:8002/save-debrief' \
  -H 'Content-Type: application/json' \
  -d '{"user_text": "<the user message verbatim>"}'
```

Response shape (when tasks-hub is healthy):
```json
{
  "ok": true,
  "via": "tasks-hub",
  "summary": {"events": N, "ok": M, "failed": K},
  "applied": [
    {"kind": "completed", "task_id": "...", "text": "...", "matched_text": "..."},
    {"kind": "deferred",  "task_id": "...", "text": "...", "defer_until": "YYYY-MM-DD"},
    {"kind": "blocked",   "task_id": "...", "text": "...", "blocked_by": "..."},
    {"kind": "created",   "task_id": "...", "text": "..."}
  ]
}
```

Or fallback (tasks-hub unreachable):
```json
{"ok": true, "via": "fallback", "parsed": {...}}
```

Reply to Диме одной строкой:

When `via=tasks-hub`:
```
Debrief обработан. Закрыто X, перенесено Y, заблокировано Z, новых N.
```

When `via=fallback`:
```
Debrief сохранён в файл, но tasks-hub недоступен — store не обновился.
```

If `summary.failed > 0`, добавь второй строкой: `Не применилось: <count>. Подробности в logs.`

NO emoji. NO trailing question. NO follow-up suggestion.

## When to use

- Slash command `/debrief`
- "вечерний debrief"
- "подведу итоги дня"
- "хочу записать debrief"
- "как прошёл день" (если пользователь явно собирается рассказывать)

NOT для "как сегодня прошло?" в смысле "что сделал по плану" — это `/tasks` (evening view).

## Failure modes

- daily-briefing connection refused → "daily-briefing контейнер не отвечает. Проверь `docker compose ps` в `/Users/dmitry/Documents/GitHub/oru/`."
- tasks-hub unreachable from daily-briefing (response `via=fallback`) → see message above; store stays out of sync until tasks-hub is back and user re-submits via `/debrief`.
- pending=true но Дима шлёт slash-команду вместо текста → route the slash, скажи "Pending debrief остался, чтобы сохранить — повтори `/debrief`."
