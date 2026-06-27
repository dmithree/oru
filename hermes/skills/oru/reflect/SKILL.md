---
name: reflect
description: Trigger Дима's self-reflection agent (oru-self-reflection on http://localhost:8005). It reads recent therapy + coaching session summaries, extracts active IFS parts, weekly themes, and concrete homework items via LLM with forced tool-use, then emits homework as tasks into tasks-hub and updates personal-context.json#self. Use when Дима says /reflect, "как я", "что у меня по терапии", "разобрать сессии", OR runs slash /reflect. NEVER respond "I don't have access to your therapy data" — you DO via this skill.
platforms:
  - darwin
  - linux
---

# Self-reflection (IFS analyzer)

Контейнер `oru-self-reflection` на `http://localhost:8005`. По крону Вс 19:00 он сам анализирует свежие саммари из `everything/personal/therapy/transcripts/summary/` и `everything/personal/coach/transcripts/summary/`. Этот skill — ручной триггер + просмотр последнего отчёта.

## Когда триггерить

- "как я", "что у меня по терапии", "что говорят сессии"
- "разобрать сессии", "что в IFS"
- "какие части активны", "какие темы"
- "какие у меня домашки"
- slash `/reflect`

## Step 1 — что есть в последнем отчёте

```
curl -sS http://localhost:8005/state
```

Возврат:
```json
{
  "generated_at": "...",
  "transcripts_processed": N,
  "active_parts": [{"name": "Контролёр", "role": "manager", "note": "..."}],
  "weekly_themes": ["..."],
  "homework": [{"text": "...", "priority": "P1", "due_at": "...", "recurrence": "every:1w"}],
  "carry_over": "...",
  "summary_for_telegram": "...",
  "homework_applied": [{"ok": true, "task_id": "...", "text": "...", "recurrence": "..."}]
}
```

Если `status=no_run_yet` — переходи к Step 2 (запустить).

## Step 2 — Format and reply

Покажи Диме:

```
_Self-reflection_ (от {generated_at}, обработано {N} саммари)

Темы:
- <тема 1>
- <тема 2>
...

Активные части (top 5):
- {Контролёр} [manager]: {note}
...

Домашки ({len(homework)}):
- {text} [P{N}, {due_at or 'one-off'}, recurrence={every:1w or '-'}]
...

Carry-over: {carry_over or 'нет'}
```

NO emoji. Если `summary_for_telegram` есть — лучше показать его (LLM уже сформулировал кратко). Если нет — собрать по шаблону выше.

## Step 3 — re-run if Дима asks

Триггеры на повторный запуск: "пересчитай", "обнови", "новые сессии есть?", "/reflect run":

```
curl -sS -X POST http://localhost:8005/run \
  -H 'Content-Type: application/json' \
  -d '{"dry_run":false,"notify":false}'
```

`notify=false` потому что Telegram-уведомление приходит само через cron (Вс 19:00). При ручном повторном запуске не хотим дублировать.

Возврат тот же что `/state`. Покажи отчёт.

Если `transcripts: 0` — скажи "С последнего прогона новых саммари нет." и выходи. Это значит cron-окно (since_days=120) не нашло свежих файлов — либо они уже обработаны, либо нет sessions за последние 4 месяца.

## Step 4 — где живут домашки

Каждая домашка из `homework_applied` это task в tasks-hub. Можно посмотреть через `/tasks` skill:

```
curl -sS "http://localhost:8004/tasks?source_prefix=agent:self-reflection&limit=20"
```

Все они с `owner_agent=self-reflection`, `source=agent:self-reflection`. Закрытие через /debrief или /cleanup работает как с любой другой задачей.

## Rules

- NO emoji
- NO trailing "хочешь обсудить?"
- Если active_parts > 5 — показать первые 5 + count "ещё N"
- recurrence "every:1w" подсветить как weekly, отсутствие — one-off
