# oru — orchestrator слой Димы

Этот репо — runtime для системы персональных агентов. Пять Docker-контейнеров на FastAPI + APScheduler + LiteLLM + SQLite, плюс несколько macOS launchd-watcher'ов на хосте.

Spec'и агентов и данные пользователя живут в соседнем репо `personal-agent/`. Этот репо — только код, конфиги и persistent state в `state/`.

## Контейнеры

Все запускаются через `docker compose up -d`. TZ=Europe/Belgrade. Bind-mount `state/` ↔ хост.

| Сервис | Порт | Что делает | Cron |
|---|---|---|---|
| `oru-health` | 8001 | Oura/Strava/вес/анализы → недельный digest, classify state, emit followup-задачи в tasks-hub | Вс 18:00 |
| `oru-daily-briefing` | 8002 | Утренний бриф + вечерний debrief. Тянет данные из tasks-hub/render/morning + Oura + Strava + transcripts. Шлёт в Telegram | 07:30, 18:45 |
| `oru-travel-continuity` | 8003 | Active-trip план, packing, sources | по требованию |
| `oru-tasks-hub` | 8004 | Единый task-store (SQLite + JSONL events). Адаптеры: markdown, reminders, linear, thoughts. View renderer (jinja). LLM-debrief ingestion. Centralized `/status/all` | каждые 15 мин ingest, Вс 21:00 cleanup |
| `oru-self-reflection` | 8005 | IFS-анализ therapy+coach транскриптов через tool-use LLM. Emit homework в tasks-hub с semantic dedup | Вс 19:00 |

Healthchecks: `curl localhost:<port>/healthz`. Aggregated: `curl localhost:8004/status/all`.

## Host launchd-watcher'ы

Macos TCC блокирует launchd от запуска скриптов под `~/Documents`. Поэтому установщик копирует их в `~/Library/Application Support/oru-host/`. См. `host-daemons/install.sh`.

| Label | Интервал | Назначение |
|---|---|---|
| `com.oru.reminders-bridge` | 15 мин | bulk JXA-дамп активных Apple Reminders → `state/reminders.json`. Tasks-hub читает это |
| `com.oru.reminders-commands-watcher` | 1 мин | применяет команды (`create`/`complete`/`snooze`) из `state/reminders-commands.jsonl` через byName-JXA. Лог в `state/reminders-commands-log.jsonl` |
| `com.oru.living-markdown-sync` | 1 ч | curl http://localhost:8004/render/living → `state/all-tasks.md` (auto-generated view единого store) |
| `com.oru.skill-cache-invalidator` | 1 мин | следит за `~/.hermes/skills/**/SKILL.md` mtime+size hash. На изменение — удаляет `.skills_prompt_snapshot.json` |

Логи: `~/Library/Logs/oru-*.log`.

## Поток данных (морнинг)

```
07:30 cron в oru-daily-briefing
  -> GET oru-tasks-hub:8004/render/morning (структурированные секции задач)
  -> GET local fetchers: Oura, Strava, transcripts
  -> LiteLLM (anthropic claude-sonnet-4-6) собирает бриф
  -> POST telegram sendMessage (с фолбэком на plain text при 400)
```

Tasks-hub в свою очередь читает `state/tasks.db` который непрерывно пополняется:
- markdown_adapter сканит `personal-agent/everything/` + `pfm/` каждые 30 мин
- reminders_adapter читает `state/reminders.json` (заполняется host-bridge)
- linear_adapter тянет stuck issues
- thoughts_adapter забирает inbox из processed thoughts
- agent_emitter принимает POST /tasks от других контейнеров (health → bloodwork, self-reflection → homework)

## Состояние store (типичное)

```
sqlite3 state/tasks.db 'SELECT status, COUNT(*) FROM tasks GROUP BY status'
```

`inbox | open | next | doing | waiting | blocked | deferred | done | dropped`. State machine в `agents/tasks-hub/src/store.py:TRANSITIONS`.

## Команды Telegram

Skill-файлы лежат в `~/.hermes/skills/oru/`:

- `/tasks` — текущий список (просрочено + сегодня + ждёт)
- `/inbox` — triage новых задач
- `/cleanup` — backpressure (stale > 30 дней)
- `/reflect` — последний self-reflection отчёт + force-rerun
- `/debrief` — вечерний flow через LLM event ingestion

## Layout

```
oru/
├── agents/
│   ├── health/                    (port 8001)
│   ├── daily-briefing/            (port 8002)
│   ├── travel-continuity/         (port 8003)
│   ├── tasks-hub/                 (port 8004) — central store
│   │   ├── src/store.py             SQLite + state machine
│   │   ├── src/events.py            append-only JSONL
│   │   ├── src/coordinator.py       hooks (recurrence, write-back)
│   │   ├── src/ingestor/            adapters
│   │   ├── src/render/              jinja views + yaml specs
│   │   ├── src/debrief.py           LLM tool-use ingestion
│   │   ├── src/agent_emitter.py     vendored HTTP client
│   │   ├── src/health_aggregator.py /status/all
│   │   └── tests/                   49 pytest tests
│   └── self-reflection/           (port 8005)
├── host-daemons/                  (macOS launchd shell scripts)
├── secrets/<agent>.env            (per-agent credentials, .gitignored)
├── state/                         (symlink → ~/Library/Application Support/oru-host-state)
│   ├── tasks.db                     SQLite store
│   ├── task-events.jsonl            event log
│   ├── reminders.json               JXA dump
│   ├── reminders-commands*.jsonl    bridge queue + log
│   ├── personal-context.json        bus (read by all)
│   └── all-tasks.md                 living view (auto-generated)
├── docker-compose.yml
└── tests/                         (integration, not container tests)
```

## Setup с нуля

```bash
# Контейнеры
cd /Users/dmitry/Documents/GitHub/oru
cp secrets/_template.env secrets/<agent>.env   # заполнить токены
docker compose up -d --build

# Host daemons (один раз)
bash host-daemons/install.sh
# Macos спросит TCC permissions для Reminders.app — разрешить.

# Sanity check
curl -sS localhost:8004/status/all | jq '.ok, .issues'
```

## Operations

**Полный статус системы:**
```
curl -sS localhost:8004/status/all | jq
```
Возвращает per-container /healthz + 4 host-watcher метрики + список issues. Используется в hermes orchestrator (когда появится) и руками.

**Логи контейнера:**
```
docker compose logs -f --tail 100 <service>
```

**Логи host-watcher'ов:**
```
tail -f ~/Library/Logs/oru-*.log
```

**Перезапуск одного агента:**
```
docker compose restart <service>           # без пересборки
docker compose up -d --build <service>     # после редактирования кода
```

**Перезапуск host-watcher'а:**
```
bash host-daemons/install.sh   # bootout + bootstrap всех 4
```

## Cross-agent контракт

Все агенты используют tasks-hub как единое хранилище followup-задач. Раньше был bus с per-agent ключами в `personal-context.json` (`health.next_followup`, `visa.deadline`, `self.homework_open`) — теперь это derived views (SQL по `owner_agent`).

В `personal-context.json` остались только view-time signals для других агентов:
- `health.state` — `recovery_needed` режет plan_333 пополам в tasks-hub `view.py:_apply_adaptive()`
- `travel.active_trip` — дропает `@home`/`@office` секции
- `self.parts_active`, `self.weekly_themes` — для morning brief

См. `personal-agent/bureau/<agent>/spec.md` для деталей контракта каждого агента.

## Архивированный legacy code

Старый pipeline до tasks-hub (`scripts/tasks_loader.py`, `morning_briefing.py`, `evening_debrief.py`, `apply_debrief.py`, `create_reminder.py`, `generate_tasks_index.py`) — лежит в `personal-agent/scripts/_archived/2026-06-task-system-pre-hermes/` с README о причинах. Не удаляется по политике "30 дней стабильности перед физическим удалением".

## Ссылки

- План реализации (20 идей): `/Users/dmitry/.claude/plans/tender-spinning-engelbart.md`
- Спецификации агентов: `personal-agent/bureau/<agent>/spec.md`
- Hermes runtime: `~/.hermes/hermes-agent/` (Nous Research)
- LLM router (canonical для personal-agent): `personal-agent/scripts/llm_router.py`. **Внутри контейнеров oru** используется LiteLLM напрямую — изоляция Docker от файлов personal-agent была преднамеренной, см. `personal-agent/CLAUDE.md`
