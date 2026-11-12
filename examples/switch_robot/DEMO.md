# 开关机器人：测试 → AI 调参 → 重测（MCP 回放记录）

生成时间 2026-11-12 09:08，由 scripts/demo_ai_loop.py 通过 MCP 调用生成。

`get_project(path="actuators.servo1")`  (0.0 s)
舵机当前供电节点：usb5v（bench wiring: servo + on the dev board 5V pin (shares the USB port with the ESP32); the PCB routes it from J1 via F1 (node v5_servo)）

`run_scenario(name="toggle_nominal")`  (6.3 s)
正常 3 N 开关：passed=True；✓ joint.rocker_axis.deg@2.6s = -3.5; ✓ joint.rocker_axis.deg@4.5s = 3.5; ✓ mcu.resets = 0; ✓ count(EVT done) = 2; ✓ probe.press.force_n.peak = 3.48759

`run_scenario(name="stall_brownout")`  (7.5 s)
9 N 硬开关：passed=False；✗ mcu.resets = 2; ✗ joint.rocker_axis.deg@3.0s = 3.5
ESP32 复位 2 次，启动原因 ['POWERON', 'BROWNOUT']；USB 5V 最低 0.97 V，LDO 3.3V 最低 2.10 V，舵机电流峰值 0.71 A
串口：0.338s boot count=1 reason=POWERON | 0.338s ready | 0.831s press target=ON angle=-17.0 | 1.348s boot count=2 reason=BROWNOUT | 1.348s ready

`get_results(run_id="20261112-090858-975-stall_brownout", t0=0.8, t1=1.6, max_points=400, signals=["source.usb.limited", "node.usb5v.v", "node.ldo3v3.v", "mcu.powered"])`  (0.0 s)
时间线：USB 口在 0.998 s 进入限流，ESP32 在 1.008 s 被欠压检测复位。

软件侧尝试：减小按压角，降低堵转时的位置误差和电流。

`sweep(scenario="stall_brownout", params={"firmware.press_angle_deg": [10, 13, 17]}, metrics=["mcu.resets", "act.servo1.peak_current_a"])`  (15.9 s)
  9 N 开关，press_angle=10°：复位 0 次，电流峰值 0.29 A
  9 N 开关，press_angle=13°：复位 0 次，电流峰值 0.49 A
  9 N 开关，press_angle=17°：复位 2 次，电流峰值 0.71 A

`sweep(scenario="toggle_nominal", params={"firmware.press_angle_deg": [10, 13, 17]}, metrics=["joint.rocker_axis.deg.min"])`  (15.5 s)
  3 N 开关，press_angle=10°：passed=False，未通过 ['joint.rocker_axis.deg@2.6s']
  3 N 开关，press_angle=13°：passed=True，未通过 []
  3 N 开关，press_angle=17°：passed=True，未通过 []
  press_angle=13° 两种开关都不复位、3 N 开关能翻，但 9 N 堵转电流 0.49 A，离 USB 口 0.50 A 限流只差 14 mA；电机和限流值都是假设值，这个余量不可信，不采用。

硬件侧尝试：舵机改接 J3（经 F1 由 J1 外接电源供电，PCB 原本的设计）。

`sweep(scenario="stall_brownout", params={"actuators.servo1.node": ["usb5v", "v5_servo"]}, metrics=["mcu.resets", "node.usb5v.min_v", "node.v5_servo.min_v"])`  (11.3 s)
  node=usb5v：复位 2 次，USB 5V 最低 0.97 V，J3 5V 最低 5.00 V
  node=v5_servo：复位 0 次，USB 5V 最低 4.98 V，J3 5V 最低 4.71 V

`set_param(path="actuators.servo1.node", value="v5_servo")`  (0.0 s)
采用硬件修改：actuators.servo1.node: usb5v → v5_servo（写入 project.yaml）

`run_scenario(name="stall_brownout")`  (4.8 s)
重测 9 N 硬开关：passed=False；✓ mcu.resets = 0; ✗ joint.rocker_axis.deg@3.0s = 3.5

`run_scenario(name="toggle_nominal")`  (6.0 s)
重测正常开关：passed=True；✓ joint.rocker_axis.deg@2.6s = -3.5; ✓ joint.rocker_axis.deg@4.5s = 3.5; ✓ mcu.resets = 0; ✓ count(EVT done) = 2; ✓ probe.press.force_n.peak = 3.491

结论：ESP32 欠压重启由“舵机和 ESP32 共用 USB 口”引起，舵机改由 J1 外接电源供电后消失；9 N 开关仍然推不动（压垫压到底、SG90 堵转），这与 simulation/README.md 的结论一致，要先量真实开关力再选舵机。所有电机、电源和开关参数都是假设值，没有实物标定。
