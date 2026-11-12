"""Firmware images for the emulated ESP32: import an ESP-IDF build directory or a
merged flash image, and patch the "fw_params" partition so parameters can be
changed without rebuilding (see firmware/switch_robot/main/params.h)."""
from __future__ import annotations

import json
import shutil
import struct
from pathlib import Path

FLASH_SIZE = 4 * 1024 * 1024
PARTITION_TABLE_OFFSET = 0x8000

# name, struct code, default — order and types must match fw_params_t
FW_PARAMS_LAYOUT = [
    ('magic', 'I', 0x50524D53), ('version', 'H', 1), ('size', 'H', 0),
    ('pin_servo', 'B', 18), ('pin_button', 'B', 0), ('idle_pwm_off', 'B', 0), ('reserved0', 'B', 0),
    ('press_angle_deg', 'f', 17.0), ('ramp_deg_s', 'f', 90.0),
    ('pwm_center_us', 'f', 1500.0), ('pwm_us_per_deg', 'f', 11.111111),
    ('press_ms', 'I', 650), ('retract_ms', 'I', 500), ('debounce_ms', 'I', 30),
    ('stall_guard_ms', 'I', 0),
]
FW_PARAMS_FORMAT = '<' + ''.join(code for _, code, _ in FW_PARAMS_LAYOUT)
FW_PARAMS_DEFAULTS = {name: default for name, _, default in FW_PARAMS_LAYOUT
                      if name not in ('magic', 'version', 'size', 'reserved0')}


def pack_params(values):
    unknown = set(values) - set(FW_PARAMS_DEFAULTS)
    if unknown:
        raise KeyError(f'unknown firmware parameter(s): {sorted(unknown)}; known: {sorted(FW_PARAMS_DEFAULTS)}')
    merged = {name: default for name, _, default in FW_PARAMS_LAYOUT}
    merged.update(values)
    merged['size'] = struct.calcsize(FW_PARAMS_FORMAT)
    return struct.pack(FW_PARAMS_FORMAT, *[merged[name] for name, _, _ in FW_PARAMS_LAYOUT])


def partitions(image):
    out = {}
    for off in range(PARTITION_TABLE_OFFSET, PARTITION_TABLE_OFFSET + 0xC00, 32):
        entry = image[off:off + 32]
        if entry[:2] != b'\xaa\x50':
            break
        _, ptype, subtype, offset, size = struct.unpack('<HBBII', entry[:12])
        label = entry[12:28].split(b'\0')[0].decode(errors='replace')
        out[label] = {'type': ptype, 'subtype': subtype, 'offset': offset, 'size': size}
    return out


def merge_idf_build(build_dir):
    """Assemble a flash image from an ESP-IDF build directory (flasher_args.json)."""
    build_dir = Path(build_dir)
    args = json.loads((build_dir / 'flasher_args.json').read_text())
    image = bytearray(b'\xff' * FLASH_SIZE)
    for offset, rel in args['flash_files'].items():
        blob = (build_dir / rel).read_bytes()
        off = int(offset, 16)
        image[off:off + len(blob)] = blob
    return bytes(image)


def import_firmware(src, out_dir, name='firmware'):
    """Copy a firmware into the project. Accepts an ESP-IDF build dir, a merged
    .bin, or a project dir containing build/. Returns the project entry."""
    src = Path(src)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    if src.is_dir() and (src / 'build' / 'flasher_args.json').exists():
        src = src / 'build'
    if src.is_dir():
        image = merge_idf_build(src)
        elf = next(iter(sorted(src.glob('*.elf'))), None)
    elif src.suffix == '.bin':
        image = src.read_bytes()
        elf = None
    else:
        raise ValueError('firmware must be an ESP-IDF build directory or a merged flash .bin '
                         '(an .elf alone has no bootloader/partition table)')
    image = trim(image)
    target = out_dir / f'{name}.bin'
    target.write_bytes(image)
    entry = {'flash': str(target.name), 'size_bytes': len(image), 'partitions': partitions(pad(image))}
    if elf is not None:
        entry['elf_source'] = str(elf)     # not copied (several MB); only needed for debugging
    return entry


def trim(image):
    """Drop trailing 0xFF so the image stored in git stays small."""
    end = len(image)
    while end > 0 and image[end - 1] == 0xFF:
        end -= 1
    return image[:(end + 0xFFF) & ~0xFFF]


def pad(image):
    if len(image) > FLASH_SIZE:
        raise ValueError('firmware image larger than 4 MB')
    return image + b'\xff' * (FLASH_SIZE - len(image))


def build_flash(firmware_path, params, out_path):
    """Write a 4 MB image with the fw_params partition filled from `params`."""
    image = bytearray(pad(Path(firmware_path).read_bytes()))
    if params:
        table = partitions(image)
        if 'fw_params' not in table:
            raise ValueError('firmware has no "fw_params" partition; firmware.params cannot be applied')
        blob = pack_params(params)
        off = table['fw_params']['offset']
        image[off:off + 4096] = b'\xff' * 4096
        image[off:off + len(blob)] = blob
    Path(out_path).write_bytes(bytes(image))
    return out_path
