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


def _hold(s, q, v=5.0, dt=125e-6, n=800):
    taus = []
    for k in range(n):
        s.last_valid = k * dt                  # pulses keep coming
        s.prepare(k * dt, q, 0.0, v, dt)
        taus.append(s.finish(v))
    return taus


def test_transmission_and_speed_limit_off_by_default():
    s = Servo('s', {'type': 'servo', 'joint': 'j', 'node': 'n', 'preset': 'sg90'})
    assert s.trans is None and s.max_speed is None
    s2 = Servo('s', {'type': 'servo', 'joint': 'j', 'node': 'n', 'preset': 'sg90', 'backlash_deg': 0,
                     'output_stiffness_nm_per_rad': None, 'max_speed_dps': None})
    s.target = s2.target = math.radians(10)
    s.last_valid = s2.last_valid = 0.0
    assert _hold(s, 0.0, n=50) == _hold(s2, 0.0, n=50)


def test_backlash_gives_free_play_at_the_horn():
    cfg = {'type': 'servo', 'joint': 'j', 'node': 'n', 'preset': 'sg90', 'backlash_deg': 4.0}
    s = Servo('s', cfg)
    s.target, s.last_valid = 0.0, 0.0
    # horn held 1 deg off target: inside the +/-2 deg gap, the gear centres itself and
    # the horn feels no torque
    taus = _hold(s, math.radians(1.0))
    assert max(abs(t) for t in taus[100:]) < 1e-9
    assert abs(math.degrees(s.trans.theta)) < 0.5
    # horn pushed 3 deg off: the gear reaches the flank and pushes back
    s2 = Servo('s', cfg)
    s2.target, s2.last_valid = 0.0, 0.0
    taus = _hold(s2, math.radians(3.0))
    assert taus[-1] < -0.01
    assert abs(math.degrees(s2.trans.theta) - 1.0) < 1e-6      # gear locked on the flank


def test_output_compliance_deflects_under_stall():
    k = 5.0
    s = Servo('s', {'type': 'servo', 'joint': 'j', 'node': 'n', 'preset': 'sg90',
                    'output_stiffness_nm_per_rad': k})
    s.target, s.last_valid = math.radians(30.0), 0.0
    taus = _hold(s, 0.0, n=4000)                                 # horn blocked at 0
    m = s.motor
    stall = m.eta * m.N * m.ke * 5.0 / m.R
    assert abs(taus[-1] - stall) / stall < 0.02
    assert abs(s.trans.theta - stall / k) < 0.02 * stall / k     # gear wound up by tau / k


def test_speed_limit_sets_move_time():
    from emcosim.electrical.motors import merged
    from emcosim.fit import bench_move
    p = merged({'preset': 'sg90', 'max_speed_dps': 200.0})
    t = bench_move(p, 90.0, 5.0)['time_s']
    assert abs(t - 87.0 / 200.0) < 0.02
    t_free = bench_move(merged({'preset': 'sg90'}), 90.0, 5.0)['time_s']
    assert t_free < 0.25
