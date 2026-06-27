---
name: health
description: Fetch Dima's weekly health digest from the local health-agent Docker container (Oura Ring + Strava). Use when Дима asks anything about his health, fitness, recovery, training load, HRV, readiness, sleep, weekly summary, Oura data, Strava data, OR sends slash command /health. The container is on http://localhost:8001 and has direct API access to all the data. NEVER respond "I don't have access to your health data" — you DO have access via this skill.
platforms: [darwin, linux]
metadata:
  hermes:
    tags: [health, fitness, personal, oru]
---

# Health digest (Dima's personal health-agent)

There is a Docker container `oru-health` running on this Mac at `http://localhost:8001`. It has direct access to Oura Ring API and Strava API. It computes weekly readiness, HRV, sleep, training volume, intensity zones, and outputs a structured digest.

## When to use

AUTOMATICALLY trigger this skill (no slash needed) when Дима mentions ANY of:
- здоровье, форма, восстановление, готовность, HRV, recovery, sleep, readiness
- тренировки, нагрузка, активность, кардио, велосипед, бег, Z2/Z3/Z4, триатлон
- "что впереди по тренировкам", "какие тренировки", "schedule", "program" — ALWAYS check the workout state file
- "что говорит Oura", "что говорит Strava", "как я по форме", "что со здоровьем"
- упоминание конкретных метрик: пульс, VO2max, рестинг-харт-рейт, stress score, recovery index
- **еда / питание:** еда, питание, диета, перекус, "что съесть", "что поесть", "когда есть", "во сколько есть", голодание, intermittent fasting, сахар, сладкое, десерт
- slash command `/health`

**CRITICAL:** Do NOT wait for a slash command or explicit permission. If the conversation context mentions health/fitness/recovery in any way, load this skill immediately and fetch the digest. This is part of your automatic context awareness, like with /travel.

It is FORBIDDEN to respond "I don't have access to your health data" — you DO have access through this skill.

## Training schedule lookup

When Дима asks "что по тренировкам впереди" or similar, DO NOT just return the health digest. ALSO check the active program and next training day:

```bash
cat /Users/dmitry/Library/Application\ Support/oru-host-state/health-workouts.json
```

This file contains:
- `active_program`: name of the current training program (e.g., `program_1_cyclist`)
- `start_date`: when the program started
- `next_day`: which day (A, B, or C) is coming next
- `history`: completed workouts with timestamps

The program files are at `/Users/dmitry/Documents/GitHub/personal-agent/inbox/health/` (e.g., `program_1_cyclist.md`). Load them as needed and describe the upcoming workout to Дима.

## Питание (личные правила Димы)

Когда Дима спрашивает про еду / питание / перекус / «что съесть» / время приёма пищи —
учитывай два его жёстких правила и встрой их в ответ:

1. **Не ест сладкое** — никакого добавленного сахара и десертов. Не предлагай сладкое,
   не используй его в примерах, при необходимости предложи несладкую альтернативу.
2. **Не ест после 16:00** — пищевое окно закрывается в 16:00, следующий приём пищи не
   раньше 8:00. Это раннее интервальное голодание ~16:8 (early time-restricted eating).
   Любые советы по еде держи внутри окна 8:00–16:00; не предлагай ужин/поздний перекус.

**Когда Дима хочет что-то съесть / тянет на перекус / голоден вне окна** — напоминай:
- сначала выпить воды: жажду мозг часто путает с голодом, стакан воды убирает тягу;
- допустимы чаи **без добавок** (без сахара, мёда, молока, сиропов) — они не ломают
  голодание и помогают перебить желание поесть.

**Всегда добавляй ОДИН точный, релевантный факт о пользе** того правила, которого касается
вопрос. Факт должен быть достоверным, без преувеличений и без выдумок — бери из набора ниже,
одну строку, по делу.

Достоверные факты (отказ от сахара):
- Нет скачков глюкозы и инсулина → ровная энергия без спадов и тяги.
- Снижает риск инсулинорезистентности и диабета 2 типа в долгую.
- Меньше хронического воспаления, лучше липидный профиль.

Достоверные факты (последний приём в 16:00, голодание ~16ч):
- Раннее TRE (приём пищи в первой половине дня) в РКИ улучшает чувствительность к инсулину,
  гликемический контроль и давление — даже без снижения веса (Sutton 2018).
- ~16ч без еды переключает метаболизм на жиры/кетоны и поддерживает аутофагию.
- Последний приём задолго до сна → стабильнее ночная глюкоза и чище сон (нет пищеварения ночью).

Для чисто диетических вопросов контейнер дёргать НЕ обязательно — достаточно правил + факта;
подключай метрики Oura/Strava, только если вопрос связывает еду с восстановлением/нагрузкой.

## When NOT to use

- Medical symptoms or illnesses (this skill is about activity/recovery metrics only)
- Generic fitness questions not tied to Dima's data

## Execution

Step 1: run terminal commands:
```
curl -sS -X POST 'http://localhost:8001/run?notify=false'
curl -sS -X GET 'http://localhost:8001/workout'
```

The `notify=false` flag is REQUIRED for `/run` — without it the container also pushes the digest to Telegram, causing a duplicate message.

Step 2: parse both JSON responses:
- `digest.state` and `digest.summary` from the first call
- `session.day`, `session.program_title`, and `session.text` from the second call

Step 3: strip the band legend from session.text. The legend is always the last line starting with "Ленты по нарастанию:" — delete it.

Step 4: respond to Дима in a HUMAN, conversational format. Two parts:

**Part 1 — живое вводное предложение (1-2 sentences).** Synthesize `digest.state` + the most important points from `digest.summary` into natural language that ties recovery state to today's training. Don't dump the bullet list verbatim. Pull only what matters: состояние, сон, HRV, была ли нагрузка. Example:

> Твоё состояние оптимальное для тренировки с лентами: сон хороший, HRV на высоком уровне, тренировочной нагрузки вчера не было.

**Part 2 — переход к тренировке + сама программа.** One natural sentence introducing the day, then the exercises. Example:

> Сегодня первый день тренировочной программы, работаем на нижнюю цепь + стабилизаторы
>
> **Болгарский сплит-присед с лентой**
> - **Подходы:** 4 × 8 на каждую сторону | **Лента:** чёрная (средняя)
> - **Техника:** ...
> - **Зачем:** ...

Use `session.text` for the exercises (без band legend, без эмодзи в названиях). Keep the техника/зачем content intact — only the framing is rewritten to be human.

DO paraphrase the summary into a natural intro. DO add a light human framing sentence for the workout. DO NOT add emoji. DO NOT add "Хочешь ещё что-то?" / "Если будут вопросы".

## Failure handling

- `curl: connection refused` or timeout → respond: "Health-агент не отвечает. Проверь: `docker compose ps` в `/Users/dmitry/Documents/GitHub/oru/`."
- HTTP 500 → respond with: "Health-агент вернул ошибку:" + first 200 chars of response body
- `state == "unknown"` or empty `summary` → respond: "Данные за неделю недоступны. Проверь подключения Oura и Strava."

## Critical rules

- NO EMOJI anywhere in your response. Not 😅 😊 ✅ 🏥 — none. Plain text only.
- NO trailing "Хочешь ещё что-то узнать?" / "Чем ещё помочь?".
- NO disclaimers like "это не медицинский совет".
- The container's summary is raw material, not the final text. Synthesize a human intro from it — don't paste the bullet list verbatim. The exercise content (техника/зачем) stays intact; only the framing around it is rewritten to sound human.
