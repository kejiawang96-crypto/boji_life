#!/usr/bin/env python3
"""Daily energy balance: intake vs expenditure -> surplus/deficit + weekly projection.

Reads:
  - BMR (conservative) from profile.md
  - intake from reports/daily/YYYY-MM-DD.md (## Meals, Day total line)
  - strength volume from data/workouts/YYYY-MM-DD.json (0.02 kcal/lb heuristic,
    shared with health-daily-sync.py)
  - workout sessions from Apple HealthKit via health-cli (energy_burned_kcal)

Writes a marker-managed ## Energy balance section into reports/daily/YYYY-MM-DD.md.

Model (approximate, labeled as such):
  expenditure = BMR * 1.2 (sedentary baseline incl. NEAT) + exercise kcal
  balance     = intake - expenditure
  projected kg/week = balance * 7 / 7700   (7700 kcal ~= 1 kg body fat)

If an Apple Watch strength session exists for the day, its measured calories
are used instead of the xunji volume estimate to avoid double-counting.
"""

import argparse
import datetime as dt
import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

REPO = Path(os.environ.get("HEALTH_LOG_REPO", Path(__file__).resolve().parent.parent)).expanduser().resolve()
TIMEZONE = os.environ.get("HEALTH_LOG_TZ", "")
START_MARKER = "<!-- BEGIN ENERGY SYNC -->"
END_MARKER = "<!-- END ENERGY SYNC -->"
KCAL_PER_KG_FAT = 7700
SEDENTARY_FACTOR = 1.2  # covers NEAT for a desk day; exercise counted separately

_spec = importlib.util.spec_from_file_location(
    "xunji_sync", REPO / "scripts" / "health-daily-sync.py")
_xunji = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_xunji)


def local_now() -> dt.datetime:
    tz = ZoneInfo(TIMEZONE) if TIMEZONE else dt.datetime.now().astimezone().tzinfo
    return dt.datetime.now(tz)


def parse_bmr() -> float:
    text = (REPO / "profile.md").read_text(encoding="utf-8")
    m = re.search(r"^-\s*BMR:\s*~?([\d,]+)\s*kcal", text, re.M)
    if not m:
        raise RuntimeError("BMR not found in profile.md")
    return float(m.group(1).replace(",", ""))


def parse_intake(day: dt.date) -> tuple[float, bool]:
    """Parse the day total from the ## Meals section of the daily log."""
    path = REPO / "reports" / "daily" / f"{day.isoformat()}.md"
    if not path.is_file():
        return 0.0, False
    text = path.read_text(encoding="utf-8")
    m = re.search(r"(?ms)^## Meals\n(.*?)(?=^## |\Z)", text)
    if not m:
        return 0.0, False
    t = re.search(r"^-\s*Day total:\s*~?([\d,]+)\s*kcal", m.group(1), re.M)
    if not t:
        return 0.0, False
    return float(t.group(1).replace(",", "")), True


def strength_volume(day: dt.date) -> tuple[float, float]:
    """Return (kcal_estimate, volume_lb) from xunji data; (0, 0) if none."""
    path = REPO / "data" / "workouts" / f"{day.isoformat()}.json"
    if not path.is_file():
        return 0.0, 0.0
    try:
        trains = _xunji.validate_response(
            json.loads(path.read_text(encoding="utf-8")))["res"]["trains"]
    except (OSError, ValueError):
        return 0.0, 0.0
    volume = 0.0
    for train in trains:
        for move in train.get("movements", []):
            for item in move.get("sets", []):
                if not item.get("done"):
                    continue
                w_val, r_val, _ = _xunji.set_weight_reps(item)
                if w_val is not None and r_val is not None:
                    volume += w_val * r_val
    return volume * _xunji.KCAL_PER_LB_VOLUME, volume


def apple_workouts(day: dt.date) -> tuple[list[dict], bool]:
    cmd = ["health-cli", "query", "sessions", "--provider", "healthkit",
           "--category", "workout",
           "--start-date", day.isoformat(), "--end-date", day.isoformat()]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
        data = json.loads(out.stdout)
    except Exception as exc:  # noqa: BLE001 - health-cli is best-effort here
        print(f"WARNING: apple workout query failed: {exc}", file=sys.stderr)
        return [], False
    records = data.get("records") or []
    complete = bool((data.get("coverage") or {}).get("complete", True))
    return records, complete


def fmt_kcal(x: float) -> str:
    return f"{x:,.0f}"


def update_daily(day: dt.date, block: str) -> None:
    path = REPO / "reports" / "daily" / f"{day.isoformat()}.md"
    if path.exists():
        content = path.read_text(encoding="utf-8")
    else:
        content = (f"# {day.isoformat()}\n\n## Sleep\n- (None)\n\n## Meals\n- (None)\n\n"
                   "## Training\n\n## Body metrics\n- (None)\n\n## Notes\n- (None)\n")
    managed = f"{START_MARKER}\n{block}\n{END_MARKER}"
    pattern = re.compile(r"(?ms)^## Energy balance\n.*?(?=^## |\Z)")
    if pattern.search(content):
        content = pattern.sub(f"## Energy balance\n{managed}\n", content)
    elif "## Body metrics" in content:
        content = content.replace("## Body metrics",
                                  f"## Energy balance\n{managed}\n\n## Body metrics", 1)
    else:
        content = content.rstrip() + f"\n\n## Energy balance\n{managed}\n"
    path.write_text(content, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", help="YYYY-MM-DD (default: today)")
    args = parser.parse_args(argv)

    day = dt.date.fromisoformat(args.date) if args.date else local_now().date()

    bmr = parse_bmr()
    intake, has_meals = parse_intake(day)
    vol_kcal, volume_lb = strength_volume(day)
    records, coverage_ok = apple_workouts(day)

    strength_watch = [r for r in records
                      if r.get("workout_type") == "traditionalStrengthTraining"]
    other = [r for r in records
             if r.get("workout_type") != "traditionalStrengthTraining"]
    watch_strength_kcal = sum(r.get("energy_burned_kcal") or 0 for r in strength_watch)
    other_kcal = sum(r.get("energy_burned_kcal") or 0 for r in other)

    if watch_strength_kcal > 0:
        strength_kcal = watch_strength_kcal
        mins = sum(r.get("active_duration_sec") or 0 for r in strength_watch) / 60
        strength_note = f"Apple Watch strength {mins:.0f} min (watch value used instead of xunji volume to avoid double-counting)"
    elif vol_kcal > 0:
        strength_kcal = vol_kcal
        strength_note = f"xunji volume ~{volume_lb:,.0f} lb (rough estimate)"
    else:
        strength_kcal = 0.0
        strength_note = "none logged"

    other_note = ("none" if not other else "; ".join(
        f"{r.get('workout_type')} ~{r.get('energy_burned_kcal') or 0:.0f} kcal"
        for r in other))
    exercise_kcal = strength_kcal + other_kcal

    baseline = bmr * SEDENTARY_FACTOR
    expenditure = baseline + exercise_kcal
    balance = intake - expenditure
    proj = balance * 7 / KCAL_PER_KG_FAT

    lines = [
        f"- BMR: {fmt_kcal(bmr)} kcal/day (conservative estimate)",
        f"- Baseline (BMR x {SEDENTARY_FACTOR}, sedentary incl. NEAT): {fmt_kcal(baseline)} kcal",
        f"- Intake: {fmt_kcal(intake)} kcal" + ("" if has_meals else " (no meals logged yet)"),
        f"- Exercise: {fmt_kcal(exercise_kcal)} kcal — strength: {strength_note}; other: {other_note}",
    ]
    if not coverage_ok:
        lines.append("- Note: Apple Health device sync incomplete for this day; more sessions may appear later")
    if has_meals:
        direction = "deficit" if balance < 0 else "surplus"
        lines += [
            f"- Expenditure: {fmt_kcal(expenditure)} kcal",
            f"- Balance: {fmt_kcal(balance)} kcal ({direction})",
            f"- Projected: {proj:+.2f} kg/week if every day looked like today ({KCAL_PER_KG_FAT} kcal ~= 1 kg)",
        ]
    else:
        lines.append("- Balance: pending meal logs")
    block = "\n".join(lines)

    update_daily(day, block)
    print(f"day: {day.isoformat()}")
    print(block)
    return 0


if __name__ == "__main__":
    sys.exit(main())
