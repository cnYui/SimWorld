#!/usr/bin/env bash
# Build Espressif's QEMU fork with the emcosim co-simulation patch.
# QEMU is GPL-2.0; the patch in this directory is GPL-2.0-or-later like the files it
# modifies. The resulting binary runs as a separate process: emcosim (Apache-2.0)
# talks to it only through a socket and its serial console.
#
# Needs: git, gcc, ninja, python3, pkg-config, libglib2.0-dev, libpixman-1-dev,
#        libgcrypt20-dev, libslirp-dev, flex, bison
# Usage: ./build.sh [install-prefix]     (default: ./out)
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
PREFIX="${1:-$HERE/out}"
TAG="esp-develop-9.0.0-20240606"
SRC="${QEMU_SRC:-$HERE/src}"

if [ ! -d "$SRC/.git" ]; then
  git clone --depth 1 -b "$TAG" https://github.com/espressif/qemu "$SRC"
fi
cd "$SRC"
if ! git apply --reverse --check "$HERE"/0001-*.patch 2>/dev/null; then
  git apply "$HERE"/0001-*.patch
fi
mkdir -p build && cd build
../configure --prefix="$PREFIX" --target-list=xtensa-softmmu --enable-gcrypt --enable-slirp \
  --disable-werror --disable-docs --disable-user --disable-capstone --disable-vnc --disable-sdl \
  --disable-gtk --disable-opengl
ninja qemu-system-xtensa
install -d "$PREFIX/bin" "$PREFIX/share/qemu"
install -m 755 qemu-system-xtensa "$PREFIX/bin/qemu-system-xtensa"
cp ../pc-bios/esp32-v3-rom.bin ../pc-bios/esp32-v3-rom-app.bin "$PREFIX/share/qemu/"
echo "built $PREFIX/bin/qemu-system-xtensa"
echo "export EMCOSIM_QEMU=$PREFIX/bin/qemu-system-xtensa EMCOSIM_QEMU_ROMS=$PREFIX/share/qemu"
