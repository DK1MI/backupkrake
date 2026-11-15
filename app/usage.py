"""Per-host storage figures from the backup runs' rsync stats.

Thanks to --link-dest, rsync's "Total transferred file size" of a run is the new
data that snapshot added on disk (unchanged files are hardlinks and cost nothing).
"""
import shutil
from datetime import datetime, timedelta
from math import floor, log10

from . import config, db

FULL_COPY_RATIO = 0.8  # a run that wrote >= 80 % of the snapshot size is (nearly) a full copy
MIN_FULL_BYTES = 1024 ** 2

# Chart geometry (SVG user units; the SVG scales to the container width)
W, H = 720, 210
LEFT, RIGHT, TOP, BOTTOM = 64, 8, 22, 26
_UNITS = ["B", "KiB", "MiB", "GiB", "TiB", "PiB"]


def fmt_bytes(n: float) -> str:
    i = 0
    while abs(n) >= 1024 and i < len(_UNITS) - 1:
        n /= 1024
        i += 1
    return f"{n:.0f} {_UNITS[i]}" if i == 0 or n >= 100 else f"{n:.1f} {_UNITS[i]}"


def backup_history(host_id: int, limit: int = 60) -> list[dict]:
    """Successful backups with stats, oldest first: date, snapshot, added bytes, total bytes, full flag."""
    runs = [r for r in db.list_runs(host_id=host_id, kind="backup", limit=limit * 3)
            if r["status"] in ("success", "warning") and r["snapshot"]
            and r["stats"].get("transferred_size") is not None][:limit]
    out = []
    for r in reversed(runs):
        added, total = r["stats"]["transferred_size"], r["stats"].get("total_size") or 0
        out.append({
            "run_id": r["id"], "snapshot": r["snapshot"],
            "date": datetime.fromisoformat(r["finished"] or r["started"]),
            "added": added, "total": total,
            "full": added >= MIN_FULL_BYTES and total > 0 and added >= FULL_COPY_RATIO * total,
        })
    return out


def _nice_step(span: float) -> float:
    """A 1/2/5 x 10^n step that divides ``span`` into about three gridlines."""
    raw = span / 3 or 1
    mag = 10 ** floor(log10(raw))
    for m in (1, 2, 5, 10):
        if raw <= m * mag:
            return m * mag
    return 10 * mag


def _ticks(ymax: float) -> tuple[float, list[float], str, int]:
    """Gridline values in a binary unit: (axis max in bytes, tick values in bytes, unit, divisor)."""
    i, div = 0, 1
    while ymax / div >= 1024 and i < len(_UNITS) - 1:
        div *= 1024
        i += 1
    step = _nice_step(ymax / div)
    top = step * max(1, -(-(ymax / div) // step))  # round up to a whole step
    n = int(round(top / step))
    return top * div, [k * step * div for k in range(n + 1)], _UNITS[i], div


def chart(history: list[dict]) -> dict | None:
    """Bar geometry for 'new data per backup'. Full copies are capped at the top of the
    scale (which is set by the incremental backups) and labelled with their size."""
    if not history:
        return None
    incremental = [h["added"] for h in history if not h["full"]]
    scale_from = max(incremental) if incremental and max(incremental) > 0 else max(h["added"] for h in history)
    ymax, tick_values, unit, div = _ticks(max(scale_from, 1))
    plot_w, plot_h = W - LEFT - RIGHT, H - TOP - BOTTOM
    slot = plot_w / len(history)
    bar_w = max(2.0, min(18.0, slot - 2))  # 2px surface gap between bars
    base = TOP + plot_h
    bars = []
    labelled_full = 0
    for i, h in enumerate(history):
        capped = h["added"] > ymax
        hgt = plot_h if capped else plot_h * h["added"] / ymax
        if h["added"] > 0:
            hgt = max(hgt, 1.5)  # tiny but non-zero stays visible
        x = LEFT + i * slot + (slot - bar_w) / 2
        r = min(4.0, bar_w / 2, hgt)
        y = base - hgt
        # rounded data end at the top, square at the baseline
        path = (f"M{x:.1f},{base:.1f} V{y + r:.1f} Q{x:.1f},{y:.1f} {x + r:.1f},{y:.1f} "
                f"H{x + bar_w - r:.1f} Q{x + bar_w:.1f},{y:.1f} {x + bar_w:.1f},{y + r:.1f} V{base:.1f} Z") if hgt > 0 else ""
        label = None
        if capped and labelled_full < 3:
            labelled_full += 1
            # keep the label inside the plot: anchor it at the bar's edge near either side
            if i < len(history) / 4:
                label = {"x": x, "anchor": "start"}
            elif i > len(history) * 3 / 4:
                label = {"x": x + bar_w, "anchor": "end"}
            else:
                label = {"x": x + bar_w / 2, "anchor": "middle"}
            label.update(y=TOP - 6, text=f"full copy {fmt_bytes(h['added'])}")
        bars.append({
            **h, "path": path, "capped": capped, "label": label,
            "hit_x": LEFT + i * slot, "hit_w": slot,
            "tip": f"{h['date']:%Y-%m-%d %H:%M}: {fmt_bytes(h['added'])} added"
                   + (" (full copy)" if h["full"] else "") + f", snapshot size {fmt_bytes(h['total'])}",
        })
    ticks = [{"y": base - plot_h * v / ymax, "label": f"{v / div:g} {unit}" if v else "0"} for v in tick_values]
    # date labels: first, middle, last (no collisions at 720 units wide)
    idx = sorted({0, len(history) // 2, len(history) - 1})
    xlabels = [{"x": LEFT + i * slot + slot / 2, "text": f"{history[i]['date']:%d %b}",
                "anchor": "start" if i == 0 and len(idx) > 1 else ("end" if i == len(history) - 1 and len(idx) > 1 else "middle")}
               for i in idx]
    return {"w": W, "h": H, "left": LEFT, "right": W - RIGHT, "base": base, "bars": bars,
            "ticks": ticks, "xlabels": xlabels}


def summary(history: list[dict], now: datetime | None = None) -> dict:
    """Headline figures for the host page."""
    if not history:
        return {}
    now = now or datetime.now().astimezone()
    recent = [h for h in history if h["date"] >= now - timedelta(days=30)]
    incremental = [h["added"] for h in history if not h["full"]]
    return {
        "latest_total": history[-1]["total"],
        "added_30d": sum(h["added"] for h in recent if not h["full"]),
        "runs_30d": len(recent),
        "avg_added": sum(incremental) / len(incremental) if incremental else None,
    }


# --- /data free space and forecast --------------------------------------------------

SAMPLE_KEEP_DAYS = 120
FORECAST_DAYS = 30           # fit the trend over this window
MIN_FORECAST_SPAN = 2        # days of samples needed before forecasting
NOT_GROWING = 1024 ** 2      # bytes/day below which usage counts as flat


def disk_now() -> dict:
    d = shutil.disk_usage(config.DATA_DIR)
    return {"total": d.total, "used": d.used, "free": d.free}


def record_sample(now: datetime | None = None, disk: dict | None = None) -> None:
    """Called hourly by housekeeping."""
    now = now or datetime.now().astimezone()
    d = disk or disk_now()
    db.execute("INSERT OR REPLACE INTO disk_samples (time, total, used, free) VALUES (?, ?, ?, ?)",
               (now.isoformat(timespec="seconds"), d["total"], d["used"], d["free"]))
    db.execute("DELETE FROM disk_samples WHERE time < ?",
               ((now - timedelta(days=SAMPLE_KEEP_DAYS)).isoformat(timespec="seconds"),))


def forecast(now: datetime | None = None, disk: dict | None = None) -> dict:
    """Least-squares trend of used space over the last 30 days.

    Returns {"free_pct", "rate"} plus "days_left"/"full_on" when the disk is filling up;
    "rate" (bytes/day) is None until there are enough samples."""
    now = now or datetime.now().astimezone()
    d = disk or disk_now()
    out = {"free_pct": 100.0 * d["free"] / d["total"] if d["total"] else 100.0,
           "rate": None, "days_left": None, "full_on": None}
    rows = db.query("SELECT time, used FROM disk_samples WHERE time >= ? ORDER BY time",
                    ((now - timedelta(days=FORECAST_DAYS)).isoformat(timespec="seconds"),))
    pts = [((datetime.fromisoformat(r["time"]) - now).total_seconds() / 86400, r["used"]) for r in rows]
    if len(pts) < 3 or pts[-1][0] - pts[0][0] < MIN_FORECAST_SPAN:
        return out
    n = len(pts)
    mx = sum(x for x, _ in pts) / n
    my = sum(y for _, y in pts) / n
    var = sum((x - mx) ** 2 for x, _ in pts)
    rate = sum((x - mx) * (y - my) for x, y in pts) / var if var else 0.0
    out["rate"] = rate
    if rate >= NOT_GROWING:
        days = d["free"] / rate
        out["days_left"] = days
        out["full_on"] = now + timedelta(days=days)
    return out
