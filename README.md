# oru — Hermes orchestrator layer

Custom orchestrator поверх [Hermes Agent](https://github.com/NousResearch/hermes-agent) runtime, обслуживающий систему персональных агентов из [personal-agent/bureau/](https://github.com/dmithree/personal-agent/tree/main/bureau).

**Не путать:**
- `oru` (этот репо) — наш orchestrator layer + конфиги для Hermes
- `oru_bot.py` в personal-agent/scripts/ — предыдущее поколение Telegram-бота, которое этот репо заменяет
- `everything/personal/oru/` в personal-agent/ — конфиги старого oru, в процессе миграции
- Hermes Agent — внешний фреймворк-runtime (Nous Research), ставится в `~/.hermes/hermes-agent/`
- `bureau/` в personal-agent — spec'и 16 агентов, остаются там

## Архитектура

```
personal-agent/                    (репо со spec'ами и данными)
├── bureau/{agent}/spec.md         (spec'и агентов)
├── bureau/{agent}/.env            (per-agent credentials)
├── bureau/{agent}/skills/         (Hermes skill files)
└── scripts/llm_router.py          (canonical LLM router — единая точка)

oru/                               (этот репо — orchestrator code)
├── hermes_orchestrator/           (поверх Hermes Agent API)
└── daemon/                        (LaunchAgent plists)

~/.hermes/                         (Hermes runtime data)
├── hermes-agent/                  (installed framework)
├── skills/                        (skill cache, Reflective Phase)
└── config.toml                    (runtime config; llm.provider=none)
```

## Setup

```bash
# 1. Install Hermes Agent runtime
curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash -s -- --skip-setup

# 2. Install orchestrator deps
cd /Users/dmitry/Documents/GitHub/oru
python3 -m venv .venv
.venv/bin/pip install -e .

# 3. Configure HERMES_HOME — see ~/.hermes/config.toml
hermes doctor
```

## LLM contract

Все LLM-вызовы идут через `personal-agent/scripts/llm_router.py:complete(task=..., agent=...)`. Hermes Agent встроенный LLM-слой отключён (`[llm] provider = "none"`). LaunchAgent plist выставляет `PYTHONPATH=/Users/dmitry/Documents/GitHub/personal-agent/scripts` — skill-функции делают `from llm_router import complete`.

Canonical правило: [personal-agent/.claude/rules/03-critical-rules.md#LLM Calls Must Go Through Router].

## Phase 0 pilot

Phase 0 цель — мигрировать `morning_briefing.py` + `evening_debrief.py` (из старого oru-scheduler) в Hermes skill `bureau/daily-briefing/skills/`. Замер токенов: до (старый flat-prompt подход) vs после (Hermes Reflective Phase skill cache).

См. план: `/Users/dmitry/.claude/plans/tender-spinning-engelbart.md`.
