开关机器人示例：本仓库的 86 开关按压机器人，机械模型直接复用 simulation/model.py（scripts/export_mjcf.py 导出成 mechanics/switch_robot.xml，网格引用 simulation/assets，不复制），PCB 是 electronics/v04 的控制板（pcb/carrier_v04.kicad_pcb，由 EasyEDA Pro 源文件转换），固件是 platform/firmware/switch_robot。

project.yaml 由 build_example.py 生成，步骤和 AI 通过 MCP 做的一样：import_pcb → import_model → import_firmware → define_joint（servo_axis、rocker_axis）→ 电源网络（C1+C2 的 470.1 µF 和 J1→F1→J3 的铜皮电阻取自 PCB）→ bind_pin（GPIO18 经 J2.2/PWM_IN、R5 到 J3.3 舵机信号；GPIO0 接按键）。翘板的双稳态弹簧（3 N 峰值、±3.5°）原来在 model.py 的 Python 代码里，这里成了 project.yaml 的 rocker_detent 元素。

工程里舵机的供电写的是“台架接法”：舵机红线接在开发板的 5V 引脚上，和 ESP32 共用电脑 USB 口（500 mA 限流）。PCB 的设计是舵机从 J3 取电、经 F1 由 J1 外接 5 V 供电，ESP32 另行供电。这样写是为了复现故障，修复就是把 actuators.servo1.node 改成 v5_servo。

三个场景：toggle_nominal 按两次键，3 N 开关先开后关，全部断言通过；stall_brownout 换成 9 N 硬开关，舵机堵转把 USB 口拉进限流，5 V 掉到 1 V 左右，LDO 输出跌到 2.1 V，ESP32 以 BROWNOUT 原因重启；servo_sweep_scripted 不跑固件，直接给 GPIO18 脉宽，用来快速检查机电链路。

DEMO.md 是 scripts/demo_ai_loop.py 通过 MCP 跑出来的完整记录：先复现故障，再扫固件按压角（13° 在两种开关下都过了，但离限流只差约 14 mA，不可信），再扫舵机供电接法，最后 set_param 把舵机改接 J3 并重测。改完后 ESP32 不再重启，正常开关照常工作；9 N 开关仍然推不动，和 simulation/README.md 的结论一致。

所有电机、电源、开关参数都是假设值，标注在 project.yaml 的 note 里。
