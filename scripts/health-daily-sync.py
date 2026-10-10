#!/usr/bin/env python3
"""Sync SynFit workouts into a private, AI-readable health-log repository.

Only the fenced SynFit block inside a daily Training section is managed by
this script. User-authored notes and other sections are preserved.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import shutil
import re
import subprocess
import sys
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

REPO = Path(os.environ.get("HEALTH_LOG_REPO", Path(__file__).resolve().parent.parent)).expanduser().resolve()
KEY_FILE = Path(os.environ.get("XUNJI_API_KEY_FILE", "~/.config/xunji/api_key")).expanduser()
TIMEZONE = os.environ.get("HEALTH_LOG_TZ", "")
API = "https://trains.xunjiapp.cn/api_trains_for_llm_v2"  # SynFit legacy API hostname
START_MARKER = "<!-- BEGIN XUNJI SYNC -->"
END_MARKER = "<!-- END XUNJI SYNC -->"
TRAINING_SECTION = re.compile(r"(?ms)^## Training\n.*?(?=^## |\Z)")
# Unit label for training weights as stored in the user's Xunji app.
WEIGHT_UNIT = "lb"
# Rough heuristic for calorie burn from lifting volume: ~0.02 kcal per lb of
# volume (weight x reps, summed over completed weighted sets). Real burn varies
# widely with rest times, intensity, and body weight, so this is only a
# ballpark figure. Bodyweight-only sets are excluded from volume.
KCAL_PER_LB_VOLUME = 0.02


def to_float(x: object) -> float | None:
    """Coerce API values (often numeric strings) to float."""
    try:
        return float(x)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def set_weight_reps(item: dict) -> tuple[float | None, float | None, str]:
    """Return (weight, reps, unit) for a set; weight/reps None when not numeric."""
    weight = item.get("weight")
    if weight is None:
        weight = item.get("weight_kg")
    unit = item.get("unit") or WEIGHT_UNIT
    return to_float(weight), to_float(item.get("reps")), str(unit)


def local_now() -> dt.datetime:
    """Use a single timezone for dates and cache timestamps."""
    return dt.datetime.now(ZoneInfo(TIMEZONE)) if TIMEZONE else dt.datetime.now().astimezone()


def today() -> dt.date:
    return local_now().date()


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_name = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                                         prefix=".health-log-", delete=False) as f:
            temp_name = f.name
            f.write(content)
        os.replace(temp_name, path)
    finally:
        if temp_name and os.path.exists(temp_name):
            os.unlink(temp_name)


def validate_response(data: object) -> dict:
    """Reject malformed/error API responses before they enter the cache."""
    if not isinstance(data, dict):
        raise ValueError("response must be a JSON object")
    if data.get("success") is False:
        raise ValueError("API returned success=false")
    result = data.get("res")
    if not isinstance(result, dict) or not isinstance(result.get("trains"), list):
        raise ValueError("response must contain res.trains (list)")
    return data


def cache_metadata_path(day: dt.date) -> Path:
    return REPO / "data" / "workouts" / f"{day.isoformat()}.meta.json"


def cache_is_fresh(day: dt.date) -> bool:
    """Use explicit fetch time, not Git checkout / file modification time."""
    dest = REPO / "data" / "workouts" / f"{day.isoformat()}.json"
    metadata_file = cache_metadata_path(day)
    if not dest.is_file() or not metadata_file.is_file():
        return False
    try:
        metadata = json.loads(metadata_file.read_text(encoding="utf-8"))
        fetched_at = dt.datetime.fromisoformat(metadata["fetched_at"])
        if fetched_at.tzinfo is None or fetched_at.utcoffset() is None:
            return False
        if metadata.get("date") != day.isoformat():
            return False
        if fetched_at.astimezone(local_now().tzinfo).date() != today():
            return False
        validate_response(json.loads(dest.read_text(encoding="utf-8")))
        return True
    except (OSError, ValueError, TypeError, KeyError):
        return False


def fetch_workouts(day: dt.date, key: str, force: bool = False) -> Path:
    dest = REPO / "data" / "workouts" / f"{day.isoformat()}.json"
    if not force and cache_is_fresh(day):
        print(f"cached {day}")
        return dest

    request_body = json.dumps({
        "schema_version": "train_open_api_v2",
        "datestr": day.isoformat(),
        "include_full_data": False,
    }).encode("utf-8")
    request = Request(API, data=request_body, method="POST", headers={
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Accept-Encoding": "gzip",
    })
    try:
        with urlopen(request, timeout=30) as response:
            raw = response.read()
            if response.headers.get("Content-Encoding", "").lower() == "gzip":
                import gzip
                raw = gzip.decompress(raw)
            data = validate_response(json.loads(raw.decode("utf-8")))
    except (HTTPError, URLError, OSError, ValueError, UnicodeError) as exc:
        raise RuntimeError(f"SynFit fetch failed for {day}: {type(exc).__name__}") from None

    # Write the raw API record first; a missing metadata file forces a
    # re-fetch rather than accepting a partially updated cache as fresh.
    atomic_write(dest, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    metadata = {
        "date": day.isoformat(),
        "fetched_at": local_now().astimezone(dt.timezone.utc).isoformat(),
        "source": "SynFit",
    }
    atomic_write(cache_metadata_path(day), json.dumps(metadata, indent=2) + "\n")
    print(f"fetched {day}")
    return dest

def completed_sets(train: dict) -> int:
    return sum(1 for move in train.get("movements", [])
               for item in move.get("sets", []) if item.get("done"))


def fmt_num(v) -> str:
    """Compact number display: 15.0 -> '15', 17.5 -> '17.5'."""
    try:
        return f"{float(v):g}"
    except (TypeError, ValueError):
        return str(v)


def exercise_stats(move: dict) -> dict:
    """Volume/top-weight/total-reps over a movement's completed sets.

    Volume counts completed weighted sets with a rep count only
    (time-based holds and bodyweight sets are excluded).
    """
    volume = 0.0
    top_w: float | None = None
    total_reps = 0.0
    n = 0
    for item in move.get("sets", []):
        if not item.get("done"):
            continue
        w_val, r_val, _ = set_weight_reps(item)
        n += 1
        if w_val is not None:
            top_w = w_val if top_w is None else max(top_w, w_val)
        if r_val is not None:
            total_reps += r_val
        if w_val is not None and r_val is not None:
            volume += w_val * r_val
    return {"sets": n, "volume": volume, "top_weight": top_w,
            "total_reps": total_reps}


def movement_rows(move: dict) -> list[tuple[str, str]]:
    """(weight display, reps display) per completed set, in order."""
    rows = []
    for item in move.get("sets", []):
        if not item.get("done"):
            continue
        w_val, r_val, unit = set_weight_reps(item)
        if w_val is None and item.get("selfWeight"):
            w_disp = "BW"
        elif w_val is None:
            w_disp = "?"
        else:
            w_disp = f"{fmt_num(w_val)} {unit}"
        rep_raw = next((item[k] for k in ("reps", "time", "duration_s")
                        if item.get(k) is not None), None)
        if rep_raw is None:
            r_disp = "?"
        elif item.get("reps") is None:
            r_disp = f"{fmt_num(rep_raw)}s"
        else:
            r_disp = fmt_num(rep_raw)
        rows.append((w_disp, r_disp))
    return rows


def prev_exercise(name: str, before: dt.date,
                  lookback_days: int = 180) -> dict | None:
    """Most recent completed session of exercise `name` before `before`.

    Matches the movement by exact name across cached workout files.
    Returns {"date", "rows", "stats"} or None.
    """
    d = before - dt.timedelta(days=1)
    for _ in range(lookback_days):
        path = REPO / "data" / "workouts" / f"{d.isoformat()}.json"
        if path.is_file():
            try:
                trains = validate_response(
                    json.loads(path.read_text(encoding="utf-8")))["res"]["trains"]
            except Exception:
                trains = []
            for train in trains:
                for move in train.get("movements", []):
                    if (move.get("name") or "Movement") == name:
                        rows = movement_rows(move)
                        if rows:
                            return {"date": d, "rows": rows,
                                    "stats": exercise_stats(move)}
        d -= dt.timedelta(days=1)
    return None


def fmt_signed(v: float) -> str:
    return f"{v:+g}"


def progression_line(name: str, cur: dict, prev: dict) -> str:
    """One-line 'vs last time' comparison for an exercise."""
    parts = []
    dv = cur["volume"] - prev["stats"]["volume"]
    pv = prev["stats"]["volume"]
    pct = f" ({dv / pv * 100:+.1f}%)" if pv else ""
    parts.append(f"容量 {fmt_signed(dv)} lb{pct}")
    if cur["top_weight"] is not None and prev["stats"]["top_weight"] is not None:
        parts.append(
            f"最高重量 {fmt_signed(cur['top_weight'] - prev['stats']['top_weight'])} lbs")
    parts.append(
        f"总次数 {fmt_signed(cur['total_reps'] - prev['stats']['total_reps'])}")
    return f"vs 上次 {prev['date'].isoformat()[5:]}: " + " · ".join(parts)


def summarize_workouts(path: Path) -> str | None:
    trains = validate_response(json.loads(path.read_text(encoding="utf-8")))["res"]["trains"]
    if not trains:
        return None
    try:
        day = dt.date.fromisoformat(path.stem)
    except ValueError:
        day = None
    blocks = []
    for train in trains:
        volume_lb = 0.0
        ex_blocks = []
        for move in train.get("movements", []):
            name = move.get("name") or "Movement"
            cur_rows = movement_rows(move)
            st = exercise_stats(move)
            volume_lb += st["volume"]
            prev = prev_exercise(name, day) if day is not None else None
            if prev:
                md = prev["date"].isoformat()[5:]
                n = max(len(cur_rows), len(prev["rows"]))
                table_rows = []
                for i in range(n):
                    cw, cr = cur_rows[i] if i < len(cur_rows) else ("", "")
                    pw, pr = (prev["rows"][i] if i < len(prev["rows"])
                              else ("", ""))
                    table_rows.append(
                        f"| {i + 1} | {cw} | {cr} | {pw} | {pr} |")
                table = "\n".join(
                    [f"| 组 | 重量 | 次数 | 上次 {md} 重量 | 上次 {md} 次数 |",
                     "|---|---|---|---|---|"] + table_rows)
                block = (f"**{name}**\n\n{table}\n\n"
                         f"{progression_line(name, st, prev)}")
            elif cur_rows:
                table = "\n".join(
                    ["| 组 | 重量 | 次数 |", "|---|---|---|"] +
                    [f"| {i + 1} | {w} | {r} |"
                     for i, (w, r) in enumerate(cur_rows)])
                block = f"**{name}**\n\n{table}"
            else:
                continue
            ex_blocks.append(block)
        kcal = int(round(volume_lb * KCAL_PER_LB_VOLUME / 10.0) * 10)
        est = (f" (~{kcal} kcal from ~{volume_lb:,.0f} lb volume, rough estimate)"
               if volume_lb else "")
        title = train.get("title") or "Training"
        body = "\n\n".join(ex_blocks) if ex_blocks else "- no completed sets"
        blocks.append(f"**{title}**{est}\n\n{body}")
    return "\n\n".join(blocks)


def update_daily(day: dt.date, training_text: str | None) -> None:
    path = REPO / "data" / "daily" / f"{day.isoformat()}.md"
    content = (path.read_text(encoding="utf-8") if path.exists() else
               f"# {day}\n\n## Sleep\n- (None)\n\n## Meals\n- (None)\n\n"
               "## Training\n\n## Body metrics\n- (None)\n\n## Notes\n- (None)\n")
    managed = f"{START_MARKER}\n{training_text or '- No training logged'}\n{END_MARKER}"
    match = TRAINING_SECTION.search(content)
    if match:
        section = match.group()
        body = section[len("## Training\n"):]
        if body.count(START_MARKER) != body.count(END_MARKER) or body.count(START_MARKER) > 1:
            raise ValueError(f"invalid managed markers in {path}")
        if START_MARKER in body:
            body = re.sub(re.escape(START_MARKER) + r".*?" + re.escape(END_MARKER),
                          lambda _: managed, body, count=1, flags=re.S)
        else:
            if body.strip() == "- (None)":
                body = ""
            body = body.rstrip() + "\n\n" + managed
        replacement = "## Training\n" + body.strip("\n") + "\n\n"
        content = content[:match.start()] + replacement + content[match.end():]
    else:
        content = content.rstrip() + "\n\n## Training\n" + managed + "\n"
    atomic_write(path, content)


def parse_meal_total(path: Path) -> dict | None:
    """Parse the day total from the ## Meals section of a daily log."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    m = re.search(r"(?ms)^## Meals\n(.*?)(?=^## |\Z)", text)
    if not m:
        return None
    t = re.search(r"^-\s*Day total:\s*~?([\d,]+)\s*kcal\s*\|\s*protein\s*([\d.]+)g"
                  r"\s*\|\s*fat\s*([\d.]+)g\s*\|\s*carbs\s*([\d.]+)g",
                  m.group(1), re.M)
    if not t:
        return None
    return {"kcal": float(t.group(1).replace(",", "")),
            "protein": float(t.group(2)), "fat": float(t.group(3)),
            "carbs": float(t.group(4))}


def count_meals(day_text: str) -> int:
    """Count `### ` meal subsections inside the ## Meals section."""
    m = re.search(r"(?ms)^## Meals\n(.*?)(?=^## |\Z)", day_text)
    if not m or "- Day total:" not in m.group(1):
        return 0
    return len(re.findall(r"(?m)^### ", m.group(1)))


def workout_stats(path: Path) -> dict | None:
    try:
        trains = validate_response(
            json.loads(path.read_text(encoding="utf-8")))["res"]["trains"]
    except (OSError, ValueError):
        return None
    if not trains:
        return None
    sets = 0
    volume = 0.0
    for train in trains:
        for move in train.get("movements", []):
            for item in move.get("sets", []):
                if not item.get("done"):
                    continue
                sets += 1
                w_val, r_val, _ = set_weight_reps(item)
                if w_val is not None and r_val is not None:
                    volume += w_val * r_val
    return {"sets": sets, "volume_lb": volume,
            "kcal": volume * KCAL_PER_LB_VOLUME}


def sleep_stats(day: dt.date) -> dict | None:
    """Read the embedded SLEEP-DATA payload from the day's daily log."""
    path = REPO / "data" / "daily" / f"{day.isoformat()}.md"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    # Format owned by scripts/health-sleep-sync.py (SLEEP_DATA_RE).
    m = re.search(r"<!-- SLEEP-DATA (\{.*?\}) -->", text)
    if not m:
        return None
    try:
        p = json.loads(m.group(1))
    except ValueError:
        return None
    if p.get("v") != 1:
        return None
    asleep = sum(p.get(k) or 0 for k in ("sleep_core_duration_sec",
                "sleep_deep_duration_sec", "sleep_rem_duration_sec"))
    if asleep <= 0:
        return None
    deep = p.get("sleep_deep_duration_sec") or 0
    rem = p.get("sleep_rem_duration_sec") or 0
    # Bedtime from the human-readable line (stable format: "- In bed HH:MM →").
    bed_min = None
    bm = re.search(r"^-\s*In bed (\d{2}):(\d{2})\s*→", text, re.M)
    if bm:
        bed_min = int(bm.group(1)) * 60 + int(bm.group(2))
    return {"asleep_sec": asleep, "quality": p.get("quality_score"),
            "deep_pct": deep / asleep * 100, "rem_pct": rem / asleep * 100,
            "efficiency": p.get("sleep_efficiency"), "bedtime_min": bed_min}


def body_metric(day: dt.date, name: str) -> float | None:
    """Read a `- <Name>: <value> ...` line from the day's ## Body metrics."""
    path = REPO / "data" / "daily" / f"{day.isoformat()}.md"
    if not path.is_file():
        return None
    m = re.search(r"(?ms)^## Body metrics\n(.*?)(?=^## |\Z)",
                  path.read_text(encoding="utf-8"))
    if not m:
        return None
    t = re.search(rf"^- {re.escape(name)}:\s*([\d.]+)", m.group(1), re.M)
    return float(t.group(1)) if t else None


def metric_readings(name: str, start: dt.date,
                    end: dt.date) -> list[tuple[str, float]]:
    out = []
    d = start
    while d <= end:
        v = body_metric(d, name)
        if v is not None:
            out.append((d.isoformat(), v))
        d += dt.timedelta(days=1)
    return out


def day_balance(day: dt.date) -> float | None:
    path = REPO / "data" / "daily" / f"{day.isoformat()}.md"
    if not path.is_file():
        return None
    m = re.search(r"^- Balance:\s*([+-]?[\d,]+)\s*kcal",
                  path.read_text(encoding="utf-8"), re.M)
    return float(m.group(1).replace(",", "")) if m else None


# Peer benchmarks for adults (NSF: 7–9h/night for ages 26–64; typical adult
# stage mix: deep 13–23%, REM 20–25%; sleep efficiency ≥85% is good).
SLEEP_BENCH_DURATION = (7.0, 9.0)
SLEEP_BENCH_DEEP = (13.0, 23.0)
SLEEP_BENCH_REM = (20.0, 25.0)
SLEEP_BENCH_EFFICIENCY = 85.0


def _verdict(val: float, lo: float, hi: float) -> str:
    if val < lo:
        return "below typical"
    if val > hi:
        return "above typical"
    return "within typical"


def sleep_analysis(sleeps: list[dict]) -> list[str]:
    """Compare averaged sleep stats against peer benchmarks, with suggestions."""
    if len(sleeps) < 3:
        return ["- Not enough nights yet for analysis (need 3+)."]
    n = len(sleeps)
    avg_h = sum(s["asleep_sec"] for s in sleeps) / n / 3600
    avg_deep = sum(s["deep_pct"] for s in sleeps) / n
    avg_rem = sum(s["rem_pct"] for s in sleeps) / n
    effs = [s["efficiency"] for s in sleeps if s["efficiency"] is not None]
    avg_eff = sum(effs) / len(effs) if effs else None
    beds = [s["bedtime_min"] for s in sleeps if s["bedtime_min"] is not None]
    avg_bed = None
    if beds and max(beds) - min(beds) <= 12 * 60:
        avg_bed = sum(beds) / len(beds)

    lines = [
        "_Peer benchmarks (adults 26–64): 7–9h/night (NSF); deep 13–23%; "
        "REM 20–25%; efficiency ≥85% is good. Wrist-tracker stage estimates "
        "are approximate._",
    ]
    dur_word = ("below" if avg_h < SLEEP_BENCH_DURATION[0]
                else "within" if avg_h <= SLEEP_BENCH_DURATION[1] else "above")
    lines.append(f"- Duration {avg_h:.1f}h/night — {dur_word} the NSF-recommended "
                 f"7–9h. {'This is the biggest lever.' if avg_h < 7 else ''}".rstrip())
    lines.append(f"- Deep sleep {avg_deep:.1f}% (~{avg_deep / 100 * avg_h * 60:.0f} min) — "
                 f"{_verdict(avg_deep, *SLEEP_BENCH_DEEP)} range 13–23% "
                 f"(~90+ min typical on a full night).")
    lines.append(f"- REM sleep {avg_rem:.1f}% — "
                 f"{_verdict(avg_rem, *SLEEP_BENCH_REM)} range 20–25%.")
    if avg_eff is not None:
        good = avg_eff >= SLEEP_BENCH_EFFICIENCY
        lines.append(f"- Sleep efficiency {avg_eff:.0f}% — "
                     f"{'excellent' if good else 'below the 85% good-sleep mark'}; "
                     + ("you fall asleep and stay asleep — the issue is window "
                        "length, not fragmentation." if good else
                        "fragmented sleep is worth looking into."))

    tips = []
    if avg_h < SLEEP_BENCH_DURATION[0]:
        tips.append("Extend the window toward 7h+ asleep — the single biggest lever. "
                    "Move bedtime earlier ~15–30 min every few days instead of all at once.")
    if avg_bed is not None and 60 < avg_bed < 360:
        hh, mm = divmod(int(avg_bed), 60)
        tips.append(f"Timing: average bedtime ~{hh:02d}:{mm:02d} is very late — deep sleep "
                    "concentrates in the first half of the night, so late bedtimes cut it "
                    "first. Shifting earlier doubles as the way to reach 7h+.")
    if avg_deep < SLEEP_BENCH_DEEP[0]:
        tips.append("Deep sleep is at the low end — it usually recovers on its own once "
                    "the window is longer and bedtime earlier.")
    tips.append("Anchor wake time: a fixed wake-up (even on weekends) plus daylight soon "
                "after waking stabilizes the rhythm fastest.")
    lines.append("")
    lines.append("**To improve:** " + " ".join(f"{i + 1}. {t}"
                for i, t in enumerate(tips)))
    return lines


def fmt_dur(sec: float) -> str:
    h, rem = divmod(int(sec), 3600)
    return f"{h}h {rem // 60:02d}m"


def build_period_summary(start: dt.date, end: dt.date, title: str,
                       subtitle: str, analysis: bool = False) -> str:
    day_list = [start + dt.timedelta(days=i)
                for i in range((end - start).days + 1)]
    days = len(day_list)

    t_days = t_sets = 0
    t_vol = t_kcal = 0.0
    meals: list[dict] = []
    sleeps: list[dict] = []
    balances: list[float] = []
    for d in day_list:
        ws = workout_stats(REPO / "data" / "workouts" / f"{d.isoformat()}.json")
        if ws:
            t_days += 1
            t_sets += ws["sets"]
            t_vol += ws["volume_lb"]
            t_kcal += ws["kcal"]
        mt = parse_meal_total(REPO / "data" / "daily" / f"{d.isoformat()}.md")
        if mt:
            meals.append(mt)
        ss = sleep_stats(d)
        if ss:
            sleeps.append(ss)
        b = day_balance(d)
        if b is not None:
            balances.append(b)
    weights = metric_readings("Fasted weight", start, end)

    lines = [f"# {title}",
             f"_{subtitle}_",
             "", "## Training"]
    if t_days:
        lines.append(f"- Trained {t_days} of {days} days · {t_sets} completed sets · "
                     f"~{t_vol:,.0f} lb volume (~{t_kcal:,.0f} kcal est.)")
    else:
        lines.append("- No training logged")
    lines.append("")
    lines.append("## Nutrition")
    if meals:
        avg_k = sum(m["kcal"] for m in meals) / len(meals)
        avg_p = sum(m["protein"] for m in meals) / len(meals)
        lines.append(f"- Meals logged {len(meals)} of {days} days · "
                     f"avg {avg_k:,.0f} kcal/day · avg protein {avg_p:.0f} g/day")
    else:
        lines.append("- No meals logged")
    lines.append("")
    lines.append("## Sleep")
    if sleeps:
        avg_s = sum(s["asleep_sec"] for s in sleeps) / len(sleeps)
        quals = [s["quality"] for s in sleeps if s["quality"] is not None]
        q = f" · avg quality {sum(quals) / len(quals):.0f}/100" if quals else ""
        lines.append(f"- {len(sleeps)} nights logged · avg {fmt_dur(avg_s)} asleep{q}")
    else:
        lines.append("- No sleep data")
    lines.append("")
    if analysis:
        lines.append("## Sleep analysis")
        lines.extend(sleep_analysis(sleeps))
        lines.append("")
    lines.append("## Weight")
    if weights:
        first, last = weights[0], weights[-1]
        change = f" ({last[1] - first[1]:+.1f} kg since {first[0]})" if len(weights) > 1 else ""
        lines.append(f"- {last[1]:.1f} kg ({last[0]}){change}")
    else:
        lines.append("- No weigh-ins")
    waists = metric_readings("Waist", start, end)
    if waists:
        wfirst, wlast = waists[0], waists[-1]
        wchange = (f" ({wlast[1] - wfirst[1]:+.1f} cm since {wfirst[0]})"
                   if len(waists) > 1 else "")
        lines.append(f"- Waist {wlast[1]:.1f} cm ({wlast[0]}){wchange}")
    lines.append("")
    lines.append("## Energy")
    if balances:
        avg_b = sum(balances) / len(balances)
        proj = avg_b * 7 / 7700
        lines.append(f"- Avg daily balance {avg_b:+,.0f} kcal → "
                     f"projected {proj:+.2f} kg/week")
    else:
        lines.append("- No energy data yet")
    lines.append("")
    return "\n".join(lines)


def build_weight_chart() -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates

    readings = metric_readings("Fasted weight", dt.date(1970, 1, 1), today())[-90:]
    if not readings:
        print("no weight readings for chart")
        return
    dates = [dt.date.fromisoformat(d) for d, _ in readings]
    weights = [w for _, w in readings]

    target = None
    m = re.search(r"target weight\s*([\d.]+)\s*kg",
                  (REPO / "profile.md").read_text(encoding="utf-8"))
    if m:
        target = float(m.group(1))

    fig, ax = plt.subplots(figsize=(12, 4.5))
    ax.plot(dates, weights, marker="o", linewidth=2, color="#2e7d32",
            label="Fasted weight")
    if target:
        ax.axhline(target, linestyle="--", color="#c62828", linewidth=1.2,
                   label=f"Target {target:g} kg")
    ax.set_ylabel("Weight (kg)")
    ax.set_title("Fasted morning weight trend")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d"))
    ax.tick_params(axis="x", rotation=45)
    pad = max(1.0, (max(weights) - min(weights)) / 2 or 1.0)
    lo = min(weights + ([target] if target else [])) - pad
    ax.set_ylim(lo, max(weights) + pad)
    # Keep the x-axis tight: a single point otherwise stretches over years.
    xmin = min(dates) - dt.timedelta(days=7)
    xmax = max(dates) + dt.timedelta(days=7)
    if (xmax - xmin).days < 30:
        mid = min(dates) + (max(dates) - min(dates)) / 2
        xmin, xmax = mid - dt.timedelta(days=15), mid + dt.timedelta(days=15)
    ax.set_xlim(xmin, xmax)
    ax.legend()
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    out = REPO / "reports" / "weight-trend.png"
    fig.savefig(out, dpi=150)
    plt.close(fig)
    print(f"weight chart -> {out}")


def build_period_summaries(as_of: dt.date | None = None) -> None:
    as_of = as_of or today()
    # Natural week (Monday–Sunday) and natural month, through today.
    wstart = as_of - dt.timedelta(days=as_of.weekday())
    atomic_write(REPO / "reports" / "weekly.md",
                 build_period_summary(wstart, as_of, "Weekly summary",
                                      f"{wstart.isoformat()} → {as_of.isoformat()} (Mon–Sun week)",
                                      analysis=True))
    mstart = as_of.replace(day=1)
    atomic_write(REPO / "reports" / "monthly.md",
                 build_period_summary(mstart, as_of, "Monthly summary",
                                      f"{mstart.isoformat()} → {as_of.isoformat()} "
                                      f"({as_of.strftime('%B %Y')})"))
    print("weekly/monthly summaries regenerated")


def section_items(content: str, header: str) -> list[str]:
    match = re.search(rf"(?ms)^## {re.escape(header)}\n(.*?)(?=^## |\Z)", content)
    return [line.strip() for line in match.group(1).splitlines()
            if line.strip().startswith("- ") and line.strip() != "- (None)"] if match else []


def escape_md(text: object) -> str:
    return str(text).replace("|", r"\|").replace("\n", " ")


def build_summary(as_of: dt.date | None = None) -> None:
    as_of = as_of or today()
    workout_files = sorted((REPO / "data" / "workouts").glob("????-??-??.json"))
    daily_files = sorted((REPO / "data" / "daily").glob("*.md"))
    training_by_day: dict[str, str] = {}
    sets_count = 0
    for file in workout_files:
        try:
            trains = validate_response(json.loads(file.read_text(encoding="utf-8")))["res"]["trains"]
        except (ValueError, OSError):
            continue
        if not trains:
            continue
        sets_count += sum(completed_sets(t) for t in trains)
        training_by_day[file.stem] = "; ".join(
            f"{t.get('title') or 'Training'} ({completed_sets(t)} sets)" for t in trains)

    dates = sorted(file.stem for file in daily_files)
    last_7 = [(as_of - dt.timedelta(days=i)).isoformat() for i in range(7)]
    trained_7 = sum(d in training_by_day for d in last_7)
    profile = REPO / "profile.md"
    goal_text = "- (not filled in yet)"
    if profile.exists():
        goals = re.search(r"(?ms)^## Current phase goal\n(.*?)(?=^## |\Z)",
                          profile.read_text(encoding="utf-8"))
        if goals and goals.group(1).strip():
            goal_text = goals.group(1).strip()

    lines = [
        "# Health Log Summary",
        f"_Last updated: {as_of.isoformat()}_",
        "",
        "## Goals",
        goal_text, "",
        "## Overall",
        f"- Days logged: {len(dates)}" +
        (f" ({dates[0]} → {dates[-1]})" if dates else ""),
        f"- Training days: {len(training_by_day)}",
        f"- Total completed sets: {sets_count}",
        f"- Trained {trained_7} of the last 7 calendar days",
        "",
        "## Recent days", "",
        "| Date | Training | Meals | Sleep |",
        "|------|----------|-------|-------|",
    ]
    for day in dates[-14:]:
        day_text = (REPO / "data" / "daily" / f"{day}.md").read_text(encoding="utf-8")
        n_meals = count_meals(day_text)
        sleep = section_items(day_text, "Sleep")
        sleep_info = sleep[0][2:].strip()[:60] if sleep else "—"
        meals_info = f"{n_meals} logged" if n_meals else "—"
        lines.append(f"| {day} | {escape_md(training_by_day.get(day, '—'))} | "
                     f"{meals_info} | {escape_md(sleep_info)} |")
    lines += [
        "",
        "## Where things live", "",
        "- Daily logs: data/daily/",
        "- Raw training data: data/workouts/",
        "- Meal records: in data/daily/ under ## Meals",
        "- Weekly/monthly summaries: reports/weekly.md, reports/monthly.md",
        "",
    ]
    atomic_write(REPO / "SUMMARY.md", "\n".join(lines))
    print("SUMMARY.md regenerated")


def git(*args: str) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(["git", *args], cwd=REPO, capture_output=True, text=True)
    if result.returncode:
        detail = (result.stderr or result.stdout).strip().splitlines()
        raise RuntimeError(f"git {' '.join(args)} failed: {detail[-1] if detail else result.returncode}")
    return result


def parse_github_remote(remote: str) -> str:
    """Accept GitHub HTTPS/SSH remotes only; return owner/repo."""
    remote = remote.strip()
    if remote.startswith("git@github.com:"):
        path = remote[len("git@github.com:"):]
    elif remote.startswith("ssh://git@github.com/"):
        path = remote[len("ssh://git@github.com/"):]
    else:
        parsed = urlsplit(remote)
        if (parsed.scheme != "https" or parsed.hostname != "github.com"
                or parsed.username or parsed.password or parsed.port is not None):
            raise RuntimeError("Push remote must be a GitHub HTTPS or SSH URL")
        path = parsed.path.lstrip("/")
    if path.endswith(".git"):
        path = path[:-4]
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", path):
        raise RuntimeError("Cannot identify GitHub repository from push remote")
    return path


def github_api_token() -> str:
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token:
        return token.strip()
    if shutil.which("gh"):
        result = subprocess.run(["gh", "auth", "token"], capture_output=True,
                                text=True, timeout=15)
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    raise RuntimeError(
        "Private repository visibility cannot be verified. Authenticate "
        "GitHub CLI (gh auth login) or provide GH_TOKEN/GITHUB_TOKEN "
        "with access to the private repository. No push attempted."
    )


def assert_private_push_remote() -> None:
    """Fail closed unless every origin push URL is confirmed private by GitHub."""
    remotes = git("remote", "get-url", "--push", "--all", "origin").stdout.splitlines()
    if not remotes:
        raise RuntimeError("No GitHub origin push URL configured; refusing sync")
    names = [parse_github_remote(url) for url in remotes]
    token = github_api_token()
    for name in names:
        request = Request(f"https://api.github.com/repos/{name}", headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "ai-health-log-sync",
        })
        try:
            with urlopen(request, timeout=15) as response:
                metadata = json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, OSError, ValueError, UnicodeError):
            raise RuntimeError(
                f"Cannot verify that GitHub push destination {name} is private; "
                "check GitHub authentication/network access. No push attempted."
            ) from None
        if not isinstance(metadata, dict) or metadata.get("private") is not True:
            raise RuntimeError(
                f"Refusing to push health data: GitHub repository {name} "
                "is public or its visibility is unverified."
            )
        print(f"verified private GitHub push destination: {name}")

def sync_git_pull() -> None:
    if git("status", "--porcelain").stdout.strip():
        raise RuntimeError("working tree is not clean; refusing to overwrite local changes")
    git("pull", "--ff-only")


def sync_git_push(day: dt.date) -> None:
    # Recheck immediately before staging/committing in case remote changed.
    assert_private_push_remote()
    git("add", "-A", "--", "data/daily", "data/workouts",
        "reports/weekly.md", "reports/monthly.md", "reports/weight-trend.png",
        "SUMMARY.md")
    if not git("diff", "--cached", "--name-only").stdout.strip():
        print("no changes")
        return
    git("commit", "-m", f"Daily sync {day}: SynFit training data")
    branch = git("symbolic-ref", "--quiet", "--short", "HEAD").stdout.strip()
    git("push", "origin", f"HEAD:refs/heads/{branch}")

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force-refresh", action="store_true",
                        help="Refetch workouts even if cached today")
    parser.add_argument("--no-git", action="store_true",
                        help="Update local files without git pull/commit/push")
    args = parser.parse_args(argv)
    try:
        if not KEY_FILE.is_file():
            raise RuntimeError(f"API key file not found: {KEY_FILE}")
        key = KEY_FILE.read_text(encoding="utf-8").strip()
        if not key:
            raise RuntimeError("SynFit API key file is empty")
        if not args.no_git:
            assert_private_push_remote()  # before touching personal files
            sync_git_pull()
        day = today()
        fetch_days = [day - dt.timedelta(days=1), day]
        ok = True
        for index, d in enumerate(fetch_days):
            # 16s between potential API reads (Xunji light-read limit: 15s).
            if index:
                time.sleep(16)
            try:
                raw = fetch_workouts(d, key, force=args.force_refresh)
                summary = summarize_workouts(raw)
                update_daily(d, summary)
            except (RuntimeError, ValueError, OSError) as exc:
                print(f"WARNING: skipped {d}: {exc}", file=sys.stderr)
                ok = False
        build_summary(day)
        build_period_summaries(day)
        build_weight_chart()
        # Never publish a partial sync.
        if not ok:
            print("sync incomplete; no git commit/push", file=sys.stderr)
            return 1
        if not args.no_git:
            sync_git_push(day)
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
