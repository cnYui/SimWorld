#!/usr/bin/env bash
# One-step setup for a fresh clone (Linux / macOS).
#   - Python environment in .venv with emcosim installed
#   - KiCad (board checks) and ngspice (circuit check) from the system package manager
#   - patched QEMU for ESP32 firmware tests: prebuilt binary from this repository's
#     GitHub release, or built from source (qemu/build.sh) when none fits this machine
# QEMU and KiCad are GPL programs that emcosim runs as separate processes; they are
# downloaded or built here, not shipped in this repository.
#
# Usage: scripts/setup.sh [--no-system] [--build-qemu]
#   --no-system   do not call apt/dnf/brew (install KiCad and ngspice yourself)
#   --build-qemu  build QEMU from source instead of downloading it
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TOOLS="$ROOT/tools"
REPO="${EMCOSIM_REPO:-cnYui/SimWorld}"   # GitHub repository whose releases carry the QEMU builds
SYSTEM=1; BUILD_QEMU=0
for a in "$@"; do
  case "$a" in
    --no-system) SYSTEM=0 ;;
    --build-qemu) BUILD_QEMU=1 ;;
    *) echo "unknown option $a"; exit 2 ;;
  esac
done
say() { printf '\n== %s\n' "$*"; }
have() { command -v "$1" >/dev/null 2>&1; }
SUDO=""; [ "$(id -u)" -ne 0 ] && have sudo && SUDO="sudo"


say "Python environment (.venv)"
PY="${PYTHON:-}"
if [ -z "$PY" ]; then
  for c in python3.12 python3.11 python3.10 python3; do have "$c" && { PY="$c"; break; }; done
fi
[ -n "$PY" ] || { echo "Python 3.10+ is needed"; exit 1; }
"$PY" -c 'import sys; sys.exit(sys.version_info < (3, 10))' || { echo "Python 3.10+ is needed ($PY is older)"; exit 1; }
[ -d "$ROOT/.venv" ] || "$PY" -m venv "$ROOT/.venv"
"$ROOT/.venv/bin/python" -m pip install -q --upgrade pip
"$ROOT/.venv/bin/python" -m pip install -q -e "$ROOT[step,dev]"
# strength check (FEA) on the 仿真 page: optional
"$ROOT/.venv/bin/python" -m pip install -q -e "$ROOT[strength]" || echo "strength check libraries could not be installed (optional)"

OS="$(uname -s)"; ARCH="$(uname -m)"
if [ "$SYSTEM" = 1 ]; then
  say "KiCad and ngspice"
  if [ "$OS" = Darwin ]; then
    have brew || { echo "Homebrew is needed on macOS (https://brew.sh)"; exit 1; }
    have ngspice || brew install ngspice
    [ -d /Applications/KiCad ] || ls /Applications/KiCad*/KiCad.app >/dev/null 2>&1 || brew install --cask kicad
  elif have apt-get; then
    pkgs="ngspice libglib2.0-0 libpixman-1-0 libgcrypt20 libslirp0 libglu1-mesa libxcursor1 libxinerama1 libxft2"
    if ! have kicad-cli; then
      if have add-apt-repository && grep -qi ubuntu /etc/os-release; then
        $SUDO add-apt-repository -y ppa:kicad/kicad-9.0-releases
      fi
      pkgs="$pkgs kicad"
    fi
    $SUDO apt-get update -q
    $SUDO apt-get install -y -q --no-install-recommends $pkgs
  elif have dnf; then
    $SUDO dnf install -y ngspice kicad glib2 pixman libgcrypt libslirp
  else
    echo "No apt/dnf/brew: install KiCad 9 or 10 and ngspice yourself."
  fi
fi

say "Patched QEMU (ESP32 firmware tests)"
mkdir -p "$TOOLS"
asset=""
[ "$OS" = Linux ] && [ "$ARCH" = x86_64 ] && asset="qemu-esp32-cosim-linux-x86_64.tar.gz"
if [ -x "$TOOLS/qemu/bin/qemu-system-xtensa" ]; then
  echo "already in tools/qemu"
else
  got=0
  if [ "$BUILD_QEMU" = 0 ] && [ -n "$asset" ] && [ -n "$REPO" ]; then
    url="https://github.com/$REPO/releases/latest/download/$asset"
    echo "downloading $url"
    if curl -fL --retry 3 -o "$TOOLS/$asset" "$url"; then
      rm -rf "$TOOLS/qemu" && mkdir -p "$TOOLS/qemu"
      tar -xzf "$TOOLS/$asset" -C "$TOOLS/qemu" && rm -f "$TOOLS/$asset" && got=1
    else
      echo "no prebuilt QEMU in the latest release; building from source instead"
    fi
  fi
  if [ "$got" = 0 ]; then
    if [ "$SYSTEM" = 1 ]; then
      if have apt-get; then
        $SUDO apt-get install -y -q --no-install-recommends git gcc ninja-build python3 pkg-config \
          libglib2.0-dev libpixman-1-dev libgcrypt20-dev libslirp-dev flex bison
      elif [ "$OS" = Darwin ]; then
        brew install ninja pkg-config glib pixman libgcrypt libslirp
      fi
    fi
    QEMU_SRC="$TOOLS/qemu-src" bash "$ROOT/qemu/build.sh" "$TOOLS/qemu"
  fi
fi

say "freerouting (PCB autorouter)"
FR_VERSION=2.5.0
if ls "$ROOT/tools/freerouting/"*.jar >/dev/null 2>&1; then echo "already in tools/freerouting"
elif ! have java; then echo "Java not found: board_autoroute uses the built-in grid router until Java 21+ is installed."
else
  mkdir -p "$ROOT/tools/freerouting"
  curl -fL -o "$ROOT/tools/freerouting/freerouting-$FR_VERSION.jar"     "https://github.com/freerouting/freerouting/releases/download/v$FR_VERSION/freerouting-$FR_VERSION.jar"     || echo "freerouting not installed; board_autoroute falls back to the built-in grid router."
fi

say "Check"
"$ROOT/.venv/bin/python" - <<'PY'
from emcosim import eda
from emcosim.toolpaths import find_ngspice
from emcosim.mcu.qemu import find_qemu
from emcosim.design.autoroute import find_freerouting
try:
    q = find_qemu()[0]
except RuntimeError:
    q = None
fr = find_freerouting()
for name, v in (('KiCad', eda.find_cli()), ('ngspice', find_ngspice()), ('QEMU', q), ('freerouting', fr and fr[-1])):
    print(f'{name:8s} {v or "NOT FOUND"}')
PY
cat <<MSG

Done. Start the app in your browser:
  $ROOT/.venv/bin/python -m emcosim --project "$ROOT/examples/switch_robot" serve
MSG
