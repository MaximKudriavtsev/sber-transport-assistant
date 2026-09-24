#!/usr/bin/env bash
set -euo pipefail

if [[ "$(uname -s)" != "Linux" ]]; then
  echo "install.sh is for a Linux server." >&2
  exit 1
fi

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run as root: sudo bash deploy/install.sh" >&2
  exit 1
fi

APP_USER="${APP_USER:-root}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
UNIT_NAME="sber-transport.service"

if [[ -x /usr/bin/python3.12 ]]; then
  PY="/usr/bin/python3.12"
elif python3 -c 'import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 12) else 1)'; then
  PY="$(command -v python3)"
else
  echo "Python 3.12 is required. Install python3.12 and python3.12-venv." >&2
  exit 1
fi

if [[ "${APP_USER}" != "root" ]] && ! id "${APP_USER}" >/dev/null 2>&1; then
  useradd --system --home "${APP_DIR}" --shell /usr/sbin/nologin "${APP_USER}"
fi

if [[ ! -f "${APP_DIR}/.env" ]]; then
  cp "${APP_DIR}/.env.example" "${APP_DIR}/.env"
  echo "Created ${APP_DIR}/.env from .env.example. Fill GIGACHAT_CREDENTIALS and set APP_ENV=production."
fi
chown -R "${APP_USER}:${APP_USER}" "${APP_DIR}"
chmod 600 "${APP_DIR}/.env"

if [[ ! -d "${APP_DIR}/.venv" ]]; then
  sudo -u "${APP_USER}" "${PY}" -m venv "${APP_DIR}/.venv"
fi

sudo -u "${APP_USER}" "${APP_DIR}/.venv/bin/python" -m pip install --upgrade pip
sudo -u "${APP_USER}" "${APP_DIR}/.venv/bin/pip" install -r "${APP_DIR}/requirements.txt"

sed \
  -e "s|/root/sber-transport-assistant|${APP_DIR}|g" \
  -e "s|^User=root|User=${APP_USER}|" \
  -e "s|^Group=root|Group=${APP_USER}|" \
  "${SCRIPT_DIR}/sber-transport.service" > "/etc/systemd/system/${UNIT_NAME}"

systemctl daemon-reload
systemctl enable "${UNIT_NAME}"
systemctl restart "${UNIT_NAME}"
systemctl --no-pager --full status "${UNIT_NAME}" || true

if command -v nginx >/dev/null 2>&1 && [[ -d /etc/nginx/sites-available ]]; then
  cp "${SCRIPT_DIR}/nginx-sber-transport.conf" /etc/nginx/sites-available/sber-transport
  ln -sfn /etc/nginx/sites-available/sber-transport /etc/nginx/sites-enabled/sber-transport
  nginx -t
  systemctl reload nginx
  echo "Nginx site enabled for gorodvdele.ru."
else
  echo "Nginx site was not enabled. Install nginx, then run this script again."
fi

curl --fail --silent --show-error "http://127.0.0.1:8000/api/health"
echo
echo "Service is installed. Diagnostics stay on localhost: curl http://127.0.0.1:8000/api/diagnostics/gigachat"
