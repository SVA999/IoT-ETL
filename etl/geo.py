"""Geolocalizacion: distancia a las estaciones e interpolacion espacial.

Ademas de "cual estacion es la mas cercana", se estima la concentracion en el
punto exacto del usuario con IDW (inverse distance weighting) sobre las
estaciones vecinas. El usuario casi nunca esta parado encima de una estacion:
si hay dos a 2 km y 3 km con valores distintos, su exposicion real esta entre
las dos, no en la de una sola.
"""
from __future__ import annotations

import math

EARTH_RADIUS_KM = 6371.0088


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Distancia sobre la superficie terrestre, en kilometros."""
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
    )
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))


def rank_by_distance(stations: list[dict], lat: float, lon: float) -> list[dict]:
    """Copia de las estaciones ordenada por cercania, con distance_km anadido."""
    out = []
    for s in stations:
        if s.get("lat") is None or s.get("lon") is None:
            continue
        item = dict(s)
        item["distance_km"] = round(
            haversine_km(lat, lon, float(s["lat"]), float(s["lon"])), 3
        )
        out.append(item)
    return sorted(out, key=lambda s: s["distance_km"])


def idw_estimate(
    neighbours: list[dict], value_key: str = "value", power: float = 2.0, k: int = 3
) -> dict | None:
    """Interpolacion espacial IDW con las k estaciones mas cercanas.

    El peso cae con el cuadrado de la distancia. Si el usuario esta practicamente
    encima de una estacion (< 100 m), se devuelve ese valor sin mezclar.
    """
    usable = [
        n for n in neighbours
        if n.get(value_key) is not None and n.get("distance_km") is not None
    ][:k]
    if not usable:
        return None

    nearest = usable[0]
    if nearest["distance_km"] < 0.1:
        return {
            "value": round(float(nearest[value_key]), 2),
            "method": "estacion_exacta",
            "contributors": [
                {"station_code": nearest.get("station_code"), "weight": 1.0,
                 "distance_km": nearest["distance_km"]}
            ],
        }

    weights = [1.0 / (max(n["distance_km"], 1e-3) ** power) for n in usable]
    total = sum(weights)
    value = sum(w * float(n[value_key]) for w, n in zip(weights, usable)) / total
    return {
        "value": round(value, 2),
        "method": f"IDW(p={power:g}, k={len(usable)})",
        "contributors": [
            {
                "station_code": n.get("station_code"),
                "station_name": n.get("station_name"),
                "distance_km": n["distance_km"],
                "value": round(float(n[value_key]), 2),
                "weight": round(w / total, 4),
            }
            for w, n in zip(weights, usable)
        ],
    }
