from emcosim.importers import easyeda_pro, kicad

from pathlib import Path

from conftest import EXAMPLE


def test_easyeda_conversion_keeps_netlist(tmp_path):
    v04 = Path(__file__).parent / 'data' / 'v04'      # copy of switch-robot-diy electronics/v04
    out = tmp_path / 'carrier.kicad_pcb'
    stats = easyeda_pro.convert(v04 / 'carrier_v04.epcb', v04 / 'carrier_v04.dsn', out, v04 / 'carrier_v04_bom.csv')
    assert stats == {'components': 16, 'tracks': 69, 'nets': 13, 'holes': 4}
    pcb = kicad.load_kicad_pcb(out)
    # same netlist as electronics/v04/README.md
    assert pcb['nets']['V5_SERVO'] == ['C1.1', 'C2.1', 'F1.2', 'J3.2']
    assert pcb['nets']['PWM_IN'] == ['J2.2', 'R5.1', 'R6.1']
    assert pcb['nets']['SERVO_SIG'] == ['J3.3', 'R5.2']
    assert abs(pcb['board']['width_mm'] - 60) < 1e-6 and abs(pcb['board']['height_mm'] - 40) < 1e-6


def test_power_values_from_pcb():
    pcb = kicad.load_kicad_pcb(EXAMPLE / 'pcb' / 'carrier_v04.kicad_pcb')
    c, parts = kicad.capacitance_between(pcb, 'V5_SERVO')
    assert parts == ['C1', 'C2'] and abs(c - 470.1e-6) < 1e-9
    r = kicad.path_resistance(pcb, 'V5_SERVO', 'F1.2', 'J3.2')
    # 0.8 mm, 1 oz copper: ~0.6 mOhm/mm; the track is ~20 mm long
    assert 0.005 < r < 0.03


def test_value_parser():
    assert kicad.parse_value('470uF') == 470e-6
    assert kicad.parse_value('100kΩ') == 100e3
    assert kicad.parse_value('4k7') == 4700
    assert kicad.parse_value('0R22') == 0.22
    assert kicad.parse_value('RUEF110') is None
