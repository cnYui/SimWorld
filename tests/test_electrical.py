import math

from emcosim.electrical.motors import Servo
from emcosim.electrical.power import BrownoutDetector, Ldo, PowerNetwork


def test_rc_step_matches_analytic():
    r, c, i = 0.5, 470e-6, 0.4
    net = PowerNetwork({'nodes': {'n': {'c_f': c}}, 'sources': {'s': {'node': 'n', 'v': 5.0, 'r_ohm': r}}})
    net.initialise()
    dt, t = 25e-6, 0.0
    worst = 0.0
    for _ in range(200):
        net.step(dt, {'n': i}, {})
        t += dt
        exact = 5.0 - i * r * (1 - math.exp(-t / (r * c)))
        worst = max(worst, abs(net.node_voltage('n') - exact))
    assert worst < 0.01


def test_current_limited_source_collapses():
    net = PowerNetwork({'nodes': {'n': {'c_f': 100e-6}},
                        'sources': {'usb': {'node': 'n', 'v': 5.0, 'r_ohm': 0.3, 'i_limit_a': 0.5}}})
    net.initialise()
    for _ in range(80):
        net.step(125e-6, {'n': 0.7}, {})
    assert net.sources['usb']['limited'] and net.node_voltage('n') < 3.0
    for _ in range(80):
        net.step(125e-6, {'n': 0.1}, {})
    assert not net.sources['usb']['limited'] and net.node_voltage('n') > 4.9


def test_ldo_dropout_and_brownout():
    ldo = Ldo('l', {'input': 'x', 'v_out': 3.3, 'dropout_v': 1.1, 'c_out_f': 22e-6})
    for _ in range(50):
        ldo.step(5.0, 0.05, 1e-4)
    assert abs(ldo.v - 3.3) < 1e-6
    for _ in range(50):
        ldo.step(3.4, 0.05, 1e-4)
    assert abs(ldo.v - 2.3) < 1e-3
    bod = BrownoutDetector({'v_trip': 2.43, 'v_release': 2.53})
    bod.good = True
    assert bod.update(0.0, 2.6) and not bod.update(0.001, 2.3) and not bod.update(0.002, 2.5)
    assert bod.update(0.003, 2.6)


def test_sg90_preset_matches_simulation_parameters():
    # simulation/parameters.json: stall 0.1765 N*m, no-load 8.72665 rad/s (at 5 V)
    s = Servo('s', {'type': 'servo', 'joint': 'j', 'node': 'n', 'preset': 'sg90'})
    m = s.motor
    stall = m.eta * m.N * m.ke * 5.0 / m.R
    no_load = 5.0 / (m.ke * m.N)
    assert abs(stall - 0.1765) / 0.1765 < 0.01
    assert abs(no_load - 8.72665) / 8.72665 < 0.01


def test_servo_decodes_pulse_and_times_out():
    s = Servo('s', {'type': 'servo', 'joint': 'j', 'node': 'n', 'preset': 'sg90'})
    s.set_signal(0.0, {'freq': 50.0, 'duty': 1311e-6 * 50})
    assert abs(math.degrees(s.target) + 17.0) < 0.02
    i0, g = s.prepare(0.01, 0.0, 0.0, 5.0, 125e-6)
    assert s.active and g > 0
    s.prepare(0.2, 0.0, 0.0, 5.0, 125e-6)
    assert not s.active            # no pulses for 200 ms
