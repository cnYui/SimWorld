# emcosim v0.1：电子 + 物理联合仿真平台

把 PCB（KiCad）、3D 模型（STL/STEP）和 ESP32 固件放进同一个仿真里：固件在 Espressif 的 QEMU 里真实运行，引脚输出驱动舵机/直流电机的电气模型，电机力矩进 MuJoCo，电源用等效电路模型，四层按同一个虚拟时钟每 1 ms 交换一次。工程是文本（YAML + MJCF），可以 diff；自带 MCP 服务，AI 可以导入、标注、跑场景、改参数、再跑。主体 Apache-2.0，QEMU 部分见 qemu/README.md（GPL，独立进程）。

第一个示例是本仓库的开关机器人（examples/switch_robot），能复现“舵机堵转 → 5 V 掉压 → ESP32 欠压重启”，并通过 MCP 跑完一轮“测试 → AI 调参 → 重测”，记录在 examples/switch_robot/DEMO.md。

本平台的所有电机、电源、开关参数默认值都是按公开规格估的假设值，没有实物标定；仿真结论只能说明“在这些参数下会怎样”。

## 快速开始

Docker（推荐，QEMU、ngspice、OpenGL 都装好了），在仓库根目录：

    docker build -f platform/Dockerfile -t emcosim .
    docker run --rm emcosim run toggle_nominal          # 按两次键，开关先开后关
    docker run --rm emcosim run stall_brownout          # 9 N 硬开关，ESP32 欠压重启
    docker run --rm -p 8765:8765 emcosim view --host 0.0.0.0   # 浏览器打开 http://127.0.0.1:8765

pip：

    pip install -e "platform[step]"          # step 可选，STEP 导入需要 cascadio
    platform/qemu/build.sh                   # 编译打过补丁的 QEMU，按提示设置 EMCOSIM_QEMU / EMCOSIM_QEMU_ROMS
    cd platform/examples/switch_robot
    emcosim run toggle_nominal
    emcosim view

不装 QEMU 也能跑 mcu_backend: scripted 的场景（例如 servo_sweep_scripted），这时引脚波形由场景文件直接给出，用来快速扫机械和电气参数。ngspice 只在离线标定时需要（apt install ngspice）。

## 接到 AI（MCP）

    claude mcp add emcosim -- emcosim --project /path/to/platform/examples/switch_robot mcp
    # Docker：claude mcp add emcosim -- docker run --rm -i emcosim mcp

工具：get_project、import_pcb、import_model、import_firmware、define_joint、bind_pin、set_param、write_scenario、run_scenario、sweep、get_results、render_frames、calibrate_power。参数一律用 project.yaml 里的点路径，例如 power.sources.adapter.r_ohm、actuators.servo1.preset、mechanics.elements.rocker_detent.peak_force_n；firmware.press_angle_deg 这类写法指固件 fw_params 分区里的字段，改了不用重新编译。set_param 会写回 project.yaml，run_scenario 和 sweep 的 overrides 只对那一次运行生效。命令行有同名子命令（emcosim --help）。

## 架构与时钟

每 1 ms（mcu.step_ms）QEMU 停一次：把引脚输出电平和经 GPIO 矩阵路由到引脚的 LEDC 频率/占空比发给仿真器，收到按键等输入电平和“电源是否正常”后再跑下一毫秒。QEMU 用 -icount shift=3,sleep=off，虚拟时间只取决于执行的指令，和主机快慢无关，同一场景重复运行结果逐位一致。

在这 1 ms 内，机械按 MuJoCo 步长（示例里 0.125 ms，即 8 个子步）推进。每个子步里，执行器先根据引脚信号和关节状态算出一个随电压变化的等效负载（诺顿等效），电源网络用后向欧拉把这些负载和节点电容一起隐式求解出各节点电压，执行器再用解出的电压算电流和力矩，最后 MuJoCo 走一步。1 ms 结束时，欠压检测器看 ESP32 供电轨这 1 ms 内的最低电压，决定下一次交换时是否让芯片保持复位。所以电气和机械之间按 0.125 ms 耦合，固件看到的输入最多晚 1 ms，这是联合仿真本身引入的唯一近似。

电源模型是电压源（内阻、可选限流，例如 USB 口 500 mA）、电阻支路（保险丝、导线、PCB 铜皮）、节点电容和 LDO（压差、输出电容只能充不能放）。没有 ESR 和电感；calibrate_power 用 ngspice 跑带 ESR 和引线电感的瞬态，再反推等效模型的有效电容和内阻，示例里把最大误差从 29 mV 降到 5 mV。

舵机模型是：脉宽解码（40~500 Hz、500~2500 µs 有效，断信号 50 ms 后停止驱动）→ 位置环（P + D、死区）→ H 桥平均值模型 → 直流电机（R、L、反电动势、减速比、效率）→ 关节力矩。直流电机支持低边开关（单引脚，续流二极管）和 H 桥（两个引脚）。sg90 预设在 5 V 下的堵转力矩和空载转速与 simulation/parameters.json 一致，堵转电流约 0.71 A 是假设。

## 工程格式

一个工程是一个目录：project.yaml，加上 pcb/、mechanics/、firmware/、scenarios/，运行结果在 runs/（不进 git）。project.yaml 的主要段落：mcu（固件、供电轨、电流、欠压阈值）、power（nodes / sources / branches / regulators）、mechanics（mjcf 基础模型、parts 导入的零件、joints、elements 例如双稳态开关、probes 接触力探针、initial_deg）、actuators、inputs、bindings（引脚 → 执行器，或按键 → 引脚）、pcb。示例的 project.yaml 由 build_example.py 按“导入 → 标注”的步骤生成，可以重放。

场景文件（scenarios/*.yaml）写 duration_s、overrides、events（按键 press、运行中改电源 set）、expect（对 metric / event / signal 做 eq、lt、gt 等判断）。每次运行在 runs/<id>/ 下保存 run.json（场景、覆盖参数、完整的展开后工程、指标、断言）、signals.npz（1 ms 采样的全部信号）、uart.log（带虚拟时间戳的串口）、给网页查看器用的 scene.json/scene.bin/frames.bin，以及 render_frames 用的 qpos.npz。

## 导入

KiCad：读 .kicad_pcb（6/7/8 版格式）的元件、值、网表、板框和走线，按走线长度和线宽算每个网络的铜皮电阻和焊盘之间的最短路径电阻，按网络汇总电容；bind_pin 给了 net 时会检查网络存在，并报告经过两端器件（例如串联电阻）能到达的网络。立创 EasyEDA 请先在 EasyEDA 里导出 KiCad；如果手上只有 EasyEDA Pro 的 .epcb，可以连同它导出的 Specctra .dsn（和可选的 BOM）用 import_pcb(path, dsn=...) 走一个只保留焊盘、走线、元件值和板框的简化转换，示例的 pcb/carrier_v04.kicad_pcb 就是这样从 electronics/v04 转出来的。

STL/OBJ 按给定单位缩放成米，STEP 经 cascadio（OpenCASCADE）转网格；按密度或给定质量算惯量（网格不闭合时用凸包，会偏大），碰撞体默认用凸包，凹形零件要自己拆分或把 MJCF 作为基础模型导入。.xml 会被当作基础 MJCF。

固件：ESP-IDF 的 build 目录（读 flasher_args.json 拼成 4 MB 镜像）或合并好的 .bin；带 fw_params 分区的固件可以由平台改参数。

## QEMU 实测结论（2026-11-12）

用 ESP-IDF v5.4.2 官方镜像自带的 Espressif QEMU 9.0.0 实测：GPIO 只模拟了 strap 寄存器，GPIO_IN 恒为 0，按键无法注入；LEDC 只把占空比换成 0~100 的整数百分比，没有频率，50 Hz 舵机信号的分辨率只有约 200 µs（约 18°）。所以 v0.1 给 QEMU 打了补丁（qemu/），补上 GPIO 输入输出、GPIO 矩阵、LEDC 频率和占空比、1 ms 锁步和欠压复位。补丁没覆盖 GPIO 中断、LEDC 渐变、RMT/MCPWM/ADC 和 Wi-Fi，固件需要轮询按键、直接设占空比。细节见 qemu/README.md。

## 已知局限

v0.1 只支持 ESP32（不含 S3/C3）、舵机和直流电机、KiCad。电机和电源都是平均值模型，不解析 PWM 纹波、EMI 和热；保险丝没有跳闸模型；LDO 用固定压差；ESP32 的电流只分运行和复位两档，没有 Wi-Fi 发射尖峰。MuJoCo 碰撞是刚体加软约束，塑料件弹性、磨损、疲劳都不在内。网页查看器只回放已存的运行，不能交互式改参数。仿真比实时稍慢（示例 4.5 s 仿真约 5 s 墙钟，一个 4 核容器）。

## 测试

    cd platform && pytest -q

设置了 EMCOSIM_QEMU 时会跑固件联合仿真的用例，装了 ngspice 时会跑标定用例，否则跳过。
