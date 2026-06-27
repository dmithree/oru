# Oru/Hermes live backup manifest

- generated:    2026-06-27T13:08:43Z
- oru commit:   09e7cfc
- hermes home:  /Users/dmitry/.hermes
- engine:       git (not backed up — reinstallable)

## Docker agents (source in oru/agents/)
- daily-briefing
- health
- self-reflection
- tasks-hub
- travel-continuity

## Docker images
- oru-health:dev (445MB)
- oru-daily-briefing:dev (445MB)
- oru-travel-continuity:dev (446MB)
- oru-tasks-hub:dev (445MB)
- oru-self-reflection:dev (445MB)

## Oru skills (~/.hermes/skills/oru)
- DESCRIPTION.md
- cleanup
- debrief
- health
- hello
- inbox
- llm-routing-troubleshooting
- nudge-engine-pattern
- oru-infra
- oru-response-format
- reflect
- tasks
- travel

## Cron jobs (~/.hermes/cron/jobs.json)
- Morning briefing (Oru) (31bafc8d623c): 0 8 * * * -> telegram:204197922
- Travel weekly research (Oru) (a34546460064): 0 9 * * 1 -> origin
- Health research scan (Oru) (94befa046be0): 0 10 * * 1/14 -> origin

## Restore
1. clone oru, fill secrets/*.env and ~/.hermes/.env from *.env.template
2. `bash hermes/install.sh`  (deploys SOUL/skills/script, upserts cron)
3. `docker compose up -d --build`  (brings up all agents)
