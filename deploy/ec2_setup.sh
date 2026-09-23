#!/usr/bin/env bash
# Instalacion de NEON AIR en una EC2 Ubuntu (t2.micro alcanza).
#
# Desde el directorio del proyecto en la instancia:
#     sudo bash deploy/ec2_setup.sh
#
# O indicando otra carpeta de origen:
#     sudo bash deploy/ec2_setup.sh /ruta/al/proyecto
#
# Security group: abrir 22 (tu IP), 80 y 443. El 8000 no se expone: queda
# detras del proxy TLS.
set -euo pipefail

APP_DIR=/opt/neon-air
# Por defecto se instala el proyecto donde vive este script, no una ruta fija:
# da igual si el codigo llego por scp, por git clone o a /home/ubuntu.
DEFAULT_SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC_DIR="${1:-$DEFAULT_SRC}"
# El usuario que va a correr el servicio: quien invoco el sudo, no un "ubuntu"
# quemado en el script (en Amazon Linux, Debian o una AMI propia no existe).
RUN_USER="${SUDO_USER:-$(id -un)}"

if [[ $EUID -ne 0 ]]; then
    echo "!! Este script necesita root: sudo bash deploy/ec2_setup.sh" >&2
    exit 1
fi

if [[ ! -f "${SRC_DIR}/app.py" ]]; then
    echo "!! No encuentro app.py en ${SRC_DIR}." >&2
    echo "   Pasa la ruta del proyecto: sudo bash deploy/ec2_setup.sh /ruta/al/proyecto" >&2
    exit 1
fi

echo ">> Origen:  ${SRC_DIR}"
echo ">> Destino: ${APP_DIR}  (servicio como usuario '${RUN_USER}')"

echo ">> Paquetes base"
export DEBIAN_FRONTEND=noninteractive
# needrestart interactivo cuelga la instalacion desatendida.
export NEEDRESTART_MODE=a
apt-get update -y
apt-get install -y python3-venv python3-pip rsync

# --- Eleccion del interprete -------------------------------------------------
# pandas, numpy y scipy se instalan como wheels precompilados. Cuando la imagen
# trae un Python mas nuevo que los wheels publicados (p. ej. 3.14), pip intenta
# compilar desde fuente y revienta en una t2.micro. Preferimos un interprete que
# ya tenga wheels y solo caemos al del sistema si no hay otro.
PYTHON=""
for candidate in python3.12 python3.11 python3.13 python3; do
    if command -v "${candidate}" >/dev/null 2>&1; then
        PYTHON="$(command -v "${candidate}")"
        break
    fi
done
PY_VERSION="$(${PYTHON} -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
echo ">> Interprete: ${PYTHON} (Python ${PY_VERSION})"
if [[ "${PYTHON}" == *"python3" && ! "${PYTHON}" =~ python3\.[0-9]+$ ]]; then
    echo "   Si la instalacion de dependencias falla compilando, instala un Python con wheels:"
    echo "     sudo add-apt-repository -y ppa:deadsnakes/ppa && sudo apt install -y python3.12-venv"
fi

echo ">> Copiando el proyecto a ${APP_DIR}"
mkdir -p "${APP_DIR}"
rsync -a --exclude '.venv' --exclude '__pycache__' --exclude '.git' \
      --exclude 'data/*.db' --exclude 'Old' \
      "${SRC_DIR}/" "${APP_DIR}/"
chown -R "${RUN_USER}":"${RUN_USER}" "${APP_DIR}"

echo ">> Entorno virtual y dependencias"
sudo -u "${RUN_USER}" "${PYTHON}" -m venv "${APP_DIR}/.venv"
sudo -u "${RUN_USER}" "${APP_DIR}/.venv/bin/pip" install --upgrade pip wheel
sudo -u "${RUN_USER}" "${APP_DIR}/.venv/bin/pip" install -r "${APP_DIR}/requirements.txt"

echo ">> Primera corrida del ETL (crea el SQLite)"
sudo -u "${RUN_USER}" "${APP_DIR}/.venv/bin/python" "${APP_DIR}/scripts/run_etl.py" --source auto || \
    echo "!! El ETL inicial fallo; el servicio lo reintentara al arrancar."

echo ">> Servicio systemd"
sed "s|^User=.*|User=${RUN_USER}|" "${APP_DIR}/deploy/neon-air.service" \
    > /etc/systemd/system/neon-air.service
systemctl daemon-reload
systemctl enable --now neon-air
sleep 2
systemctl --no-pager status neon-air | head -n 12 || true

echo ">> Refresco periodico del dato en vivo (cada 30 min)"
cat > /etc/cron.d/neon-air <<CRON
*/30 * * * * ${RUN_USER} cd ${APP_DIR} && .venv/bin/python scripts/run_etl.py --source live --quiet >> /var/log/neon-air-etl.log 2>&1
CRON

cat <<'FIN'

  Listo. La app escucha en el puerto 8000 de la instancia.
  Comprobacion rapida:   curl -s localhost:8000/api/health

  Siguiente paso OBLIGATORIO para que funcione el GPS:
  el navegador solo entrega la ubicacion en https. Instala el proxy:

      sudo apt install -y debian-keyring debian-archive-keyring apt-transport-https curl
      curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
        | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
      curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
        | sudo tee /etc/apt/sources.list.d/caddy-stable.list
      sudo apt update && sudo apt install -y caddy
      sudo cp /opt/neon-air/deploy/Caddyfile /etc/caddy/Caddyfile   # cambia el dominio
      sudo systemctl restart caddy

  Sin dominio propio puedes probar por tunel desde tu maquina:
      ssh -i clave.pem -L 5000:localhost:8000 ubuntu@<ip>
  y abrir http://localhost:5000 (localhost tambien es contexto seguro).

FIN
