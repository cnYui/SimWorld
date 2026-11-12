"""Regenerate the press-arm strength report (REPORT.md, results.json, PNG plots).

    pip install -e "platform[strength]"
    python platform/examples/switch_robot/strength/run_strength.py

Needs at least one stored run of the stall_brownout scenario under runs/ (git-ignored):
    cd platform/examples/switch_robot && emcosim run stall_brownout
Set EMCOSIM_STRENGTH_RUN=<run_id> to use a specific run; otherwise the stall_brownout run
with the highest contact-force peak is used. Takes about 10 minutes on a 4-core machine.
"""
from __future__ import annotations

import dataclasses
import json
import os
import sys
import time
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parent
sys.path.insert(0, str(PROJECT.parent.parent))  # platform/ (works without pip install -e)

from emcosim import strength  # noqa: E402
from emcosim.strength import PrintSettings, check_part, fillet_study, loads_from_run, to_json  # noqa: E402

MODE_ZH = {'in_plane': '层内', 'interlayer_normal': '层间拉', 'interlayer_shear': '层间剪',
           'interlayer_combined': '层间组合'}


def log(*a):
    print(*a, flush=True)


def arm_case(cfg, material, build, ps, case, loads, mesh_size=None, keep_fields=False):
    fixtures = cfg['fixtures_horn']
    if case == 'press':  # contact force peak from the run, on the finger underside
        f = loads['press_force_peak_n']
    else:  # servo stall torque reacted at the contact tip: F = T / lever (tip at y = 24 mm)
        f = loads['servo_stall_torque_nm'] * 1000.0 / cfg['tip_lever_mm']
    ld = [{'name': case, 'select': cfg['tip_face'], 'force_n': [0, 0, f]}]
    return check_part(PROJECT, cfg['part_mesh'], material, build, fixtures, ld, print_settings=ps,
                      transform=cfg['transform'], unit_scale=cfg.get('unit_scale', 1.0),
                      mesh_size_mm=mesh_size or cfg['mesh_size_mm'], exclude_mm=cfg['exclude_mm'],
                      singular_zones=cfg['singular_zones'], probes=cfg['probes'], corners=cfg['corners'],
                      cache_dir='strength/.cache', keep_fields=keep_fields)


def summary(r):
    return {'min_sf': {m: r['modes'][m]['min_sf'] for m in r['modes']},
            'at_mm': {m: r['modes'][m]['at_mm'] for m in r['modes']},
            'governing': r['governing_mode'], 'max_deflection_mm': r['max_deflection_mm'],
            'tip_deflection_mm': r['probes']['tip']['|u|_mm'], 'max_von_mises_mpa': r['max_von_mises_mpa'],
            'zones': {m: {z: q['min_sf_mesh_dependent'] for z, q in r['modes'][m].get('singular_zones', {}).items()}
                      for m in r['modes']},
            'corners': r.get('corners'), 'mesh': r['mesh'], 'timing_s': r['timing_s'], 'solver': r['solver']}


def main():
    ok, missing = strength.available()
    if not ok:
        sys.exit(f'missing optional deps {missing}: pip install "emcosim[strength]"')
    t_start = time.time()
    cfg = yaml.safe_load((HERE / 'press_arm.yaml').read_text(encoding='utf8'))
    run_id = os.environ.get('EMCOSIM_STRENGTH_RUN')
    loads = loads_from_run(PROJECT, run_id=run_id, scenario=cfg['run']['scenario'], pick='worst')
    log('loads', loads)
    base = PrintSettings(**cfg['print_settings'])
    out = {'loads': loads, 'print_settings': base.describe(), 'matrix': {}, 'config': cfg}

    # ---- 1. material x orientation x load case
    fields = {}
    for mat in cfg['materials']:
        for oname, (bdir, _desc) in cfg['orientations'].items():
            for case in ('press', 'torque'):
                keep = (mat == 'PLA' and case == 'press')
                r = arm_case(cfg, mat, bdir, base, case, loads, keep_fields=keep)
                if keep:
                    fields[oname] = r.pop('_fields')
                out['matrix'][f'{mat}|{oname}|{case}'] = summary(r)
                out['part'] = r['part']
                log(mat, oname, case, round(r['min_sf'], 2), r['governing_mode'],
                    round(r['max_deflection_mm'], 4), r['timing_s'], r['solver'])

    # ---- 2. fillet study on the built-in L-bracket (PLA; three build directions)
    out['fillet'] = {}
    for bname, conv in (('x', True), ('z', False), ('y', False)):
        fs = fillet_study('PLA', bname, print_settings=base, convergence=conv)
        out['fillet'][bname] = fs
        log('fillet', bname, [(r['r_mm'], round(r['Kt'], 2)) for r in fs['rows']], fs['time_s'])
    fs_petg = fillet_study('PETG', 'x', print_settings=base, convergence=False)
    out['fillet']['x_PETG'] = fs_petg

    # ---- 3. print-settings sweep on the worst load case in the better orientation
    out['sweep'] = {}
    for mat in cfg['materials']:
        best_o, worst_case, best_sf = None, None, -1
        for oname in cfg['orientations']:
            cases = {c: out['matrix'][f'{mat}|{oname}|{c}']['min_sf'] for c in ('press', 'torque')}
            c_min = min(cases, key=lambda c: min(cases[c].values()))
            sf = min(cases[c_min].values())
            if sf > best_sf:
                best_o, worst_case, best_sf = oname, c_min, sf
        bdir = cfg['orientations'][best_o][0]
        rows = []
        combos = [(lh, nz, 3) for nz in (0.2, 0.4, 0.6) for lh in (0.12, 0.2, 0.28)]
        combos += [(0.2, 0.4, w) for w in (2, 4)]
        for lh, nz, w in combos:
            ps = dataclasses.replace(base, layer_height_mm=lh, nozzle_mm=nz, walls=w, infill_pct=20.0)
            r = arm_case(cfg, mat, bdir, ps, worst_case, loads, mesh_size=cfg['sweep_mesh_size_mm'])
            rows.append({'layer_height_mm': lh, 'nozzle_mm': nz, 'walls': w, 'printable': ps.printable,
                         'interlayer_factor': ps.interlayer_factor(strength.get_material(mat)),
                         'notch_factor': ps.layer_notch_factor, 'wall_mm': ps.wall_thickness_mm,
                         'min_sf': {m: r['modes'][m]['min_sf'] for m in r['modes']},
                         'governing': r['governing_mode'], 'tip_deflection_mm': r['probes']['tip']['|u|_mm'],
                         'shell_fraction': r['mesh']['shell_fraction_of_volume']})
            log('sweep', mat, best_o, worst_case, lh, nz, w, round(r['min_sf'], 2), r['governing_mode'])
        out['sweep'][mat] = {'orientation': best_o, 'case': worst_case, 'rows': rows}

    out['elapsed_s'] = round(time.time() - t_start, 1)
    to_json(out, HERE / 'results.json')
    plots(out, fields)
    write_report(out, cfg)
    log('done in', out['elapsed_s'], 's')


# --------------------------------------------------------------------------- plots

C1, C2, C3 = '#2a78d6', '#eb6834', '#1baf7a'


def _style(ax):
    for s in ('top', 'right'):
        ax.spines[s].set_visible(False)
    ax.grid(True, color='#e4e3df', linewidth=0.8)
    ax.set_axisbelow(True)


def plots(out, fields):
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        import numpy as np
    except ImportError:
        log('matplotlib missing, no plots')
        return
    # fillet: Kt vs r (three build dirs give nearly the same Kt; show x) + convergence
    fs = out['fillet']['x']
    fig, axs = plt.subplots(1, 2, figsize=(9, 3.6))
    r = [row['r_mm'] for row in fs['rows']]
    kt = [row['Kt'] for row in fs['rows']]
    axs[0].plot(r, kt, '-o', color=C1, lw=2, ms=6)
    for x, y in zip(r, kt):
        axs[0].annotate(f'{y:.2f}', (x, y), textcoords='offset points', xytext=(6, 4), fontsize=8)
    axs[0].set_xlabel('fillet radius r [mm]')
    axs[0].set_ylabel('Kt (max principal / 6PL/bh²)')
    axs[0].set_title('L-bracket inside corner', fontsize=10)
    _style(axs[0])
    for rr, col in ((0.0, C2), (1.0, C1)):
        cv = [c for c in fs['convergence'] if c['r_mm'] == rr]
        if cv:
            axs[1].plot([c['h_corner_mm'] for c in cv], [c['Kt'] for c in cv], '-o', color=col, lw=2, ms=6,
                        label='sharp (r=0)' if rr == 0 else f'r={rr:g} mm')
    axs[1].set_xscale('log')
    axs[1].invert_xaxis()
    axs[1].set_xlabel('element size at corner [mm] (finer →)')
    axs[1].set_ylabel('Kt')
    axs[1].set_title('mesh convergence', fontsize=10)
    axs[1].legend(frameon=False, fontsize=8)
    _style(axs[1])
    fig.tight_layout()
    fig.savefig(HERE / 'fillet_kt.png', dpi=130)
    plt.close(fig)

    # sweep: min SF vs layer height per nozzle
    fig, axs = plt.subplots(1, len(out['sweep']), figsize=(9, 3.6), sharey=True)
    axs = np.atleast_1d(axs)
    for ax, (mat, sw) in zip(axs, out['sweep'].items()):
        for nz, col in ((0.2, C1), (0.4, C2), (0.6, C3)):
            rows = [x for x in sw['rows'] if x['nozzle_mm'] == nz and x['walls'] == 3]
            xs = [x['layer_height_mm'] for x in rows]
            ys = [min(x['min_sf'].values()) for x in rows]
            ax.plot(xs, ys, '-o', color=col, lw=2, ms=6, label=f'nozzle {nz} mm')
            for x in rows:
                if not x['printable']:
                    ax.plot(x['layer_height_mm'], min(x['min_sf'].values()), 'x', color='#555', ms=9)
        ax.set_title(f'{mat}, {sw["orientation"]}, {sw["case"]}', fontsize=10)
        ax.set_xlabel('layer height [mm]  (x = not printable)')
        _style(ax)
    axs[0].set_ylabel('min safety factor (all modes)')
    axs[0].legend(frameon=False, fontsize=8)
    fig.tight_layout()
    fig.savefig(HERE / 'sweep_sf.png', dpi=130)
    plt.close(fig)

    # arm: von Mises on the y-z side view, PLA press case, as-exported orientation
    F = fields.get('Z_as_exported')
    if F is not None:
        X, vm = F['X'], F['vm']
        o = np.argsort(vm)
        fig, ax = plt.subplots(figsize=(9, 2.6))
        sc = ax.scatter(X[o, 1], X[o, 2], c=vm[o], s=4, cmap='Blues', vmin=0, vmax=np.percentile(vm, 99.5))
        ax.set_aspect('equal')
        ax.set_xlabel('y [mm]')
        ax.set_ylabel('z [mm]')
        ax.set_title('press arm, PLA, press force on +y finger: von Mises [MPa] (side view, all depths)',
                     fontsize=9)
        fig.colorbar(sc, ax=ax, shrink=0.8)
        for s in ('top', 'right'):
            ax.spines[s].set_visible(False)
        fig.tight_layout()
        fig.savefig(HERE / 'arm_vm.png', dpi=130)
        plt.close(fig)


# --------------------------------------------------------------------------- report

def f2(x):
    if x is None:
        return '-'
    return '>99' if x > 99 else f'{x:.1f}'


def write_report(out, cfg):
    (HERE / 'REPORT.md').write_text(render(out, cfg), encoding='utf8')


def render(out, cfg):
    L = out['loads']
    M = out['matrix']
    ps = out['print_settings']
    mats = cfg['materials']
    onames = list(cfg['orientations'])
    f_t = L['servo_stall_torque_nm'] * 1000 / cfg['tip_lever_mm']
    from emcosim.strength import MATERIALS
    lines = []
    A = lines.append

    def mins(key):
        return min(M[key]['min_sf'].values())

    press_rows = {(m, o): M[f'{m}|{o}|press'] for m in mats for o in onames}
    worst_key = min(press_rows, key=lambda k: min(press_rows[k]['min_sf'].values()))
    best = {m: max(onames, key=lambda o: mins(f'{m}|{o}|press')) for m in mats}

    A('# 按压臂强度校核（FDM 打印件，线弹性静力有限元）')
    A('')
    A(f'对象是 V0.6 双指按压臂 PRINT_03_double_press_arm.stl，用 emcosim.strength 做了线弹性静力有限元：二次四面体，'
      f'材料按 FDM 的横观各向同性处理（层内和沿打印方向两套参数），外壁和顶底层按实体、内部填充按 '
      f'{ps["infill_pct"]:.0f}% {ps["infill_pattern"]} 折减刚度和强度，失效按层内、层间拉、层间剪和层间组合四种方式分别算安全系数。'
      f'载荷取自仿真运行 {L["run_id"]}（{L["scenario"]}，9 N 硬开关、舵机堵转）。'
      f'本报告由 run_strength.py 生成，所有数值都依赖下面列出的假设，没有任何实物测试。')
    A('')
    A(f'结论先说：在 {L["press_force_peak_n"]:.2f} N 的接触力峰值下，按压臂本体不是薄弱环节。'
      f'两种材料、三种打印朝向里最小安全系数是 {f2(min(press_rows[worst_key]["min_sf"].values()))}'
      f'（{worst_key[0]}，{worst_key[1]}，{MODE_ZH[press_rows[worst_key]["governing"]]}），'
      f'其余都更高；指尖最大变形不到 {max(r["max_deflection_mm"] for r in press_rows.values()) * 1000:.0f} µm。'
      f'朝向的影响明显：竖着打（Y_upright）时棒材的弯曲应力正好垂直于层面，层间拉成为控制模式，安全系数降到平放的一半左右；'
      f'现在 STL 的导出朝向（Z_as_exported，棒材 4×56 面贴床、指杆朝上）和侧放（X_on_side）都让弯曲应力落在层内，二者差别不大。'
      f'真正需要担心的是长期蠕变（PLA）、舵盘和螺丝的连接、以及指杆根部的尖角，后面分别说明。')
    A('')
    A('## 载荷')
    A('')
    A(f'loads_from_run 读取 runs/<id>/run.json 和 signals.npz：probe.press.force_n（压臂碰撞体与翘板碰撞体的接触力）峰值 '
      f'{L["press_force_peak_n"]:.2f} N，出现在 t = {L["press_force_peak_t_s"]:.3f} s；act.servo1.torque 的峰值 '
      f'{L["servo_torque_peak_nm"]:.4f} N·m。舵机预设 {L.get("servo_preset")} 在 {L["stall_supply_v"]:.1f} V 下按 V/R·ke·减速比·效率 '
      f'算出的堵转力矩是 {L["servo_stall_torque_nm"]:.4f} N·m（motors.py 注释给的是 0.1765 N·m）。'
      f'工况一（press）把接触力峰值作为指杆底面（压垫粘接面）上的均布力，方向沿 +z（翘板把指杆往上顶）；'
      f'工况二（torque）把堵转力矩换算成同一接触点的等效力 T / 24 mm = {f_t:.2f} N。'
      f'接触力峰值比堵转等效力大，是因为撞上硬开关那一刻有臂和齿轮系的惯性冲击，所以 press 是控制工况。'
      f'两个工况都在舵盘处固定：三个孔（中心 r1.25、y = ±9 的两个 r1.1）的孔壁全约束，相当于螺丝和孔壁粘死，'
      f'舵盘端面的接触和螺丝预紧都没建；这让孔边的应力是理想化的，所以孔和加载面 {cfg["exclude_mm"]:.1f} mm 以内的节点不参与最小安全系数统计（结果里另存了含这些点的原始值）。')
    A('')
    A('## 材料和打印参数（全部是假设值）')
    A('')
    A('材料表在 emcosim/strength/materials.py，取自 Prusament、Bambu Lab 等材料数据表里 XY 与 Z 方向拉伸样条的典型值和几篇文献的范围，'
      '对应 0.2 mm 层高、0.4 mm 喷嘴、实心样条，都没有在本项目实测，实际离散度在 ±30% 以上。层间拉伸强度取层内的 30%~60%。')
    A('')
    A('| 材料 | E_xy MPa | E_z MPa | G_xz MPa | 层内拉 MPa | 层内压 MPa | 层间拉 MPa | 层间剪 MPa | 软化温度 °C |')
    A('|---|---|---|---|---|---|---|---|---|')
    for n, m in MATERIALS.items():
        A(f'| {n} | {m["E_xy"]:.0f} | {m["E_z"]:.0f} | {m["G_xz"]:.0f} | {m["Xt"]:.0f} | {m["Xc"]:.0f} | {m["Zt"]:.0f} | '
          f'{m["S_il"]:.0f} | {m["T_soften_c"]:.0f} |')
    A('')
    A('PLA 在室温下就会蠕变，持续受力（比如压臂长期顶住开关、或者舵机保持力矩）时变形会慢慢增加；接近玻璃化温度（约 55~60 °C）明显变软，'
      '夏天车里或靠近发热器件时，远低于上表的应力也可能让零件下垂。这个校核只算短时静力，没有蠕变和疲劳，所以对 PLA 持续载荷建议安全系数至少 3~4。'
      'PETG 软化温度更高、更韧，蠕变比 PLA 小。')
    A('')
    A(f'基准打印参数：层高 {ps["layer_height_mm"]} mm，喷嘴 {ps["nozzle_mm"]} mm（线宽 {ps["line_width"]:.2f} mm），'
      f'{ps["walls"]} 圈外壁（{ps["wall_thickness_mm"]:.2f} mm），顶底各 {ps["top_bottom_layers"]} 层（{ps["top_bottom_thickness_mm"]:.2f} mm），'
      f'{ps["infill_pct"]:.0f}% {ps["infill_pattern"]} 填充。打印参数进入模型的方式（PrintSettings，均为假设关系）：'
      '离表面不到外壁厚度（侧面）或顶底层厚度（法向接近打印方向的面）的区域按实体算，其余是填充芯，刚度乘 ρ^n、强度乘 ρ^m（ρ 为填充率，'
      'grid 取 n = m = 1，即按实体比例混合；gyroid 取 1.6/1.5）；层间强度随层高/喷嘴比变化，比值 0.5 时为 1，系数 1 + 0.6×(0.5 − h/d)，限制在 0.5~1.15，'
      '可选的喷嘴温度每高 1 °C 加 0.5%；层高超过喷嘴直径 80% 视为不可打印；侧壁上的层纹按浅缺口 Kt = 1 + 2√(0.12h/0.05 mm)、静载缺口敏感系数 0.3 折算，'
      '相对 0.2 mm 层高放大侧壁表面垂直于层面的拉应力；内圆角能可靠做出的最小半径取喷嘴直径的一半（圆角在层面内由走线形成时），'
      '圆角靠层的台阶堆出来时取两倍层高。')
    A('')
    A('失效判据在材料坐标系（1、2 在层面内，3 沿打印方向）里逐点计算：层内用层面内两个主应力分别对比层内拉、压强度；层间拉用 σ33（只算拉）对比层间拉伸强度；'
      '层间剪用 √(σ13² + σ23²) 对比层间剪切强度；层间组合用二次式 (σ33⁺/Zt)² + (τ/S)² = 1/SF²（Hoffman/Tsai-Hill 在层面上的简化形式）。'
      '安全系数 = 强度 / 应力，按节点（二次单元的顶点和棱中点，相邻单元应变取平均）取最小值。')
    A('')
    A('## 按压臂结果')
    A('')
    A('三种朝向（打印方向写在零件坐标系里：x 是舵机轴、棒材厚 4 mm；y 沿棒材；z 向上，指杆朝 −z）：')
    A('')
    for o in onames:
        A(f'- {o}：打印方向 {cfg["orientations"][o][0]}，{cfg["orientations"][o][1]}')
    A('')
    A('表中是排除孔边和加载面 1 mm 后的最小安全系数（>99 表示该模式基本不受力），变形是全零件最大位移。')
    A('')
    A('| 材料 | 朝向 | 工况 | 层内 | 层间拉 | 层间剪 | 层间组合 | 控制模式 | 最大变形 mm |')
    A('|---|---|---|---|---|---|---|---|---|')
    for m in mats:
        for o in onames:
            for c in ('press', 'torque'):
                r = M[f'{m}|{o}|{c}']
                sf = r['min_sf']
                A(f'| {m} | {o} | {c} | {f2(sf["in_plane"])} | {f2(sf["interlayer_normal"])} | {f2(sf["interlayer_shear"])} | '
                  f'{f2(sf["interlayer_combined"])} | {MODE_ZH[r["governing"]]} | {r["max_deflection_mm"]:.3f} |')
    A('')
    k0 = f'{mats[0]}|{onames[0]}|press'
    at = M[k0]['at_mm']['in_plane']
    A(f'最小值都出现在棒材靠近舵盘的一段：y 约 {abs(at[1]):.0f} mm、棒材上下表面附近（例如 {mats[0]} {onames[0]} 层内模式在 '
      f'({at[0]:.1f}, {at[1]:.1f}, {at[2]:.1f}) mm），也就是舵盘螺丝孔外侧、悬臂弯矩最大的位置。'
      f'手算校核：力 {L["press_force_peak_n"]:.1f} N、到 y = 11 mm 的力臂 13 mm，4×6 mm 截面弯曲应力约 '
      f'{L["press_force_peak_n"] * 13 / (4 * 36 / 6):.1f} MPa，与有限元一致。'
      '剪切和层间组合模式的最小值在中间填充芯里：填充芯强度按 20% 折减，所以即使应力不高也会先到。'
      f'朝向的差别：{mats[0]} 最好的朝向是 {best[mats[0]]}，{mats[1]} 是 {best[mats[1]]}；Y_upright 时层间拉控制，安全系数最低。'
      'PETG 的刚度低，变形比 PLA 大约 60%，但强度表里两者层间强度接近，安全系数差别不大。')
    A('')
    zinfo = M[k0]['zones']
    zvals = [v for mz in zinfo.values() for v in mz.values() if v is not None and v < 1e6]
    cflags = M[f'{mats[0]}|{onames[0]}|press']['corners']['finger_root']
    A(f'指杆根部（指杆内侧面 y = ±20.5 mm 与棒材下表面 z = −3 mm 的交线）是尖的内角，有限元在那里的峰值随网格加密不断变大，不是真实应力，'
      f'所以 1 mm 范围内单独统计，不进上表（当前网格下该区域最小安全系数约 {f2(min(zvals)) if zvals else "-"}，只作参考）。'
      f'在 Z_as_exported 和 Y_upright 两种朝向里，这条棱的一个面正好是层面（{"是" if cflags["on_layer_boundary"] else "否"}在层分界上），'
      '也就是应力集中点和层间熔接线重合，这是最不利的组合；X_on_side 时棱平行于打印方向，圆角由走线形成。'
      '不过这个角离加载点只有 3.5 mm，弯矩很小（约 9 N × 3.5 mm ≈ 31 N·mm，名义应力约 1.3 MPa），即使按下面圆角研究里尖角附近的 Kt ≈ 3~4 估算也只有 4~5 MPa，'
      '不控制强度。如果以后加长指杆或改成侧向受力，这个角要加圆角。')
    A('')
    A('![按压臂 von Mises 应力，侧视](arm_vm.png)')
    A('')
    A('## 内角圆角研究')
    A('')
    fx = out['fillet']['x']
    g = fx['geometry_mm']
    A(f'尖内角是奇异点，峰值没有意义，所以用一个内置的参数化 L 形支架代表这类内角：立壁 {g["W"]:.0f}×{g["H"]:.0f} mm 底面固定，'
      f'悬臂 {g["L"]:.0f} mm 长、{g["h"]:.0f} mm 高、{g["b"]:.0f} mm 宽，端面受 {fx["load_n"]:.0f} N 向上的力，使内角受拉；'
      f'gmsh OCC 生成精确的圆弧圆角并在圆角处局部加密。Kt 定义为圆角附近最大主应力除以悬臂根部名义弯曲应力 6PL/(bh²) = {fx["sigma_nom_mpa"]:.2f} MPa。'
      f'半径默认取喷嘴直径的一半（{ps["min_inside_radius_mm"]:.1f} mm，可靠做出的最小内圆角）、0.4（线宽量级）、1、2、3 mm。'
      '表里的安全系数只对应这个 10 N 的示例载荷（名义应力已经 18.75 MPa，远高于按压臂上的应力），用来比较不同半径和朝向的相对差别，不是按压臂的安全系数。Kt 是几何量，三种打印方向下只因刚度各向异性略有差别；安全系数下表用 PLA、打印方向沿悬臂轴（x），这时立壁面是层面、最大主应力几乎垂直于层面，是最坏组合。')
    A('')
    A('| r mm | Kt | 峰值应力 MPa | 层内 SF | 层间拉 SF | 层间组合 SF | 角在层分界上 | 峰值方向垂直层面 |')
    A('|---|---|---|---|---|---|---|---|')
    for row in fx['rows']:
        md = row['modes']
        A(f'| {row["r_mm"]:g} | {row["Kt"]:.2f} | {row["sigma1_peak_mpa"]:.1f} | {f2(md["in_plane"]["min_sf"])} | '
          f'{f2(md["interlayer_normal"]["min_sf"])} | {f2(md["interlayer_combined"]["min_sf"])} | '
          f'{"是" if row["flags"]["on_layer_boundary"] else "否"} | {"是" if row["flags"]["peak_stress_across_layers"] else "否"} |')
    A('')
    other = []
    for b in ('z', 'y'):
        if b in out['fillet']:
            rows = out['fillet'][b]['rows']
            other.append(f'打印方向 {b} 时 Kt 为 ' + '、'.join(f'{r["Kt"]:.2f}' for r in rows) +
                         f'（同样的半径顺序），r = 1 mm 的最小安全系数 '
                         f'{f2(min(next(r for r in rows if r["r_mm"] == 1.0)["modes"][k]["min_sf"] for k in ("in_plane", "interlayer_normal", "interlayer_combined")))}'
                         f'，角{"在" if rows[0]["flags"]["on_layer_boundary"] else "不在"}层分界上')
    A('对照：' + '；'.join(other) + '。打印方向 z 时悬臂下表面是层面，角也在层分界上，但峰值应力沿悬臂方向、落在层内；'
      '打印方向 y 时圆角棱平行于打印方向，圆角由每层的走线形成，半径小于喷嘴一半时做不出来。'
      'PETG 在最坏方向（x）的结果见 results.json 的 fillet.x_PETG。')
    A('')
    conv = fx['convergence']
    A('网格收敛：r = 1 mm 时把圆角处单元尺寸从 r/2 加密到 r/8，Kt 为 ' +
      '、'.join(f'{c["Kt"]:.2f}（{c["h_corner_mm"]:.3g} mm）' for c in conv if c['r_mm'] == 1.0) +
      '，基本收敛；尖角（r = 0）从 0.4 mm 加密到 0.05 mm，"Kt" 为 ' +
      '、'.join(f'{c["Kt"]:.2f}' for c in conv if c['r_mm'] == 0.0) +
      '，一直上升不收敛，说明尖角的峰值只反映网格尺寸，不能直接拿来判断强度。实际打印出的尖角也不是数学上的尖角，'
      '而是一个大小不受控的小圆角加层纹缺口，所以设计上应给出明确的圆角，0.4 mm 打印时至少 1 mm，能放下 2 mm 更好：从表上看 r 从 0.4 增到 2 mm，Kt 大约减半。')
    A('')
    A('![圆角 Kt 与网格收敛](fillet_kt.png)')
    A('')
    A('## 打印参数扫描')
    A('')
    A(f'对按压臂的 press 工况、各材料最好的朝向，扫描层高 {{0.12, 0.2, 0.28}} mm × 喷嘴 {{0.2, 0.4, 0.6}} mm（3 圈外壁），以及 0.4 mm 喷嘴、0.2 mm 层高下外壁 {{2, 3, 4}} 圈，'
      f'填充都是 20%。为了控制时间，扫描用更粗的网格（目标棱长 {cfg["sweep_mesh_size_mm"]} mm，主表是 {cfg["mesh_size_mm"]} mm），'
      '与主表同一参数下的数值会有几个百分点的差别。表中是所有模式里的最小安全系数和对应模式；0.2 mm 喷嘴配 0.28 mm 层高超过喷嘴 80%，标为不可打印，仅作外推参考。')
    A('')
    for m, sw in out['sweep'].items():
        A(f'{m}（{sw["orientation"]}，{sw["case"]}）：')
        A('')
        A('| 喷嘴 mm | 外壁 | 外壁厚 mm | 层高 0.12 | 层高 0.2 | 层高 0.28 |')
        A('|---|---|---|---|---|---|')
        for nz in (0.2, 0.4, 0.6):
            rows = [r for r in sw['rows'] if r['nozzle_mm'] == nz and r['walls'] == 3]
            cells = []
            for r in rows:
                c = f'{min(r["min_sf"].values()):.1f} {MODE_ZH[r["governing"]]}'
                if not r['printable']:
                    c += '（不可打印）'
                cells.append(c)
            A(f'| {nz} | 3 | {rows[0]["wall_mm"]:.2f} | ' + ' | '.join(cells) + ' |')
        for w in (2, 3, 4):
            r = next(r for r in sw['rows'] if r['nozzle_mm'] == 0.4 and r['walls'] == w and r['layer_height_mm'] == 0.2)
            A(f'| 0.4 | {w} | {r["wall_mm"]:.2f} | - | {min(r["min_sf"].values()):.1f} {MODE_ZH[r["governing"]]} | - |')
        A('')
    A(sweep_text(out))
    A('')
    A('![打印参数扫描](sweep_sf.png)')
    A('')
    A('## 局限和没有算的东西')
    A('')
    A('材料数值全是文献和数据表的典型值，没有本项目的拉伸样条；不同品牌、颜色、受潮状态、打印机和冷却风扇都会让层间强度变化很大，层间数值的不确定性最大。'
      '打印参数的影响（层高、温度、填充、层纹缺口）是按趋势设定的简单关系，量级是估计，没有拟合实验。'
      '模型是小变形线弹性静力，没有塑性、蠕变、疲劳、冲击速率效应和温度；按压是反复动作（每天若干次），PLA 的长期蠕变和疲劳可能比这里的静强度更早出问题。'
      '固定方式是理想化的：孔壁全约束比真实的螺丝加塑料舵盘更硬，真实情况下舵盘本身、M2 自攻螺丝在 PLA 孔里的拔出、孔边挤压可能先坏，这些都没有算，孔边 1 mm 内的结果不可信。'
      '载荷方向取零件坐标系的 +z，没有考虑按压角度（约 17°）带来的侧向分量和摩擦；压垫是外购弹性体，只作为受力面。'
      '打印缺陷（欠挤出、气孔、层间开裂、翘曲、拉丝）都不在模型里，填充芯按均匀连续体折算，壳/芯边界由节点到表面的距离判断，分辨率约为网格尺寸。'
      f'网格：fTetWild 由 STL 生成，体积误差 {abs(out["part"]["volume_mm3"] / out["part"]["surface_volume_mm3"] - 1) * 100:.1f}%（STL 是闭合的；不闭合时 fTetWild 用缠绕数判断内外并给出警告）。')
    A('')
    A('## 复现')
    A('')
    A('    pip install -e "platform[strength]"')
    A('    cd platform/examples/switch_robot && emcosim run stall_brownout      # 生成 runs/（不进 git）')
    A('    python strength/run_strength.py                                       # 约 10 分钟，4 核')
    A('')
    A(f'设置 EMCOSIM_STRENGTH_RUN=<run_id> 可指定运行；默认取 stall_brownout 运行里接触力峰值最大的一次。'
      f'配置在 strength/press_arm.yaml（零件坐标变换、固定、加载面、尖角区域），完整数值在 results.json。本次总用时 {out["elapsed_s"] / 60:.1f} 分钟。')
    A('')
    return '\n'.join(lines)


def sweep_text(out):
    parts = []
    for m, sw in out['sweep'].items():
        rows = {(r['layer_height_mm'], r['nozzle_mm'], r['walls']): r for r in sw['rows']}
        def sf(k):
            return min(rows[k]['min_sf'].values())
        parts.append(f'{m}：0.4 mm 喷嘴下层高 0.12 → 0.28 mm，最小安全系数 {sf((0.12, 0.4, 3)):.1f} → {sf((0.28, 0.4, 3)):.1f}；'
                     f'0.2 mm 层高下喷嘴 0.2 → 0.6 mm，{sf((0.2, 0.2, 3)):.1f} → {sf((0.2, 0.6, 3)):.1f}；'
                     f'外壁 2 → 4 圈，{sf((0.2, 0.4, 2)):.1f} → {sf((0.2, 0.4, 4)):.1f}')
    return ('；'.join(parts) + '。在这个零件上，外壁圈数和喷嘴（决定外壁厚度）影响最大：棒材只有 4 mm 厚，'
            '外壁变厚后填充芯变小甚至消失，控制模式从填充芯里的层间剪切转到外壁上的层内应力；'
            '层高主要通过层间强度系数和层纹缺口起作用，在层间不控制的朝向上影响较小；0.4 mm 喷嘴下层高越小安全系数反而略低，是因为顶底层数固定为 4 层，0.12 mm 层高时顶底实体只有 0.48 mm，填充芯变大（实际可以按厚度而不是层数设顶底层）。'
            '建议按压臂用 0.4 mm 喷嘴、0.2 mm 层高、至少 4 圈外壁（相当于实心），填充率对这么细的零件意义不大。')


if __name__ == '__main__':
    if '--report-only' in sys.argv:
        _out = json.loads((HERE / 'results.json').read_text(encoding='utf8'))
        write_report(_out, _out['config'])
    else:
        main()
