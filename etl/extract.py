"""E de ETL: extraccion desde las fuentes publicas de SIATA.

Dos fuentes, misma forma de salida (dataframe "largo" canonico):

1. SIATA en vivo  -> Geoportal fastgeoapi (GeoJSON + series horarias de 72 h).
2. Archivo local  -> Datos_SIATA_Aire_pm25.json (2019-2020, respaldo de clase).

Aqui no se limpia nada: la extraccion entrega el dato *crudo* tal cual lo
publica la fuente, para que la transformacion pueda auditar cuantos nulos,
centinelas y outliers traia el origen.
"""
from __future__ import annotations

import io
import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pandas as pd
import requests

import config

log = logging.getLogger(__name__)

COLUMNS = [
    "station_id",
    "station_code",
    "station_name",
    "lat",
    "lon",
    "timestamp",
    "pollutant",
    "value_raw",
    "quality_raw",
    "source",
]

_SESSION = requests.Session()
_SESSION.headers.update({"User-Agent": config.USER_AGENT})

_CACHE: dict[str, tuple[float, Any]] = {}


def _cached(key: str, ttl: int, producer):
    """Cache en memoria con TTL: evita golpear a SIATA en cada request."""
    hit = _CACHE.get(key)
    if hit and (time.time() - hit[0]) < ttl:
        return hit[1]
    value = producer()
    _CACHE[key] = (time.time(), value)
    return value


def clear_cache() -> None:
    _CACHE.clear()


def _empty() -> pd.DataFrame:
    return pd.DataFrame(columns=COLUMNS)


# --------------------------------------------------------------------------- #
# Fuente 1: SIATA en vivo
# --------------------------------------------------------------------------- #
def fetch_live_snapshot() -> list[dict]:
    """Ultimo promedio 24 h de PM2.5 + ICA por estacion (GeoJSON publico)."""
    r = _SESSION.get(config.URL_LIVE_SNAPSHOT, timeout=config.HTTP_TIMEOUT)
    r.raise_for_status()
    features = r.json().get("features", [])
    out = []
    for f in features:
        p = f.get("properties", {})
        coords = f.get("geometry", {}).get("coordinates") or [None, None]
        lon, lat = coords[0], coords[1]
        out.append(
            {
                "station_id": int(p["codigo"]),
                "station_code": p.get("estacion"),
                "station_name": p.get("nombreEstacion"),
                "municipio": p.get("Municipio"),
                "lat": lat,
                "lon": lon,
                "pm25_24h": p.get("PM25_24H_prom"),
                "ica_24h": p.get("ICA_24H_prom"),
                "color_siata": p.get("color"),
                "ventana_inicio": p.get("fechaInicio"),
                "ventana_fin": p.get("fechaFin"),
                "figura_24h": p.get("Figura_24h"),
            }
        )
    return out


def _fetch_series_72h(meta: dict) -> pd.DataFrame:
    """Serie horaria de 72 h de una estacion. Vacio si la estacion falla."""
    url = config.URL_LIVE_72H.format(codigo=meta["station_id"])
    try:
        r = _SESSION.get(url, timeout=config.HTTP_TIMEOUT)
        r.raise_for_status()
        info = r.json()["info"]
    except Exception as exc:  # una estacion caida no debe tumbar la ingesta
        log.warning("Serie 72h no disponible para %s: %s", meta["station_code"], exc)
        return _empty()

    values = info.get("PM25_72H") or []
    if not values:
        return _empty()

    # El JSON solo trae la hora final: reconstruimos el eje temporal hacia atras.
    end = pd.to_datetime(info.get("Hora_fin"), errors="coerce")
    if pd.isna(end):
        return _empty()
    index = pd.date_range(end=end, periods=len(values), freq="h")

    df = pd.DataFrame(
        {
            "station_id": meta["station_id"],
            "station_code": meta["station_code"],
            "station_name": meta["station_name"],
            "lat": meta["lat"],
            "lon": meta["lon"],
            "timestamp": index,
            "pollutant": "pm25",
            "value_raw": pd.to_numeric(pd.Series(values), errors="coerce").to_numpy(),
            "quality_raw": float("nan"),
            "source": "SIATA_LIVE",
        }
    )
    return df[COLUMNS]


def fetch_live() -> pd.DataFrame:
    """Ingesta en vivo: catalogo + 72 h horarias de todas las estaciones."""
    stations = fetch_live_snapshot()
    if not stations:
        return _empty()
    with ThreadPoolExecutor(max_workers=8) as pool:
        frames = [f for f in pool.map(_fetch_series_72h, stations) if not f.empty]
    if not frames:
        return _empty()
    return pd.concat(frames, ignore_index=True)


def fetch_catalog() -> pd.DataFrame:
    """Catalogo oficial de estaciones de calidad del aire (CSV publico)."""
    r = _SESSION.get(config.URL_CATALOGO, timeout=config.HTTP_TIMEOUT)
    r.raise_for_status()
    r.encoding = r.encoding or "utf-8"
    df = pd.read_csv(io.StringIO(r.text), skip_blank_lines=True)
    df.columns = [c.strip() for c in df.columns]
    return df


# --------------------------------------------------------------------------- #
# Fuente 2: archivo historico local
# --------------------------------------------------------------------------- #
def fetch_file(path=None) -> pd.DataFrame:
    """Lee Datos_SIATA_Aire_pm25.json y lo aplana a la forma canonica."""
    path = path or config.FALLBACK_FILE
    if not path.exists():
        raise FileNotFoundError(f"No se encontro el archivo de respaldo: {path}")

    with open(path, encoding="utf-8") as fh:
        stations = json.load(fh)

    rows = []
    for s in stations:
        sid = int(s["codigoSerial"])
        code = s.get("nombreCorto") or str(sid)
        name = s.get("nombre") or code
        lat, lon = float(s["latitud"]), float(s["longitud"])
        for d in s.get("datos", []):
            rows.append(
                (
                    sid,
                    code,
                    name,
                    lat,
                    lon,
                    d.get("fecha"),
                    str(d.get("variableConsulta", "pm25")).lower(),
                    d.get("valor"),
                    d.get("calidad"),
                    "SIATA_FILE",
                )
            )

    df = pd.DataFrame(rows, columns=COLUMNS)
    df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
    df["value_raw"] = pd.to_numeric(df["value_raw"], errors="coerce")
    df["quality_raw"] = pd.to_numeric(df["quality_raw"], errors="coerce")
    return df


# --------------------------------------------------------------------------- #
# Orquestador de extraccion
# --------------------------------------------------------------------------- #
def extract(source: str = "auto") -> tuple[pd.DataFrame, dict]:
    """Devuelve (dataframe crudo, metadatos de la ingesta).

    source: "live" | "file" | "auto" (intenta en vivo y cae al archivo).
    """
    meta: dict[str, Any] = {"requested": source, "live_error": None}

    if source in ("live", "auto"):
        try:
            df = _cached("live", config.LIVE_TTL_SECONDS, fetch_live)
            if not df.empty:
                meta.update(
                    source="SIATA_LIVE",
                    realtime=True,
                    endpoint=config.URL_LIVE_SNAPSHOT,
                    rows=int(len(df)),
                )
                return df.copy(), meta
            meta["live_error"] = "El Geoportal respondio sin series horarias."
        except Exception as exc:
            log.warning("Ingesta en vivo fallida: %s", exc)
            meta["live_error"] = f"{type(exc).__name__}: {exc}"
        if source == "live":
            meta.update(source="SIATA_LIVE", realtime=True, rows=0)
            return _empty(), meta

    df = _cached("file", 3600, fetch_file)
    meta.update(
        source="SIATA_FILE",
        realtime=False,
        endpoint=config.FALLBACK_FILE.name,
        rows=int(len(df)),
    )
    return df.copy(), meta
