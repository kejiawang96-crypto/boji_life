#!/usr/bin/env python3
"""Sync Apple HealthKit sleep data into the private health-log repository.

Reads sleep sessions plus related vitals (respiratory rate, resting HR, HRV)
via `health-cli` and updates the ## Sleep section of data/daily/ logs
(marker-managed). The block carries both a human-readable summary and a
machine-readable <!-- SLEEP-DATA {...} --> comment; 30-day trend charts
(reports/sleep/) and period summaries read that embedded data back from the
daily logs, so data/sleep/ is not needed.

Only the fenced SLEEP SYNC block inside a daily Sleep section is managed by
this script. User-authored notes and every other provider's data are preserved.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from zoneinfo import ZoneInfo

REPO = Path(os.environ.get("HEALTH_LOG_REPO",
                           Path(__file__).resolve().parent.parent)).expanduser().resolve()
TIMEZONE = os.environ.get("HEALTH_LOG_TZ", "America/Chicago")
EXPECTED_REMOTE = os.environ.get(
    "HEALTH_LOG_EXPECTED_REMOTE", "your-github-username/your-health-log")
# Set HEALTH_LOG_EXPECTED_REMOTE to "owner/repo" of your private repo.
# Pushes are refused unless the origin remote matches it.

SLEEP_START = "<!-- BEGIN SLEEP SYNC -->"
SLEEP_END = "<!-- END SLEEP SYNC -->"
SLEEP_SECTION = re.compile(r"(?ms)^## Sleep\n.*?(?=^## |\Z)")
# Machine-readable sleep payload embedded in the daily log's Sleep block.
# Charts and summaries read this back; keep the key names stable.
SLEEP_DATA_RE = re.compile(r"<!-- SLEEP-DATA (\{.*?\}) -->")
LOOKBACK_DAYS = 35
CHART_DAYS = 30


def run(cmd: list[str]) -> str:
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if result.returncode:
        raise RuntimeError(f"{' '.join(cmd[:4])} failed: "
                           f"{(result.stderr or result.stdout).strip()[:200]}")
    return result.stdout


def health_cli(*args: str) -> dict:
    if not shutil.which("health-cli"):
        raise RuntimeError("health-cli not found on PATH")
    return json.loads(run(["health-cli", *args, "--provider", "healthkit"]))


def today() -> dt.date:
    return dt.datetime.now(ZoneInfo(TIMEZONE)).date()


def parse_dt(s: str, tzname: str | None) -> dt.datetime:
    d = dt.datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
    return d.replace(tzinfo=ZoneInfo(tzname or TIMEZONE))


def fmt_dur(sec: float | None) -> str:
    if sec is None:
        return "-"
    m = int(round(sec / 60))
    return f"{m // 60}h {m % 60:02d}m"


def fmt_num(v: float | None, digits: int = 1) -> str:
    return "-" if v is None else f"{v:.{digits}f}"


def fetch_sleep() -> list[dict]:
    end = today()
    start = end - dt.timedelta(days=LOOKBACK_DAYS)
    data = health_cli("query", "sessions", "--category", "sleep",
                      "--start-date", start.isoformat(), "--end-date", end.isoformat())
    return data.get("records", [])


def fetch_vitals() -> dict[str, dict]:
    end = today()
    start = end - dt.timedelta(days=LOOKBACK_DAYS)
    data = health_cli(
        "query", "metrics",
        "--start-date", start.isoformat(), "--end-date", end.isoformat(),
        "--fields", "respiratory_rate_average,resting_hr_average_bpm,"
                    "heart_rate_variability_ms")
    out: dict[str, dict] = {}
    for row in data.get("records", []):
        out[row.get("date", "")] = row
    return out


def build_block(sess: dict, vitals: dict) -> str:
    tz = sess.get("timezone") or TIMEZONE
    bed = parse_dt(sess["start_datetime"], tz)
    wake = parse_dt(sess["end_datetime"], tz)
    in_bed = (wake - bed).total_seconds()
    asleep = sum(sess.get(k) or 0 for k in (
        "sleep_core_duration_sec", "sleep_deep_duration_sec",
        "sleep_rem_duration_sec", "sleep_asleep_unspecified_duration_sec"))
    lines = [
        SLEEP_START,
        f"- In bed {bed:%H:%M} → {wake:%H:%M} ({fmt_dur(in_bed)}); "
        f"asleep {fmt_dur(asleep)}",
        f"- Quality score {fmt_num(sess.get('quality_score'), 0)}/100 · "
        f"efficiency {fmt_num(sess.get('sleep_efficiency'))}% · "
        f"awakenings {fmt_num(sess.get('number_of_awakenings'), 0)}",
        f"- Deep {fmt_dur(sess.get('sleep_deep_duration_sec'))} · "
        f"Core {fmt_dur(sess.get('sleep_core_duration_sec'))} · "
        f"REM {fmt_dur(sess.get('sleep_rem_duration_sec'))} · "
        f"Awake {fmt_dur(sess.get('sleep_awake_duration_sec'))}",
        f"- Resp. rate {fmt_num(vitals.get('respiratory_rate_average'))}/min · "
        f"resting HR {fmt_num(vitals.get('resting_hr_average_bpm'), 0)} bpm · "
        f"HRV {fmt_num(vitals.get('heart_rate_variability_ms'), 0)} ms "
        f"(daily avg)",
        "- Source: Apple Health (HealthKit)",
        "<!-- SLEEP-DATA " + json.dumps({
            "v": 1,
            "date": wake.date().isoformat(),
            "sleep_deep_duration_sec": sess.get("sleep_deep_duration_sec"),
            "sleep_core_duration_sec": sess.get("sleep_core_duration_sec"),
            "sleep_rem_duration_sec": sess.get("sleep_rem_duration_sec"),
            "sleep_awake_duration_sec": sess.get("sleep_awake_duration_sec"),
            "sleep_efficiency": sess.get("sleep_efficiency"),
            "quality_score": sess.get("quality_score"),
            "number_of_awakenings": sess.get("number_of_awakenings"),
            "respiratory_rate_average": vitals.get("respiratory_rate_average"),
            "resting_hr_average_bpm": vitals.get("resting_hr_average_bpm"),
            "heart_rate_variability_ms": vitals.get("heart_rate_variability_ms"),
        }, ensure_ascii=False) + " -->",
        SLEEP_END,
    ]
    return "\n".join(lines)


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8",
                                         dir=path.parent, delete=False) as f:
            tmp = f.name
            f.write(content)
        os.replace(tmp, path)
    finally:
        if tmp and os.path.exists(tmp):
            os.unlink(tmp)


def update_daily_sleep(day_iso: str, block: str) -> None:
    path = REPO / "data" / "daily" / f"{day_iso}.md"
    content = (path.read_text(encoding="utf-8") if path.exists() else
               f"# {day_iso}\n\n## Sleep\n- (None)\n\n## Meals\n- (None)\n\n"
               f"## Training\n\n## Body metrics\n- (None)\n\n## Notes\n- (None)\n")
    match = SLEEP_SECTION.search(content)
    if match:
        section = match.group()
        body = section[len("## Sleep\n"):]
        if body.count(SLEEP_START) != body.count(SLEEP_END) or \
                body.count(SLEEP_START) > 1:
            raise ValueError(f"invalid sleep markers in {path}")
        if SLEEP_START in body:
            body = re.sub(re.escape(SLEEP_START) + r".*?" + re.escape(SLEEP_END),
                          lambda _: block, body, count=1, flags=re.S)
        else:
            if body.strip() == "- (None)":
                body = ""
            body = body.rstrip() + "\n\n" + block
        replacement = "## Sleep\n" + body.strip("\n") + "\n\n"
        content = content[:match.start()] + replacement + content[match.end():]
    else:
        content = content.rstrip() + "\n\n## Sleep\n" + block + "\n"
    atomic_write(path, content)


def read_sleep_history(days: int) -> list[dict]:
    """Read embedded SLEEP-DATA payloads from daily logs (newest `days` days).

    Returns chart-compatible night dicts: {"date", "sess", "vitals"}.
    """
    end = today()
    start = end - dt.timedelta(days=days - 1)
    nights = []
    d = start
    while d <= end:
        path = REPO / "data" / "daily" / f"{d.isoformat()}.md"
        if path.is_file():
            m = SLEEP_DATA_RE.search(path.read_text(encoding="utf-8"))
            if m:
                try:
                    p = json.loads(m.group(1))
                except ValueError:
                    p = None
                if p and p.get("v") == 1:
                    sess = {k: p.get(k) for k in (
                        "sleep_deep_duration_sec", "sleep_core_duration_sec",
                        "sleep_rem_duration_sec", "sleep_awake_duration_sec",
                        "sleep_efficiency", "quality_score",
                        "number_of_awakenings")}
                    vitals = {k: p.get(k) for k in (
                        "respiratory_rate_average", "resting_hr_average_bpm",
                        "heart_rate_variability_ms")}
                    nights.append({"date": d, "sess": sess, "vitals": vitals})
        d += dt.timedelta(days=1)
    return nights


def build_charts(nights: list[dict]) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates

    nights = sorted(nights, key=lambda n: n["date"])[-CHART_DAYS:]
    if not nights:
        print("no sleep nights for charts")
        return
    outdir = REPO / "reports" / "sleep"
    outdir.mkdir(parents=True, exist_ok=True)
    dates = [n["date"] for n in nights]
    labels = [d.strftime("%m-%d") for d in dates]

    def hours(key: str) -> list[float]:
        return [(n["sess"].get(key) or 0) / 3600 for n in nights]

    # 1. Stage durations, stacked bars
    fig, ax = plt.subplots(figsize=(12, 4.5))
    bottom = [0.0] * len(nights)
    for key, color, name in [
            ("sleep_deep_duration_sec", "#1f3a5f", "Deep"),
            ("sleep_core_duration_sec", "#4a90c2", "Core"),
            ("sleep_rem_duration_sec", "#9b59b6", "REM"),
            ("sleep_awake_duration_sec", "#e0e0e0", "Awake")]:
        vals = hours(key)
        ax.bar(labels, vals, bottom=bottom, label=name, color=color)
        bottom = [b + v for b, v in zip(bottom, vals)]
    ax.set_ylabel("Hours")
    ax.set_title("Sleep stages per night (last 30 days)")
    ax.legend(ncol=4)
    ax.tick_params(axis="x", rotation=45)
    fig.tight_layout()
    fig.savefig(outdir / "sleep-stages-30d.png", dpi=150)
    plt.close(fig)

    # 2. Quality score + efficiency
    fig, ax = plt.subplots(figsize=(12, 4.5))
    ax.plot(labels,
            [n["sess"].get("quality_score") for n in nights],
            marker="o", label="Quality score", color="#2c7a2c")
    ax.plot(labels,
            [n["sess"].get("sleep_efficiency") for n in nights],
            marker="s", label="Efficiency %", color="#c27a1e")
    ax.set_ylim(0, 105)
    ax.set_title("Sleep quality score and efficiency (last 30 days)")
    ax.legend()
    ax.tick_params(axis="x", rotation=45)
    fig.tight_layout()
    fig.savefig(outdir / "sleep-quality-30d.png", dpi=150)
    plt.close(fig)

    # 3. Respiratory rate + resting HR
    fig, ax = plt.subplots(figsize=(12, 4.5))
    ax.plot(labels,
            [n["vitals"].get("respiratory_rate_average") for n in nights],
            marker="o", label="Resp. rate /min", color="#1f77b4")
    ax2 = ax.twinx()
    ax2.plot(labels,
             [n["vitals"].get("resting_hr_average_bpm") for n in nights],
             marker="s", label="Resting HR bpm", color="#d62728")
    ax.set_title("Respiratory rate and resting HR, daily avg (last 30 days)")
    ax.tick_params(axis="x", rotation=45)
    lines, labs = ax.get_legend_handles_labels()
    lines2, labs2 = ax2.get_legend_handles_labels()
    ax.legend(lines + lines2, labs + labs2)
    fig.tight_layout()
    fig.savefig(outdir / "sleep-vitals-30d.png", dpi=150)
    plt.close(fig)
    print(f"charts written to {outdir}")


def git(*args: str) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(["git", *args], cwd=REPO, capture_output=True,
                            text=True, timeout=60)
    if result.returncode:
        detail = (result.stderr or result.stdout).strip().splitlines()
        raise RuntimeError(
            f"git {' '.join(args)} failed: "
            f"{detail[-1] if detail else result.returncode}")
    return result


def assert_expected_remote() -> None:
    remotes = git("remote", "get-url", "--push", "--all",
                  "origin").stdout.splitlines()
    if not remotes:
        raise RuntimeError("no origin push URL; refusing sync")
    for url in remotes:
        path = url.strip()
        for prefix in ("git@github.com:", "ssh://git@github.com/",
                       "git@ssh.github.com:"):
            if path.startswith(prefix):
                path = path[len(prefix):]
                break
        if path.endswith(".git"):
            path = path[:-4]
        if path != EXPECTED_REMOTE:
            raise RuntimeError(
                f"refusing to push: unexpected origin {url!r}")
    print(f"verified push destination: {EXPECTED_REMOTE} (private repo)")


def rebuild_summaries() -> None:
    """Refresh weekly/monthly summaries + SUMMARY.md so they stay current.

    Runs health-daily-sync.py in --summaries-only --no-git mode (pure local
    rebuild, no API key needed); the caller stages the outputs in its own
    commit via its push pathspec.
    """
    r = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "health-daily-sync.py"),
         "--summaries-only", "--no-git"],
        capture_output=True, text=True, timeout=180)
    if r.stdout.strip():
        print(r.stdout.strip())
    if r.returncode:
        raise RuntimeError(f"summaries rebuild failed: {r.stderr.strip()}")


def sync_git_push(day: dt.date) -> None:
    assert_expected_remote()
    git("add", "-A", "--", "data/daily", "data/sleep", "reports/sleep",
        "reports/weekly.md", "reports/weekly", "reports/monthly.md",
        "reports/monthly", "SUMMARY.md")
    if not git("diff", "--cached", "--name-only").stdout.strip():
        print("no changes")
        return
    git("commit", "-m", f"Sleep sync {day}: Apple HealthKit sleep data")
    branch = git("symbolic-ref", "--quiet", "--short", "HEAD").stdout.strip()
    git("push", "origin", f"HEAD:refs/heads/{branch}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-git", action="store_true",
                        help="Update local files without git pull/commit/push")
    args = parser.parse_args(argv)
    try:
        if not args.no_git:
            assert_expected_remote()
            if git("status", "--porcelain").stdout.strip():
                raise RuntimeError("working tree not clean; refusing sync")
            git("pull", "--ff-only")

        sessions = fetch_sleep()
        vitals = fetch_vitals()
        # One entry per wake date. Duplicate records of the same night are
        # common (e.g. Watch with stage data vs. phone "in bed" without);
        # prefer a session that carries stage data over a longer stage-less
        # one, while ignoring short outliers like naps.
        STAGE_KEYS = ("sleep_deep_duration_sec", "sleep_core_duration_sec",
                      "sleep_rem_duration_sec")

        def has_stages(s: dict) -> bool:
            return any(s.get(k) for k in STAGE_KEYS)

        by_date: dict[str, list] = {}
        for s in sessions:
            if not s.get("start_datetime") or not s.get("end_datetime"):
                continue
            tz = s.get("timezone") or TIMEZONE
            wake = parse_dt(s["end_datetime"], tz).date().isoformat()
            dur = (parse_dt(s["end_datetime"], tz) -
                   parse_dt(s["start_datetime"], tz)).total_seconds()
            by_date.setdefault(wake, []).append({**s, "_dur": dur})

        nights = []
        for wake_iso in sorted(by_date):
            cands = by_date[wake_iso]
            longest = max(c["_dur"] for c in cands)
            pool = [c for c in cands if c["_dur"] >= 0.5 * longest]
            staged = [c for c in pool if has_stages(c)]
            sess = max(staged or pool, key=lambda c: c["_dur"])
            sess = {k: v for k, v in sess.items() if not k.startswith("_")}
            vit = vitals.get(wake_iso, {})
            update_daily_sleep(wake_iso, build_block(sess, vit))
            nights.append(wake_iso)
        print(f"synced {len(nights)} nights")

        build_charts(read_sleep_history(LOOKBACK_DAYS))
        rebuild_summaries()

        if not args.no_git:
            sync_git_push(today())
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
