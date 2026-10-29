"""Read-only, bounded analytics over retained observations."""

import json
import math

from .storage import Store


def summary(store: Store, start: float, end: float) -> dict:
    rows = store.db.execute(
        "SELECT state,start,end FROM intervals WHERE end>=? AND start<? ORDER BY start,id",
        (start, end),
    ).fetchall()
    # Sweep overlapping intervals (for example after clock corrections). Unknown
    # evidence takes priority so overlaps cannot inflate monitoring coverage.
    edges = {}
    for row in rows:
        left, right = max(start, row["start"]), min(end, row["end"])
        if right <= left:
            continue
        state = row["state"]
        edges.setdefault(left, []).append((state, 1))
        edges.setdefault(right, []).append((state, -1))
    totals = dict.fromkeys(("online", "degraded", "offline", "unknown", "gap"), 0.0)
    active = dict.fromkeys(totals, 0)
    previous = start
    for timestamp, changes in sorted(edges.items()):
        state = next((s for s in ("gap", "unknown", "offline", "degraded", "online")
                      if active[s] > 0), "gap")
        totals[state] += timestamp - previous
        for state, change in changes:
            active[state] += change
        previous = timestamp
    totals["gap"] += end - previous
    aggregate = store.db.execute("""
        SELECT COUNT(*) AS samples, COUNT(latency_ms) AS latency_samples,
            AVG(latency_ms) AS mean_ms, MIN(latency_ms) AS min_ms,
            MAX(latency_ms) AS max_ms
        FROM samples WHERE ts>=? AND ts<?
    """, (start, end)).fetchone()
    result = dict(aggregate)
    count = result["latency_samples"]
    percentile = None
    if count:
        percentile = store.db.execute("""
            SELECT latency_ms FROM samples
            WHERE ts>=? AND ts<? AND latency_ms IS NOT NULL
            ORDER BY latency_ms LIMIT 1 OFFSET ?
        """, (start, end, math.ceil(.95 * count) - 1)).fetchone()[0]
    outages = [row for row in rows if row["state"] == "offline"
               and (row["end"] > start or row["start"] >= start)]
    measured = totals["online"] + totals["degraded"] + totals["offline"]
    result.update({
        "from": start, "to": end, "seconds": totals,
        "coverage_percent": measured / (end - start) * 100,
        "interruptions": len(outages),
        "longest_interruption_seconds": max(
            (min(end, row["end"]) - max(start, row["start"]) for row in outages), default=0),
        "p95_ms": percentile,
    })
    return result


def insights(store: Store, start: float, end: float) -> dict:
    span = end - start
    current = summary(store, start, end)
    previous = summary(store, max(0, start - span), start) if start > 0 else None
    comparable = bool(previous and previous["coverage_percent"] >= 80
                      and current["coverage_percent"] >= 80
                      and previous["to"] - previous["from"] == span)
    # Seven-day ranges get UTC daily rows; shorter ranges use UTC hours.
    step = 86400 if span > 86400 else 3600
    left = start
    periods = []
    while left < end:
        right = min(end, (math.floor(left / step) + 1) * step)
        periods.append(summary(store, left, right))
        left = right
    return {"current": current, "previous": previous, "comparable": comparable,
            "periods": periods, "period_seconds": step}


def incident(store: Store, interval_id: int) -> dict | None:
    row = store.db.execute("SELECT * FROM intervals WHERE id=?", (interval_id,)).fetchone()
    if row is None:
        return None
    item = dict(row)
    start, end, state = row["start"], row["end"], row["state"]
    first = store.db.execute(
        "SELECT * FROM samples WHERE ts>=? AND ts<=? AND state=? ORDER BY ts,id LIMIT 1",
        (start, end, state),
    ).fetchone()
    last = store.db.execute(
        "SELECT * FROM samples WHERE ts>=? AND ts<=? AND state=? ORDER BY ts DESC,id DESC LIMIT 1",
        (start, end, state),
    ).fetchone()
    before = store.db.execute(
        "SELECT ts,state,latency_ms FROM samples WHERE ts<? ORDER BY ts DESC,id DESC LIMIT 1",
        (start,),
    ).fetchone()
    after = store.db.execute(
        "SELECT ts,state,latency_ms FROM samples WHERE ts>=? AND (ts>? OR state!=?) "
        "ORDER BY ts,id LIMIT 1", (end, end, state),
    ).fetchone()
    probes = store.db.execute("""
        SELECT json_extract(p.value,'$.name') AS name,
            json_extract(p.value,'$.kind') AS kind,
            SUM(json_extract(p.value,'$.outcome')='ok') AS passed,
            SUM(json_extract(p.value,'$.outcome')='fail') AS failed,
            SUM(json_extract(p.value,'$.outcome')='error') AS errors,
            AVG(json_extract(p.value,'$.latency_ms')) AS mean_ms
        FROM samples s, json_each(s.probes) p
        WHERE s.ts>=? AND s.ts<=? AND s.state=?
        GROUP BY name,kind ORDER BY kind,name LIMIT 16
    """, (start, end, state)).fetchall() if state != "gap" else []
    events = store.db.execute(
        "SELECT * FROM events WHERE ts>=? AND ts<=? AND kind LIKE 'alert_%' "
        "ORDER BY ts,id LIMIT 100", (max(0, start - 30), end + 600),
    ).fetchall()
    def sample(value):
        if value is None:
            return None
        result = dict(value)
        if "probes" in result:
            result["probes"] = json.loads(result["probes"])
        return result

    return {"interval": item, "first": sample(first), "last": sample(last),
            "before": sample(before), "after": sample(after),
            "probes": [dict(p) for p in probes],
            "nearby_alert_events": [dict(e) for e in events],
            "active": store.active_id == interval_id}
