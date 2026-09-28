#!/usr/bin/env bash
#
# esp32-usb-bridge uninstaller - removes what install.sh put in place.
#
#   sudo ./uninstall.sh
#   sudo ./uninstall.sh --purge-packages   # also apt-remove the dependencies
#
set -euo pipefail

DEST_DRIVER_DIR="/opt/esp-bridge"
DEST_BIN_DIR="/usr/local/bin"
UDEV_RULE="/etc/udev/rules.d/99-cp2102-esp32-bridge.rules"
PKG_LIST=(python3 python3-serial python3-usb libusb-1.0-0)

PURGE_PACKAGES=0

log() { printf '[+] %s\n' "$*"; }
info() { printf '[i] %s\n' "$*"; }
warn() { printf '[!] %s\n' "$*" >&2; }

while (($#)); do
  case "$1" in
    --purge-packages) PURGE_PACKAGES=1 ;;
    -h|--help) sed -n '2,10p' "${BASH_SOURCE[0]:-}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) warn "unknown option: $1"; exit 1 ;;
  esac
  shift
done

if (( EUID != 0 )); then
  if [[ -n "${BASH_SOURCE[0]:-}" && -f "${BASH_SOURCE[0]:-}" ]] && command -v sudo >/dev/null 2>&1; then
    exec sudo -- "$0" "$@"
  fi
  warn "root required"
  exit 1
fi

log "Removing commands"
rm -f "$DEST_BIN_DIR/esp" "$DEST_BIN_DIR/esp-monitor"
# Only remove the esptool.py shim if it is ours.
if [[ -L "$DEST_BIN_DIR/esptool.py" ]] && [[ "$(readlink "$DEST_BIN_DIR/esptool.py")" == "$DEST_BIN_DIR/esp" ]]; then
  rm -f "$DEST_BIN_DIR/esptool.py"
  info "removed esptool.py shim"
else
  [[ -e "$DEST_BIN_DIR/esptool.py" ]] && info "left $DEST_BIN_DIR/esptool.py (not created by us)"
fi

log "Removing driver directory $DEST_DRIVER_DIR"
rm -rf "$DEST_DRIVER_DIR"

log "Removing udev rule"
rm -f "$UDEV_RULE"
if command -v udevadm >/dev/null 2>&1; then
  udevadm control --reload-rules >/dev/null 2>&1 || true
fi

if (( PURGE_PACKAGES )); then
  log "Removing packages: ${PKG_LIST[*]}"
  apt-get remove --purge -y "${PKG_LIST[@]}" >/dev/null 2>&1 || warn "could not remove some packages"
  info "esptool left installed - remove it too with: apt-get remove esptool"
fi

info "Note: stub JSON files restored inside esptool's package are left as-is."
log "Done. 'esp' and 'esp-monitor' are gone; the kernel/USB state is untouched."
