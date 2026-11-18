#!/usr/bin/env bash

set -Eeuo pipefail

APP_NAME="gree-usb-monitor"
INSTALL_DIR="/opt/${APP_NAME}"
SERVICE_FILE="/etc/systemd/system/${APP_NAME}.service"
DEFAULT_HTTP_PORT="8090"
HTTP_PORT=""
INSTALL_USER=""
PORT_FROM_ARGUMENT=0

SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

usage() {
  cat <<'USAGE'
Gree USB Monitor telepítő Raspberry Pi-re

Használat:
  sudo ./install.sh
  sudo ./install.sh --port 8090

Kapcsolók:
  --port PORT     A webes felület HTTP-portja (1024-65535).
  --user USER     A szolgáltatást futtató Linux-felhasználó.
  -h, --help      Súgó megjelenítése.

Port megadása nélkül a telepítő interaktívan rákérdez. Az alapérték 8090.
USAGE
}

fail() {
  printf 'Hiba: %s\n' "$*" >&2
  exit 1
}

while (($#)); do
  case "$1" in
    --port)
      [[ $# -ge 2 ]] || fail "A --port után adj meg egy portszámot."
      HTTP_PORT="$2"
      PORT_FROM_ARGUMENT=1
      shift 2
      ;;
    --user)
      [[ $# -ge 2 ]] || fail "A --user után adj meg egy felhasználónevet."
      INSTALL_USER="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      fail "Ismeretlen kapcsoló: $1"
      ;;
  esac
done

[[ ${EUID} -eq 0 ]] || fail "A telepítőt root jogosultsággal futtasd: sudo ./install.sh"
command -v python3 >/dev/null || fail "A python3 nem található. Telepítsd a python3 és python3-venv csomagokat."
command -v systemctl >/dev/null || fail "A systemd nem érhető el ezen a rendszeren."
[[ -f "${SOURCE_DIR}/server.py" && -d "${SOURCE_DIR}/static" ]] || fail "A telepítőt a Gree repó gyökeréből futtasd."
python3 -c 'import sys; raise SystemExit(sys.version_info < (3, 10))' || fail "Python 3.10 vagy újabb szükséges."

if [[ -z "${INSTALL_USER}" ]]; then
  if [[ -n "${SUDO_USER:-}" && "${SUDO_USER}" != "root" ]]; then
    INSTALL_USER="${SUDO_USER}"
  elif command -v getent >/dev/null && getent passwd 1000 >/dev/null; then
    INSTALL_USER="$(getent passwd 1000 | cut -d: -f1)"
  elif id pi >/dev/null 2>&1; then
    INSTALL_USER="pi"
  else
    fail "Nem található normál felhasználó. Add meg a --user kapcsolóval."
  fi
fi

id "${INSTALL_USER}" >/dev/null 2>&1 || fail "Nincs ilyen Linux-felhasználó: ${INSTALL_USER}"
INSTALL_GROUP="$(id -gn "${INSTALL_USER}")"

valid_port() {
  [[ "$1" =~ ^[0-9]+$ ]] && ((10#$1 >= 1024 && 10#$1 <= 65535))
}

port_in_use() {
  command -v ss >/dev/null || return 1
  ss -H -ltn "sport = :$1" 2>/dev/null | grep -q .
}

port_owned_by_existing_service() {
  [[ -f "${INSTALL_DIR}/.env" ]] || return 1
  systemctl is-active --quiet "${APP_NAME}.service" || return 1
  grep -qx "GREE_PORT=$1" "${INSTALL_DIR}/.env"
}

while true; do
  if [[ -z "${HTTP_PORT}" ]]; then
    if [[ -t 0 ]]; then
      read -r -p "Webes HTTP-port [${DEFAULT_HTTP_PORT}]: " HTTP_PORT
      HTTP_PORT="${HTTP_PORT:-${DEFAULT_HTTP_PORT}}"
    else
      HTTP_PORT="${DEFAULT_HTTP_PORT}"
    fi
  fi

  if ! valid_port "${HTTP_PORT}"; then
    if ((PORT_FROM_ARGUMENT)); then
      fail "Érvénytelen port: ${HTTP_PORT}. Használj 1024 és 65535 közötti számot."
    fi
    printf 'A portnak 1024 és 65535 közötti számnak kell lennie.\n' >&2
    HTTP_PORT=""
    continue
  fi

  if port_in_use "${HTTP_PORT}" && ! port_owned_by_existing_service "${HTTP_PORT}"; then
    if ((PORT_FROM_ARGUMENT)); then
      fail "A(z) ${HTTP_PORT} port már foglalt. Válassz másikat a --port kapcsolóval."
    fi
    printf 'A(z) %s port már foglalt, válassz másikat.\n' "${HTTP_PORT}" >&2
    HTTP_PORT=""
    continue
  fi
  break
done

printf '\nGree USB Monitor telepítése\n'
printf '  Könyvtár:   %s\n' "${INSTALL_DIR}"
printf '  Felhasználó: %s\n' "${INSTALL_USER}"
printf '  HTTP-port:   %s\n\n' "${HTTP_PORT}"

install -d -m 0755 "${INSTALL_DIR}" "${INSTALL_DIR}/static" "${INSTALL_DIR}/deploy"
install -m 0755 "${SOURCE_DIR}/server.py" "${INSTALL_DIR}/server.py"
install -m 0644 "${SOURCE_DIR}/requirements.txt" "${INSTALL_DIR}/requirements.txt"
install -m 0644 "${SOURCE_DIR}/README.md" "${INSTALL_DIR}/README.md"
cp -a "${SOURCE_DIR}/static/." "${INSTALL_DIR}/static/"

if [[ ! -x "${INSTALL_DIR}/.venv/bin/python" ]]; then
  if ! python3 -m venv "${INSTALL_DIR}/.venv"; then
    fail "Nem sikerült virtuális környezetet létrehozni. Telepítsd: sudo apt install python3-venv"
  fi
fi
"${INSTALL_DIR}/.venv/bin/python" -m pip install --disable-pip-version-check -r "${INSTALL_DIR}/requirements.txt"

ENV_TMP="$(mktemp)"
SERVICE_TMP="$(mktemp)"
cleanup() {
  rm -f "${ENV_TMP}" "${SERVICE_TMP}"
}
trap cleanup EXIT

if [[ -f "${INSTALL_DIR}/.env" ]]; then
  sed "s/^GREE_PORT=.*/GREE_PORT=${HTTP_PORT}/" "${INSTALL_DIR}/.env" >"${ENV_TMP}"
  grep -q '^GREE_PORT=' "${INSTALL_DIR}/.env" || printf 'GREE_PORT=%s\n' "${HTTP_PORT}" >>"${ENV_TMP}"
  grep -q '^GREE_HOST=' "${INSTALL_DIR}/.env" || printf 'GREE_HOST=0.0.0.0\n' >>"${ENV_TMP}"
else
  printf '%s\n' \
    '# A telepítő által generált beállítások.' \
    'GREE_HOST=0.0.0.0' \
    "GREE_PORT=${HTTP_PORT}" \
    '# GREE_SERIAL_PORT=/dev/ttyUSB0' \
    '# GREE_BAUDRATE=115200' \
    >"${ENV_TMP}"
fi
install -m 0644 "${ENV_TMP}" "${INSTALL_DIR}/.env"

sed \
  -e "s|__SERVICE_USER__|${INSTALL_USER}|g" \
  -e "s|__SERVICE_GROUP__|${INSTALL_GROUP}|g" \
  -e "s|__INSTALL_DIR__|${INSTALL_DIR}|g" \
  "${SOURCE_DIR}/deploy/gree-usb-monitor.service" >"${SERVICE_TMP}"
install -m 0644 "${SERVICE_TMP}" "${SERVICE_FILE}"

if getent group dialout >/dev/null 2>&1 && ! id -nG "${INSTALL_USER}" | tr ' ' '\n' | grep -qx dialout; then
  usermod -aG dialout "${INSTALL_USER}"
  printf 'A(z) %s felhasználó bekerült a dialout csoportba.\n' "${INSTALL_USER}"
fi

systemctl daemon-reload
systemctl enable "${APP_NAME}.service" >/dev/null
systemctl restart "${APP_NAME}.service"

if ! systemctl is-active --quiet "${APP_NAME}.service"; then
  journalctl -u "${APP_NAME}.service" -n 20 --no-pager >&2 || true
  fail "A szolgáltatás nem indult el. A fenti napló segít a hiba azonosításában."
fi

DEVICE_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
DEVICE_IP="${DEVICE_IP:-raspberry-pi-ip}"

printf '\nTelepítés kész.\n'
printf 'Webes felület: http://%s:%s\n' "${DEVICE_IP}" "${HTTP_PORT}"
printf 'Állapot: sudo systemctl status %s\n' "${APP_NAME}"
printf 'Napló:   sudo journalctl -u %s -f\n' "${APP_NAME}"
