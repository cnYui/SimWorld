import struct

from emcosim import firmware

from conftest import EXAMPLE


def test_params_layout_size_matches_c_struct():
    # sizeof(fw_params_t) in firmware/switch_robot/main/params.h (packed)
    assert struct.calcsize(firmware.FW_PARAMS_FORMAT) == 44


def test_flash_patch_writes_partition(tmp_path):
    fw = EXAMPLE / 'firmware' / 'switch_robot.bin'
    out = firmware.build_flash(fw, {'press_angle_deg': 12.5, 'stall_guard_ms': 300}, tmp_path / 'f.bin')
    img = out.read_bytes()
    assert len(img) == firmware.FLASH_SIZE
    part = firmware.partitions(img)['fw_params']
    vals = struct.unpack(firmware.FW_PARAMS_FORMAT, img[part['offset']:part['offset'] + 44])
    names = [n for n, _, _ in firmware.FW_PARAMS_LAYOUT]
    rec = dict(zip(names, vals))
    assert rec['magic'] == 0x50524D53 and rec['press_angle_deg'] == 12.5 and rec['stall_guard_ms'] == 300
