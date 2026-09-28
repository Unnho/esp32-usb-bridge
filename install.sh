#!/usr/bin/env bash
#
# esp32-usb-bridge installer
#
# Sets up everything needed to flash/monitor an ESP32 whose board uses a
# Silicon Labs CP2102 USB-UART bridge, on a host where the kernel provides no
# CONFIG_USB_SERIAL_CP210X driver (so there is no /dev/ttyUSB* node and no
# module available to create one).
#
# Idempotent: safe to run any number of times.
#
#   sudo ./install.sh
#   curl -fsSL https://raw.githubusercontent.com/Unnho/esp32-usb-bridge/main/install.sh | sudo bash
#
# Options:
#   -n, --dry-run      print what would happen without doing it
#       --no-verify    skip the live hardware self-test
#   -h, --help         show this help
#
set -euo pipefail

# ----------------------------------------------------------------- settings --
RAW_BASE="${ESP32_USB_BRIDGE_RAW:-https://raw.githubusercontent.com/Unnho/esp32-usb-bridge/main}"
STUB_BASE="${ESPTOOL_STUB_BASE:-https://raw.githubusercontent.com/espressif/esptool}"
STUB_FALLBACK_TAG="v4.7.0"

DEST_DRIVER_DIR="/opt/esp-bridge"
DEST_BIN_DIR="/usr/local/bin"
UDEV_RULE="/etc/udev/rules.d/99-cp2102-esp32-bridge.rules"

PKG_LIST=(python3 python3-serial python3-usb libusb-1.0-0 curl ca-certificates)
STUB_FILES=(
  stub_flasher_32.json
  stub_flasher_8266.json
  stub_flasher_32s2.json
  stub_flasher_32s3.json
)
SOURCE_FILES=(cp210x_usb.py esp esp-monitor selftest.py LICENSE README.md AGENTS.md docs/PROTOCOL.md uninstall.sh)

DRY_RUN=0
DO_VERIFY=1

# ----------------------------------------------------------------- helpers --
if [[ -t 1 ]]; then
  C_G=$'\033[32m'; C_Y=$'\033[33m'; C_R=$'\033[31m'; C_B=$'\033[1m'; C_0=$'\033[0m'
else
  C_G=""; C_Y=""; C_R=""; C_B=""; C_0=""
fi

log()  { printf '%s[+]%s %s\n' "$C_G" "$C_0" "$*"; }
info() { printf '%s[i]%s %s\n' "$C_B" "$C_0" "$*"; }
warn() { printf '%s[!]%s %s\n' "$C_Y" "$C_0" "$*" >&2; }
die()  { printf '%s[x]%s %s\n' "$C_R" "$C_0" "$*" >&2; exit 1; }

run() {
  if (( DRY_RUN )); then
    printf '    %sdry-run>%s %s\n' "$C_B" "$C_0" "$*"
    return 0
  fi
  "$@"
}

usage() {
  sed -n '2,20p' "${BASH_SOURCE[0]:-}" 2>/dev/null | sed 's/^# \{0,1\}//' || true
}

# ------------------------------------------------------------------- args ---
while (($#)); do
  case "$1" in
    -n|--dry-run) DRY_RUN=1 ;;
    --no-verify)  DO_VERIFY=0 ;;
    -h|--help)    usage; exit 0 ;;
    *) die "unknown option: $1 (try --help)" ;;
  esac
  shift
done

# ------------------------------------------------------------------- root ---
if (( EUID != 0 )) && (( ! DRY_RUN )); then
  if [[ -n "${BASH_SOURCE[0]:-}" && -f "${BASH_SOURCE[0]:-}" ]] && command -v sudo >/dev/null 2>&1; then
    log "re-running as root via sudo"
    exec sudo -- "$0" "$@"
  fi
  die "root required. Try:  curl -fsSL $RAW_BASE/install.sh | sudo bash"
fi

# ----------------------------------------------------------- locate sources --
SRC_DIR=""
# Scratch dir used when this script is run without its files (curl | bash).
# It must NOT be `local` to fetch_sources(): the EXIT trap below runs after
# the function has returned, and `local` + `set -u` then aborts the script
# with "tmp: unbound variable" - i.e. a successful install reporting failure.
FETCH_TMP=""

cleanup_fetch_tmp() {
  if [[ -n "${FETCH_TMP:-}" ]]; then
    rm -rf "${FETCH_TMP:-}"
    FETCH_TMP=""
  fi
  return 0
}

if [[ -n "${BASH_SOURCE[0]:-}" && -f "${BASH_SOURCE[0]:-}" ]]; then
  SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fi

fetch_sources() {
  command -v curl >/dev/null 2>&1 || die "curl is needed to download the sources"
  FETCH_TMP="$(mktemp -d)"
  trap cleanup_fetch_tmp EXIT
  log "No local copy of the sources found - downloading from $RAW_BASE"
  local f
  for f in "${SOURCE_FILES[@]}"; do
    mkdir -p "$FETCH_TMP/$(dirname "$f")"
    run curl -fsSL "$RAW_BASE/$f" -o "$FETCH_TMP/$f" || die "download failed: $f"
  done
  if (( DRY_RUN == 0 )) && [[ ! -f "$FETCH_TMP/cp210x_usb.py" ]]; then
    die "downloaded sources are incomplete - check your network/proxy"
  fi
  SRC_DIR="$FETCH_TMP"
}

if [[ -z "$SRC_DIR" || ! -f "$SRC_DIR/cp210x_usb.py" || ! -f "$SRC_DIR/esp" ]]; then
  fetch_sources
fi
log "Using sources from: $SRC_DIR"

# ---------------------------------------------------------------- packages ---
install_packages() {
  command -v apt-get >/dev/null 2>&1 || die \
"apt-get not found. This installer targets Debian/Ubuntu.
Install manually: python3, python3-serial, python3-usb, libusb-1.0-0, esptool, curl
then re-run, or copy the files by hand (see README.md -> 'Manual install')."

  log "Updating package indexes"
  if ! run apt-get update; then
    warn "apt-get update failed - continuing with cached indexes"
  fi

  log "Installing packages: ${PKG_LIST[*]}"
  run apt-get install -y --no-install-recommends "${PKG_LIST[@]}" || die "package install failed"

  if ! python3 -c "import esptool" >/dev/null 2>&1; then
    log "esptool not available via apt - falling back to pip"
    run apt-get install -y --no-install-recommends python3-pip
    run python3 -m pip install --break-system-packages esptool \
      || run python3 -m pip install esptool \
      || die "could not install esptool"
  fi
}

check_python_deps() {
  (( DRY_RUN )) && return 0
  python3 -c "import serial, usb.core, esptool" >/dev/null 2>&1 \
    || die "python dependencies missing (serial / usb / esptool)"
}

# --------------------------------------------------------------- esptool stubs --
# Debian's esptool package ships without the classic ESP32 (and ESP32-S3) stub
# loader JSONs, which esptool needs at runtime -> FileNotFoundError.
esptool_version() {
  local v=""
  v="$(python3 -c 'import esptool; print(getattr(esptool, "__version__", ""))' 2>/dev/null || true)"
  if [[ -z "$v" ]] && command -v esptool >/dev/null 2>&1; then
    v="$(esptool version 2>/dev/null | head -n1 | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -n1 || true)"
  fi
  printf '%s' "${v:-4.7.0}"
}

restore_stubs() {
  local stub_dir
  stub_dir="$(python3 -c 'import esptool, os; print(os.path.join(os.path.dirname(esptool.__file__), "targets", "stub_flasher"))' 2>/dev/null || true)"
  [[ -n "$stub_dir" && -d "$stub_dir" ]] || { warn "esptool stub directory not found - skipping stub restore"; return 0; }

  local ver tag f url
  ver="$(esptool_version)"
  tag="v${ver}"
  log "esptool $ver -> stubs in $stub_dir"

  for f in "${STUB_FILES[@]}"; do
    [[ -f "$stub_dir/$f" ]] && continue
    log "restoring missing stub: $f"
    (( DRY_RUN )) && continue

    url="$STUB_BASE/$tag/esptool/targets/stub_flasher/$f"
    if ! curl -fsSL --retry 2 "$url" -o "$stub_dir/$f.tmp" 2>/dev/null; then
      url="$STUB_BASE/$STUB_FALLBACK_TAG/esptool/targets/stub_flasher/$f"
      curl -fsSL --retry 2 "$url" -o "$stub_dir/$f.tmp" 2>/dev/null || {
        warn "could not download $f (no network?) - flash commands will fail until it is provided"
        rm -f "$stub_dir/$f.tmp"
        continue
      }
    fi

    if python3 -c "import json,sys; json.load(open(sys.argv[1]))" "$stub_dir/$f.tmp" 2>/dev/null; then
      chmod 644 "$stub_dir/$f.tmp"
      mv "$stub_dir/$f.tmp" "$stub_dir/$f"
      info "installed $f"
    else
      warn "$f is not valid JSON - discarded"
      rm -f "$stub_dir/$f.tmp"
    fi
  done
}

# --------------------------------------------------------------- deploy files --
deploy_files() {
  log "Installing driver -> $DEST_DRIVER_DIR"
  run install -d -m 755 "$DEST_DRIVER_DIR"
  run install -m 644 "$SRC_DIR/cp210x_usb.py" "$DEST_DRIVER_DIR/cp210x_usb.py"
  run install -m 755 "$SRC_DIR/selftest.py"   "$DEST_DRIVER_DIR/selftest.py"
  local doc
  for doc in README.md AGENTS.md LICENSE uninstall.sh; do
    [[ -f "$SRC_DIR/$doc" ]] && run install -m 644 "$SRC_DIR/$doc" "$DEST_DRIVER_DIR/$doc"
  done
  run install -d -m 755 "$DEST_DRIVER_DIR/docs"
  [[ -f "$SRC_DIR/docs/PROTOCOL.md" ]] && run install -m 644 "$SRC_DIR/docs/PROTOCOL.md" "$DEST_DRIVER_DIR/docs/PROTOCOL.md"

  log "Installing commands -> $DEST_BIN_DIR"
  run install -d -m 755 "$DEST_BIN_DIR"
  run install -m 755 "$SRC_DIR/esp"         "$DEST_BIN_DIR/esp"
  run install -m 755 "$SRC_DIR/esp-monitor" "$DEST_BIN_DIR/esp-monitor"

  # esptool.py -> esp, so the conventional spelling hits our wrapper.
  if [[ -e "$DEST_BIN_DIR/esptool.py" && ! -L "$DEST_BIN_DIR/esptool.py" ]]; then
    warn "$DEST_BIN_DIR/esptool.py exists and is not a symlink - leaving it alone (use 'esp' instead)"
  else
    run ln -sfn "$DEST_BIN_DIR/esp" "$DEST_BIN_DIR/esptool.py"
  fi
}

install_udev_rule() {
  command -v udevadm >/dev/null 2>&1 || { info "udev not present - skipping rule"; return 0; }
  log "Installing udev rule $UDEV_RULE"
  if (( DRY_RUN )); then printf '    %sdry-run>%s write %s\n' "$C_B" "$C_0" "$UDEV_RULE"; return 0; fi
  cat > "$UDEV_RULE" <<'RULE'
# esp32-usb-bridge: let non-root processes open the Silicon Labs CP210x bridge.
# The userspace driver in /opt/esp-bridge talks to it through /dev/bus/usb/*.
# Loosen/tighten MODE if your security policy requires it.
SUBSYSTEM=="usb", ATTR{idVendor}=="10c4", ATTR{idProduct}=="ea60", MODE="0666", TAG+="uaccess"
RULE
  udevadm control --reload-rules >/dev/null 2>&1 || true
  udevadm trigger --subsystem-match=usb >/dev/null 2>&1 || true
}

# ------------------------------------------------------------------ verify ---
verify_syntax() {
  (( DRY_RUN )) && return 0
  log "Syntax-checking installed files"
  python3 - "$DEST_DRIVER_DIR/cp210x_usb.py" "$DEST_BIN_DIR/esp" "$DEST_BIN_DIR/esp-monitor" "$DEST_DRIVER_DIR/selftest.py" <<'PY'
import ast, sys
for path in sys.argv[1:]:
    with open(path) as fh:
        ast.parse(fh.read(), filename=path)
    print("    ok:", path)
PY
}

report_kernel_state() {
  (( DRY_RUN )) && return 0
  if [[ -r /proc/config.gz ]]; then
    if zcat /proc/config.gz 2>/dev/null | grep -q '^CONFIG_USB_SERIAL_CP210X=y'; then
      info "kernel has a built-in CP210X driver (a /dev/ttyUSB* node may already exist)"
    elif zcat /proc/config.gz 2>/dev/null | grep -q '^CONFIG_USB_SERIAL_CP210X=m'; then
      info "kernel has CP210X as a module - check modprobe cp210x if you prefer the kernel path"
    else
      info "kernel has no CP210X support -> userspace driver required (this installer)"
    fi
  fi
}

device_present() {
  (( DRY_RUN )) && return 1
  python3 -c 'import usb.core,sys; sys.exit(0 if usb.core.find(idVendor=0x10c4, idProduct=0xea60) else 1)' 2>/dev/null
}

verify_hardware() {
  (( DRY_RUN )) && return 0

  log "Checking esptool wrapper"
  "$DEST_BIN_DIR/esp" version >/dev/null 2>&1 || die "'esp version' failed"

  if (( ! DO_VERIFY )); then
    info "skipping hardware self-test (--no-verify)"
    return 0
  fi

  if ! device_present; then
    warn "no CP2102 (10c4:ea60) on USB right now - plug in the board and run:"
    warn "    python3 $DEST_DRIVER_DIR/selftest.py"
    return 0
  fi

  log "Board detected - running live self-test (this resets the chip once)"
  if python3 "$DEST_DRIVER_DIR/selftest.py"; then
    log "Self-test PASSED"
  else
    warn "Self-test did not complete. Re-run: python3 $DEST_DRIVER_DIR/selftest.py"
    warn "If it reports 'Resource busy', close any running esp-monitor first."
  fi
}

summary() {
  echo
  printf '%s== esp32-usb-bridge installed ==%s\n' "$C_B" "$C_0"
  cat <<EOF
  driver      $DEST_DRIVER_DIR/cp210x_usb.py
  commands    $DEST_BIN_DIR/esp  ($DEST_BIN_DIR/esptool.py)
  console     $DEST_BIN_DIR/esp-monitor
  docs        $DEST_DRIVER_DIR/README.md
              $DEST_DRIVER_DIR/AGENTS.md
              $DEST_DRIVER_DIR/docs/PROTOCOL.md

Try:
  esp bridge-info
  esp chip_id
  esp flash_id
  esp-monitor --reset
  python3 $DEST_DRIVER_DIR/selftest.py

Note: stock '/usr/bin/esptool' cannot see the board (there is no tty node).
Use 'esp' or 'esptool.py'.
EOF
}

# -------------------------------------------------------------------- main ---
log "esp32-usb-bridge installer"
(( DRY_RUN )) && warn "DRY RUN - nothing will be changed"

install_packages
check_python_deps
restore_stubs
deploy_files
install_udev_rule
verify_syntax
report_kernel_state
verify_hardware
summary
