---
name: "llm-routing-troubleshooting"
title: "LLM Routing & Dead Letter Queue (DLQ) Troubleshooting"
description: "Diagnose and repair multi-agent LLM routing failures: locate DLQ messages, categorize errors (code bugs, rate limits, fallback exhaustion), and apply targeted fixes."
trigger: |
  - Agent fails to process a routed LLM task (e.g., `artem-bot` cannot handle `artem_select_llama`)
  - DLQ alert arrives: "gave up on <task> (agent=X, retries=N)"
  - Rate-limit errors cascade across fallback models
  - Multi-agent LLM routing system produces silent failures or timeout loops
  - Need to investigate why a task landed in Dead Letter Queue
tags: ["multi-agent", "llm-routing", "debugging", "rate-limiting", "personal-agent"]
---

## Overview

Multi-agent LLM systems use routing layers to dispatch tasks to specialized agents (e.g., `artem-bot` for character reply selection, `claude-3-agent` for general reasoning). When routing or LLM calls fail repeatedly, tasks accumulate in **Dead Letter Queues (DLQ)** and trigger alerts.

This skill teaches how to:
1. Locate and inspect DLQ messages
2. Diagnose root causes (code bugs, rate limits, model failures)
3. Understand rate-limit cascades and fallback model exhaustion
4. Repair broken agents or adjust routing configs

## Trigger Conditions

Use this skill when:
- An agent returns HTTP 5xx or timeout during LLM invocation
- A routed task hits 5+ consecutive failures and gets moved to DLQ
- You receive an alert like: `"LLM router DLQ: gave up on <timestamp>_<agent>_<hash>.json (retries=5). Manual review needed."`
- Rate-limit errors (HTTP 429) dominate the retry attempts for a free-tier model group

## Investigation Flow

### Phase 1: Locate the DLQ Message

**Pattern:** DLQ storage is typically in `~/Documents/GitHub/personal-agent/scripts/llm_dlq/dead/`

```bash
find ~/Documents/GitHub/personal-agent/scripts/llm_dlq/dead -name "*.json" | head -5
```

Each file is named `<timestamp>Z_<agent>_<hash>.json` and contains:
- `queued_at` — ISO timestamp when task was first routed
- `agent` — target agent name
- `task` — task identifier
- `system` — system prompt or context string
- `messages` — the user query and candidates (for LLM) or inputs
- `attempts[]` — array of failures with error messages and latencies

### Phase 2: Analyze Failure Patterns in `attempts[]`

Common failure types and their meanings:

**Type A: Code bugs (e.g., TypeError, AttributeError)**
```json
{"error": "TypeError: '>' not supported between instances of 'list' and 'float'"}
```
→ Agent code has a logic error. Fix in source, rebuild image, restart container.

**Type B: Rate limits (HTTP 429) from free-tier models**
```json
{"error": "RateLimitError: litellm.RateLimitError: OpenrouterException - {...\"code\":429...}"}
```
→ Free Llama via OpenRouter/Venice hit rate limit. Options:
  - Wait and retry (Venice quota resets ~hourly)
  - Add your own OpenRouter API key to bypass free tier
  - Switch to a different model alias in the router config

**Type C: Upstream model errors**
```json
{"error": "APIConnectionError: Connection timeout trying to reach API"}
```
→ Provider is down or unreachable. Check provider status, verify credentials.

**Type D: Fallback exhaustion**
```json
{"error": "No fallback model group found for original model_group=X..."}
```
→ Primary model + all fallbacks failed. Check litellm config for model group definitions.

### Phase 3: Root Cause Deduction

**If all attempts show the SAME error:** Single systemic issue (bug, rate limit, credentials).
**If attempts show DIFFERENT errors:** Cascading failures (primary fails → fallback tried → fallback fails).
**If latency is high (~60s) before timeout:** Likely a slow API call or network timeout, not a fast code error.

### Phase 4: Repair Workflow

**For code bugs:**
1. Identify the agent (e.g., `artem-bot`)
2. Locate source: `~/Documents/GitHub/oru/agents/<agent>/src/`
3. Fix the bug in the source code
4. Rebuild: `docker compose build <agent>` (code is COPYed at build time, not mounted)
5. Restart: `docker compose up -d <agent>`
6. Re-queue the task or wait for auto-retry

**For rate limits on free-tier models:**
1. Check OpenRouter account settings for available credits/limits
2. Option A: Add API key to `litellm.yaml` under model group aliases
3. Option B: Reduce QPS (queries per second) by adjusting router backoff
4. Option C: Switch to a different model (e.g., use `openrouter-qwen` instead of `artem-free-llama`)

**For model fallback exhaustion:**
1. Check `litellm.yaml` or agent config for `model_groups` and `fallbacks`
2. Add a working fallback (e.g., add Grok, Qwen, DeepSeek as backup options)
3. Verify that fallback models have valid credentials/keys

---

## Key Files & Paths

| Item | Location |
|------|----------|
| DLQ inbox | `~/Documents/GitHub/personal-agent/scripts/llm_dlq/dead/` |
| DLQ monitoring daemon | `~/Library/LaunchAgents/com.personal.llm-dlq-drain.plist` |
| DLQ monitoring script | `~/Documents/GitHub/personal-agent/scripts/llm_dlq_drain.py` |
| LiteLLM config | TBD — check agent Dockerfile or runtime config |
| Multi-agent router | TBD — check oru repo coordinator.py or equivalent |

---

## Pitfalls

- **Pitfall: Rebuilding instead of restarting.** If you only restart a container, code changes won't apply — Docker images have code COPYed at build time. Always `docker compose build <service>` + `docker compose up -d <service>`.
- **Pitfall: Assuming transient != permanent.** A single 429 is transient; 5 consecutive 429s with increasing latency means rate limit is real, not fluke.
- **Pitfall: Ignoring latency in error logs.** 60s latency before timeout ≠ fast code error. Indicates network issue or slow provider.
- **Pitfall: Forgetting to clear old DLQ messages.** After fixing a bug, old messages stay in `/dead/`. Safe to delete once root cause is confirmed.

---

## References

- `references/dlq-investigation-20260625.md` — Live example from session: OpenRouter free Llama rate limiting + TypeError in artem-bot

---

## Next Steps

- [ ] Identify which agent is failing (check alert / DLQ filename)
- [ ] Inspect the DLQ file's `attempts[]` to categorize the error type
- [ ] Apply the repair workflow for that error type
- [ ] Verify fix (restart container, check logs, optionally re-queue task)
