# NEON AIR · Calidad del aire del Valle de Aburrá

PWA con estética *cyberpunk 2077* que responde una sola pregunta desde el celular:
**¿el aire donde estoy parado está bueno o malo?**

Detrás del botón hay un pipeline ETL completo sobre datos públicos de **SIATA**:
ingesta, limpieza, control de calidad, detección de outliers, imputación de faltantes
por ensamble de interpoladores y validación experimental de la imputación.

Práctica académica de ETL + IoT · UPB · 7.º semestre.

---

## 1. Qué hace

| | |
|---|---|
| **Botón GPS** | Lee la posición del dispositivo y devuelve el semáforo de contaminación en ese punto exacto. |
| **Tiempo real** | Consume los endpoints públicos del Geoportal SIATA (23 estaciones, PM2.5 horario de las últimas 72 h). |
| **Respaldo** | Si SIATA no responde, cae automáticamente al archivo histórico `Datos_SIATA_Aire_pm25.json` (21 estaciones, 1 año, 183 981 registros). |
| **ETL** | Nulos, centinelas, fuera de rango, banderas de calidad, outliers y huecos: cada defecto se detecta, se marca y se reporta. |
| **Imputación** | Ensamble ponderado de interpolación **Lineal + Cúbica + Nearest**, calibrado por validación interna. |
| **Validación** | Experimento que oculta 5/10/20/30 % de datos reales y mide MAE, RMSE, MAPE, R² y % de valores imposibles. |
| **PWA** | Instalable, responsiva y con service worker: abre sin red y muestra la última lectura cacheada. |

---

## 2. Arranque rápido

```bash
pip install -r requirements.txt
python app.py
```

Abre `http://localhost:5000`. El pipeline se calienta solo en segundo plano al arrancar.

ETL desde la terminal:

```bash
python scripts/run_etl.py --source auto --benchmark
```

Pruebas:

```bash
python -m unittest discover -s tests -v
```

> **El GPS necesita contexto seguro.** Los navegadores solo entregan
> `navigator.geolocation` sobre `https://` o `localhost`. En una EC2 con IP pública
> y HTTP plano el botón no va a funcionar: hay que poner el proxy con TLS
> (`deploy/Caddyfile`). Mientras tanto, la app ofrece entrada manual de coordenadas.

---

## 3. Fuentes de datos de SIATA

Endpoints públicos verificados (descubiertos inspeccionando el bundle del Geoportal):

| Recurso | URL | Contenido |
|---|---|---|
| PM2.5 por estación | `geoportal.siata.gov.co/fastgeoapi/geodata/geodataJson/1/pm25_minio` | GeoJSON: promedio 24 h, ICA, color, coordenadas |
| Serie horaria 72 h | `.../geodata/geographJson/1/pm25/{codigo}` | 72 valores horarios, con `null` en los huecos reales |
| Serie diaria 30 d | `.../geodata/geographJson/1/pm25_30d/{codigo}` | Promedios diarios del último mes |
| Catálogo oficial | `siata.gov.co/CalidadAire/cadatos/Estaciones.txt` | CSV con código, nombre, municipio, lat/lon y qué mide cada estación |
| Último dato recibido | `siata.gov.co/CalidadAire/verificacionEstacionesRedAireValidadoSIATA.php` | Tablero de disponibilidad por contaminante |

El `restfullcc` que aparece en `Old/siatadocs.txt` corresponde a Ciudadanos Científicos
y **no** entrega las mediciones actuales de la red de calidad del aire; por eso no se usa.

El archivo local sirve de respaldo y, además, es el sustrato del experimento de
validación: un año completo de datos horarios da estadística que 72 h no dan.

---

## 4. El pipeline

```
SIATA en vivo (fastgeoapi)  ─┐
                             ├─> EXTRACT ─> TRANSFORM ─> LOAD ─> API Flask ─> PWA
Datos_SIATA_Aire_pm25.json ─┘   (crudo)    (limpieza +    (SQLite)            │
                                            imputación)                       GPS
                                                                              │
                                                                       🟢 🟡 🟠 🔴
```

### 4.1 Reglas de limpieza (`etl/transform.py`)

| # | Regla | Bandera | Qué encuentra en el dato real |
|---|---|---|---|
| 1 | Timestamp o estación inválidos | *fila descartada* | Registros irreparables |
| 2 | Duplicados (estación, hora) | *se conserva el último* | Reenvíos de la telemetría |
| 3 | Nulo de origen | `is_missing_source` | `null` explícito del endpoint |
| 4 | Valores centinela | `is_sentinel` | `-9999`, `99999`, `985`, `995` (1 959 en el histórico) |
| 5 | Rango físico 0–500 µg/m³ | `is_out_of_range` | Negativos y picos absurdos (3 271) |
| 6 | Bandera de calidad SIATA ≥ 2.6 | `is_bad_quality` | El propio SIATA marca el dato como no OK (5 006) |
| 7 | Outlier local (Hampel/MAD) | `is_outlier` | Picos aislados incoherentes con su vecindad (6 044) |
| 8 | Rejilla horaria completa | `is_gap` | Horas que la fuente ni siquiera reporta |
| 9 | Imputación | `is_imputed` | 9 011 valores reconstruidos |

**Nada se sobrescribe.** `value_raw` se conserva siempre; `value_clean` es lo observado
y válido; `value_final` es la serie utilizable. Un dato imputado nunca se confunde con
uno medido: trae `is_imputed`, `imputation_method` y un puntaje de `confidence`.

El filtro de Hampel exige además una desviación mínima absoluta de 5 µg/m³: en series
muy planas el MAD se vuelve diminuto y marcaría como outlier cualquier pico legítimo.

Resultado sobre el histórico completo: disponibilidad **93.99 % → 98.89 %**.

### 4.2 La imputación: tres métodos trabajando juntos

La consigna era usar lineal, cúbica y nearest *al mismo tiempo*. Un promedio simple de
los tres es peor que el mejor de los tres, así que el ensamble se construye en cuatro pasos
(`etl/imputation.py`):

1. **Candidatos acotados.** Se interpola con los tres métodos, sin extrapolar nunca fuera
   del rango observado, y cada candidato se encierra en la *envolvente* de los datos
   vecinos al hueco (± 25 %). Eso es lo que domestica al spline cúbico: sin esa cota
   producía **4–8 % de valores físicamente imposibles**; con ella, **0 %**.

2. **Calibración por validación interna.** Se ocultan artificialmente bloques de datos
   realmente observados y se mide cuánto se equivoca cada método al reconstruirlos.

3. **Pesos por tamaño de hueco.** La calibración se hace separada para huecos de 1 h,
   2–3 h y 4+ h, porque cada método gana en un régimen distinto. Los pesos salen de un
   **stacking**: mínimos cuadrados no negativos con suma 1,
   `min ‖P·w − y‖ s.a. w ≥ 0, Σw = 1`, regularizado hacia el reparto uniforme.
   Como `w = (1,0,0)` es solución factible, el óptimo **no puede ser peor que el mejor
   método individual** sobre la muestra de calibración.

4. **Límite de honestidad.** Huecos de más de 6 h (`MAX_GAP_HOURS`) **no se imputan**:
   se marcan `MISSING`. Inventar seis horas de aire es peor que admitir que no hay dato.

### 4.3 Qué dijo el experimento

`GET /api/imputation/benchmark` — ocultando 5/10/20/30 % en bloques de 1 a 6 h sobre
4 estaciones del histórico (n = 42 949 puntos ocultos):

| Método | MAE | RMSE | MAPE % | R² | Imposibles % |
|---|---|---|---|---|---|
| **linear** | **5.405** | **7.277** | 35.45 | **0.641** | 0.0 |
| **ensemble** | 5.419 | 7.309 | **35.35** | 0.638 | 0.0 |
| nearest | 6.200 | 8.479 | 38.81 | 0.513 | 0.0 |
| cubic | 6.867 | 9.248 | 42.83 | 0.419 | 0.0 |

Lectura honesta del resultado: en PM2.5 horario del Valle de Aburrá **la interpolación
lineal domina**, y el stacking lo detecta solo — le asigna la mayor parte del peso en los
tres regímenes de hueco. El ensamble queda empatado con ella (diferencia de 0.2 % en MAE,
y mejor MAPE) y muy por encima de nearest y cúbica. Es decir: el ensamble no regala
precisión y sí aporta robustez, porque en una estación cuyo comportamiento cambie, los
pesos se recalculan sin tocar código. Los números no están escritos a mano: se recalculan
cada vez que se corre el experimento.

---

## 5. Semáforo

Escala ICA de PM2.5 de la **Resolución 2254 de 2017** (Colombia), sobre el promedio móvil
de 24 h, que es la base legal del índice:

| PM2.5 µg/m³ | ICA | Categoría |
|---|---|---|
| 0 – 12 | 0 – 50 | Buena |
| 12.1 – 37 | 51 – 100 | Aceptable |
| 37.1 – 55 | 101 – 150 | Dañina para grupos sensibles |
| 55.1 – 150 | 151 – 200 | Dañina a la salud |
| 150.1 – 250 | 201 – 300 | Muy dañina |
| 250.1 – 500 | 301 – 500 | Peligrosa |

El valor en la posición del usuario no es simplemente el de la estación más cercana: se
estima con **IDW** (inverse distance weighting, p = 2) sobre las 3 estaciones vecinas,
porque nadie está parado justo encima de una estación. Si estás a menos de 100 m de una,
se usa esa y ya.

---

## 6. API

| Método | Endpoint | Para qué |
|---|---|---|
| GET | `/api/health` | Estado del servicio, fuente vigente y almacén |
| GET | `/api/scale` | Escala ICA completa |
| GET | `/api/stations` | Todas las estaciones con su última lectura y semáforo |
| GET | `/api/stations/{id}` | Detalle de una estación |
| GET | `/api/stations/{id}/history?limit=` | Serie con las 3 interpolaciones y el ensamble |
| GET | `/api/stations/nearest?lat=&lon=` | Estación más cercana |
| GET | `/api/air-quality/nearest?lat=&lon=&k=` | **El botón**: semáforo en tu posición |
| GET | `/api/quality` | Reporte global de calidad del dato + reglas aplicadas |
| GET | `/api/quality/{id}` | Calidad del dato de una estación |
| GET | `/api/imputation/{id}` | Pesos, errores de calibración y muestras imputadas |
| GET | `/api/imputation/benchmark` | Experimento Linear vs Cubic vs Nearest vs Ensamble |
| POST | `/api/etl/run` | Dispara el pipeline (`{"source": "auto\|live\|file"}`) |
| GET | `/api/etl/runs` | Historial de corridas |
| GET | `/api/siata/catalog` | Catálogo oficial de estaciones en vivo |

Ejemplo:

```bash
curl "http://localhost:5000/api/air-quality/nearest?lat=6.2442&lon=-75.5812"
```

---

## 7. Almacén (SQLite)

`data/siata_warehouse.db`, cuatro tablas:

- `measurements` — el dato con toda su trazabilidad (crudo, limpio, las tres
  interpolaciones, el ensamble, el final y cada bandera).
- `stations` — catálogo con coordenadas.
- `etl_runs` — bitácora de corridas con su reporte de calidad en JSON.
- `imputation_benchmark` — resultados del experimento por corrida.

SQLite a propósito: mantiene el proyecto desplegable en una `t2.micro` sin servicios extra.
El esquema está listo para migrar a PostGIS si el proyecto crece.

---

## 8. Despliegue en EC2

```bash
scp -i clave.pem -r siata-dos ubuntu@<ip>:/tmp/neon-air
ssh -i clave.pem ubuntu@<ip>
sudo bash /tmp/neon-air/deploy/ec2_setup.sh
```

El script instala dependencias, crea el venv, corre el ETL inicial, registra el servicio
systemd (gunicorn, 2 workers) y deja un cron que refresca el dato en vivo cada 30 min.

Después, **HTTPS obligatorio** para que funcionen el GPS y la instalación de la PWA:

```bash
sudo apt install -y caddy
sudo cp deploy/Caddyfile /etc/caddy/Caddyfile   # cambia el dominio
sudo systemctl restart caddy
```

Security group: 22 (solo tu IP), 80 y 443. El 8000 queda interno.
Sin dominio, para probar: `ssh -L 5000:localhost:8000 ...` y abrir `http://localhost:5000`.

---

## 9. Estructura

```
app.py                  Servidor Flask (PWA + API)
config.py               Umbrales, endpoints y reglas en un solo lugar
etl/
  extract.py            Ingesta: SIATA en vivo y archivo local
  transform.py          Limpieza, banderas y control de calidad
  imputation.py         Ensamble Linear + Cubic + Nearest (stacking por hueco)
  validation.py         Experimento de ocultamiento y métricas
  load.py               Almacén SQLite
  pipeline.py           Orquestador + capa de consulta
  aqi.py                Semáforo ICA (Res. 2254/2017)
  geo.py                Haversine + IDW
api/routes.py           Endpoints REST
static/                 PWA: CSS, JS, service worker, manifest, iconos
templates/index.html    Interfaz
scripts/run_etl.py      CLI del pipeline (cron-able)
tests/test_etl.py       22 pruebas sobre una serie sintética con defectos inyectados
deploy/                 systemd, Caddy y script de instalación en EC2
```

Ajustes por variables de entorno: `PORT`, `SIATA_DB_PATH`, `SIATA_FALLBACK_FILE`,
`SIATA_MAX_GAP_HOURS`, `SIATA_LIVE_TTL`, `SIATA_HTTP_TIMEOUT`.

---

## 10. Alcance y límites

- Este proyecto **no corrige** los datos de SIATA. Es un sistema independiente de
  evaluación, limpieza e imputación para consumo de información pública, como sugiere la
  propia investigación previa (`Old/siatadocs.txt`).
- El semáforo es orientativo. La autoridad ambiental del Valle de Aburrá es el AMVA y la
  fuente oficial es SIATA.
- Solo PM2.5. Los endpoints del Geoportal exponen otras capas (black carbon, ruido) que
  el pipeline podría absorber sin cambios estructurales.
- El archivo histórico cubre 2019-02 a 2020-02: sirve de respaldo y de banco de pruebas,
  no como lectura actual del aire.
