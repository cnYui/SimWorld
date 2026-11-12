import pytest

from emcosim import api
from emcosim.project import Project

from conftest import EXAMPLE, needs_ngspice, needs_qemu


def test_scripted_servo_flips_rocker():
    s = api.run_scenario(Project(EXAMPLE), 'servo_sweep_scripted')
    assert s['passed'], s['checks']
    assert 3.0 < s['metrics']['probe.press.force_n.peak'] < 6.0


@needs_qemu
def test_firmware_toggles_switch():
    s = api.run_scenario(Project(EXAMPLE), 'toggle_nominal')
    assert s['passed'], s['checks']


@needs_qemu
def test_stall_browns_out_esp32():
    s = api.run_scenario(Project(EXAMPLE), 'stall_brownout')
    assert s['metrics']['mcu.resets'] >= 1
    assert 'BROWNOUT' in s['metrics']['mcu.boot_reasons']


@needs_qemu
def test_fix_by_moving_servo_supply():
    s = api.run_scenario(Project(EXAMPLE), 'stall_brownout', {'actuators.servo1.node': 'v5_servo'})
    assert s['metrics']['mcu.resets'] == 0


@needs_ngspice
def test_calibration_reduces_error():
    from emcosim import calibrate
    r = calibrate.calibrate(Project(EXAMPLE).data['power'], 'v5_servo', 'charger', 0.7)
    assert r['max_error_after_v'] < r['max_error_before_v']
    assert r['max_error_after_v'] < 0.01
