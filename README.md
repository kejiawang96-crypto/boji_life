# AI Health Log

[中文](README.zh-CN.md)

A personal health data hub: one private Git repo holds training, meals, sleep
and body metrics, so any AI assistant can read the full context and keep
logging without asking twice.

Forked from the [ai-health-log template](https://github.com/ZhenhaoLi00/ai-health-log)
and extended with richer training tables, progression tracking, energy
balance, sleep analysis and charts.

> **Privacy first:** this is a *template*. Create your own repo from it
> (GitHub → *Use this template*) and keep that repo **private** — it will
> hold your real body data. Never commit API keys or tokens; see
> [SETUP.md](SETUP.md). The sync script checks every GitHub push destination
> and refuses to push unless each is verified private.

## Set up with your AI

Copy everything between the lines and paste it into any AI assistant
(ChatGPT, Claude, Codex, Muse, ...). It will walk you through the whole setup.

---

I want to set up my own AI health log from this template.

Please:
1. Read README.md, README.zh-CN.md, AGENTS.md and SETUP.md in the template repo first.
2. Ask me for: my timezone, height, current weight, primary goal (fat loss /
   muscle gain / maintenance), daily protein and calorie targets (suggest them
   if I don't know), training frequency, and which training app I use
   (e.g. SynFit (训记), Apple Health, or manual logging).
3. Walk me through each step: creating a PRIVATE repo from the template,
   filling in profile.md, getting my training app's API key and storing it
   outside the repo (chmod 600), configuring git access (deploy key or PAT),
   running scripts/health-daily-sync.py, and scheduling it to run daily
   (GitHub Actions workflow included).
4. Follow the repo conventions: never commit secrets, cache API responses by
   date, keep estimates labeled as estimates, and always show me a change
   summary and wait for my confirmation before writing anything back to a
   third-party API.

---

## What's tracked

| Source | What | How |
|---|---|---|
| 训记 / Xunji (SynFit) Open API | Strength training logs | `scripts/health-daily-sync.py`, daily via GitHub Actions |
| Apple HealthKit | Workouts, sleep | Assistant's health connector → daily log |
| Manual | Meals (photo estimates), fasted weight, waist, notes | Just tell your assistant |

Start by filling in `profile.md`: height, weight, age, goal, protein/calorie
targets, training plan, and a conservative BMR.

## Repository layout

```
README.md / README.zh-CN.md  This documentation
SETUP.md                     Step-by-step setup guide
AGENTS.md                    Conventions for AI assistants editing this repo
SUMMARY.md                   Human-readable overview (auto-regenerated, do not edit)
profile.md                   Your goals, targets, baselines (fill in first)

data/daily/YYYY-MM-DD.md     The daily log — everything human-readable lives here
data/daily/EXAMPLE.md        Format example with fake data
data/workouts/               Raw SynFit API cache, one JSON per date (machine use)

reports/weekly.md            This natural week, Mon–Sun (auto-regenerated)
reports/monthly.md           This calendar month (auto-regenerated)
reports/weight-trend.png     Fasted-weight chart with target line
reports/sleep/               30-day sleep charts: stages, quality, vitals

scripts/health-daily-sync.py Sync SynFit → daily log, regenerate reports, push
scripts/health-energy-sync.py Daily energy balance ledger
scripts/health-sleep-sync.py  Apple HealthKit sleep → daily log
scripts/health-workout-sync.py Apple HealthKit workouts → daily log (nightly)
```

## The daily log

One file per day, `data/daily/YYYY-MM-DD.md`, with these sections:

- `## Sleep` — human summary plus a hidden `<!-- SLEEP-DATA {...} -->`
  machine block (stages, quality, efficiency, vitals)
- `## Meals` — `Day total:` line, then one `###` subsection per meal.
  Photo estimates are marked `estimated`; only nutrition labels or
  user-stated values are `verified`
- `## Training` — auto-managed SynFit block (see below)
- `## Body metrics` — fasted weight (daily), waist (weekly)
- `## Energy balance` — intake vs expenditure ledger (see below)
- `## Notes`

### Training entries

Each exercise renders as its own markdown table — one row per completed set,
never merged — with weights in the API's native unit (lb for 训记):

**Bench press**

| Set | Weight | Reps | Last 12-28 weight | Last 12-28 reps |
|---|---|---|---|---|
| 1 | 135 lbs | 10 | 135 lbs | 8 |
| 2 | 135 lbs | 10 | 135 lbs | 8 |

When the same exercise was trained before, the table gains right-hand columns
with that session's sets side by side, plus a progression line:

`vs last 12-28: volume +620 lb (+21.7%) · top weight +20 lbs · total reps +6`

(Volume = Σ weight × reps over completed sets.)

Each session also gets a rough calorie estimate from lifted volume.

### Energy balance

`scripts/health-energy-sync.py` maintains a daily ledger:

- **Expenditure** = BMR × 1.2 (sedentary baseline incl. daily activity) + exercise
- **Exercise calories**: measured values (e.g. Apple Watch) preferred; the
  SynFit volume-based estimate is the fallback (never double-counted)
- **Projection**: daily balance × 7 / 7700 = expected kg/week at this rate

Re-run after every meal log.

## Reports

- **Weekly** (`reports/weekly.md`): the natural week (Monday–Sunday) through
  today — training volume, meals, weight trend, energy balances, and a
  **sleep analysis** comparing the week's nights against benchmarks
  (7–9 h for adults, deep 13–23 %, REM 20–25 %, efficiency ≥ 85 %).
- **Monthly** (`reports/monthly.md`): the calendar month through today.
- Charts: `reports/weight-trend.png` (fasted weight + target line) and
  `reports/sleep/` (30-day stages / quality / vitals).

## Automation

`.github/workflows/daily-sync.yml` runs `scripts/health-daily-sync.py`
**daily** (default ~22:30 `America/Chicago` — adjust the cron and
`HEALTH_LOG_TZ` to your timezone): pulls → fetches yesterday + today from
SynFit (`--force-refresh`, cached per date otherwise) → updates the daily
log → regenerates `SUMMARY.md`, weekly/monthly reports and the weight chart
→ commits and pushes when anything changed. Also manually triggerable via
`workflow_dispatch`.

Schedule `scripts/health-workout-sync.py` nightly on whatever runs your
assistant (cron, scheduled task): it pulls the day's Apple HealthKit workouts
into the daily log's `## Training` section and re-runs the energy-balance
script. Set `HEALTH_LOG_EXPECTED_REMOTE` to `owner/repo` of your private repo
so the push guard knows your destination.

## Conventions for AI assistants

1. **Read before writing.** Check `profile.md`, `AGENTS.md` and today's
   `data/daily/` log before adding anything.
2. **One file per day.** Everything human-readable goes in
   `data/daily/YYYY-MM-DD.md`. `data/workouts/` is machine cache — read it,
   don't hand-edit it.
3. **Hands off managed blocks.** Only the fenced `XUNJI SYNC` block inside
   `## Training` is owned by the sync script; preserve everything outside the
   markers, and never edit the markers themselves.
4. **Estimated vs verified.** Photo-based meal estimates are always
   `estimated`. Never present an estimate as a measurement.
5. **Regenerate, don't hand-edit.** `SUMMARY.md` and everything under
   `reports/` are generated.
6. **Confirm before writing back** to any third-party API: show the change
   summary first and wait for the user's OK. Reading and local repo updates
   don't need confirmation.
7. **Never commit secrets.** API keys live outside the repo (see SETUP.md).

## Data sources

This template ships with a SynFit (训记) integration. The layout also works
for manual logging or other training apps — have your AI adapt
`scripts/health-daily-sync.py` or log directly into `data/daily/`.

## License

[MIT](LICENSE) covers the template software and documentation, **not your
private health data**. Your personal logs must not be redistributed.
