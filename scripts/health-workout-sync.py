#!/usr/bin/env python3
"""Nightly Apple HealthKit workout sync into the daily log.

Reads:
  - workout sessions for the day via `health-cli` (best-effort)

Writes:
  - a marker-managed "### Apple Health" workout list inside the daily log's
    ## Training section (placed after the XUNJI SYNC block; never touches it)
  - then re-runs scripts/health-energy-sync.py so the ## Energy balance
    ledger reflects the day's exercise calories

Commits and pushes when anything changed. Refuses to push unless the origin
remote is the expected private repo.
"""

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

REPO = Path(os.environ.get("HEALTH_LOG_REPO", Path(__file__).resolve().parent.parent)).expanduser().resolve()
TIMEZONE = os.environ.get("HEALTH_LOG_TZ", "")
EXPECTED_REMOTE = os.environ.get(
    "HEALTH_LOG_EXPECTED_REMOTE", "your-github-username/your-health-log")
# Set HEALTH_LOG_EXPECTED_REMOTE to "owner/repo" of your private repo.
# Pushes are refused unless the origin remote matches it.
START_MARKER = "<!-- BEGIN APPLE-WORKOUT SYNC -->"
END_MARKER = "<!-- END APPLE-WORKOUT SYNC -->"

TYPE_CN = {
    "traditionalStrengthTraining": "力量训练",
    "badminton": "羽毛球",
    "running": "跑步",
    "walking": "步行",
    "cycling": "骑行",
    "swimming": "游泳",
    "hiking": "徒步",
    "yoga": "瑜伽",
    "elliptical": "椭圆机",
    "rowing": "划船",
    "tennis": "网球",
    "tableTennis": "乒乓球",
    "basketball": "篮球",
    "soccer": "足球",
}


def local_now() -> dt.datetime:
    tz = ZoneInfo(TIMEZONE) if TIMEZONE else dt.datetime.now().astimezone().tzinfo
    return dt.datetime.now(tz)


def git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", str(REPO), *args],
                          capture_output=True, text=True, check=False)


def assert_expected_remote() -> None:
    remotes = git("remote", "get-url", "--push", "--all", "origin")
    if remotes.returncode != 0 or not remotes.stdout.strip():
        raise RuntimeError("no origin push URL; refusing sync")
    urls = {u.strip() for u in remotes.stdout.splitlines() if u.strip()}
    ok = any(EXPECTED_REMOTE in u for u in urls)
    if not ok:
        raise RuntimeError(
            f"refusing to push: unexpected origin {sorted(urls)!r}")
    print(f"verified push destination: {EXPECTED_REMOTE} (private repo)")


def fetch_workouts(day: dt.date) -> tuple[list[dict], bool]:
    cmd = ["health-cli", "query", "sessions", "--provider", "healthkit",
           "--category", "workout",
           "--start-date", day.isoformat(), "--end-date", day.isoformat()]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
        data = json.loads(out.stdout)
    except Exception as exc:  # noqa: BLE001 - health-cli is best-effort
        print(f"WARNING: apple workout query failed: {exc}", file=sys.stderr)
        return [], False
    records = data.get("records") or []
    complete = bool((data.get("coverage") or {}).get("complete", True))
    return records, complete


def hhmm(s: str | None) -> str:
    if not s:
        return "?"
    m = re.search(r"(\d{2}):(\d{2})", s)
    return f"{m.group(1)}:{m.group(2)}" if m else "?"


def fmt_num(v) -> str | None:
    try:
        return f"{float(v):g}"
    except (TypeError, ValueError):
        return None


def describe(r: dict) -> str:
    wtype = r.get("workout_type") or "workout"
    name = TYPE_CN.get(wtype, wtype)
    span = f"{hhmm(r.get('start_datetime'))} → {hhmm(r.get('end_datetime'))}"
    parts = [span]
    secs = r.get("active_duration_sec")
    mins = f"{float(secs) / 60:.0f}" if secs else None
    if mins:
        parts.append(f"{mins} min")
    kcal = fmt_num(r.get("energy_burned_kcal"))
    if kcal:
        parts.append(f"~{kcal} kcal")
    hr = r.get("hr_average_bpm")
    if hr is not None:
        try:
            parts.append(f"avg HR {float(hr):.0f} bpm")
        except (TypeError, ValueError):
            pass
    line = f"- {name} {' · '.join(parts)}"
    if wtype == "traditionalStrengthTraining":
        line += "（详情见上方训记记录）"
    return line


def update_daily(day: dt.date, records: list[dict]) -> None:
    path = REPO / "data" / "daily" / f"{day.isoformat()}.md"
    if path.exists():
        content = path.read_text(encoding="utf-8")
    else:
        content = (f"# {day.isoformat()}\n\n## Sleep\n- (None)\n\n## Meals\n- (None)\n\n"
                   "## Training\n\n## Body metrics\n- (None)\n\n## Notes\n- (None)\n")
    body = "\n".join(describe(r) for r in records) if records else "- (当天无手表运动记录)"
    managed = f"{START_MARKER}\n{body}\n{END_MARKER}"
    section = "### Apple Health\n" + managed

    if START_MARKER in content:
        content = re.sub(re.escape(START_MARKER) + r".*?" + re.escape(END_MARKER),
                         lambda _: managed, content, count=1, flags=re.S)
        path.write_text(content, encoding="utf-8")
        return

    # Insert after the XUNJI block when present, else at end of ## Training.
    xunji_end = "<!-- END XUNJI SYNC -->"
    if xunji_end in content:
        content = content.replace(xunji_end, f"{xunji_end}\n\n{section}", 1)
    else:
        m = re.search(r"(?ms)^## Training\n(.*?)(?=^## |\Z)", content)
        if m:
            new_section = "## Training\n" + m.group(1).rstrip("\n") + f"\n\n{section}\n"
            content = content[:m.start()] + new_section + content[m.end():]
        else:
            content = content.rstrip() + f"\n\n## Training\n\n{section}\n"
    path.write_text(content, encoding="utf-8")


def run_energy_sync(day: dt.date) -> None:
    env = dict(os.environ)
    env["HEALTH_LOG_REPO"] = str(REPO)
    if TIMEZONE:
        env["HEALTH_LOG_TZ"] = TIMEZONE
    r = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "health-energy-sync.py"),
         "--date", day.isoformat()],
        capture_output=True, text=True, env=env, timeout=300)
    print(r.stdout.strip())
    if r.returncode != 0:
        print(r.stderr.strip(), file=sys.stderr)
        raise RuntimeError("health-energy-sync.py failed")


def rebuild_summaries() -> None:
    """Refresh weekly/monthly summaries + SUMMARY.md so they stay current.

    Runs health-daily-sync.py in --summaries-only --no-git mode (pure local
    rebuild, no API key needed); the caller stages the outputs in its own
    commit via its push pathspec.
    """
    env = dict(os.environ)
    env["HEALTH_LOG_REPO"] = str(REPO)
    if TIMEZONE:
        env["HEALTH_LOG_TZ"] = TIMEZONE
    r = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "health-daily-sync.py"),
         "--summaries-only", "--no-git"],
        capture_output=True, text=True, env=env, timeout=180)
    if r.stdout.strip():
        print(r.stdout.strip())
    if r.returncode != 0:
        print(r.stderr.strip(), file=sys.stderr)
        raise RuntimeError("summaries rebuild failed")


def sync_git_push(day: dt.date) -> None:
    assert_expected_remote()
    git("add", "-A", "--", "data/daily",
        "reports/weekly.md", "reports/weekly", "reports/monthly.md",
        "reports/monthly", "SUMMARY.md")
    if not git("diff", "--cached", "--name-only").stdout.strip():
        print("no changes")
        return
    git("commit", "-m", f"Workout sync {day}: Apple HealthKit workouts")
    branch = git("symbolic-ref", "--quiet", "--short", "HEAD").stdout.strip()
    git("push", "origin", f"HEAD:refs/heads/{branch}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", help="YYYY-MM-DD (default: today)")
    parser.add_argument("--no-git", action="store_true",
                        help="Update local files without git commit/push")
    args = parser.parse_args(argv)

    day = dt.date.fromisoformat(args.date) if args.date else local_now().date()

    records, coverage_ok = fetch_workouts(day)
    print(f"day: {day.isoformat()}, workouts: {len(records)}, "
          f"coverage_complete: {coverage_ok}")
    update_daily(day, records)
    run_energy_sync(day)
    rebuild_summaries()
    if not args.no_git:
        sync_git_push(day)
    return 0


if __name__ == "__main__":
    sys.exit(main())
