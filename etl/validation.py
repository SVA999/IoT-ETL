"""Validacion cientifica de la imputacion.

Pregunta que responde este modulo:

    Que metodo reconstruye mejor las concentraciones de PM2.5 de la red SIATA
    bajo distintos porcentajes de datos faltantes?

Metodologia (la de la guia del proyecto):

    serie con buena disponibilidad
        -> ocultar artificialmente 5 / 10 / 20 / 30 %
        -> imputar con Linear, Cubic, Nearest y el ENSAMBLE
        -> comparar contra el valor real
        -> MAE / RMSE / MAPE / R2 / tiempo / % valores imposibles

Los huecos se ocultan en BLOQUES, no en puntos sueltos: en la vida real una
estacion se cae varias horas seguidas, y un hueco de 4 h es mucho mas dificil
que 4 huecos de 1 h. Medirlo con puntos sueltos daria un resultado optimista.
"""
from __future__ import annotations

import time

import numpy as np
import pandas as pd

import config
from .imputation import METHODS, blend, estimate_weights, gap_lengths, interpolate

DEFAULT_FRACTIONS = (0.05, 0.10, 0.20, 0.30)
DEFAULT_BLOCKS = (1, 2, 3, 4, 6)


def _metrics(truth: np.ndarray, pred: np.ndarray) -> dict:
    ok = ~np.isnan(pred) & ~np.isnan(truth)
    n = int(ok.sum())
    if n == 0:
        return {"n": 0, "mae": None, "rmse": None, "mape": None, "r2": None,
                "impossible_pct": None, "coverage_pct": 0.0}
    t, p = truth[ok], pred[ok]
    err = p - t
    # MAPE solo sobre valores no nulos: con t = 0 la division explota.
    nz = np.abs(t) > 1e-9
    mape = float(np.mean(np.abs(err[nz] / t[nz])) * 100) if nz.any() else None
    ss_res = float(np.sum(err ** 2))
    ss_tot = float(np.sum((t - t.mean()) ** 2))
    impossible = np.sum((p < config.PM25_MIN) | (p > config.PM25_MAX))
    return {
        "n": n,
        "mae": round(float(np.mean(np.abs(err))), 4),
        "rmse": round(float(np.sqrt(np.mean(err ** 2))), 4),
        "mape": round(mape, 3) if mape is not None else None,
        "r2": round(1 - ss_res / ss_tot, 4) if ss_tot > 0 else None,
        "impossible_pct": round(100 * float(impossible) / n, 3),
        "coverage_pct": round(100 * n / len(truth), 2),
    }


def _mask_blocks(
    series: pd.Series, fraction: float, rng: np.random.Generator
) -> list[int]:
    """Oculta bloques contiguos hasta cubrir `fraction` de los datos observados."""
    observed = series.notna().to_numpy()
    idx = np.flatnonzero(observed)[1:-1]  # nunca los extremos
    if len(idx) < 20:
        return []
    target = int(len(idx) * fraction)
    hidden: set[int] = set()
    pool = list(idx)
    rng.shuffle(pool)
    for start in pool:
        if len(hidden) >= target:
            break
        length = int(rng.choice(DEFAULT_BLOCKS))
        block = list(range(start, min(start + length, len(series) - 1)))
        if not block or any(p in hidden or not observed[p] for p in block):
            continue
        hidden.update(block)
    return sorted(hidden)


def benchmark_series(
    series: pd.Series,
    fractions=DEFAULT_FRACTIONS,
    repeats: int = 3,
    seed: int = 7,
) -> list[dict]:
    """Corre el experimento sobre una sola serie horaria."""
    rng = np.random.default_rng(seed)
    rows = []
    for fraction in fractions:
        acc: dict[str, list[dict]] = {}
        elapsed: dict[str, list[float]] = {}
        for _ in range(repeats):
            hidden = _mask_blocks(series, fraction, rng)
            if len(hidden) < 10:
                continue
            probe = series.copy()
            probe.iloc[hidden] = np.nan
            truth = series.iloc[hidden].to_numpy(dtype=float)

            candidates = {}
            preds: dict[str, np.ndarray] = {}
            for m in METHODS:
                t0 = time.perf_counter()
                estimate = interpolate(probe, m)
                elapsed.setdefault(m, []).append(time.perf_counter() - t0)
                candidates[m] = estimate
                preds[m] = estimate.iloc[hidden].to_numpy(dtype=float)

            # El ensamble se calibra SOLO con lo que queda visible: si usara los
            # datos ocultos para elegir pesos estariamos haciendo trampa.
            t0 = time.perf_counter()
            weights, _ = estimate_weights(probe, rng=np.random.default_rng(seed))
            gaps = gap_lengths(probe.isna().to_numpy())
            ens = blend(pd.DataFrame(candidates, index=probe.index), gaps, weights)
            elapsed.setdefault("ensemble", []).append(time.perf_counter() - t0)
            preds["ensemble"] = ens.iloc[hidden].to_numpy(dtype=float)

            for name, pred in preds.items():
                acc.setdefault(name, []).append(_metrics(truth, pred))

        for name, runs in acc.items():
            valid = [r for r in runs if r["n"]]
            if not valid:
                continue
            rows.append(
                {
                    "missing_pct": round(fraction * 100, 1),
                    "method": name,
                    "n": int(np.sum([r["n"] for r in valid])),
                    "mae": round(float(np.mean([r["mae"] for r in valid])), 4),
                    "rmse": round(float(np.mean([r["rmse"] for r in valid])), 4),
                    "mape": round(
                        float(np.mean([r["mape"] for r in valid if r["mape"] is not None])), 3
                    )
                    if any(r["mape"] is not None for r in valid)
                    else None,
                    "r2": round(
                        float(np.mean([r["r2"] for r in valid if r["r2"] is not None])), 4
                    )
                    if any(r["r2"] is not None for r in valid)
                    else None,
                    "impossible_pct": round(
                        float(np.mean([r["impossible_pct"] for r in valid])), 3
                    ),
                    "coverage_pct": round(
                        float(np.mean([r["coverage_pct"] for r in valid])), 2
                    ),
                    "time_ms": round(float(np.mean(elapsed.get(name, [0])) * 1000), 3),
                }
            )
    return rows


def benchmark(
    df: pd.DataFrame,
    max_stations: int = 6,
    fractions=DEFAULT_FRACTIONS,
    repeats: int = 3,
) -> dict:
    """Corre el experimento sobre las estaciones con mejor disponibilidad.

    `df` es la salida de transform(): usamos value_clean (solo dato observado y
    validado) como verdad de referencia.
    """
    if df.empty:
        return {"rows": [], "stations": [], "winner": None}

    ranked = (
        df.groupby(["station_id", "station_code"])["value_clean"]
        .apply(lambda s: s.notna().mean())
        .sort_values(ascending=False)
    )
    chosen = ranked.head(max_stations)

    all_rows, used = [], []
    for (sid, code), availability in chosen.items():
        serie = (
            df[df["station_id"] == sid]
            .set_index("timestamp")["value_clean"]
            .astype("float64")
            .sort_index()
        )
        rows = benchmark_series(serie, fractions=fractions, repeats=repeats)
        for r in rows:
            r["station_id"] = int(sid)
            r["station_code"] = code
        all_rows.extend(rows)
        used.append(
            {
                "station_id": int(sid),
                "station_code": code,
                "availability_pct": round(float(availability) * 100, 2),
                "points": int(serie.notna().sum()),
            }
        )

    summary = _summarize(all_rows)
    return {
        "methodology": (
            "Ocultamiento artificial por bloques de 1-6 h sobre datos observados, "
            f"{repeats} repeticiones por nivel de faltantes; el ensamble se calibra "
            "solo con los datos visibles."
        ),
        "fractions": [round(f * 100, 1) for f in fractions],
        "stations": used,
        "rows": all_rows,
        "summary": summary,
        "winner": min(summary, key=lambda r: r["mae"])["method"] if summary else None,
    }


def _summarize(rows: list[dict]) -> list[dict]:
    """Promedia las metricas por metodo a traves de estaciones y % de faltantes."""
    if not rows:
        return []
    df = pd.DataFrame(rows)
    agg = (
        df.groupby("method")
        .agg(
            mae=("mae", "mean"),
            rmse=("rmse", "mean"),
            mape=("mape", "mean"),
            r2=("r2", "mean"),
            impossible_pct=("impossible_pct", "mean"),
            time_ms=("time_ms", "mean"),
            n=("n", "sum"),
        )
        .reset_index()
        .sort_values("mae")
    )
    return [
        {
            "method": r["method"],
            "mae": round(float(r["mae"]), 4),
            "rmse": round(float(r["rmse"]), 4),
            "mape": None if pd.isna(r["mape"]) else round(float(r["mape"]), 3),
            "r2": None if pd.isna(r["r2"]) else round(float(r["r2"]), 4),
            "impossible_pct": round(float(r["impossible_pct"]), 3),
            "time_ms": round(float(r["time_ms"]), 3),
            "n": int(r["n"]),
        }
        for _, r in agg.iterrows()
    ]
