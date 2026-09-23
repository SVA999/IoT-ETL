#!/usr/bin/env bash
# Instalacion de NEON AIR en una EC2 Ubuntu 22.04/24.04 (t2.micro alcanza).
#
#   scp -i clave.pem -r siata-dos ubuntu@<ip>:/tmp/neon-air
#   ssh -i clave.pem ubuntu@<ip>
#   sudo bash /tmp/neon-air/deploy/ec2_setup.sh
#
# Security group: abrir 22 (tu IP), 80 y 443. El 8000 no se expone: queda
# detras del proxy.
set -euo pipefail

APP_DIR=/opt/neon-air
SRC_DIR=${1:-/tmp/neon-air}

echo ">> Paquetes base"
apt-get update -y
apt-get install -y python3-venv python3-pip rsync

echo ">> Copiando el proyecto a ${APP_DIR}"
mkdir -p "${APP_DIR}"
rsync -a --exclude '.venv' --exclude '__pycache__' --exclude 'data/*.db' \
      "${SRC_DIR}/" "${APP_DIR}/"
chown -R ubuntu:ubuntu "${APP_DIR}"

echo ">> Entorno virtual y dependencias"
sudo -u ubuntu python3 -m venv "${APP_DIR}/.venv"
sudo -u ubuntu "${APP_DIR}/.venv/bin/pip" install --upgrade pip
sudo -u ubuntu "${APP_DIR}/.venv/bin/pip" install -r "${APP_DIR}/requirements.txt"

echo ">> Primera corrida del ETL (crea el SQLite)"
sudo -u ubuntu "${APP_DIR}/.venv/bin/python" "${APP_DIR}/scripts/run_etl.py" --source auto || \
    echo "!! El ETL inicial fallo; el servicio lo reintentara al arrancar."

echo ">> Servicio systemd"
cp "${APP_DIR}/deploy/neon-air.service" /etc/systemd/system/neon-air.service
systemctl daemon-reload
systemctl enable --now neon-air
systemctl --no-pager status neon-air | head -n 12

echo ">> Refresco periodico del dato en vivo (cada 30 min)"
cat > /etc/cron.d/neon-air <<'CRON'
*/30 * * * * ubuntu cd /opt/neon-air && .venv/bin/python scripts/run_etl.py --source live --quiet >> /var/log/neon-air-etl.log 2>&1
CRON

cat <<'FIN'

  Listo. La app escucha en el puerto 8000 de la instancia.

  Siguiente paso OBLIGATORIO para que funcione el GPS:
  el navegador solo entrega la ubicacion en https. Instala el proxy:

      sudo apt install -y caddy
      sudo nano /etc/caddy/Caddyfile     # pega deploy/Caddyfile con tu dominio
      sudo systemctl restart caddy

  Sin dominio propio puedes probar por tunel:  ssh -L 5000:localhost:8000 ...
  y abrir http://localhost:5000 (localhost tambien es contexto seguro).

FIN
