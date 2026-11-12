"""A project built from scratch through the API: STEP flywheel on a hinge,
driven by a DC motor behind a low-side switch, scripted MCU."""
import pytest

from emcosim import api
from emcosim.project import Project


def _step_disk(path):
    gmsh = pytest.importorskip('gmsh')
    gmsh.initialize()
    gmsh.option.setNumber('General.Terminal', 0)
    gmsh.model.add('disk')
    gmsh.model.occ.addCylinder(0, 0, 0, 0, 0, 5, 20)      # mm
    gmsh.model.occ.synchronize()
    gmsh.write(str(path))
    gmsh.finalize()


def test_step_flywheel_with_dc_motor(tmp_path):
    pytest.importorskip('cascadio')
    step = tmp_path / 'disk.step'
    _step_disk(step)
    p = Project.create(tmp_path / 'proj', 'flywheel')
    info = api.import_model(p, step, name='disk', density=7800, collision='none')
    assert abs(info['mass_kg'] - 3.14159 * 0.02 ** 2 * 0.005 * 7800) < 0.002
    api.define_joint(p, 'spin', body='disk', type='hinge', axis=[0, 0, 1], damping=1e-5)
    p.data['power'] = {'nodes': {'vbat': {'c_f': 100e-6}},
                       'sources': {'bat': {'node': 'vbat', 'v': 6.0, 'r_ohm': 0.2}}}
    p.data['mechanics']['timestep_s'] = 0.0005
    p.save()
    api.bind_pin(p, 'GPIO5', actuator='m1', actuator_type='dc_motor', joint='spin', supply_node='vbat',
                 preset='tt_motor')
    sc = {'name': 'spin_up', 'duration_s': 1.0, 'mcu_backend': 'scripted',
          'pin_script': [{'t': 0.0, 'pin': 5, 'duty': 1.0, 'freq': 1000}],
          'expect': [{'signal': 'joint.spin.dps', 'at': 1.0, 'gt': 100}]}
    s = api.run_scenario(p, sc)
    assert s['passed'], s['checks']
    no_load_dps = 6.0 / (0.0045 * 48) * 57.2958
    assert s['metrics']['joint.spin.deg.max'] > 100
    assert s['metrics']['act.m1.peak_current_a'] > 1.0          # start-up current ~ V/R
    assert no_load_dps > 1000
