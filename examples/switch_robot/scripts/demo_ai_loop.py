"""Replays the "test -> AI adjusts -> retest" loop through the MCP server, the way
an agent would, and writes the transcript to DEMO.md.

    python scripts/demo_ai_loop.py            # project.yaml is restored afterwards
    python scripts/demo_ai_loop.py --keep     # keep the fix in project.yaml

The decisions below are hard-coded so the run is reproducible; an agent
connected to `emcosim mcp` makes the same calls.
"""
import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

HERE = Path(__file__).resolve().parents[1]
log = []


def note(text):
    log.append(text)
    print(text, flush=True)


async def call(s, tool, **args):
    t0 = time.perf_counter()
    res = await s.call_tool(tool, args)
    text = res.content[0].text
    if getattr(res, 'isError', False) or getattr(res, 'is_error', False):
        raise RuntimeError(f'{tool} failed: {text}')
    data = json.loads(text)
    note(f'\n`{tool}({", ".join(f"{k}={json.dumps(v, ensure_ascii=False)}" for k, v in args.items())})`'
         f'  ({time.perf_counter() - t0:.1f} s)')
    return data


def checks(summary):
    return '; '.join(f"{'✓' if c['pass'] else '✗'} {c['check']} = {c['value']}" for c in summary['checks'])


async def main(keep):
    backup = (HERE / 'project.yaml').read_text(encoding='utf8')
    exe = [sys.executable, '-m', 'emcosim', '--project', str(HERE), 'mcp']
    params = StdioServerParameters(command=exe[0], args=exe[1:], env=dict(os.environ))
    try:
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                note('# 开关机器人：测试 → AI 调参 → 重测（MCP 回放记录）')
                note(f'\n生成时间 {time.strftime("%Y-%m-%d %H:%M")}，由 scripts/demo_ai_loop.py 通过 MCP 调用生成。')

                ov = await call(s, 'get_project', path='actuators.servo1')
                note(f"舵机当前供电节点：{ov['value']['node']}（{ov['value'].get('note', '')}）")

                a = await call(s, 'run_scenario', name='toggle_nominal')
                note(f"正常 3 N 开关：passed={a['passed']}；{checks(a)}")

                b = await call(s, 'run_scenario', name='stall_brownout')
                m = b['metrics']
                note(f"9 N 硬开关：passed={b['passed']}；{checks(b)}")
                note(f"ESP32 复位 {m['mcu.resets']} 次，启动原因 {m['mcu.boot_reasons']}；"
                     f"USB 5V 最低 {m['node.usb5v.min_v']:.2f} V，LDO 3.3V 最低 {m['node.ldo3v3.min_v']:.2f} V，"
                     f"舵机电流峰值 {m['act.servo1.peak_current_a']:.2f} A")
                note('串口：' + ' | '.join(b['mcu_events']))

                sig = await call(s, 'get_results', run_id=b['run_id'], t0=0.8, t1=1.6, max_points=400,
                                 signals=['source.usb.limited', 'node.usb5v.v', 'node.ldo3v3.v', 'mcu.powered'])
                d = sig['data']
                lim = next((t for t, v in zip(d['t'], d['source.usb.limited']) if v > 0.5), None)
                off = next((t for t, v in zip(d['t'], d['mcu.powered']) if v < 0.5), None)
                note(f'时间线：USB 口在 {lim} s 进入限流，ESP32 在 {off} s 被欠压检测复位。')

                note('\n软件侧尝试：减小按压角，降低堵转时的位置误差和电流。')
                sw = await call(s, 'sweep', scenario='stall_brownout', params={'firmware.press_angle_deg': [10, 13, 17]},
                                metrics=['mcu.resets', 'act.servo1.peak_current_a'])
                for row in sw['rows']:
                    note(f"  9 N 开关，press_angle={row['firmware.press_angle_deg']}°：复位 {row['mcu.resets']} 次，"
                         f"电流峰值 {row['act.servo1.peak_current_a']:.2f} A")
                sw2 = await call(s, 'sweep', scenario='toggle_nominal', params={'firmware.press_angle_deg': [10, 13, 17]},
                                 metrics=['joint.rocker_axis.deg.min'])
                for row in sw2['rows']:
                    note(f"  3 N 开关，press_angle={row['firmware.press_angle_deg']}°：passed={row['passed']}，"
                         f"未通过 {row['failed_checks']}")

                both = [r1 for r1, r2 in zip(sw['rows'], sw2['rows']) if r1['mcu.resets'] == 0 and r2['passed']]
                for r1 in both:
                    note(f"  press_angle={r1['firmware.press_angle_deg']}° 两种开关都不复位、3 N 开关能翻，但 9 N 堵转电流 "
                         f"{r1['act.servo1.peak_current_a']:.2f} A，离 USB 口 0.50 A 限流只差 "
                         f"{(0.5 - r1['act.servo1.peak_current_a']) * 1000:.0f} mA；电机和限流值都是假设值，这个余量不可信，不采用。")
                note('\n硬件侧尝试：舵机改接 J3（经 F1 由 J1 外接电源供电，PCB 原本的设计）。')
                hw = await call(s, 'sweep', scenario='stall_brownout',
                                params={'actuators.servo1.node': ['usb5v', 'v5_servo']},
                                metrics=['mcu.resets', 'node.usb5v.min_v', 'node.v5_servo.min_v'])
                for row in hw['rows']:
                    note(f"  node={row['actuators.servo1.node']}：复位 {row['mcu.resets']} 次，USB 5V 最低 "
                         f"{row['node.usb5v.min_v']:.2f} V，J3 5V 最低 {row['node.v5_servo.min_v']:.2f} V")

                ch = await call(s, 'set_param', path='actuators.servo1.node', value='v5_servo')
                note(f"采用硬件修改：{ch['path']}: {ch['old']} → {ch['new']}（写入 project.yaml）")

                c = await call(s, 'run_scenario', name='stall_brownout')
                note(f"重测 9 N 硬开关：passed={c['passed']}；{checks(c)}")
                e = await call(s, 'run_scenario', name='toggle_nominal')
                note(f"重测正常开关：passed={e['passed']}；{checks(e)}")
                note('\n结论：ESP32 欠压重启由“舵机和 ESP32 共用 USB 口”引起，舵机改由 J1 外接电源供电后消失；'
                     '9 N 开关仍然推不动（压垫压到底、SG90 堵转），这与 simulation/README.md 的结论一致，'
                     '要先量真实开关力再选舵机。所有电机、电源和开关参数都是假设值，没有实物标定。')
    finally:
        if not keep:
            (HERE / 'project.yaml').write_text(backup, encoding='utf8')
    (HERE / 'DEMO.md').write_text('\n'.join(log) + '\n', encoding='utf8')
    print('\nwrote', HERE / 'DEMO.md')


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--keep', action='store_true')
    asyncio.run(main(ap.parse_args().keep))
