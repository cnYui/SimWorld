#!/usr/bin/env bash
# Build the patched Espressif QEMU for Windows in an MSYS2 MINGW64 shell
# (this is what the GitHub Actions workflow runs). Produces OUT/ with
# qemu-system-xtensa.exe, the MinGW DLLs it needs and share/qemu/ ROMs.
#
#   pacman -S --needed git mingw-w64-x86_64-{gcc,glib2,pixman,libgcrypt,ninja,meson,python,python-distlib,python-setuptools,pkgconf} \
#             bison flex diffutils make
#   qemu/build-windows.sh /c/out/qemu
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
OUT="${1:-$HERE/out-win}"
TAG="esp-develop-9.0.0-20240606"
SRC="${QEMU_SRC:-$HERE/src-win}"

if [ ! -d "$SRC/.git" ]; then
  git clone --depth 1 -b "$TAG" https://github.com/espressif/qemu "$SRC"
fi
cd "$SRC"
if ! git apply --reverse --check "$HERE"/0001-*.patch 2>/dev/null; then
  git apply "$HERE"/0001-*.patch
fi
mkdir -p build && cd build
../configure --target-list=xtensa-softmmu --enable-gcrypt --disable-slirp \
  --disable-werror --disable-docs --disable-user --disable-capstone --disable-vnc --disable-sdl \
  --disable-gtk --disable-opengl --disable-guest-agent --disable-tools
ninja qemu-system-xtensa.exe
mkdir -p "$OUT/share/qemu"
cp qemu-system-xtensa.exe "$OUT/"
cp ../pc-bios/esp32-v3-rom.bin ../pc-bios/esp32-v3-rom-app.bin "$OUT/share/qemu/"
# Copy every MinGW DLL the executable loads (recursively, via ldd).
ldd "$OUT/qemu-system-xtensa.exe" | awk '/mingw64/ {print $3}' | sort -u | while read -r dll; do
  cp "$dll" "$OUT/"
done
"$OUT/qemu-system-xtensa.exe" --version
echo "built $OUT"
