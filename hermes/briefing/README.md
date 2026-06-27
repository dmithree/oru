# Morning briefing — Hermes side

The morning brief is **owned by the Hermes agent** (it voices the data in Oru's
voice via `SOUL.md` and owns delivery to Telegram). This folder is the
Hermes-side half of the mechanism, kept together. The other half — the data
generator — is the `oru-daily-briefing` Docker container under
`oru/agents/daily-briefing/`.

## Flow

```
cron "Morning briefing (Oru)" (31bafc8d623c, 08:00 MSK)
  → runs ~/.hermes/scripts/morning_briefing_data.sh   (deployed copy of this folder's script)
      → POST http://127.0.0.1:8002/run-morning?notify=false   (oru-daily-briefing container)
          container aggregates Oura/Strava/tasks/Linear/reminders/transcripts, builds brief,
          saves it, but does NOT send (notify=false)
      → script prints brief.summary to stdout
  → Hermes injects stdout as "Script Output", agent rewrites it in Oru voice (SOUL.md)
  → Hermes delivers the final message to Telegram (deliver: telegram:204197922)
```

Single sender: the container's internal 07:30 scheduler runs with `notify=False`
(see `oru/agents/daily-briefing/src/main.py::_morning_job`), so only this cron
path delivers.

## Files

- `morning_briefing_data.sh` — the data layer. Canonical source; deployed as a
  **real copy** to `~/.hermes/scripts/` by `../install.sh` (the cron sandbox
  rejects symlinks that resolve outside `~/.hermes/scripts`).
- `cron.json` — canonical declaration of the cron job (one job object). Upserted
  into the live `~/.hermes/cron/jobs.json` by `../install.sh`, preserving runtime
  fields (`last_run_at`, `next_run_at`, `last_status`, …).

## Deploy

```
bash oru/hermes/install.sh
```

Re-run after editing any file here or under `oru/hermes/` (deploy is by copy, so
edits don't take effect until redeployed).
