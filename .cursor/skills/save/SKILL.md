---
name: save
description: Smart git save — analyzes changes, generates meaningful commit message, commits and pushes to GitHub. Use when the user runs /save, or asks to save changes, commit and push, or push to GitHub.
---

# Smart Git Save

Commits and pushes all changes to GitHub with auto-generated commit messages.

## Triggers

- `/save`
- "save changes", "commit and push", "push to github"

## Workflow

### 1. Snapshot the live Oru/Hermes runtime

Back up the deployed `~/.hermes` Oru layer (skills, persona, cron jobs, scripts,
non-secret config + restore manifest) into `backup/hermes/` so this push captures
it. The script is secret-safe — allowlist only, plus a secret-value scan that
ABORTS on any leak:

```bash
bash hermes/backup.sh
```

If it exits non-zero (secret detected), **STOP** — do not stage or commit. Report
the offending file. Skip this step only if `hermes/backup.sh` is absent.

### 2. Update Workspace Structure (if applicable)

If `scripts/update_cursorrules_structure.py` exists, run it before staging:

```bash
python3 scripts/update_cursorrules_structure.py
```

Skip this step if the script is not present in the repo.

### 3. Inspect and Stage Changes

Run in parallel:

```bash
git status
git diff
git log -5 --oneline
```

Then stage:

```bash
git add -A
```

### 4. Generate Commit Message

Analyze staged changes and create a descriptive commit message:
- Format: `Update workspace: YYYY-MM-DD HH:MM` for routine saves
- For significant changes, use a more descriptive message focused on the "why"

Do not commit files that likely contain secrets (`.env`, credentials, etc.).

### 5. Commit

```bash
git commit -m "Update workspace: $(date '+%Y-%m-%d %H:%M')"
```

Use a descriptive message instead of the timestamp when changes are significant.

### 6. Push

```bash
git push origin main || git push https://github.com/dmithree/oru.git main
```

Use SSH first, fall back to HTTPS if SSH fails.

## Rules

- Run workspace structure update only when the script exists
- Use timestamp-based commit messages for routine saves
- Use descriptive messages for significant changes
- Fall back SSH → HTTPS for push
- Never skip hooks, force-push to main, or amend unless explicitly requested
