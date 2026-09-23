"""Endpoints REST de NEON AIR.

Contrato pensado para que la PWA solo tenga que hacer:

    GPS -> lat/lon -> /api/air-quality/nearest -> semaforo

El resto de endpoints existen para auditar el ETL: calidad de datos,
imputacion, comparacion de metodos y estado de las corridas.
"""
from __future__ import annotations

import logging
import threading

from flask import Blueprint, jsonify, request

import config
from etl import load, pipeline
from etl.aqi import scale
from etl.extract import fetch_catalog
from etl.validation import benchmark as run_benchmark

log = logging.getLogger(__name__)
api = Blueprint("api", __name__, url_prefix="/api")

_BENCH_LOCK = threading.Lock()
_BENCH_CACHE: dict = {}


def _state(refresh: bool = False, source: str | None = None):
    return pipeline.get_pipeline_result(refresh=refresh, source=source)


def _float_arg(name: str, lo: float, hi: float) -> float:
    raw = request.args.get(name)
    if raw is None:
        raise ValueError(f"Falta el parametro '{name}'.")
    try:
        value = float(raw)
    except ValueError:
        raise ValueError(f"El parametro '{name}' debe ser numerico.")
    if not lo <= value <= hi:
        raise ValueError(f"'{name}' fuera de rango [{lo}, {hi}].")
    return value


@api.errorhandler(ValueError)
def _bad_request(exc: ValueError):
    return jsonify({"error": str(exc)}), 400


# --------------------------------------------------------------------------- #
# Estado
# --------------------------------------------------------------------------- #
@api.get("/health")
def health():
    ready = pipeline.has_state()
    body = {
        "status": "online",
        "app": config.APP_NAME,
        "version": config.APP_VERSION,
        "pipeline_ready": ready,
        "pipeline_age_s": pipeline.state_age_seconds(),
        "fallback_file": config.FALLBACK_FILE.name,
        "fallback_available": config.FALLBACK_FILE.exists(),
        "algorithm": "ensamble calibrado: linear + cubic + nearest",
        "db": load.db_stats(),
    }
    if ready:
        state = _state()
        body["dataset"] = {
            "source": state["ingest"]["source"],
            "realtime": bool(state["ingest"].get("realtime")),
            "rows": state["report"]["rows"],
            "stations": state["report"]["stations"],
            "window": state["report"]["window"],
            "live_error": state["ingest"].get("live_error"),
            "last_run_s": state["duration_s"],
        }
    return jsonify(body)


@api.get("/scale")
def ica_scale():
    return jsonify({"pollutant": "PM2.5", "unit": "ug/m3",
                    "norm": "Resolucion 2254 de 2017 (Colombia)",
                    "levels": scale()})


# --------------------------------------------------------------------------- #
# Estaciones
# --------------------------------------------------------------------------- #
@api.get("/stations")
def stations():
    state = _state()
    return jsonify(
        {
            "source": state["ingest"]["source"],
            "realtime": bool(state["ingest"].get("realtime")),
            "count": state["report"]["stations"],
            "stations": pipeline.all_stations(state),
        }
    )


@api.get("/stations/nearest")
def stations_nearest():
    lat = _float_arg("lat", -90, 90)
    lon = _float_arg("lon", -180, 180)
    result = pipeline.nearest_air_quality(_state(), lat, lon)
    return jsonify(result["nearest_station"])


@api.get("/stations/<int:station_id>")
def station_detail(station_id: int):
    snap = pipeline.station_snapshot(_state(), station_id)
    if not snap:
        return jsonify({"error": f"Estacion {station_id} sin datos en el run vigente."}), 404
    return jsonify(snap)


@api.get("/stations/<int:station_id>/history")
def station_history(station_id: int):
    limit = max(1, min(int(request.args.get("limit", 72)), 2000))
    data = pipeline.history(_state(), station_id, limit)
    if not data:
        return jsonify({"error": f"Estacion {station_id} no encontrada."}), 404
    return jsonify(data)


# --------------------------------------------------------------------------- #
# El boton del semaforo
# --------------------------------------------------------------------------- #
@api.get("/air-quality/nearest")
def air_quality_nearest():
    lat = _float_arg("lat", -90, 90)
    lon = _float_arg("lon", -180, 180)
    k = max(1, min(int(request.args.get("k", 3)), 8))
    return jsonify(pipeline.nearest_air_quality(_state(), lat, lon, k=k))


# --------------------------------------------------------------------------- #
# Calidad del dato
# --------------------------------------------------------------------------- #
@api.get("/quality")
def quality_report():
    state = _state()
    report = dict(state["report"])
    report["source"] = state["ingest"]["source"]
    report["realtime"] = bool(state["ingest"].get("realtime"))
    report["rules"] = {
        "sentinels": list(config.SENTINELS),
        "physical_range": [config.PM25_MIN, config.PM25_MAX],
        "siata_quality_flag_ok_below": config.QUALITY_FLAG_OK,
        "hampel": {"window": config.HAMPEL_WINDOW, "sigmas": config.HAMPEL_SIGMAS,
                   "min_abs": config.HAMPEL_MIN_ABS},
        "max_gap_hours": config.MAX_GAP_HOURS,
    }
    return jsonify(report)


@api.get("/quality/<int:station_id>")
def quality_station(station_id: int):
    state = _state()
    for s in state["report"]["per_station"]:
        if s["station_id"] == station_id:
            return jsonify(s)
    return jsonify({"error": f"Estacion {station_id} no encontrada."}), 404


@api.get("/imputation/<int:station_id>")
def imputation_station(station_id: int):
    """Como quedo la imputacion de esa estacion: pesos, errores y huecos."""
    state = _state()
    detail = next(
        (s for s in state["report"]["per_station"] if s["station_id"] == station_id),
        None,
    )
    if not detail:
        return jsonify({"error": f"Estacion {station_id} no encontrada."}), 404
    data = state["data"]
    g = data[(data["station_id"] == station_id) & data["is_imputed"]]
    return jsonify(
        {
            "station_id": station_id,
            "station_code": detail["station_code"],
            "method": "ensamble ponderado (linear + cubic + nearest)",
            "weights_by_gap": detail["weights"],
            "cv_mae_by_gap": detail["cv_mae"],
            "imputed": int(len(g)),
            "unresolved": detail["unresolved"],
            "max_gap_hours": config.MAX_GAP_HOURS,
            "mean_confidence": round(float(g["confidence"].mean()), 3) if len(g) else None,
            "samples": [
                {
                    "timestamp": r["timestamp"].isoformat(),
                    "linear": round(float(r["value_linear"]), 2),
                    "cubic": round(float(r["value_cubic"]), 2),
                    "nearest": round(float(r["value_nearest"]), 2),
                    "ensemble": round(float(r["value_ensemble"]), 2),
                    "gap_length": int(r["gap_length"]),
                    "confidence": round(float(r["confidence"]), 3),
                }
                for _, r in g.tail(20).iterrows()
            ],
        }
    )


@api.get("/imputation/benchmark")
def imputation_benchmark():
    """Comparacion Linear vs Cubic vs Nearest vs Ensamble ocultando datos reales."""
    refresh = request.args.get("refresh") == "1"
    state = _state()
    stations = max(1, min(int(request.args.get("stations", 4)), 10))
    repeats = max(1, min(int(request.args.get("repeats", 2)), 5))
    key = (state["ingest"]["source"], state["report"]["window"]["end"], stations, repeats)

    with _BENCH_LOCK:
        if _BENCH_CACHE.get("key") == key and not refresh:
            return jsonify(_BENCH_CACHE["value"])

    frame, dataset = pipeline.benchmark_frame(state, max_stations=stations)
    result = run_benchmark(frame, max_stations=stations, repeats=repeats)
    result["dataset"] = dataset
    result["dataset_note"] = (
        "Experimento corrido sobre el archivo historico de SIATA (un ano de datos"
        " horarios): la ventana en vivo de 72 h es demasiado corta para medir R2"
        " con sentido."
        if dataset == "SIATA_FILE" and state["ingest"]["source"] != "SIATA_FILE"
        else f"Experimento corrido sobre el dataset vigente ({dataset})."
    )
    with _BENCH_LOCK:
        _BENCH_CACHE["key"] = key
        _BENCH_CACHE["value"] = result
    return jsonify(result)


# --------------------------------------------------------------------------- #
# Operacion del ETL
# --------------------------------------------------------------------------- #
@api.post("/etl/run")
def etl_run():
    body = request.get_json(silent=True) or {}
    source = body.get("source", request.args.get("source", "auto"))
    if source not in ("auto", "live", "file"):
        raise ValueError("source debe ser 'auto', 'live' o 'file'.")
    with_bench = bool(body.get("benchmark", False))
    state = pipeline.run_pipeline(source, with_benchmark=with_bench)
    with _BENCH_LOCK:
        _BENCH_CACHE.clear()
    return jsonify(
        {
            "run_id": state["run_id"],
            "source": state["ingest"]["source"],
            "realtime": bool(state["ingest"].get("realtime")),
            "duration_s": state["duration_s"],
            "rows": state["report"]["rows"],
            "stations": state["report"]["stations"],
            "report": {k: v for k, v in state["report"].items() if k != "per_station"},
            "benchmark": state["benchmark"]["summary"] if state["benchmark"] else None,
        }
    )


@api.get("/etl/runs")
def etl_runs():
    return jsonify({"runs": load.last_runs(int(request.args.get("limit", 10)))})


@api.get("/siata/catalog")
def siata_catalog():
    """Catalogo oficial de estaciones publicado por SIATA (CSV en vivo)."""
    try:
        df = fetch_catalog()
    except Exception as exc:
        return jsonify({"available": False, "detail": f"{type(exc).__name__}: {exc}"}), 503
    keep = [c for c in ("Código", "Codigo", "Nombre_Estacion", "Nombre", "PM2.5",
                        "Latitud", "Longitud", "Municipio") if c in df.columns]
    return jsonify(
        {
            "available": True,
            "source": config.URL_CATALOGO,
            "rows": len(df),
            "stations": df[keep].fillna("").to_dict("records") if keep else [],
        }
    )
