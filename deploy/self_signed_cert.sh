#!/usr/bin/env bash
# Genera un certificado autofirmado PARA UNA IP y activa TLS en gunicorn.
#
#     sudo bash deploy/self_signed_cert.sh 3.84.130.65
#
# Sirve para que el navegador considere la pagina "contexto seguro" y entregue
# el GPS, sin necesidad de dominio. Tiene dos limitaciones que conviene conocer
# antes de usarlo (ver el mensaje final del script).
set -euo pipefail

IP="${1:-}"
if [[ -z "${IP}" ]]; then
    echo "Uso: sudo bash deploy/self_signed_cert.sh <ip-publica>" >&2
    exit 1
fi

APP_DIR=/opt/neon-air
CERT_DIR="${APP_DIR}/certs"
RUN_USER="${SUDO_USER:-ubuntu}"

mkdir -p "${CERT_DIR}"

# subjectAltName es lo que de verdad importa: desde hace anos los navegadores
# ignoran el CN y validan solo el SAN. Un certificado sin "IP:<ip>" aqui se
# rechaza pase lo que pase. 364 dias porque Safari/iOS descarta los que duran
# mas de 398.
openssl req -x509 -nodes -days 364 -newkey rsa:2048 \
    -keyout "${CERT_DIR}/neon-air.key" \
    -out "${CERT_DIR}/neon-air.crt" \
    -subj "/CN=${IP}" \
    -addext "subjectAltName=IP:${IP}" \
    -addext "basicConstraints=CA:FALSE" \
    -addext "keyUsage=digitalSignature,keyEncipherment" \
    -addext "extendedKeyUsage=serverAuth"

# La llave vive junto a la app y la lee el usuario del servicio: en
# /etc/ssl/private solo puede leerla root, y gunicorn no corre como root.
chown -R "${RUN_USER}":"${RUN_USER}" "${CERT_DIR}"
chmod 600 "${CERT_DIR}/neon-air.key"
chmod 644 "${CERT_DIR}/neon-air.crt"

echo ">> Certificado generado en ${CERT_DIR}"
openssl x509 -in "${CERT_DIR}/neon-air.crt" -noout -subject -ext subjectAltName -dates

# Activamos TLS y el puerto 443 en la unidad de systemd.
UNIT=/etc/systemd/system/neon-air.service
sed -i \
    -e 's|^Environment="BIND=.*|Environment="BIND=0.0.0.0:443"|' \
    -e "s|^Environment=\"TLS_ARGS=.*|Environment=\"TLS_ARGS=--certfile ${CERT_DIR}/neon-air.crt --keyfile ${CERT_DIR}/neon-air.key\"|" \
    -e 's|^#AmbientCapabilities=|AmbientCapabilities=|' \
    "${UNIT}"
systemctl daemon-reload
systemctl restart neon-air
sleep 2
systemctl --no-pager status neon-air | head -n 12 || true

cat <<FIN

  Abre en el celular:  https://${IP}

  Dos cosas que vas a encontrarte, porque el certificado no lo firma una
  autoridad conocida:

  1. El navegador muestra una advertencia. En Chrome Android:
     "Configuracion avanzada" -> "Acceder a ${IP} (no seguro)".
     Despues de aceptarla el GPS SI funciona: para el navegador la pagina
     ya es contexto seguro. En iPhone hay que instalar el .crt como perfil
     (Ajustes -> General -> VPN y gestion de dispositivos) y ademas activarlo
     en Ajustes -> General -> Informacion -> Certificados de confianza.

  2. El service worker probablemente NO se registre (Chrome lo bloquea en
     origenes con error de certificado). La app funciona igual; lo que se
     pierde es la instalacion como PWA y el modo sin conexion.

  Si quieres las dos cosas sin advertencias y sin comprar dominio, usa
  ${IP}.sslip.io con Caddy: es un certificado real de Let's Encrypt.

  Recuerda abrir el puerto 443 en el security group.
FIN
