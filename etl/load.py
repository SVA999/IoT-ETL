"""L de ETL: carga en el almacen SQLite.

SQLite mantiene el proyecto desplegable en una EC2 t2.micro sin servicios
extra. El esquema separa el dato crudo del limpio y del imputado, siguiendo la
regla de oro del pipeline: nunca sobrescribir el origen.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

import pandas as pd

import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS measurements (
    station_id        INTEGER NOT NULL,
    station_code      TEXT,
    station_name      TEXT,
    lat               REAL,
    lon               REAL,
    timestamp         TEXT NOT NULL,
    pollutant         TEXT NOT NULL,
    value_raw         REAL,
    quality_raw       REAL,
    value_clean       REAL,
    value_linear      REAL,
    value_cubic       REAL,
    value_nearest     REAL,
    value_ensemble    REAL,
    value_final       REAL,
    is_missing_source INTEGER,
    is_sentinel       INTEGER,
    is_out_of_range   INTEGER,
    is_bad_quality    INTEGER,
    is_outlier        INTEGER,
    is_gap            INTEGER,
    is_imputed        INTEGER,
    is_unresolved     INTEGER,
    gap_length        INTEGER,
    confidence        REAL,
    imputation_method TEXT,
    quality_flag      TEXT,
    source            TEXT,
    ingested_at       TEXT,
    PRIMARY KEY (station_id, pollutant, timestamp, source)
);
CREATE INDEX IF NOT EXISTS idx_meas_station_ts
    ON measurements (station_id, timestamp);

CREATE TABLE IF NOT EXISTS stations (
    station_id   INTEGER PRIMARY KEY,
    station_code TEXT,
    station_name TEXT,
    lat          REAL,
    lon          REAL,
    updated_at   TEXT
);

CREATE TABLE IF NOT EXISTS etl_runs (
    run_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT,
    finished_at TEXT,
    source      TEXT,
    realtime    INTEGER,
    rows_in     INTEGER,
    rows_out    INTEGER,
    duration_s  REAL,
    report      TEXT
);

CREATE TABLE IF NOT EXISTS imputation_benchmark (
    run_id       INTEGER,
    station_id   INTEGER,
    station_code TEXT,
    missing_pct  REAL,
    method       TEXT,
    mae          REAL,
    rmse         REAL,
    mape         REAL,
    r2           REAL,
    impossible_pct REAL,
    time_ms      REAL,
    n            INTEGER
);
"""

PERSISTED_COLUMNS = [
    "station_id", "station_code", "station_name", "lat", "lon", "timestamp",
    "pollutant", "value_raw", "quality_raw", "value_clean", "value_linear",
    "value_cubic", "value_nearest", "value_ensemble", "value_final",
    "is_missing_source", "is_sentinel", "is_out_of_range", "is_bad_quality",
    "is_outlier", "is_gap", "is_imputed", "is_unresolved", "gap_length",
    "confidence", "imputation_method", "quality_flag", "source", "ingested_at",
]


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)


def _prepare(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    for col in PERSISTED_COLUMNS:
        if col not in out:
            out[col] = None
    out = out[PERSISTED_COLUMNS]
    for col in ("timestamp", "ingested_at"):
        out[col] = pd.to_datetime(out[col], errors="coerce").dt.strftime(
            "%Y-%m-%d %H:%M:%S"
        )
    bool_cols = [c for c in PERSISTED_COLUMNS if c.startswith("is_")]
    for col in bool_cols:
        out[col] = out[col].fillna(False).astype(bool).astype(int)
    return out.astype(object).where(pd.notna(out), None)


def save_measurements(df: pd.DataFrame, source: str) -> int:
    """Reemplaza las mediciones de esa fuente por el resultado del run actual."""
    if df.empty:
        return 0
    rows = _prepare(df)
    placeholders = ",".join("?" * len(PERSISTED_COLUMNS))
    with connect() as conn:
        conn.execute("DELETE FROM measurements WHERE source = ?", (source,))
        conn.executemany(
            f"INSERT OR REPLACE INTO measurements VALUES ({placeholders})",
            rows.itertuples(index=False, name=None),
        )
        stations = (
            df[["station_id", "station_code", "station_name", "lat", "lon"]]
            .drop_duplicates("station_id")
            .astype(object)
        )
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        conn.executemany(
            "INSERT OR REPLACE INTO stations VALUES (?,?,?,?,?,?)",
            [(*r, now) for r in stations.itertuples(index=False, name=None)],
        )
    return len(rows)


def save_run(
    started: datetime,
    finished: datetime,
    source: str,
    realtime: bool,
    rows_in: int,
    rows_out: int,
    report: dict,
) -> int:
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO etl_runs (started_at, finished_at, source, realtime,"
            " rows_in, rows_out, duration_s, report) VALUES (?,?,?,?,?,?,?,?)",
            (
                started.isoformat(timespec="seconds"),
                finished.isoformat(timespec="seconds"),
                source,
                int(realtime),
                rows_in,
                rows_out,
                round((finished - started).total_seconds(), 3),
                json.dumps(report, default=str),
            ),
        )
        return int(cur.lastrowid)


def save_benchmark(run_id: int, rows: list[dict]) -> None:
    if not rows:
        return
    with connect() as conn:
        conn.executemany(
            "INSERT INTO imputation_benchmark (run_id, station_id, station_code,"
            " missing_pct, method, mae, rmse, mape, r2, impossible_pct, time_ms, n)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    run_id,
                    r.get("station_id"),
                    r.get("station_code"),
                    r.get("missing_pct"),
                    r.get("method"),
                    r.get("mae"),
                    r.get("rmse"),
                    r.get("mape"),
                    r.get("r2"),
                    r.get("impossible_pct"),
                    r.get("time_ms"),
                    r.get("n"),
                )
                for r in rows
            ],
        )


def last_runs(limit: int = 10) -> list[dict]:
    with connect() as conn:
        cur = conn.execute(
            "SELECT run_id, started_at, finished_at, source, realtime, rows_in,"
            " rows_out, duration_s FROM etl_runs ORDER BY run_id DESC LIMIT ?",
            (limit,),
        )
        return [dict(r) for r in cur.fetchall()]


def db_stats() -> dict:
    try:
        with connect() as conn:
            measurements = conn.execute(
                "SELECT COUNT(*) FROM measurements"
            ).fetchone()[0]
            stations = conn.execute("SELECT COUNT(*) FROM stations").fetchone()[0]
            runs = conn.execute("SELECT COUNT(*) FROM etl_runs").fetchone()[0]
        return {
            "path": str(config.DB_PATH),
            "measurements": measurements,
            "stations": stations,
            "runs": runs,
        }
    except sqlite3.Error as exc:
        return {"path": str(config.DB_PATH), "error": str(exc)}
