"""Pruebas del pipeline sin tocar la red: se arma una serie sintetica con
todos los defectos que trae el dato real de SIATA (nulos, centinelas, valores
fuera de rango, banderas malas, outliers, huecos) y se verifica que cada regla
los detecte y que la imputacion los reconstruya.

    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
from etl.aqi import classify  # noqa: E402
from etl.geo import haversine_km, idw_estimate, rank_by_distance  # noqa: E402
from etl.imputation import blend, estimate_weights, gap_lengths, impute_series  # noqa: E402
from etl.transform import clean, transform  # noqa: E402
from etl.validation import benchmark_series  # noqa: E402


def synthetic(hours: int = 480, seed: int = 3) -> pd.DataFrame:
    """Serie horaria realista de PM2.5 con ciclo diario, ruido y defectos."""
    rng = np.random.default_rng(seed)
    index = pd.date_range("2024-01-01", periods=hours, freq="h")
    hour = index.hour.to_numpy()
    values = 22 + 9 * np.sin((hour - 6) / 24 * 2 * np.pi) + rng.normal(0, 2.2, hours)
    values = np.clip(values, 1, None)

    df = pd.DataFrame(
        {
            "station_id": 1,
            "station_code": "TEST-01",
            "station_name": "Estacion de prueba",
            "lat": 6.25,
            "lon": -75.57,
            "timestamp": index,
            "pollutant": "pm25",
            "value_raw": values,
            "quality_raw": 1.0,
            "source": "TEST",
        }
    )
    # Defectos inyectados a proposito, en posiciones conocidas:
    df.loc[10, "value_raw"] = np.nan          # nulo de origen
    df.loc[20:22, "value_raw"] = -9999.0      # centinela
    df.loc[40, "value_raw"] = 995.0           # centinela de instrumento
    df.loc[60, "value_raw"] = 812.0           # fuera del rango fisico
    df.loc[80, "quality_raw"] = 4.31          # bandera de calidad mala
    df.loc[100, "value_raw"] = values[100] + 90  # pico aislado (outlier)
    df.loc[200:210, "value_raw"] = np.nan     # hueco largo (irrecuperable)
    return df


class TestLimpieza(unittest.TestCase):
    def setUp(self):
        self.df = clean(synthetic())

    def test_detecta_cada_defecto(self):
        self.assertTrue(self.df.loc[10, "is_missing_source"])
        self.assertTrue(self.df.loc[20:22, "is_sentinel"].all())
        self.assertTrue(self.df.loc[40, "is_sentinel"])
        self.assertTrue(self.df.loc[60, "is_out_of_range"])
        self.assertTrue(self.df.loc[80, "is_bad_quality"])
        self.assertTrue(self.df.loc[100, "is_outlier"])

    def test_value_clean_descarta_lo_invalido(self):
        for pos in (10, 20, 40, 60, 80, 100):
            self.assertTrue(pd.isna(self.df.loc[pos, "value_clean"]),
                            f"la posicion {pos} debio quedar sin valor limpio")

    def test_no_marca_outliers_de_mas(self):
        # Con ciclo diario + ruido normal, el filtro no deberia tumbar mas del 3 %.
        self.assertLess(self.df["is_outlier"].mean(), 0.03)

    def test_value_raw_nunca_se_sobrescribe(self):
        self.assertEqual(self.df.loc[20, "value_raw"], -9999.0)


class TestImputacion(unittest.TestCase):
    def setUp(self):
        self.df, self.report = transform(synthetic())

    def test_rellena_huecos_cortos(self):
        for pos in (10, 21, 40, 60, 80, 100):
            row = self.df.loc[pos]
            self.assertTrue(row["is_imputed"], f"la posicion {pos} debio imputarse")
            self.assertFalse(pd.isna(row["value_final"]))

    def test_no_inventa_huecos_largos(self):
        largo = self.df.loc[200:210]
        self.assertTrue(largo["is_unresolved"].all())
        self.assertTrue(largo["value_final"].isna().all())
        self.assertTrue((largo["quality_flag"] == "MISSING").all())

    def test_valores_imputados_son_fisicamente_posibles(self):
        imputados = self.df.loc[self.df["is_imputed"], "value_final"]
        self.assertTrue((imputados >= config.PM25_MIN).all())
        self.assertTrue((imputados <= config.PM25_MAX).all())

    def test_los_tres_metodos_participan(self):
        pesos = self.report["per_station"][0]["weights"]
        for bucket, w in pesos.items():
            self.assertAlmostEqual(sum(w.values()), 1.0, places=6, msg=bucket)
            self.assertEqual(set(w), {"linear", "cubic", "nearest"})

    def test_reporte_cuadra_con_las_filas(self):
        r = self.report
        self.assertEqual(r["rows"], len(self.df))
        self.assertEqual(r["imputed"], int(self.df["is_imputed"].sum()))
        self.assertGreater(r["availability_final_pct"], r["availability_raw_pct"])


class TestEnsamble(unittest.TestCase):
    def test_gap_lengths(self):
        mask = np.array([False, True, True, False, True, False])
        np.testing.assert_array_equal(gap_lengths(mask), [0, 2, 2, 0, 1, 0])

    def test_pesos_suman_uno_por_bucket(self):
        serie = synthetic()["value_raw"].astype(float)
        serie.index = pd.date_range("2024-01-01", periods=len(serie), freq="h")
        pesos, _ = estimate_weights(serie.mask(serie.abs() > 500))
        for bucket, w in pesos.items():
            self.assertAlmostEqual(sum(w.values()), 1.0, places=6, msg=bucket)
            self.assertTrue(all(v >= 0 for v in w.values()))

    def test_blend_respeta_pesos(self):
        index = pd.date_range("2024-01-01", periods=3, freq="h")
        candidatos = pd.DataFrame(
            {"linear": [10.0, 10.0, 10.0], "cubic": [20.0, 20.0, 20.0],
             "nearest": [30.0, 30.0, 30.0]}, index=index)
        pesos = {b: {"linear": 0.5, "cubic": 0.5, "nearest": 0.0} for b in ("1", "2-3", "4+")}
        mezcla = blend(candidatos, np.array([1, 1, 1]), pesos)
        self.assertTrue(np.allclose(mezcla.to_numpy(), 15.0))

    def test_el_ensamble_no_es_peor_que_el_peor_metodo(self):
        serie = synthetic(720)["value_raw"].astype(float)
        serie.index = pd.date_range("2024-01-01", periods=len(serie), freq="h")
        serie = serie.mask((serie < 0) | (serie > 500))
        filas = benchmark_series(serie, fractions=(0.10,), repeats=2)
        errores = {r["method"]: r["mae"] for r in filas}
        self.assertIn("ensemble", errores)
        self.assertLessEqual(errores["ensemble"], max(
            errores["linear"], errores["cubic"], errores["nearest"]))

    def test_serie_sin_datos_no_revienta(self):
        vacia = pd.Series(
            [np.nan] * 5, index=pd.date_range("2024-01-01", periods=5, freq="h"))
        resultado = impute_series(vacia)
        self.assertFalse(resultado["is_imputed"].any())


class TestSemaforo(unittest.TestCase):
    def test_cortes_de_la_resolucion_2254(self):
        self.assertEqual(classify(8)["label"], "BUENA")
        self.assertEqual(classify(25)["label"], "ACEPTABLE")
        self.assertEqual(classify(45)["label"], "DANINA GRUPOS SENSIBLES")
        self.assertEqual(classify(120)["label"], "DANINA A LA SALUD")
        self.assertEqual(classify(400)["label"], "PELIGROSA")

    def test_bueno_solo_hasta_el_limite_aceptable(self):
        self.assertTrue(classify(12)["good"])
        self.assertTrue(classify(36.9)["good"])
        self.assertFalse(classify(37.5)["good"])

    def test_ica_creciente(self):
        valores = [0, 12, 25, 37, 55, 150, 250]
        indices = [classify(v)["ica"] for v in valores]
        self.assertEqual(indices, sorted(indices))

    def test_sin_dato(self):
        self.assertIsNone(classify(None)["value"])
        self.assertEqual(classify(None)["label"], "SIN DATO")


class TestGeo(unittest.TestCase):
    def test_distancia_conocida(self):
        # Medellin centro -> Itagui, ~10 km en linea recta.
        d = haversine_km(6.2442, -75.5812, 6.1685, -75.5972)
        self.assertTrue(8 < d < 10, d)

    def test_orden_por_cercania(self):
        estaciones = [
            {"station_code": "LEJOS", "lat": 6.45, "lon": -75.33, "pm25_24h": 30},
            {"station_code": "CERCA", "lat": 6.25, "lon": -75.58, "pm25_24h": 10},
        ]
        orden = rank_by_distance(estaciones, 6.2442, -75.5812)
        self.assertEqual(orden[0]["station_code"], "CERCA")

    def test_idw_queda_entre_los_vecinos(self):
        vecinos = [
            {"station_code": "A", "distance_km": 1.0, "pm25_24h": 10},
            {"station_code": "B", "distance_km": 4.0, "pm25_24h": 40},
        ]
        idw = idw_estimate(vecinos, value_key="pm25_24h")
        self.assertTrue(10 <= idw["value"] <= 40)
        # El mas cercano debe pesar mas.
        self.assertGreater(idw["contributors"][0]["weight"], idw["contributors"][1]["weight"])

    def test_sobre_la_estacion_no_mezcla(self):
        vecinos = [
            {"station_code": "A", "distance_km": 0.05, "pm25_24h": 10},
            {"station_code": "B", "distance_km": 4.0, "pm25_24h": 40},
        ]
        idw = idw_estimate(vecinos, value_key="pm25_24h")
        self.assertEqual(idw["value"], 10)
        self.assertEqual(idw["method"], "estacion_exacta")


if __name__ == "__main__":
    unittest.main(verbosity=2)
