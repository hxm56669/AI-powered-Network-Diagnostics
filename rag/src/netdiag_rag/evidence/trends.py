"""RSSI changes use distinct monotonic samples from one source/identity only."""
import statistics

from ..settings import TrendSettings
from ..util import timestamp


def rssi_trend(samples, coverage, min_coverage, settings: TrendSettings):
    points = {}
    for row in samples:
        if row["status"] != "ok":
            continue
        tick = row["monotonic_ns"]
        if tick in points and points[tick]["value"] != row["value"]:
            return {"direction": "unknown", "reason": "conflicting_timestamp"}
        points[tick] = row
    rows = sorted(points.values(), key=lambda r: timestamp(r["observed_at"]))
    unknown = {"direction": "unknown", "distinct_samples": len(rows)}
    if len(rows) < settings.min_samples or coverage < min_coverage:
        return {**unknown, "reason": "insufficient_coverage_or_samples"}
    for a, b in zip(rows, rows[1:]):
        delta = (b["monotonic_ns"] - a["monotonic_ns"]) / 1e9
        wall = (timestamp(b["observed_at"]) - timestamp(a["observed_at"])).total_seconds()
        if delta <= 0 or abs(delta - wall) > max(.25, wall * .1):
            return {**unknown, "reason": "clock_discontinuity"}
        if delta > settings.max_gap_intervals * max(a["interval_ms"], b["interval_ms"]) / 1000:
            return {**unknown, "reason": "sample_gap"}
    times = [(r["monotonic_ns"] - rows[0]["monotonic_ns"]) / 1e9 for r in rows]
    if times[-1] < settings.min_span_s:
        return {**unknown, "reason": "short_span"}
    values = [r["value"] for r in rows]
    edge = min(3, len(rows) // 3)
    change = statistics.median(values[-edge:]) - statistics.median(values[:edge])
    avg_t, avg_y = statistics.mean(times), statistics.mean(values)
    slope = sum((t - avg_t) * (y - avg_y) for t, y in zip(times, values)) / sum((t - avg_t)**2 for t in times)
    differences = [b - a for a, b in zip(values, values[1:])]
    changes = [x for x in differences if x != 0]
    positive = sum(x > 0 for x in changes) / len(changes) if changes else 0
    negative = sum(x < 0 for x in changes) / len(changes) if changes else 0
    if change <= -settings.change_db and min(differences) <= -settings.abrupt_drop_db:
        direction = "abrupt_drop"
    elif change <= -settings.change_db and negative >= settings.directional_fraction:
        direction = "declining"
    elif change >= settings.change_db and positive >= settings.directional_fraction:
        direction = "recovering"
    elif max(values) - min(values) < settings.change_db:
        direction = "stable"
    else:
        direction = "fluctuating"
    # Legacy largest_step_db is the minimum signed step (negative for a drop).
    return {"direction": direction, "distinct_samples": len(rows), "span_s": times[-1],
            "change_db": round(change, 4), "slope_db_per_s": round(slope, 6),
            "largest_step_db": min(differences), "method": "monotonic-linear-edge-median-v1"}
