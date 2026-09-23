"""Semaforo de contaminacion (ICA) para PM2.5.

Se usan los puntos de corte de la Resolucion 2254 de 2017 del Ministerio de
Ambiente de Colombia, que es la norma que aplica al Valle de Aburra y la misma
escala que reporta SIATA en su Geoportal. El indice se interpola linealmente
dentro de cada categoria, igual que hace la autoridad ambiental.
"""
from __future__ import annotations

# (conc_min, conc_max, ica_min, ica_max, etiqueta, color, consejo)
BREAKPOINTS = [
    (0.0, 12.0, 0, 50, "BUENA", "#00f0a8",
     "Aire limpio. Actividad al aire libre sin restricciones."),
    (12.1, 37.0, 51, 100, "ACEPTABLE", "#fcee0a",
     "Aceptable para la mayoria. Personas muy sensibles: modera esfuerzos largos."),
    (37.1, 55.0, 101, 150, "DANINA GRUPOS SENSIBLES", "#ff9f1c",
     "Ninos, adultos mayores y personas con asma o EPOC deben reducir actividad al aire libre."),
    (55.1, 150.0, 151, 200, "DANINA A LA SALUD", "#ff003c",
     "Evita ejercicio al aire libre. Usa tapabocas si debes salir."),
    (150.1, 250.0, 201, 300, "MUY DANINA", "#b026ff",
     "Permanece en interiores con ventanas cerradas. Riesgo para toda la poblacion."),
    (250.1, 500.0, 301, 500, "PELIGROSA", "#7e0023",
     "Emergencia sanitaria. No salgas salvo que sea indispensable."),
]

SAFE_MAX = 37.0  # por encima de esto el semaforo deja de ser "aire sano"


def classify(value: float | None) -> dict:
    """Traduce una concentracion de PM2.5 (ug/m3) al semaforo ICA."""
    if value is None:
        return {
            "value": None,
            "ica": None,
            "label": "SIN DATO",
            "level": -1,
            "color": "#6b7280",
            "good": None,
            "advice": "No hay dato utilizable para esta zona en la ventana consultada.",
        }

    v = max(0.0, float(value))
    for level, (c_lo, c_hi, i_lo, i_hi, label, color, advice) in enumerate(BREAKPOINTS):
        if v <= c_hi or level == len(BREAKPOINTS) - 1:
            lo = 0.0 if level == 0 else c_lo
            span = max(c_hi - lo, 1e-9)
            ica = i_lo + (min(v, c_hi) - lo) * (i_hi - i_lo) / span
            return {
                "value": round(v, 2),
                "ica": int(round(ica)),
                "label": label,
                "level": level,
                "color": color,
                "good": v <= SAFE_MAX,
                "advice": advice,
            }
    raise AssertionError("inalcanzable")


def scale() -> list[dict]:
    """La escala completa, para dibujar la leyenda en la interfaz."""
    return [
        {
            "label": label,
            "color": color,
            "pm25_min": c_lo,
            "pm25_max": c_hi,
            "ica_min": i_lo,
            "ica_max": i_hi,
            "advice": advice,
        }
        for c_lo, c_hi, i_lo, i_hi, label, color, advice in BREAKPOINTS
    ]
