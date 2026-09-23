"""Orquestador del ETL y capa de consulta que usa la API.

El pipeline corre completo (extraer -> transformar -> cargar) y deja el
resultado tanto en SQLite como en memoria, porque la PWA consulta muchas veces
y el ETL sobre el historico completo tarda decenas de segundos: pagarlo en
cada request seria absurdo.
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone

import numpy as np
import pandas as pd

import config

from . import load
from .aqi import classify
from .extract import extract
from .geo import idw_estimate, rank_by_distance
from .transform import transform
from .validation import benchmark

log = logging.getLogger(__name__)

_LOCK = threading.Lock()
# Serializa las corridas: sin esto, varias peticiones simultaneas al cargar la
# PWA lanzan pipelines en paralelo y el que termina de ultimo (normalmente el
# del archivo historico, que es mas lento) pisa el resultado en vivo.
_RUN_LOCK = threading.RLock()
_STATE: dict = {}


# --------------------------------------------------------------------------- #
# Ejecucion del pipeline
# --------------------------------------------------------------------------- #
def run_pipeline(
    source: str = "auto", with_benchmark: bool = False, persist: bool = True
) -> dict:
    """Corre el ETL completo y actualiza el estado en memoria."""
    with _RUN_LOCK:
        return _run_pipeline_locked(source, with_benchmark, persist)


def _run_pipeline_locked(source: str, with_benchmark: bool, persist: bool) -> dict:
    started = datetime.now(timezone.utc)
    raw, ingest_meta = extract(source)
    if raw.empty:
        raise RuntimeError(
            "Ninguna fuente entrego datos. "
            f"Detalle: {ingest_meta.get('live_error') or 'sin detalle'}"
        )

    processed, report = transform(raw)
    bench = benchmark(processed, max_stations=4, repeats=2) if with_benchmark else None
    finished = datetime.now(timezone.utc)

    run_id = None
    if persist:
        try:
            load.init_db()
            load.save_measurements(processed, ingest_meta["source"])
            run_id = load.save_run(
                started,
                finished,
                ingest_meta["source"],
                bool(ingest_meta.get("realtime")),
                len(raw),
                len(processed),
                report,
            )
            if bench:
                load.save_benchmark(run_id, bench["rows"])
        except Exception as exc:  # el almacen no debe tumbar la respuesta
            log.warning("No se pudo persistir el run: %s", exc)

    state = {
        "data": processed,
        "report": report,
        "ingest": ingest_meta,
        "benchmark": bench,
        "run_id": run_id,
        "started_at": started,
        "finished_at": finished,
        "duration_s": round((finished - started).total_seconds(), 2),
    }
    with _LOCK:
        _STATE.clear()
        _STATE.update(state)
    return state


def _usable(current: dict | None, refresh: bool, source: str | None) -> bool:
    if not current or refresh:
        return False
    return source is None or current["ingest"]["source"] == source


def get_pipeline_result(refresh: bool = False, source: str | None = None) -> dict:
    """Estado vigente del pipeline; lo corre si aun no existe o si se pide."""
    with _LOCK:
        current = dict(_STATE) if _STATE else None
    if _usable(current, refresh, source):
        _maybe_refresh_live(current)
        return current

    with _RUN_LOCK:
        # Otro hilo pudo haber corrido el pipeline mientras esperabamos el turno.
        with _LOCK:
            current = dict(_STATE) if _STATE else None
        if _usable(current, refresh, source):
            return current
        return run_pipeline(source or "auto")


def _maybe_refresh_live(current: dict) -> None:
    """Refresca en segundo plano cuando la lectura vigente ya envejecio.

    Aplica en los dos sentidos: renueva el dato en vivo cuando caduca, y
    reintenta la conexion con SIATA cuando estamos operando con el archivo de
    respaldo porque el Geoportal estaba caido. No se bloquea al usuario: se le
    entrega la lectura vigente y la siguiente consulta ya vera la actualizada.
    """
    if current["ingest"].get("requested") == "file":
        return  # el usuario pidio el historico a proposito
    age = state_age_seconds()
    if age is None or age < config.LIVE_TTL_SECONDS:
        return
    if not _RUN_LOCK.acquire(blocking=False):
        return  # ya hay una corrida en curso
    _RUN_LOCK.release()

    def _refresh():
        try:
            run_pipeline("auto")
            log.info("Dato en vivo refrescado (tenia %.0f s)", age)
        except Exception as exc:
            log.warning("Refresco en vivo fallido: %s", exc)

    threading.Thread(target=_refresh, name="etl-refresh", daemon=True).start()


def state_age_seconds() -> float | None:
    with _LOCK:
        finished = _STATE.get("finished_at")
    if not finished:
        return None
    return (datetime.now(timezone.utc) - finished).total_seconds()


def has_state() -> bool:
    with _LOCK:
        return bool(_STATE)


# --------------------------------------------------------------------------- #
# Consultas sobre el resultado
# --------------------------------------------------------------------------- #
def _station_frame(state: dict, station_id: int) -> pd.DataFrame:
    df = state["data"]
    return df[df["station_id"] == int(station_id)].sort_values("timestamp")


def _latest_row(g: pd.DataFrame) -> pd.Series | None:
    usable = g[g["value_final"].notna()]
    return None if usable.empty else usable.iloc[-1]


def _rolling_24h(g: pd.DataFrame) -> float | None:
    """Promedio movil de 24 h, que es la base del ICA de PM2.5."""
    tail = g[g["value_final"].notna()].tail(24)["value_final"]
    if tail.empty:
        return None
    return float(tail.mean())


def station_snapshot(state: dict, station_id: int) -> dict | None:
    """Ultimo estado conocido de una estacion, ya con semaforo."""
    g = _station_frame(state, station_id)
    if g.empty:
        return None
    row = _latest_row(g)
    if row is None:
        return None
    mean24 = _rolling_24h(g)
    per_station = {s["station_id"]: s for s in state["report"]["per_station"]}
    quality = per_station.get(int(station_id), {})
    return {
        "station_id": int(station_id),
        "station_code": row["station_code"],
        "station_name": row["station_name"],
        "lat": float(row["lat"]),
        "lon": float(row["lon"]),
        "timestamp": row["timestamp"].isoformat(),
        "pm25_hour": round(float(row["value_final"]), 2),
        "pm25_24h": round(mean24, 2) if mean24 is not None else None,
        "is_imputed": bool(row["is_imputed"]),
        "imputation_method": row["imputation_method"],
        "confidence": round(float(row["confidence"]), 3),
        "quality_flag": row["quality_flag"],
        "methods": {
            "linear": _num(row.get("value_linear")),
            "cubic": _num(row.get("value_cubic")),
            "nearest": _num(row.get("value_nearest")),
            "ensemble": _num(row.get("value_ensemble")),
        },
        "traffic_light": classify(mean24 if mean24 is not None else row["value_final"]),
        "data_quality": {
            k: quality.get(k)
            for k in (
                "availability_raw_pct",
                "availability_final_pct",
                "observed",
                "imputed",
                "outliers",
                "sentinels",
                "gaps",
                "unresolved",
                "indicator",
            )
        },
    }


def _num(value) -> float | None:
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return None if np.isnan(v) else round(v, 2)


def all_stations(state: dict) -> list[dict]:
    """Snapshot de todas las estaciones del run vigente."""
    ids = sorted(state["data"]["station_id"].unique())
    snaps = [station_snapshot(state, int(sid)) for sid in ids]
    return [s for s in snaps if s]


def history(state: dict, station_id: int, limit: int = 72) -> dict | None:
    """Serie reciente con las tres interpolaciones y el ensamble, para graficar."""
    g = _station_frame(state, station_id)
    if g.empty:
        return None
    tail = g.tail(limit)
    return {
        "station_id": int(station_id),
        "station_code": tail["station_code"].iloc[0],
        "station_name": tail["station_name"].iloc[0],
        "points": [
            {
                "timestamp": r["timestamp"].isoformat(),
                "raw": _num(r["value_raw"]),
                "clean": _num(r["value_clean"]),
                "linear": _num(r["value_linear"]),
                "cubic": _num(r["value_cubic"]),
                "nearest": _num(r["value_nearest"]),
                "ensemble": _num(r["value_ensemble"]),
                "final": _num(r["value_final"]),
                "flag": r["quality_flag"],
                "imputed": bool(r["is_imputed"]),
                "outlier": bool(r["is_outlier"]),
                "gap_length": int(r["gap_length"]) if pd.notna(r["gap_length"]) else 0,
                "confidence": _num(r["confidence"]),
            }
            for _, r in tail.iterrows()
        ],
    }


def nearest_air_quality(state: dict, lat: float, lon: float, k: int = 3) -> dict:
    """Respuesta del boton de GPS: semaforo en la posicion del usuario."""
    snapshots = all_stations(state)
    if not snapshots:
        raise RuntimeError("El pipeline no tiene estaciones con dato utilizable.")

    ranked = rank_by_distance(snapshots, lat, lon)
    nearest = ranked[0]

    # Estimacion en el punto exacto del usuario mezclando las k mas cercanas.
    base = "pm25_24h" if nearest.get("pm25_24h") is not None else "pm25_hour"
    idw = idw_estimate(ranked, value_key=base, k=k)
    estimated = idw["value"] if idw else nearest.get(base)

    return {
        "position": {"lat": lat, "lon": lon},
        "traffic_light": classify(estimated),
        "estimate": {
            "pm25": estimated,
            "basis": "promedio 24 h" if base == "pm25_24h" else "ultima hora",
            "spatial_method": idw["method"] if idw else "estacion mas cercana",
            "contributors": idw["contributors"] if idw else [],
        },
        "nearest_station": nearest,
        "neighbours": ranked[:k],
        "source": {
            "dataset": state["ingest"]["source"],
            "realtime": bool(state["ingest"].get("realtime")),
            "endpoint": state["ingest"].get("endpoint"),
            "window_end": state["report"]["window"]["end"],
            "live_error": state["ingest"].get("live_error"),
        },
    }


_BENCH_FRAME: dict = {}


def benchmark_frame(state: dict, max_stations: int = 4) -> tuple[pd.DataFrame, str]:
    """Elige el dataset sobre el que tiene sentido comparar los interpoladores.

    La ingesta en vivo solo trae 72 h por estacion: alcanza para operar la app,
    pero no para un experimento serio (con ~200 puntos ocultos el R2 se vuelve
    ruido). Cuando la ventana vigente es corta, el benchmark se corre sobre el
    archivo historico, que tiene un ano completo de datos horarios.
    """
    rows_per_station = len(state["data"]) / max(state["report"]["stations"], 1)
    if rows_per_station >= 500:
        return state["data"], state["ingest"]["source"]

    cached = _BENCH_FRAME.get(max_stations)
    if cached is not None:
        return cached, "SIATA_FILE"

    with _RUN_LOCK:
        cached = _BENCH_FRAME.get(max_stations)
        if cached is None:
            raw, _ = extract("file")
            ranked = (
                raw.groupby("station_id")["value_raw"]
                .apply(lambda s: s.notna().mean())
                .sort_values(ascending=False)
                .head(max_stations)
                .index
            )
            cached, _ = transform(raw[raw["station_id"].isin(ranked)])
            _BENCH_FRAME[max_stations] = cached
    return cached, "SIATA_FILE"


def ensure_started(source: str = "auto") -> None:
    """Calienta el pipeline en segundo plano al arrancar el servidor."""
    if has_state():
        return

    def _warm():
        try:
            run_pipeline(source)
            log.info("Pipeline listo (%s filas)", _STATE["report"]["rows"])
        except Exception as exc:
            log.error("No se pudo precalentar el pipeline: %s", exc)

    threading.Thread(target=_warm, name="etl-warmup", daemon=True).start()
