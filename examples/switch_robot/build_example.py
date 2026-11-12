"""Rebuild project.yaml for the switch-robot example by walking the same steps
an AI agent takes through the MCP server: import -> annotate. Running it
again produces the same file, so the committed project.yaml is reproducible.

    python build_example.py            # uses firmware/switch_robot.bin as committed
    python build_example.py --fw ../../firmware/switch_robot/build   # after idf.py build
"""
import argparse
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))

from emcosim import api  # noqa: E402
from emcosim.importers import kicad  # noqa: E402
from emcosim.project import Project  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument('--fw', default=None, help='ESP-IDF build dir or merged .bin to import')
args = ap.parse_args()

(HERE / 'project.yaml').unlink(missing_ok=True)
prj = Project.create(HERE, 'switch_robot')

# 1. PCB. carrier_v04.kicad_pcb was converted from electronics/v04 (EasyEDA Pro)
#    with scripts/easyeda_to_kicad.py; replace it with EasyEDA's own KiCad export when available.
pcb = api.import_pcb(prj, HERE / 'pcb' / 'carrier_v04.kicad_pcb')

# 2. Mechanics: the MuJoCo model of simulation/ (exported by scripts/export_mjcf.py).
api.import_model(prj, HERE / 'mechanics' / 'switch_robot.xml')

# 3. Firmware (minimal ESP32 firmware in platform/firmware/switch_robot).
api.import_firmware(prj, args.fw or HERE / 'firmware' / 'switch_robot.bin', name='switch_robot')

# 4. Joints: both exist in the MJCF; define_joint records their role and limits.
api.define_joint(prj, 'servo_axis', body='press_arm', type='hinge', axis=[1, 0, 0], range_deg=[-40, 40])
api.define_joint(prj, 'rocker_axis', body='rocker', type='hinge', axis=[1, 0, 0], range_deg=[-3.92, 3.92])
mech = prj.data['mechanics']
mech['timestep_s'] = 0.000125
mech['initial_deg'] = {'rocker_axis': 3.5}          # light OFF
mech['elements'] = {'rocker_detent': {'type': 'bistable', 'joint': 'rocker_axis', 'stable_deg': 3.5,
                                      'peak_force_n': 3.0, 'lever_m': 0.024,
                                      'note': 'assumed, same as simulation/parameters.json'}}
mech['probes'] = {'press': {'type': 'contact', 'geoms_a': 'arm_collision', 'geoms_b': 'rocker_collision'}}

# 5. Power network, values from the PCB where it has them.
summary = prj.pcb()
c_servo, caps = kicad.capacitance_between(summary, 'V5_SERVO')
cu_in = kicad.path_resistance(summary, 'VIN5', 'J1.1', 'F1.1')
cu_servo = kicad.path_resistance(summary, 'V5_SERVO', 'F1.2', 'J3.2')
cu_gnd = kicad.path_resistance(summary, 'GND', 'J1.2', 'J3.1')
prj.data['power'] = {
    'nodes': {
        'vin': {'c_f': 0.0, 'net': 'VIN5', 'note': 'J1 pin 1'},
        'v5_servo': {'c_f': c_servo, 'net': 'V5_SERVO', 'note': f'{"+".join(caps)} from PCB'},
        'usb5v': {'c_f': 10e-6, 'note': 'MCU dev board USB 5 V (separately powered, README J2)'},
    },
    'sources': {
        'adapter': {'node': 'vin', 'v': 5.0, 'r_ohm': 0.25,
                    'note': 'assumed: 5 V 2 A adapter + 1 m 22 AWG lead, round trip'},
        'usb': {'node': 'usb5v', 'v': 5.0, 'r_ohm': 0.3, 'i_limit_a': 0.5,
                'note': 'assumed: PC USB 2.0 port (500 mA limit) + cable'},
    },
    'branches': {
        'f1': {'from': 'vin', 'to': 'v5_servo', 'r_ohm': round(0.15 + cu_in + cu_servo + cu_gnd, 4),
               'note': f'RUEF110 cold resistance 0.15 ohm (assumed) + PCB copper {cu_in + cu_servo + cu_gnd:.4f} ohm'},
    },
    'regulators': {
        'ldo3v3': {'type': 'ldo', 'input': 'usb5v', 'v_out': 3.3, 'dropout_v': 1.1, 'c_out_f': 22e-6,
                   'i_q_a': 0.005, 'note': 'AMS1117-3.3 class on the dev board (assumed)'},
    },
}
prj.data['mcu'].update({'supply': 'ldo3v3', 'current': {'run_a': 0.05, 'reset_a': 0.02},
                        'brownout': {'v_trip': 2.43, 'v_release': 2.53,
                                     'note': 'ESP32 brown-out level 0 (CONFIG_ESP_BROWNOUT_DET_LVL_SEL_0)'}})
prj.save()

# 6. Pin bindings: GPIO18 -> J2.2 PWM_IN -> R5 -> J3.3 servo signal; BOOT button on GPIO0.
#    Servo supply as wired on the bench: red wire on the dev board's 5V pin (USB),
#    not on J3/J1 as the PCB intends. This is the configuration whose fault the
#    example reproduces; the fix is `set_param actuators.servo1.node v5_servo`.
api.bind_pin(prj, 'GPIO18', actuator='servo1', actuator_type='servo', joint='servo_axis',
             supply_node='usb5v', preset='sg90', net='PWM_IN')
prj.data['actuators']['servo1']['note'] = ('bench wiring: servo + on the dev board 5V pin (shares the USB port '
                                           'with the ESP32); the PCB routes it from J1 via F1 (node v5_servo)')
api.bind_pin(prj, 'GPIO0', input='button1', active='low')
prj.save()
# 7. Printable parts (mechanical/PRINT_*.stl, exported in print orientation) for
#    "send to Bambu Studio". Settings are suggestions, not tested prints.
prj.data['printing'] = {'parts': {
    'press_arm': {'file': '../../../mechanical/PRINT_03_double_press_arm.stl', 'label': '按压臂', 'material': 'PETG',
                  'settings': {'layer_height': 0.16, 'wall_loops': 4, 'sparse_infill_density': '40%'},
                  'note': 'bar flat on the bed, fingers up, no supports (mechanical/README.md); '
                          'settings are suggestions pending the strength check'},
    'frame': {'file': '../../../mechanical/PRINT_01_86_adjustable_frame.stl', 'label': '86 盒框', 'material': 'PLA',
              'settings': {'layer_height': 0.2, 'wall_loops': 3, 'sparse_infill_density': '20%'}},
    'servo_cradle': {'file': '../../../mechanical/PRINT_02_strap_servo_cradle.stl', 'label': '舵机支架',
                     'material': 'PETG', 'settings': {'layer_height': 0.2, 'wall_loops': 3,
                                                      'sparse_infill_density': '25%'}},
    'gateway_case': {'file': '../../../mechanical/PRINT_04_gateway_case.stl', 'label': '网关盒', 'material': 'PLA',
                     'settings': {'layer_height': 0.2, 'wall_loops': 2, 'sparse_infill_density': '15%'}},
    'gateway_lid': {'file': '../../../mechanical/PRINT_05_gateway_lid.stl', 'label': '网关盒盖', 'material': 'PLA',
                    'settings': {'layer_height': 0.2, 'wall_loops': 2, 'sparse_infill_density': '15%'}},
}}
prj.save()
# 8. The board people actually order is the V0.5 carrier (electronics/v05, ESP32-C3 on
#    board, USB-C input), powered by a USB-C phone charger in the wall socket. Steps 5-6
#    above keep the V0.4 numbers for the PCB import; the default network is V0.5. The
#    stall_brownout scenario overrides it back to the bench wiring to reproduce the fault.
prj.data['power'] = {
    'nodes': {
        'vin5': {'c_f': 30e-6, 'net': 'VIN5', 'note': 'C3 + C4 (10 uF) + USB-C VBUS decoupling'},
        'v5_servo': {'c_f': c_servo, 'net': 'V5_SERVO', 'note': 'C1 470 uF + C2 100 nF'},
    },
    'sources': {
        'charger': {'node': 'vin5', 'v': 4.75, 'r_ohm': 0.3, 'i_limit_a': 2.0,
                    'note': 'assumed: 5.1 V 2 A USB charger, 1 m USB-C cable (0.2 ohm round trip), SS34 drop '
                            'modelled as a fixed 0.35 V + 0.1 ohm (forward bias only; it blocks back-feed into USB)'},
    },
    'branches': {
        'f1': {'from': 'vin5', 'to': 'v5_servo', 'r_ohm': round(0.15 + cu_in + cu_servo + cu_gnd, 4),
               'note': f'RUEF110 cold resistance 0.15 ohm (assumed) + PCB copper {cu_in + cu_servo + cu_gnd:.4f} ohm'},
    },
    'regulators': {
        'ldo3v3': {'type': 'ldo', 'input': 'vin5', 'v_out': 3.3, 'dropout_v': 1.1, 'c_out_f': 20e-6,
                   'i_q_a': 0.005, 'note': 'U4 AMS1117-3.3, C5 10 uF + module decoupling'},
    },
}
prj.data['actuators']['servo1']['node'] = 'v5_servo'
prj.data['actuators']['servo1']['note'] = ('V0.5: J3 pin 2 (5 V after F1). The bench wiring (servo on the dev board '
                                           '5V pin) is reproduced by the stall_brownout scenario overrides')
prj.save()
# 9. The V0.5 board through KiCad (the PCB kernel). Source: 嘉立创EDA's own exports of the V0.5
#    project (.epro and Protel netlist, electronics/v05/export on branch claude/v05-eda-export).
#    KiCad's importer turns the .epro into a .kicad_pcb; the netlist fills in pad nets it misses.
#    Re-imported only when KiCad is installed; the committed board is used otherwise.
from emcosim import eda  # noqa: E402
v05 = HERE / 'pcb' / 'v05'
if eda.find_cli():
    print(eda.import_board(v05 / 'source' / 'Switch_Robot_Carrier_V05.epro', v05 / 'carrier_v05.kicad_pcb',
                           netlist=v05 / 'source' / 'carrier_v05.net'))
prj.data['board'] = {'kicad_pcb': 'pcb/v05/carrier_v05.kicad_pcb', 'netlist': 'pcb/v05/source/carrier_v05.net',
                     'source': 'pcb/v05/source/Switch_Robot_Carrier_V05.epro', 'rules': 'JLCPCB 2-layer (kicad_dru)'}
# 10. Circuit check with ngspice on that netlist: what feeds the board and what the run draws.
prj.data['circuit'] = {
    'netlist': 'pcb/v05/source/carrier_v05.net',
    'supply': {'net': 'VBUS', 'v': 5.1, 'r_ohm': 0.2, 'note': 'assumed: 5.1 V USB charger, 1 m USB-C cable'},
    'loads': {'servo': {'net': 'V5_SERVO', 'signal': 'act.servo1.i_supply'},
              'mcu': {'net': '3V3', 'a': 0.05, 'note': 'ESP32-C3 average draw, same as mcu.current.run_a (assumed)'}},
    'compare': {'VIN5': 'node.vin5.v', 'V5_SERVO': 'node.v5_servo.v', '3V3': 'mcu.rail_v'},
}
prj.save()
print('project.yaml rebuilt;', pcb['components'], 'components,', len(pcb['nets']), 'nets')
