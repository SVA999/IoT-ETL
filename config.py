"""Configuracion central del proyecto NEON-AIR / SIATA ETL."""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("SIATA_DATA_DIR", BASE_DIR / "data"))
CACHE_DIR = DATA_DIR / "cache"
DATA_DIR.mkdir(parents=True, exist_ok=True)
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# --- Fuentes -----------------------------------------------------------------
# Archivo historico entregado en clase (fallback cuando SIATA no responde).
FALLBACK_FILE = Path(
    os.getenv("SIATA_FALLBACK_FILE", BASE_DIR / "Datos_SIATA_Aire_pm25.json")
)

# Endpoints publicos verificados del Geoportal SIATA (FastAPI interno "fastgeoapi").
GEOAPI_BASE = "https://geoportal.siata.gov.co/fastgeoapi/geodata"
URL_LIVE_SNAPSHOT = f"{GEOAPI_BASE}/geodataJson/1/pm25_minio"      # GeoJSON: PM2.5 24h + ICA por estacion
URL_LIVE_72H = f"{GEOAPI_BASE}/geographJson/1/pm25/{{codigo}}"      # Serie horaria de 72 h
URL_LIVE_30D = f"{GEOAPI_BASE}/geographJson/1/pm25_30d/{{codigo}}"  # Serie diaria de 30 d
# Catalogo oficial de estaciones (CSV publico).
URL_CATALOGO = "https://siata.gov.co/CalidadAire/cadatos/Estaciones.txt"
# Tablero publico de ultimo dato recibido por estacion.
URL_VERIFICACION = (
    "https://siata.gov.co/CalidadAire/verificacionEstacionesRedAireValidadoSIATA.php"
)

HTTP_TIMEOUT = float(os.getenv("SIATA_HTTP_TIMEOUT", 15))
USER_AGENT = "NEON-AIR-PWA/1.0 (practica academica UPB; ETL IoT)"

# --- Almacenamiento ----------------------------------------------------------
DB_PATH = Path(os.getenv("SIATA_DB_PATH", DATA_DIR / "siata_warehouse.db"))

# --- Reglas de calidad de datos ---------------------------------------------
# Valores centinela que SIATA usa para "sin dato" / fallo de instrumento.
SENTINELS = (-9999.0, 9999.0, 99999.0, 985.0, 995.0, 999.0)
# Rango fisicamente plausible para PM2.5 en ug/m3.
PM25_MIN, PM25_MAX = 0.0, 500.0
# SIATA marca el dato como OK cuando su bandera de calidad es < 2.6.
QUALITY_FLAG_OK = 2.6
# Umbral del Hampel/MAD para marcar outliers locales.
HAMPEL_WINDOW = 11
HAMPEL_SIGMAS = 3.5
# Ademas del criterio estadistico exigimos una desviacion minima en ug/m3:
# en series muy planas el MAD se vuelve diminuto y marcaria picos legitimos.
HAMPEL_MIN_ABS = 5.0
# Ensamble de imputacion: exponente del peso inverso al error y umbral para
# expulsar del voto a un metodo que en esa serie es mucho peor que el mejor.
WEIGHT_POWER = 3.0
WEIGHT_REJECT_RATIO = 1.6
# Regularizacion del stacking hacia el reparto uniforme (0 = stacking puro).
ENSEMBLE_RIDGE = 0.15
# Huecos mas largos que esto no se imputan (se declaran irrecuperables).
MAX_GAP_HOURS = int(os.getenv("SIATA_MAX_GAP_HOURS", 6))

# --- Frescura de la cache en vivo -------------------------------------------
LIVE_TTL_SECONDS = int(os.getenv("SIATA_LIVE_TTL", 900))  # 15 min

# --- App ---------------------------------------------------------------------
APP_NAME = "NEON AIR"
APP_VERSION = "1.0.0"
PORT = int(os.getenv("PORT", 5000))
