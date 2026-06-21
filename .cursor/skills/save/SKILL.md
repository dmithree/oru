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

### 1. Update Workspace Structure (if applicable)

If `scripts/update_cursorrules_structure.py` exists, run it before staging:

```bash
python3 scripts/update_cursorrules_structure.py
```

Skip this step if the script is not present in the repo.

### 2. Inspect and Stage Changes

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

### 3. Generate Commit Message

Analyze staged changes and create a descriptive commit message:
- Format: `Update workspace: YYYY-MM-DD HH:MM` for routine saves
- For significant changes, use a more descriptive message focused on the "why"

Do not commit files that likely contain secrets (`.env`, credentials, etc.).

### 4. Commit

```bash
git commit -m "Update workspace: $(date '+%Y-%m-%d %H:%M')"
```

Use a descriptive message instead of the timestamp when changes are significant.

### 5. Push

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
