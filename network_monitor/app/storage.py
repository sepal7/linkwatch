import csv
import io
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()


class Store:
    def __init__(self, path: Path, retention_days: int, max_samples: int):
        self.path = path
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.retention_days, self.max_samples = retention_days, max_samples
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=NORMAL;
            PRAGMA journal_size_limit=4194304;
            CREATE TABLE IF NOT EXISTS samples (
                id INTEGER PRIMARY KEY, ts REAL NOT NULL, state TEXT NOT NULL,
                latency_ms REAL, probes TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS sample_time ON samples(ts);
            CREATE TABLE IF NOT EXISTS intervals (
                id INTEGER PRIMARY KEY, start REAL NOT NULL, end REAL NOT NULL,
                state TEXT NOT NULL, reason TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS interval_time ON intervals(end);
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY, ts REAL NOT NULL, kind TEXT NOT NULL,
                message TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """)
        self.active_id = None
        self.last = self.latest()
        self.prune_counter = 0
        self.pending_gap_id = None

    def latest(self):
        row = self.db.execute("SELECT * FROM samples ORDER BY id DESC LIMIT 1").fetchone()
        if row is None:
            return None
        value = dict(row)
        value["probes"] = json.loads(value["probes"])
        return value

    def start(self, now: float):
        with self.db:
            previous = self.last["ts"] if self.last else now
            cursor = self.db.execute(
                "INSERT INTO intervals(start,end,state,reason) VALUES (?,?,'gap','App starting or restarted')",
                (min(previous, now), max(previous, now)),
            )
            self.pending_gap_id = cursor.lastrowid
            self.event(now, "start", "Monitoring started; no observations are assumed during downtime")
        self.active_id = None
        self.session_start = now

    def event(self, now: float, kind: str, message: str):
        self.db.execute("INSERT INTO events(ts,kind,message) VALUES (?,?,?)",
                        (now, kind, message[:1000]))

    def log(self, now: float, kind: str, message: str):
        with self.db:
            self.event(now, kind, message)

    def record(self, now: float, state: str, probes: list[dict], gap_after: float):
        latencies = [p["latency_ms"] for p in probes
                     if p["kind"] == "external" and p["outcome"] == "ok"]
        latency = round(sum(latencies) / len(latencies), 1) if latencies else None
        previous = self.last
        gap = bool(previous and (now < previous["ts"] or now - previous["ts"] > gap_after))
        with self.db:
            if self.pending_gap_id is not None:
                self.db.execute("UPDATE intervals SET end=MAX(end,?) WHERE id=?",
                                (now, self.pending_gap_id))
                self.pending_gap_id = None
            if gap and self.active_id is not None:
                self.db.execute(
                    "INSERT INTO intervals(start,end,state,reason) VALUES (?,?,'gap',?)",
                    (min(previous["ts"], now), max(previous["ts"], now),
                     "Missed sampling window or system clock changed"),
                )
                self.event(now, "gap", "Missed sampling window or system clock changed")
                self.active_id = None
            self.db.execute(
                "INSERT INTO samples(ts,state,latency_ms,probes) VALUES (?,?,?,?)",
                (now, state, latency, json.dumps(probes, separators=(",", ":"))),
            )
            if self.active_id is None or previous is None or previous["state"] != state:
                if self.active_id is not None:
                    self.db.execute("UPDATE intervals SET end=? WHERE id=?", (now, self.active_id))
                cursor = self.db.execute(
                    "INSERT INTO intervals(start,end,state,reason) VALUES (?,?,?,'Observed probes')",
                    (now, now, state),
                )
                self.active_id = cursor.lastrowid
                self.event(now, state, f"Observed state: {state}")
            else:
                self.db.execute("UPDATE intervals SET end=? WHERE id=?", (now, self.active_id))
            self.prune_counter += 1
            if self.prune_counter >= 60:
                self.prune(now)
                self.prune_counter = 0
        self.last = {"ts": now, "state": state, "latency_ms": latency, "probes": probes,
                     "cycle_seconds": now - previous["ts"] if previous and not gap else None}
        return gap

    def prune(self, now: float):
        cutoff = now - self.retention_days * 86400
        self.db.execute("DELETE FROM samples WHERE ts < ?", (cutoff,))
        self.db.execute(
            "DELETE FROM samples WHERE id <= COALESCE((SELECT id FROM samples "
            "ORDER BY id DESC LIMIT 1 OFFSET ?),0)", (self.max_samples,),
        )
        oldest = self.db.execute("SELECT MIN(ts) FROM samples").fetchone()[0]
        cutoff = max(cutoff, oldest) if oldest is not None else cutoff
        self.db.execute("DELETE FROM intervals WHERE end < ? AND id != ?",
                        (cutoff, self.active_id or -1))
        self.db.execute("UPDATE intervals SET start=? WHERE start < ? AND end >= ?",
                        (cutoff, cutoff, cutoff))
        for table in ("events", "intervals"):
            if table == "events":
                self.db.execute("DELETE FROM events WHERE ts < ?", (cutoff,))
            self.db.execute(
                f"DELETE FROM {table} WHERE id IN (SELECT id FROM {table} "
                "ORDER BY id DESC LIMIT -1 OFFSET 10000)"
            )

    def setting(self, key: str, default: str = ""):
        row = self.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def set_setting(self, key: str, value: str):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (key, value))

    def history(self, start: float, end: float, points: int = 240):
        width = max((end - start) / points, 0.001)
        rows = self.db.execute("""
            SELECT CAST((ts-?)/? AS INTEGER) AS bucket,
                MIN(ts) AS start, MAX(ts) AS end, COUNT(*) AS count,
                AVG(latency_ms) AS latency_ms, MAX(latency_ms) AS max_latency_ms,
                SUM(state='offline') AS offline, SUM(state='degraded') AS degraded,
                SUM(state='unknown') AS unknown
            FROM samples WHERE ts >= ? AND ts <= ?
            GROUP BY bucket ORDER BY bucket LIMIT ?
        """, (start, width, start, end, points + 1)).fetchall()
        intervals = self.db.execute(
            "SELECT * FROM intervals WHERE end >= ? AND start <= ? ORDER BY start,id",
            (start, end),
        ).fetchall()
        clipped = []
        for row in intervals:
            item = dict(row)
            item["start"], item["end"] = max(start, item["start"]), min(end, item["end"])
            item["duration_seconds"] = max(0, item["end"] - item["start"])
            clipped.append(item)
        events = self.db.execute(
            "SELECT * FROM events WHERE ts >= ? AND ts <= ? ORDER BY id DESC LIMIT 100",
            (start, end),
        ).fetchall()
        return {"buckets": [dict(row) for row in rows], "intervals": clipped,
                "events": [dict(row) for row in events], "from": start, "to": end,
                "bucket_seconds": width}

    def export_rows(self, start: float, end: float, kind: str):
        # A separate read transaction preserves one consistent export while new
        # samples arrive or retention pruning runs. Rows are streamed, not loaded.
        reader = sqlite3.connect(self.path)
        try:
            reader.execute("BEGIN")
            if kind == "samples":
                yield ["timestamp_utc", "observed_state", "external_mean_tcp_ms", "probes_json"]
                query = "SELECT ts,state,latency_ms,probes FROM samples WHERE ts>=? AND ts<=? ORDER BY ts,id"
                for row in reader.execute(query, (start, end)):
                    yield [iso(row[0]), row[1], row[2], row[3]]
            else:
                yield ["start_utc", "end_utc", "state", "observed_duration_seconds", "reason"]
                query = "SELECT start,end,state,reason FROM intervals WHERE end>=? AND start<=? ORDER BY start,id"
                for row in reader.execute(query, (start, end)):
                    left, right = max(start, row[0]), min(end, row[1])
                    yield [iso(left), iso(right), row[2], round(max(0, right-left), 3), row[3]]
        finally:
            reader.close()

    @staticmethod
    def csv_line(row: list):
        output = io.StringIO(newline="")
        csv.writer(output).writerow(row)
        return output.getvalue().encode("utf-8")

    def close(self):
        self.db.close()
