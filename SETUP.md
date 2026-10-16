# Setup Guide

Follow these steps to get your own private AI health log running.

## 1. Create your repo

- Create a **new, private** GitHub repo (e.g. `my-health-log`). Do **not**
  fork with history — start clean so no one else's data comes along.
- Copy the files from this template into it (or clone this template and push
  to your new remote).

## 2. Fill in your profile

Edit `profile.md`:

- Current phase goal: fat loss / muscle gain / maintenance
- Daily protein and calorie targets
- Training frequency and program
- Body baseline: height, weight, body fat

## 3. Get your training data (SynFit / 训记)

1. In SynFit (训记), find the Open API section and create an API key.
2. Save it **outside the repo**:
   ```bash
   mkdir -p ~/.config/xunji
   # paste your key into this file, then:
   chmod 600 ~/.config/xunji/api_key
   ```
3. The sync script reads the key from `$XUNJI_API_KEY_FILE`
   (default: `~/.config/xunji/api_key`). This is a **legacy technical
   identifier retained for backward compatibility**; the app's English name
   is SynFit. The key is never printed or committed.

API limits to respect: light reads ≥ 15s apart, full reads ≥ 30s, writes ≥ 45s.

## 4. Configure git access for automation

If a machine (or agent) should push on your behalf without your personal login:

- **Option A — Deploy key (recommended for one repo):** repo Settings →
  Deploy keys → add a key with write access. It only works for that repo.
- **Option B — Fine-grained PAT:** scoped to the single repo, contents:
  read+write.

Point the script at your key:
```bash
export GIT_SSH_COMMAND="ssh -i ~/.ssh/my-health-log-deploy -o IdentitiesOnly=yes"
```

### Verify the GitHub remote is private

Before fetching data or pushing, the sync script checks **all GitHub push
URLs** using GitHub's authenticated REST API. It refuses to push to a public
repo or when visibility cannot be verified. Use one of:

- `gh auth login` to sign in with GitHub CLI (recommended); or
- Set `GH_TOKEN` / `GITHUB_TOKEN` with read access to the target private
  repository's metadata.

**SSH deploy-key access alone is not sufficient** for the separate REST API
privacy check. On cron/automation hosts, ensure the `gh` executable and
authentication are available to the running user. To update files without
any git push or privacy API check, use `--no-git` (local-only).

## 5. Run the sync

```bash
export HEALTH_LOG_REPO=~/my-health-log        # your local clone
export HEALTH_LOG_TZ=America/Chicago         # your timezone
python3 scripts/health-daily-sync.py
```

The script: pulls → fetches yesterday + today from SynFit (cached by explicit fetch timestamp) →
updates the daily logs → regenerates `SUMMARY.md`, `reports/weekly.md`,
`reports/monthly.md` and `reports/weight-trend.png` →
commits and pushes only when something changed, and only to verified private
GitHub push URLs. Each date keeps raw `data/workouts/YYYY-MM-DD.json` plus
`data/workouts/YYYY-MM-DD.meta.json` with `fetched_at` in UTC. Cache
validation uses this timestamp, not the local file's modification time.
Pre-existing raw JSON without companion metadata is fetched once to establish
the new cache. Use `--force-refresh` to refresh a record on demand.

## 6. Schedule it daily

Example cron (runs ~22:30 local time, quiet on success):

```cron
30 22 * * * HEALTH_LOG_REPO=~/my-health-log /usr/bin/python3 ~/my-health-log/scripts/health-daily-sync.py >> ~/.health-log-sync.log 2>&1
```

## 7. Connect Apple Health (optional)

If your AI assistant supports a HealthKit connector, authorize it on your
phone. Workouts (runs, badminton, gym, ...) and sleep then flow in
automatically — have the assistant merge them into `reports/daily/`.

## 8. Log meals

Just tell your assistant what you ate. It estimates calories/protein and
writes them into the `## Meals` section of `reports/daily/YYYY-MM-DD.md`
(one `###` subsection per meal, with a `Day total:` line). Photo estimates
are marked `estimated`; only nutrition labels or values you state are
`verified`.
