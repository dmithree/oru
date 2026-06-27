# Oru (Dima's personal automation)

Custom skills for Dima's personal Hermes deployment ("Oru"). Each skill talks
to a specific local service running on this Mac.

## Skills

- **weekly-health-digest** — fetches health metrics from the `oru-health` Docker
  container (Oura + Strava). Use when Дима asks about his health, fitness,
  recovery, HRV, or types `/health`.

## Architecture

Skills in this category bridge between Hermes (the agent) and Dima's
self-hosted containerized agents (Docker services on localhost). Each skill is
a thin orchestration layer: invoke local HTTP endpoint, parse JSON, return to
user verbatim. Heavy logic (data fetching, classification, LLM analysis) lives
inside the containers.
