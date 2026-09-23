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
> `navigator.geolocation` sobre `https://` o `localhost`. En una EC2 con IP pública y HTTP
> plano el botón no funciona: hay que servir por HTTPS (ver [8.4](#84-https-con-sslipio--el-paso-que-hace-funcionar-el-gps)).
> Mientras tanto, la app ofrece entrada manual de coordenadas.

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

## 8. Despliegue en EC2 (paso a paso)

Probado de punta a punta en una `t2.micro` con Ubuntu. El resultado final es la app
servida por HTTPS en `https://<ip>.sslip.io`, con el GPS funcionando en el celular y la
PWA instalable.

### 8.1 La instancia

- Ubuntu, `t2.micro` o `t3.micro` (1 GB de RAM alcanza).
- **Security group** → Inbound rules:

  | Puerto | Origen | Para qué |
  |---|---|---|
  | 22 | tu IP | SSH |
  | 80 | 0.0.0.0/0 | validación de Let's Encrypt y redirección a HTTPS |
  | 443 | 0.0.0.0/0 | la app |

  El **8000 no se abre**: gunicorn queda interno, detrás del proxy.

- Conviene una **IP elástica**. Si la IP cambia, cambia el nombre `sslip.io` y hay que
  reemitir el certificado.

### 8.2 Subir el proyecto

```bash
git clone <tu-repo> ~/IoT-ETL          # o: scp -i clave.pem -r siata-dos ubuntu@<ip>:~/IoT-ETL
cd ~/IoT-ETL
```

### 8.3 Instalar

```bash
sudo bash deploy/ec2_setup.sh
```

El script se instala **desde la carpeta donde está parado** (no desde una ruta fija),
detecta el usuario que invocó `sudo`, elige un intérprete que tenga wheels de numpy/scipy,
crea el venv en `/opt/neon-air/.venv`, corre el ETL inicial, registra el servicio systemd
y deja un cron que refresca el dato en vivo cada 30 min.

Comprobación antes de seguir:

```bash
curl -s localhost:8000/api/health
```

Si eso devuelve el JSON, el backend está listo. Si no:
`sudo journalctl -u neon-air -n 40 --no-pager`.

### 8.4 HTTPS con sslip.io — el paso que hace funcionar el GPS

El navegador sólo entrega `navigator.geolocation` en **contexto seguro**: `https://` o
`localhost`. Y no existe autoridad que emita certificados para una IP pelada, así que
hace falta un nombre.

`sslip.io` es un DNS público que resuelve cualquier IP embebida en el nombre:
`3.84.130.65.sslip.io` → `3.84.130.65`. Como es un nombre real, Let's Encrypt **sí** emite
certificado para él, y Caddy lo saca y lo renueva solo.

Instalar Caddy desde su repositorio oficial (en Ubuntu reciente no está en los repos por
defecto, `apt install caddy` a secas falla):

```bash
sudo apt install -y debian-keyring debian-archive-keyring apt-transport-https curl
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt update && sudo apt install -y caddy
```

Configurar el proxy (reemplaza la IP por la tuya):

```bash
printf '3.84.130.65.sslip.io {\n  encode gzip\n  reverse_proxy localhost:8000\n}\n' | sudo tee /etc/caddy/Caddyfile
sudo systemctl restart caddy
```

Verificar que el certificado salió:

```bash
curl -sI https://3.84.130.65.sslip.io | head -1
sudo journalctl -u caddy -n 20 --no-pager
```

Listo: abres `https://3.84.130.65.sslip.io` en el celular, el botón de GPS responde y
desde el menú de Chrome puedes usar *Añadir a pantalla de inicio* para instalarla como
PWA.

`deploy/Caddyfile` trae la versión completa (cabeceras de seguridad y `no-cache` para el
service worker) por si quieres usar un dominio propio en vez de `sslip.io`.

### 8.5 Alternativa sin Caddy: certificado autofirmado

```bash
sudo bash deploy/self_signed_cert.sh 3.84.130.65
```

Genera el par con `subjectAltName=IP:...` —sin esa extensión los navegadores lo rechazan,
porque hace años ignoran el CN— lo deja legible por el usuario del servicio y activa TLS
directamente en gunicorn (`--certfile`/`--keyfile`), sin proxy.

Sirve para el GPS **después de aceptar la advertencia** del navegador, pero Chrome bloquea
el registro del service worker en orígenes con certificado inválido: se pierde la
instalación como PWA y el modo sin conexión. Por eso `sslip.io` es la opción preferida.

### 8.6 Operación

```bash
sudo systemctl status neon-air          # estado
sudo journalctl -u neon-air -f          # logs en vivo
sudo systemctl restart neon-air         # reiniciar
```

Para actualizar después de cambiar código:

```bash
cd ~/IoT-ETL && git pull
sudo bash deploy/ec2_setup.sh && sudo systemctl restart neon-air
```

En el navegador entra con **Ctrl+Shift+R**: el service worker cachea el frontend y si no
forzas la recarga puedes quedarte viendo la versión anterior.

El servicio corre con **un solo worker de gunicorn y 8 hilos**, a propósito: cada worker
es un proceso independiente que correría su propio ETL y cargaría su propia copia del
histórico en pandas, y en 1 GB de RAM eso termina con el OOM killer matando un worker a
mitad de una respuesta.

> `POST /api/etl/run` queda accesible desde internet: cualquiera que encuentre la URL puede
> disparar el pipeline completo. Para una app de práctica es aceptable; si te preocupa,
> restringe el origen en el security group mientras pruebas.

### 8.7 Problemas que ya aparecieron (y su causa real)

| Síntoma | Causa | Solución |
|---|---|---|
| `rsync: change_dir "/tmp/neon-air" failed` | El script tenía una ruta de origen fija | Usa la versión actual: se instala desde su propia carpeta, o pásale la ruta como argumento |
| `sudo: command not found` justo después de un `apt` | Bash cacheó la ruta vieja del binario | `hash -r`, o abre una sesión SSH nueva |
| `pip` compilando numpy/scipy durante minutos y fallando | La AMI trae un Python más nuevo que los wheels publicados (p. ej. 3.14) | `requirements.txt` usa rangos y el script prefiere `python3.12`/`3.11` si existen |
| `http://<ip>:5000` no carga | El 5000 es sólo del modo desarrollo (`python app.py`) | El servicio usa el 8000, y de cara a internet el 443 |
| `https://<ip>:8000` no carga | Ese puerto habla HTTP plano, no TLS | Entra por `https://<ip>.sslip.io` |
| `Cannot read properties of null` en la consola de la app | Respuesta 200 con cuerpo ilegible: JSON con `NaN` o truncado por el OOM killer | Serializador que convierte no-finitos a `null` + un solo worker. El helper del frontend ahora reporta el error real |
| El botón de GPS no responde | Contexto no seguro (HTTP plano) | HTTPS (8.4). Mientras tanto, la app ofrece coordenadas manuales |
| Los cambios no se ven en el navegador | Service worker sirviendo la copia cacheada | Ctrl+Shift+R |

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
deploy/
  ec2_setup.sh          Instalación completa en la instancia
  neon-air.service      Unidad systemd (gunicorn, 1 worker, TLS opcional)
  Caddyfile             Proxy HTTPS para dominio propio
  self_signed_cert.sh   Certificado autofirmado sobre la IP (alternativa a Caddy)
```

Ajustes por variables de entorno: `SIATA_DB_PATH`, `SIATA_FALLBACK_FILE`,
`SIATA_MAX_GAP_HOURS`, `SIATA_LIVE_TTL`, `SIATA_HTTP_TIMEOUT` y `PORT` (sólo en modo
desarrollo; en la EC2 el puerto se define con `BIND` en la unidad de systemd).

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
