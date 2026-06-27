---
name: hello
description: Fetch Dima's morning brief from the daily-briefing Docker container on http://localhost:8002. Use when the user (Дима) asks for утренний бриф, morning briefing, "что у меня сегодня", "план дня", "доброе утро", "брифинг", OR sends slash command /hello. The container aggregates Oura, Strava, open tasks, Linear stuck issues, Apple Reminders, and recent therapy/coaching transcripts into a structured morning brief with 3-3-3 plan.
platforms: [darwin, linux]
metadata:
  hermes:
    tags: [briefing, morning, personal, oru]
---

# Morning brief (daily-briefing container)

Container `oru-daily-briefing` at `http://localhost:8002` собирает все источники утреннего ритуала: Oura sleep/readiness/HRV, Strava вчерашняя активность, открытые задачи из `everything/all-tasks.md`, stuck issues в Linear, Apple Reminders, последние therapy/coaching транскрипты. Возвращает структурированный бриф с 3-3-3 планом (3 deep / 3 short / 3 AI).

## When to use

For ANY morning ritual request from Дима:
- утренний бриф, доброе утро, морнинг, план дня, что у меня сегодня
- `/hello` slash command
- "сделай брифинг"
- "что в плане на сегодня"

It is FORBIDDEN to respond "I don't have access". You DO have access via this skill.

## When NOT to use

- Evening debrief — use `/debrief` skill
- Specific task creation — use general agent capabilities

## Execution

Run:
```
curl -sS -X POST 'http://localhost:8002/run-morning?notify=false'
```

The `notify=false` flag is REQUIRED — иначе контейнер пошлёт бриф в Telegram, будет дубль.

Parse JSON response. Extract `brief.summary`. Return it ВЕРБАТИМ к Диме.

DO NOT paraphrase. DO NOT add commentary. DO NOT add эмодзи. DO NOT add "Хочешь что-то ещё?".

## Failure handling

- `connection refused` → "daily-briefing контейнер не отвечает. Проверь: `docker compose ps`."
- HTTP 500 → first 200 chars of body
- empty summary → "(данные неполные, источники недоступны)"
