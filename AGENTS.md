# Instructions for AI assistants

This repository is a personal health-data store, **not** a place to invent observations or give medical diagnoses.

## Before editing
1. Read README.md, profile.md, and the relevant daily log.
2. Treat imported records as source data. Never replace real measurements with estimates.
3. Store all access credentials outside the repository. Do not print tokens or private responses in logs.
4. Never push real personal health data to a public repository; use a private copy.
   The sync script verifies all GitHub origin push URLs before writing/pushing
   (requires an authenticated GitHub CLI or GitHub API token).

## Ownership and provenance
- data/workouts/YYYY-MM-DD.json: raw SynFit response, owned by sync script; do not hand-edit.
- data/daily/YYYY-MM-DD.md: human/agent notes plus an automatically owned SynFit block.
- reports/weekly.md, reports/monthly.md, reports/weight-trend.png, reports/sleep/ and SUMMARY.md: generated outputs; do not hand-edit.
- data/meals/: retired; meals now live in the daily log's ## Meals section.
- data/metrics/: retired; body metrics now live in the daily log's ## Body metrics section.
- data/sleep/: retired; sleep now lives in the daily log's ## Sleep section.

The two markers inside a daily Training section delimit an auto-managed block:

<!-- BEGIN XUNJI SYNC -->
- Imported workout entries only
<!-- END XUNJI SYNC -->

Training entries render as one markdown table per exercise (one row per
completed set, no merging of identical sets). When the same exercise was
trained on an earlier day, the table gains two right-hand columns with that
session's sets side by side, plus a progression line:

**哑铃推肩**

| 组 | 重量 | 次数 | 上次 10-07 重量 | 上次 10-07 次数 |
|---|---|---|---|---|
| 1 | 15 lbs | 12 | 12.5 lbs | 12 |
| 2 | 15 lbs | 12 | 12.5 lbs | 12 |

vs 上次 10-07: 容量 +120 lb (+17.4%) · 最高重量 +2.5 lbs · 总次数 +0

Volume = Σ(weight × reps) over completed weighted sets. The lookup walks back
through data/workouts/*.json (up to 180 days) matching the exact movement name.
(Markdown tables cannot carry cell colors, so the previous session is
distinguished by its column headers rather than red text.)

Preserve everything outside these markers. Never edit or delete the markers manually.
The `XUNJI SYNC` marker spelling is a legacy internal identifier retained for
existing logs; SynFit is the app's English name.

## Record conventions
- Use ISO 8601 dates (YYYY-MM-DD) and explicit timezone for timestamps.
- Record measurement units: kg, cm, hours, kcal, and g.
- Include source (e.g. SynFit, Apple Health, user), timestamp and confidence/estimated flag when relevant.
- For meals, distinguish estimates from verified nutrition-label values.
- Missing data means unknown, not zero. Do not claim a rest day because there is no workout record.
- Check whether a record already exists before adding another one for the same event.

## Meal logging
- The user sends a photo per meal and names the slot: breakfast / lunch /
  dinner; anything else is a snack.
- Log meals directly in the day's `data/daily/YYYY-MM-DD.md` under `## Meals`:
  first line `- Day total: N kcal | protein Pg | fat Fg | carbs Cg (estimated)`,
  then one `### <Slot> — <time>` subsection per meal listing visible items and
  a `- Subtotal:` line. (`data/meals/` is retired.)
- Each meal records: time (with timezone), visible items, and estimated
  macros — calories (kcal), protein (g), fat (g), carbs (g).
- All photo-based estimates are marked `estimated`; only use `verified` when
  the value comes from a nutrition label or the user states it.
- Scripts parse the `- Day total:` line and count `### ` subsections; keep
  that format.

## Body metrics
- Fasted morning weight and waist go directly under `## Body metrics` in the
  day's daily log, one per line:
  `- Fasted weight: 70.0 kg (morning)`, `- Waist: 80.0 cm (Sunday)` (example values).
- Scripts parse the `- Fasted weight:` and `- Waist:` lines (weight chart,
  weekly/monthly summaries); keep that exact format.
- (`data/metrics/` is retired.)

## Sleep
- `scripts/health-sleep-sync.py` syncs Apple HealthKit sleep into the daily
  log's `## Sleep` section (marker-managed). The block holds a human-readable
  summary plus a machine-readable `<!-- SLEEP-DATA {...} -->` comment; charts
  (reports/sleep/) and period summaries read the embedded data back from the
  daily logs. (`data/sleep/` is retired.)

## Energy balance
- `scripts/health-energy-sync.py` computes the day's balance from profile BMR
  (conservative), meal intake, xunji strength volume, and Apple Health workouts.
- Writes a marker-managed `## Energy balance` section into the daily log:
  `<!-- BEGIN ENERGY SYNC -->` / `<!-- END ENERGY SYNC -->`.
- Model: expenditure = BMR x 1.2 (sedentary incl. NEAT) + exercise kcal;
  projected kg/week = balance x 7 / 7700.
- If an Apple Watch strength session exists, its measured calories replace the
  xunji volume estimate to avoid double-counting.
- Re-run it after logging meals so the day's numbers stay current.

## Writes and conflicts
1. Pull the latest revision before modifying a file. If another assistant changed it, reconcile rather than overwriting.
2. Present a concise summary of proposed changes before writing to third-party APIs. Get explicit approval first.
3. A local health-log update may follow the user's request, but preserve human notes and all other providers' data.
4. Do not force push, erase history, or silently overwrite conflicting records.
5. Do not automatically send personal data to external analysis services. Seek permission before changing integrations.

## Analysis
- Derive trends from dated observations and state the analysis window.
- Explain gaps in data coverage and avoid clinical diagnoses.
- Keep advice separate from underlying observations.
