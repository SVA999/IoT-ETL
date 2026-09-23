"""T de ETL: limpieza, control de calidad e imputacion.

Orden de las reglas (cada una deja su propia bandera para poder auditar):

  1. Tipos y timestamps invalidos      -> se descartan filas irreparables.
  2. Duplicados (estacion, hora)       -> se conserva el ultimo reporte.
  3. Nulos de origen                   -> is_missing_source
  4. Valores centinela (-9999, 99999,
     985, 995, ...)                    -> is_sentinel
  5. Rango fisico [0, 500] ug/m3       -> is_out_of_range
  6. Bandera de calidad de SIATA >=2.6 -> is_bad_quality
  7. Outliers locales (Hampel/MAD)     -> is_outlier
  8. Rejilla horaria completa          -> is_gap (huecos que el origen ni
                                          siquiera reporta)
  9. Imputacion por ensamble           -> is_imputed + confianza

Nada se sobrescribe: value_raw se conserva siempre y value_final es la serie
utilizable. Asi el dato imputado nunca se confunde con el observado.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

import config
from .imputation import METHODS, impute_series

AUDIT_FLAGS = [
    "is_missing_source",
    "is_sentinel",
    "is_out_of_range",
    "is_bad_quality",
    "is_outlier",
    "is_gap",
]


def _hampel_outliers(values: pd.Series) -> pd.Series:
    """Filtro de Hampel: mediana movil + MAD, robusto frente a picos aislados."""
    window = config.HAMPEL_WINDOW
    if values.notna().sum() < window:
        return pd.Series(False, index=values.index)
    med = values.rolling(window, center=True, min_periods=3).median()
    mad = (values - med).abs().rolling(window, center=True, min_periods=3).median()
    sigma = 1.4826 * mad
    # Con MAD = 0 (serie plana) no hay dispersion que medir: no marcamos nada.
    deviation = (values - med).abs()
    threshold = np.maximum(config.HAMPEL_SIGMAS * sigma, config.HAMPEL_MIN_ABS)
    flagged = (sigma > 0) & (deviation > threshold)
    return flagged.fillna(False)


def clean(df: pd.DataFrame) -> pd.DataFrame:
    """Aplica las reglas 1-7 y devuelve el dataframe con banderas y value_clean."""
    df = df.copy()
    df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
    df["value_raw"] = pd.to_numeric(df["value_raw"], errors="coerce")
    df["quality_raw"] = pd.to_numeric(df.get("quality_raw"), errors="coerce")
    df = df.dropna(subset=["timestamp", "station_id"])
    # El dato mas reciente de una misma hora es el que manda.
    df = df.drop_duplicates(
        subset=["station_id", "pollutant", "timestamp"], keep="last"
    ).sort_values(["station_id", "timestamp"])

    v = df["value_raw"]
    df["is_missing_source"] = v.isna()
    df["is_sentinel"] = v.isin(config.SENTINELS)
    df["is_out_of_range"] = (~v.isna()) & (
        (v < config.PM25_MIN) | (v > config.PM25_MAX)
    )
    df["is_bad_quality"] = df["quality_raw"].ge(config.QUALITY_FLAG_OK).fillna(False)

    invalid = (
        df["is_missing_source"]
        | df["is_sentinel"]
        | df["is_out_of_range"]
        | df["is_bad_quality"]
    )
    df["value_clean"] = v.mask(invalid)

    # Los outliers se buscan sobre lo que sobrevivio, no sobre los centinelas.
    df["is_outlier"] = (
        df.groupby("station_id", group_keys=False)["value_clean"]
        .apply(_hampel_outliers)
        .astype(bool)
    )
    df.loc[df["is_outlier"], "value_clean"] = np.nan
    return df


def _to_hourly_grid(g: pd.DataFrame) -> pd.DataFrame:
    """Reindexa una estacion a rejilla horaria continua, marcando los huecos."""
    g = g.set_index("timestamp").sort_index()
    full = pd.date_range(g.index.min(), g.index.max(), freq="h")
    out = g.reindex(full)
    out["is_gap"] = ~out.index.isin(g.index)
    for col in ("station_id", "station_code", "station_name", "lat", "lon",
                "pollutant", "source"):
        if col in out:
            out[col] = out[col].ffill().bfill()
    for flag in AUDIT_FLAGS:
        if flag in out:
            out[flag] = out[flag].fillna(False).astype(bool)
    out.index.name = "timestamp"
    return out.reset_index()


def transform(df: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    """Ejecuta limpieza + rejilla + imputacion. Devuelve (tabla final, reporte)."""
    if df.empty:
        return df, {"stations": 0, "rows": 0}

    cleaned = clean(df)
    frames, per_station = [], []

    for sid, g in cleaned.groupby("station_id", sort=True):
        grid = _to_hourly_grid(g)
        result = impute_series(grid.set_index("timestamp")["value_clean"])

        grid = grid.set_index("timestamp")
        for m in METHODS:
            grid[f"value_{m}"] = result["candidates"][m]
        grid["value_ensemble"] = result["ensemble"]
        grid["value_final"] = result["final"]
        grid["is_imputed"] = result["is_imputed"]
        grid["is_unresolved"] = result["unresolved"]
        grid["gap_length"] = result["gap_length"]
        grid["confidence"] = np.where(
            grid["is_imputed"], result["confidence"], 1.0
        )
        grid["imputation_method"] = np.where(
            grid["is_imputed"],
            "ensemble(linear+cubic+nearest)",
            None,
        )
        grid["quality_flag"] = np.select(
            [
                grid["value_clean"].notna(),
                grid["is_imputed"].astype(bool),
            ],
            ["OBSERVED", "IMPUTED"],
            default="MISSING",
        )
        grid = grid.reset_index()

        total = len(grid)
        observed = int(grid["value_clean"].notna().sum())
        imputed = int(grid["is_imputed"].sum())
        per_station.append(
            {
                "station_id": int(sid),
                "station_code": grid["station_code"].iloc[0],
                "station_name": grid["station_name"].iloc[0],
                "lat": float(grid["lat"].iloc[0]),
                "lon": float(grid["lon"].iloc[0]),
                "rows": total,
                "observed": observed,
                "missing_source": int(grid["is_missing_source"].sum()),
                "sentinels": int(grid["is_sentinel"].sum()),
                "out_of_range": int(grid["is_out_of_range"].sum()),
                "bad_quality": int(grid["is_bad_quality"].sum()),
                "outliers": int(grid["is_outlier"].sum()),
                "gaps": int(grid["is_gap"].sum()),
                "imputed": imputed,
                "unresolved": int(grid["is_unresolved"].sum()),
                "availability_raw_pct": round(100 * observed / total, 2) if total else 0,
                "availability_final_pct": round(
                    100 * int(grid["value_final"].notna().sum()) / total, 2
                )
                if total
                else 0,
                "weights": {
                    bucket: {m: round(w, 4) for m, w in ws.items()}
                    for bucket, ws in result["weights"].items()
                },
                "cv_mae": {
                    bucket: {
                        m: (None if np.isnan(v) else round(v, 3))
                        for m, v in errs.items()
                    }
                    for bucket, errs in result["cv_mae"].items()
                },
                "indicator": _siata_indicator(100 * observed / total if total else 0),
            }
        )
        frames.append(grid)

    final = pd.concat(frames, ignore_index=True)
    final["ingested_at"] = pd.Timestamp.utcnow().tz_localize(None)

    report = _global_report(final, per_station)
    return final, report


def _siata_indicator(pct: float) -> str:
    """Indicador de disponibilidad con los cortes que usa SIATA (>90 / 75-90 / <75)."""
    if pct > 90:
        return "OK"
    if pct >= 75:
        return "ADVERTENCIA"
    return "ERROR"


def _global_report(df: pd.DataFrame, per_station: list[dict]) -> dict:
    total = len(df)
    observed = int(df["value_clean"].notna().sum())
    imputed = int(df["is_imputed"].sum())
    usable = int(df["value_final"].notna().sum())
    return {
        "rows": total,
        "stations": int(df["station_id"].nunique()),
        "window": {
            "start": df["timestamp"].min().isoformat(),
            "end": df["timestamp"].max().isoformat(),
        },
        "observed": observed,
        "missing_source": int(df["is_missing_source"].sum()),
        "sentinels": int(df["is_sentinel"].sum()),
        "out_of_range": int(df["is_out_of_range"].sum()),
        "bad_quality": int(df["is_bad_quality"].sum()),
        "outliers": int(df["is_outlier"].sum()),
        "gaps": int(df["is_gap"].sum()),
        "imputed": imputed,
        "unresolved": int(df["is_unresolved"].sum()),
        "availability_raw_pct": round(100 * observed / total, 2) if total else 0,
        "availability_final_pct": round(100 * usable / total, 2) if total else 0,
        "recovered_pct": round(100 * imputed / total, 2) if total else 0,
        "indicator": _siata_indicator(100 * observed / total if total else 0),
        "per_station": per_station,
    }
