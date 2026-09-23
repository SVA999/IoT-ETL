"""Imputacion de datos faltantes por ENSAMBLE de interpoladores.

La consigna del proyecto es usar interpolacion lineal, cubica y nearest
"todas juntas para unir fuerzas". Aqui eso se implementa como un ensamble
ponderado, auto-calibrado y sensible al tamano del hueco, no como un
promedio ciego:

1.  Se calculan las tres interpolaciones candidatas sobre la serie horaria
    (solo hacia adentro: nunca se extrapola fuera del rango observado) y cada
    una se acota a la envolvente de los datos vecinos, que es lo que evita el
    overshoot del spline cubico.

2.  Se estiman los pesos por VALIDACION INTERNA: se ocultan artificialmente
    bloques de datos realmente observados y se mide el MAE de cada metodo al
    reconstruirlos. Y se hace POR TAMANO DE HUECO (1 h, 2-3 h, 4+ h), porque
    cada metodo gana en un regimen distinto: nearest sostiene bien el hueco de
    una hora, la recta gana en los cortos y el cubico aporta curvatura en los
    largos. Un peso unico para todos los huecos desperdicia justamente eso.

3.  El valor imputado es la combinacion ponderada de los tres, usando los
    pesos del bucket al que pertenece ese hueco, recortada al rango fisico.

4.  Cada imputacion sale con un puntaje de confianza que castiga los huecos
    largos y el desacuerdo entre los tres metodos.

Huecos mas largos que MAX_GAP_HOURS no se imputan: se declaran irrecuperables
y quedan como MISSING, porque inventarlos seria peor que reconocer que no hay
dato.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

import config

METHODS = ("linear", "cubic", "nearest")
BUCKETS = ("1", "2-3", "4+")
_EPS = 1e-6


# --------------------------------------------------------------------------- #
# Utilidades
# --------------------------------------------------------------------------- #
def bucket_of(gap: int) -> str:
    """Clasifica un hueco por su duracion en horas."""
    if gap <= 1:
        return "1"
    if gap <= 3:
        return "2-3"
    return "4+"


def gap_lengths(mask_missing: np.ndarray) -> np.ndarray:
    """Longitud del hueco al que pertenece cada posicion (0 si hay dato)."""
    out = np.zeros(len(mask_missing), dtype=int)
    start = None
    for i, missing in enumerate(mask_missing):
        if missing and start is None:
            start = i
        elif not missing and start is not None:
            out[start:i] = i - start
            start = None
    if start is not None:
        out[start:] = len(mask_missing) - start
    return out


def _clamp_to_envelope(
    original: pd.Series, filled: pd.Series, nodes: int = 2, margin_ratio: float = 0.25
) -> pd.Series:
    """Encierra cada hueco en la envolvente de sus vecinos observados.

    El spline cubico es el mejor siguiendo curvas suaves y el peor cuando el
    hueco cae entre dos cambios bruscos: se dispara (overshoot) y produce
    negativos o cientos de ug/m3. En vez de descartarlo, lo acotamos al rango
    de los `nodes` datos observados a cada lado del hueco, con un margen del
    25 % para no aplanar la curva: conserva su forma pero no puede inventar
    fisica imposible.
    """
    arr = filled.to_numpy(dtype="float64", copy=True)
    observed = original.notna().to_numpy()
    values = original.to_numpy(dtype="float64")
    obs_idx = np.flatnonzero(observed)
    if obs_idx.size < 2:
        return filled

    i, n = 0, len(arr)
    while i < n:
        if observed[i]:
            i += 1
            continue
        j = i
        while j < n and not observed[j]:
            j += 1
        left = obs_idx[obs_idx < i][-nodes:]
        right = obs_idx[obs_idx >= j][:nodes]
        ref = np.concatenate([values[left], values[right]])
        if ref.size:
            lo, hi = float(np.min(ref)), float(np.max(ref))
            margin = margin_ratio * (hi - lo)
            arr[i:j] = np.clip(arr[i:j], lo - margin, hi + margin)
        i = j

    arr = np.clip(arr, config.PM25_MIN, config.PM25_MAX)
    return pd.Series(arr, index=filled.index)


def interpolate(series: pd.Series, method: str) -> pd.Series:
    """Interpola una serie con el metodo pedido, sin extrapolar."""
    valid = int(series.notna().sum())
    if valid < 2:
        return pd.Series(np.nan, index=series.index, dtype="float64")
    # El spline cubico necesita al menos 4 nodos; si no los hay, degrada a recta.
    if method == "cubic" and valid < 4:
        method = "linear"
    try:
        filled = series.interpolate(method=method, limit_area="inside")
    except Exception:
        filled = series.interpolate(method="linear", limit_area="inside")
    filled = _clamp_to_envelope(series, filled)
    # Donde habia dato observado no se toca nada: solo se acota lo estimado.
    return filled.where(series.isna(), series)


def candidate_frame(series: pd.Series) -> pd.DataFrame:
    """Las tres interpolaciones candidatas, una por columna."""
    return pd.DataFrame({m: interpolate(series, m) for m in METHODS}, index=series.index)


def _equal_weights() -> dict[str, dict[str, float]]:
    return {b: {m: 1 / len(METHODS) for m in METHODS} for b in BUCKETS}


def _stacking_weights(preds: np.ndarray, truth: np.ndarray) -> dict[str, float] | None:
    """Pesos optimos por minimos cuadrados no negativos con suma 1 (stacking).

    Esta es la parte que de verdad "une fuerzas": en vez de repartir el voto a
    ojo, se resuelve

        min ||P w - y||    sujeto a   w >= 0,  sum(w) = 1

    donde P son las predicciones de los tres metodos sobre los datos que se
    ocultaron y `y` su valor real. Como w = (1,0,0) es una solucion factible,
    el optimo nunca puede ser peor que el mejor metodo individual sobre la
    muestra de calibracion; y cuando los errores de los metodos no estan
    correlacionados, la mezcla los cancela y queda por debajo de todos.
    """
    ok = ~np.isnan(preds).any(axis=1) & ~np.isnan(truth)
    if ok.sum() < 3 * len(METHODS):
        return None
    P, y = preds[ok], truth[ok]
    # La restriccion sum(w)=1 se impone como una ecuacion muy ponderada.
    penalty = 10 * max(float(np.abs(y).max()), 1.0)
    P_aug = np.vstack([P, penalty * np.ones((1, P.shape[1]))])
    y_aug = np.concatenate([y, [penalty]])

    # Regularizacion hacia el reparto uniforme: sin ella la solucion tiende a
    # una esquina (todo el peso a un metodo) porque los tres candidatos estan
    # muy correlacionados. El ridge mantiene a los tres en la mezcla y hace
    # que los pesos generalicen mejor fuera de la muestra de calibracion.
    ssr_best = float(min(np.sum((P[:, i] - y) ** 2) for i in range(P.shape[1])))
    lam = config.ENSEMBLE_RIDGE * max(ssr_best, _EPS) / max(len(METHODS), 1)
    root = np.sqrt(lam)
    P_aug = np.vstack([P_aug, root * np.eye(len(METHODS))])
    y_aug = np.concatenate([y_aug, root * np.full(len(METHODS), 1 / len(METHODS))])
    try:
        from scipy.optimize import nnls

        w, _ = nnls(P_aug, y_aug)
    except Exception:
        return None
    total = float(w.sum())
    if not np.isfinite(total) or total <= 0:
        return None
    return {m: float(w[i] / total) for i, m in enumerate(METHODS)}


def _weights_from_errors(mae: dict[str, float]) -> dict[str, float] | None:
    """Peso inverso al error, elevado a WEIGHT_POWER.

    Con exponente 1 los pesos quedan casi uniformes y el peor metodo arrastra
    al ensamble; el exponente premia al que mejor reconstruye ese regimen, y
    WEIGHT_REJECT_RATIO saca del voto al que queda muy por detras del mejor.
    """
    usable = {m: v for m, v in mae.items() if v is not None and not np.isnan(v)}
    if not usable:
        return None
    best = min(usable.values())
    inv = {
        m: (1.0 / (v + _EPS)) ** config.WEIGHT_POWER
        if v <= config.WEIGHT_REJECT_RATIO * best
        else 0.0
        for m, v in usable.items()
    }
    total = sum(inv.values())
    if total <= 0:
        return None
    return {m: inv.get(m, 0.0) / total for m in METHODS}


# --------------------------------------------------------------------------- #
# Calibracion de pesos por tamano de hueco
# --------------------------------------------------------------------------- #
def estimate_weights(
    series: pd.Series,
    rng: np.random.Generator | None = None,
    folds: int = 3,
    block_sizes=(1, 2, 3, 4, 6),
) -> tuple[dict[str, dict[str, float]], dict[str, dict[str, float]]]:
    """Calibra el ensamble. Devuelve (pesos por bucket, MAE por bucket y metodo)."""
    rng = rng or np.random.default_rng(42)
    observed = series.notna().to_numpy()
    empty_mae = {b: {m: float("nan") for m in METHODS} for b in BUCKETS}
    if int(observed.sum()) < 24:  # poca evidencia: todos votan igual
        return _equal_weights(), empty_mae

    obs_idx = np.flatnonzero(observed)[1:-1]  # los extremos no se pueden ocultar
    if len(obs_idx) < 12:
        return _equal_weights(), empty_mae

    # errores[bucket][metodo] -> lista de |error| de cada punto oculto
    errors = {b: {m: [] for m in METHODS} for b in BUCKETS}
    # muestras[bucket] -> (predicciones de los 3 metodos, valor real) para stacking
    samples: dict[str, list[tuple[list[float], float]]] = {b: [] for b in BUCKETS}
    target = max(1, int(len(obs_idx) * 0.10))

    for _ in range(folds):
        probe = series.copy()
        hidden: list[int] = []
        bucket_of_point: list[str] = []
        taken: set[int] = set()
        pool = list(obs_idx)
        rng.shuffle(pool)
        for start in pool:
            if len(hidden) >= target:
                break
            length = int(rng.choice(block_sizes))
            block = list(range(start, min(start + length, len(series) - 1)))
            # Solo se ocultan bloques completamente observados y aun libres.
            if not block or any(p in taken or not observed[p] for p in block):
                continue
            hidden.extend(block)
            bucket_of_point.extend([bucket_of(len(block))] * len(block))
            taken.update(block)
        if len(hidden) < 5:
            continue

        probe.iloc[hidden] = np.nan
        truth = series.iloc[hidden].to_numpy(dtype=float)
        buckets = np.array(bucket_of_point)
        fold_preds = {}
        for m in METHODS:
            pred = interpolate(probe, m).iloc[hidden].to_numpy(dtype=float)
            fold_preds[m] = pred
            ok = ~np.isnan(pred)
            if not ok.any():
                continue
            abs_err = np.abs(pred[ok] - truth[ok])
            for b in BUCKETS:
                sel = buckets[ok] == b
                if sel.any():
                    errors[b][m].extend(abs_err[sel].tolist())

        matrix = np.column_stack([fold_preds[m] for m in METHODS])
        for b in BUCKETS:
            sel = buckets == b
            if sel.any():
                samples[b].extend(zip(matrix[sel].tolist(), truth[sel].tolist()))

    mae = {
        b: {
            m: (float(np.mean(v)) if len(v) >= 5 else float("nan"))
            for m, v in errors[b].items()
        }
        for b in BUCKETS
    }
    # Respaldo global por si algun bucket quedo sin muestras suficientes.
    pooled = {
        m: float(np.mean([e for b in BUCKETS for e in errors[b][m]]))
        if any(errors[b][m] for b in BUCKETS)
        else float("nan")
        for m in METHODS
    }
    pooled_weights = _weights_from_errors(pooled) or {m: 1 / len(METHODS) for m in METHODS}

    weights = {}
    for b in BUCKETS:
        rows = samples[b]
        stacked = None
        if rows:
            P = np.array([r[0] for r in rows], dtype="float64")
            y = np.array([r[1] for r in rows], dtype="float64")
            stacked = _stacking_weights(P, y)
        # Stacking primero; si no hay muestra suficiente, peso inverso al error;
        # y si tampoco, los pesos globales de la serie.
        weights[b] = stacked or _weights_from_errors(mae[b]) or pooled_weights
    return weights, mae


# --------------------------------------------------------------------------- #
# Combinacion
# --------------------------------------------------------------------------- #
def blend(
    candidates: pd.DataFrame,
    gaps: np.ndarray,
    weights: dict[str, dict[str, float]],
) -> pd.Series:
    """Combina los candidatos usando los pesos del bucket de cada hueco.

    Si en un punto algun metodo no pudo estimar, su peso se reparte entre los
    que si pudieron (renormalizacion punto a punto).
    """
    w = np.array(
        [[weights[bucket_of(int(g))][m] for m in METHODS] for g in gaps],
        dtype="float64",
    )
    values = candidates[list(METHODS)].to_numpy(dtype="float64")
    available = ~np.isnan(values)
    w = w * available
    total = w.sum(axis=1)
    mixed = np.where(
        total > 0,
        (np.nan_to_num(values) * w).sum(axis=1) / np.where(total > 0, total, 1.0),
        np.nan,
    )
    return pd.Series(
        np.clip(mixed, config.PM25_MIN, config.PM25_MAX), index=candidates.index
    )


# --------------------------------------------------------------------------- #
# Imputacion
# --------------------------------------------------------------------------- #
def impute_series(series: pd.Series, calibrate: bool = True) -> dict:
    """Imputa una serie horaria con el ensamble Linear + Cubic + Nearest.

    `series` debe venir ya en rejilla horaria completa, con NaN en los huecos.
    """
    series = series.astype("float64")
    missing = series.isna().to_numpy()
    gaps = gap_lengths(missing)

    candidates = candidate_frame(series)
    if calibrate:
        weights, mae = estimate_weights(series)
    else:
        weights = _equal_weights()
        mae = {b: {m: float("nan") for m in METHODS} for b in BUCKETS}

    ensemble = blend(candidates, gaps, weights)

    imputable = missing & (gaps > 0) & (gaps <= config.MAX_GAP_HOURS)
    imputable &= ensemble.notna().to_numpy()

    final = series.copy()
    final[imputable] = ensemble[imputable]

    # Confianza: penaliza huecos largos y desacuerdo entre los tres metodos.
    spread = candidates.std(axis=1, ddof=0)
    observed_values = series.dropna()
    scale = max(float(np.median(np.abs(observed_values))), 1.0) if len(observed_values) else 1.0
    agreement = (1 - (spread / (scale + _EPS))).clip(0, 1)
    gap_penalty = 1 - (pd.Series(gaps, index=series.index) - 1).clip(lower=0) / (
        config.MAX_GAP_HOURS + _EPS
    )
    confidence = (0.6 * agreement + 0.4 * gap_penalty.clip(0, 1)).clip(0, 1)

    return {
        "candidates": candidates,
        "ensemble": ensemble,
        "final": final,
        "is_imputed": pd.Series(imputable, index=series.index),
        "unresolved": pd.Series(missing & ~imputable, index=series.index),
        "gap_length": pd.Series(gaps, index=series.index),
        "confidence": confidence,
        "weights": weights,
        "cv_mae": mae,
    }
