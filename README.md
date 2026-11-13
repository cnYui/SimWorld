# SimWorld：电子 + 物理联合仿真

把 PCB 和 3D 模型放进同一个场景，跑真实的电路和物理测试：ESP32 固件在 QEMU 里真实运行，引脚输出驱动舵机/直流电机的电气模型，电机力矩进 MuJoCo，电源用等效电路模型，按同一个虚拟时钟每 1 ms 交换一次；板子用 KiCad 做 DRC/ERC，用 ngspice 按真实网表复核电源。平台本身不带 AI，通过 MCP 把这些能力开放给你自己的 Claude Code 或 Codex，让它改模型、改 PCB、查嘉立创元件，直到测试通过。界面在浏览器里。

自带示例是一个按墙上开关的舵机机器人（examples/switch_robot，带 V0.5 控制板），能复现“舵机堵转 → 5 V 掉压 → ESP32 欠压重启”，记录在 examples/switch_robot/DEMO.md。所有电机、电源、开关参数默认值都是按公开规格估的假设值，没有实物标定。

代码里的包名和命令仍叫 emcosim。主体 Apache-2.0；QEMU 补丁是 GPL，KiCad、ngspice、QEMU 都作为独立程序运行，由安装脚本下载，不在本仓库里（见 NOTICE）。

## 快速开始

Windows 10/11：

    git clone https://github.com/cnYui/SimWorld.git
    cd SimWorld
    scripts\setup.cmd        # 建 .venv、装依赖；用 winget 装 KiCad；ngspice 和打过补丁的 QEMU 下载到 tools\
    scripts\start.cmd        # 在浏览器里打开 SimWorld

Linux / macOS：

    git clone https://github.com/cnYui/SimWorld.git && cd SimWorld
    scripts/setup.sh          # 同上；KiCad、ngspice 用 apt/dnf/brew 装，QEMU 下载预编译版，没有合适的就从源码编译
    scripts/start.sh

QEMU 预编译版来自本仓库的 Release（打 v* 标签时由 .github/workflows/release.yml 编译）。只想快速看看也可以不装 QEMU：标着“快速”的测试用脚本代替固件，照样能跑。

页面按做事的顺序分成三页：“PCB 元件”管电路（元件清单和立创料号、KiCad 检查、ngspice 电路检查、电源和单片机参数、引脚接线、跟着仿真动的电路图）；“建模”管机械（3D 模型、零件和关节、导入 STL/STEP、每个打印件的材料和打印设置、电机型号和齿轮、开关手感、导出 3MF 或发到 Bambu Studio）；“仿真”把两边放在一起跑（测试和 3D 回放、按这次受力算的打印件强度和带应力颜色的 3D 参考图、舵机齿轮应力、历史结果对比）。新建的空工程从“建模”开始。

Docker（QEMU、ngspice 都装好了）：

    docker build -t emcosim .
    docker run --rm -p 8765:8765 -e EMCOSIM_APP_TOKEN=local emcosim serve --host 0.0.0.0 --no-browser
    # 浏览器打开 http://127.0.0.1:8765/#token=local

## 接到 AI（MCP）

页面右下角“连接 AI”有两个按钮：复制给 Claude Code、复制给 Codex。把复制的话发给你的 AI，它会自己把 SimWorld 的 MCP 服务配好。连上后它操作的就是浏览器里开着的那个页面：可以直接说“打开示例工程”“打开 D:\xxx 这个工程”“新建一个工程”（open_example、open_project、new_project，页面会跟着切过去），也可以让它改模型、改 PCB、跑测试，跑的每次测试都会出现在页面上。没打开工程时也能连。

手动配置：

    # Claude Code
    claude mcp add -s user emcosim -- <SimWorld 目录>/.venv/bin/python -m emcosim mcp

    # Codex：~/.codex/config.toml
    [mcp_servers.emcosim]
    command = "<SimWorld 目录>/.venv/bin/python"
    args = ["-m", "emcosim", "mcp"]

（Windows 上 python 在 .venv\Scripts\python.exe。）MCP 服务通过 %APPDATA%\emcosim\live.json（Linux 是 ~/.config/emcosim/live.json）找到正在运行的页面。

内核工具（v0.3）：board_import（经 KiCad 的导入器读 .kicad_pcb、嘉立创EDA 导出的 .epro、Altium、Eagle，加上原 EDA 的网表补齐焊盘网络）、board_drc / board_erc（KiCad 的 DRC/ERC，导入时写入嘉立创两层板工艺规则）、board_netlist、board_edit（用 KiCad 自己的板子模型移元件、改值、改字段、改线宽和间距，每次留 .bak 备份并自动重跑 DRC）、schematic_edit、board_export_3d、circuit_check（用板子的真实网表和 ngspice 重算一次运行的电源路径，和联合仿真对比）、model_info / model_edit（按名字改 MuJoCo 模型的属性，改完必须能被 MuJoCo 编译，否则不保存）。元件查询（内置）：parts_search、part_info、parts_alternatives、board_parts_status 查嘉立创在库元件、实时库存和价格、可替换料，数据来自开源的 PCB Parts MCP（github.com/Averyy/jlcpcb-mcp，MIT 许可，作者公开运行在 pcbparts.dev/mcp）；emcosim 作为它的客户端把这几个工具并进自己的 MCP，用户只需要连一个服务。上游项目实时查询用的是绕过网站反爬的 HTTP 客户端，emcosim 不在本机运行那一部分；EMCOSIM_PARTS_MCP 可以指向自己部署的实例。面板 PCB 页的“现在库存”列也走这条路。需要本机装好 KiCad 9（kicad.org/download，Windows 默认路径会自动找到，或设 EMCOSIM_KICAD_CLI）和 ngspice。

工具：get_project、import_pcb、import_model、import_firmware、define_joint、bind_pin、set_param、write_scenario、run_scenario、sweep、get_results、render_frames、calibrate_power、fit_measurements、compare_trace、send_to_bambu_studio。参数一律用 project.yaml 里的点路径，例如 power.sources.adapter.r_ohm、actuators.servo1.preset、mechanics.elements.rocker_detent.peak_force_n；firmware.press_angle_deg 这类写法指固件 fw_params 分区里的字段，改了不用重新编译。set_param 会写回 project.yaml，run_scenario 和 sweep 的 overrides 只对那一次运行生效。命令行有同名子命令（emcosim --help）。

## 架构与时钟

每 1 ms（mcu.step_ms）QEMU 停一次：把引脚输出电平和经 GPIO 矩阵路由到引脚的 LEDC 频率/占空比发给仿真器，收到按键等输入电平和“电源是否正常”后再跑下一毫秒。QEMU 用 -icount shift=3,sleep=off，虚拟时间只取决于执行的指令，和主机快慢无关，同一场景重复运行的信号和指标逐位一致（串口日志里个别 ESP-IDF 时间戳在欠压重启后偶尔差 1 个 tick）。QEMU 和仿真器之间在 Windows 上走本机 TCP，其他系统默认 UNIX socket，可以用环境变量 EMCOSIM_COSIM_TRANSPORT=tcp|unix 指定；串口输出写到临时文件，按帧读取，所以每行的时间戳和主机系统无关。

在这 1 ms 内，机械按 MuJoCo 步长（示例里 0.125 ms，即 8 个子步）推进。每个子步里，执行器先根据引脚信号和关节状态算出一个随电压变化的等效负载（诺顿等效），电源网络用后向欧拉把这些负载和节点电容一起隐式求解出各节点电压，执行器再用解出的电压算电流和力矩，最后 MuJoCo 走一步。1 ms 结束时，欠压检测器看 ESP32 供电轨这 1 ms 内的最低电压，决定下一次交换时是否让芯片保持复位。所以电气和机械之间按 0.125 ms 耦合，固件看到的输入最多晚 1 ms，这是联合仿真本身引入的唯一近似。

电源模型是电压源（内阻、可选限流，例如 USB 口 500 mA）、电阻支路（保险丝、导线、PCB 铜皮）、节点电容和 LDO（压差、输出电容只能充不能放）。没有 ESR 和电感；calibrate_power 用 ngspice 跑带 ESR 和引线电感的瞬态，再反推等效模型的有效电容和内阻，示例里把最大误差从 29 mV 降到 5 mV。

舵机模型是：脉宽解码（40~500 Hz、500~2500 µs 有效，断信号 50 ms 后停止驱动）→ 位置环（P + D、死区）→ H 桥平均值模型 → 直流电机（R、L、反电动势、减速比、效率）→ 关节力矩。直流电机支持低边开关（单引脚，续流二极管）和 H 桥（两个引脚）。sg90 预设在 5 V 下的堵转力矩和空载转速与 simulation/parameters.json 一致，堵转电流约 0.71 A 是假设。

## 用实测数据校准

仿真的主要误差来自参数，不是求解器。examples/switch_robot/measurements/HOWTO.md 说明怎么用 USB 电流表、万用表、手机慢动作和厨房秤测舵机堵转电流和掉压、转 90° 的时间、开关翻转力、USB 口内阻和限流；填进 template.yaml 后，emcosim fit 表格.yaml（或 MCP 工具 fit_measurements）用同一套模型反推舵机电阻、反电动势常数、齿轮效率、摩擦、电源内阻和限流、开关峰值力，给出每个参数的 ±1σ、拟合前后的残差和 project.yaml 的改动，加 --apply 才写回。测不出来的参数（例如只能看到 ke×减速比，减速比本身看不到）保持原值并说明原因。示波器或记录型万用表的波形可以用 emcosim compare 波形.csv node.usb5v.v 和某次运行对比，给出均方根误差。舵机模型新增了齿轮间隙 backlash_deg、输出端扭转刚度 output_stiffness_nm_per_rad 和位置环限速 max_speed_dps，默认都关闭，旧结果不变。measurements/example_sg90_usb.yaml 是编的示例数据，只用来演示流程。

## 发送到拓竹 Bambu Studio

project.yaml 的 printing.parts 列出可打印零件（开关机器人示例带了 5 个 PRINT_*.stl，在 examples/switch_robot/print/）。emcosim print [零件名] 或界面里的“打印”页会把选中的零件打成一个 3MF（保持 STL 导出时的打印朝向，平铺在热床上），然后交给 Bambu Studio 打开：先找 bambu-studio.exe（注册表和默认安装目录）用命令行参数打开，找不到时用 MakerWorld 用的 bambustudio://open?file= 链接，文件由本机 127.0.0.1 临时提供一次。--with-settings 会把推荐的层高、外墙、填充写进 3MF 的 Metadata/model_settings.config，这个格式拓竹没有公开，是按 Bambu Studio 自己保存的格式写的，没在真机上验证，默认关闭。拓竹 wiki 公开的 bambu-connect://import-file 协议导入的是切好片的 .gcode.3mf，用于直接下发打印机，不适合导入模型。

## 3D 打印件强度校核

页面上：在“仿真”页切到“强度和应力”，点“开始检测”，按当前回放的那次仿真算（约半分钟）。打印件要在 project.yaml 里有 `printing.parts.<名字>.strength: <配置>`，配置写夹持位置和受力位置（例子见 `examples/switch_robot/strength/press_arm.yaml`）；材料和层高、墙数、填充取“建模”页的设置。舵机齿轮应力用 Lewis 公式按仿真里的最大力矩和堵转力矩估算，齿轮尺寸和许用应力是同类舵机的典型值（估计），可在 `actuators.<名字>.gear` 改。MCP 里对应 strength_check、gear_check。

完整报告：

`pip install -e ".[strength]"` 后运行 `python examples/switch_robot/strength/run_strength.py`（4 核约 13 分钟，`--report-only` 只重出报告）。用仿真里的接触力峰值和舵机堵转扭矩作载荷，对打印件做线弹性有限元，材料按 PLA/PETG 的横观各向同性处理，层间强度单独折减，可比较平放、侧放、竖放三种打印方向，并扫描喷嘴直径、层高、壁数、填充率；另有直角内角圆角半径的应力集中系数研究。结果见 `examples/switch_robot/strength/REPORT.md`。材料与打印参数关系取自文献和数据手册，未经实测，结论只能用来比较方案。

## 工程格式

一个工程是一个目录：project.yaml，加上 pcb/、mechanics/、firmware/、scenarios/，运行结果在 runs/（不进 git）。project.yaml 的主要段落：mcu（固件、供电轨、电流、欠压阈值）、power（nodes / sources / branches / regulators）、mechanics（mjcf 基础模型、parts 导入的零件、joints、elements 例如双稳态开关、probes 接触力探针、initial_deg）、actuators、inputs、bindings（引脚 → 执行器，或按键 → 引脚）、pcb。示例的 project.yaml 由 build_example.py 按“导入 → 标注”的步骤生成，可以重放。

场景文件（scenarios/*.yaml）写 duration_s、overrides、events（按键 press、运行中改电源 set）、expect（对 metric / event / signal 做 eq、lt、gt 等判断）。每次运行在 runs/<id>/ 下保存 run.json（场景、覆盖参数、完整的展开后工程、指标、断言）、signals.npz（1 ms 采样的全部信号）、uart.log（带虚拟时间戳的串口）、给网页查看器用的 scene.json/scene.bin/frames.bin，以及 render_frames 用的 qpos.npz。

## 导入

PCB 以 KiCad 为内核（v0.3）：板子文件统一转成 .kicad_pcb，检查和修改都调用 KiCad 本身。嘉立创EDA 专业版没有导出 KiCad 的功能，用它的“导出工程（.epro）”和 Protel 网表交给 board_import；KiCad 9 的 EasyEDA Pro 导入器会把贴片焊盘读成 1 µm 孔的通孔焊盘、漏掉部分焊盘网络和元件值，board_import 会按原网表修正这几处并报告修了多少。示例 pcb/v05/ 就是这样从 V0.5 板导入的，KiCad DRC 剩 1 个错误（C1 和 F1 的外形框重叠，需要对照实物确认）。

旧的自带解析器：读 .kicad_pcb（6/7/8 版格式）的元件、值、网表、板框和走线，按走线长度和线宽算每个网络的铜皮电阻和焊盘之间的最短路径电阻，按网络汇总电容；bind_pin 给了 net 时会检查网络存在，并报告经过两端器件（例如串联电阻）能到达的网络。立创 EasyEDA 请先在 EasyEDA 里导出 KiCad；如果手上只有 EasyEDA Pro 的 .epcb，可以连同它导出的 Specctra .dsn（和可选的 BOM）用 import_pcb(path, dsn=...) 走一个只保留焊盘、走线、元件值和板框的简化转换，示例的 pcb/carrier_v04.kicad_pcb 就是这样从 electronics/v04 转出来的。

STL/OBJ 按给定单位缩放成米，STEP 经 cascadio（OpenCASCADE）转网格；按密度或给定质量算惯量（网格不闭合时用凸包，会偏大），碰撞体默认用凸包，凹形零件要自己拆分或把 MJCF 作为基础模型导入。.xml 会被当作基础 MJCF。

固件：ESP-IDF 的 build 目录（读 flasher_args.json 拼成 4 MB 镜像）或合并好的 .bin；带 fw_params 分区的固件可以由平台改参数。

## QEMU 实测结论（2026-11-12）

用 ESP-IDF v5.4.2 官方镜像自带的 Espressif QEMU 9.0.0 实测：GPIO 只模拟了 strap 寄存器，GPIO_IN 恒为 0，按键无法注入；LEDC 只把占空比换成 0~100 的整数百分比，没有频率，50 Hz 舵机信号的分辨率只有约 200 µs（约 18°）。所以 v0.1 给 QEMU 打了补丁（qemu/），补上 GPIO 输入输出、GPIO 矩阵、LEDC 频率和占空比、1 ms 锁步和欠压复位。补丁没覆盖 GPIO 中断、LEDC 渐变、RMT/MCPWM/ADC 和 Wi-Fi，固件需要轮询按键、直接设占空比。细节见 qemu/README.md。

## 已知局限

v0.1 只支持 ESP32（不含 S3/C3）、舵机和直流电机、KiCad。电机和电源都是平均值模型，不解析 PWM 纹波、EMI 和热；保险丝没有跳闸模型；LDO 用固定压差；ESP32 的电流只分运行和复位两档，没有 Wi-Fi 发射尖峰。MuJoCo 碰撞是刚体加软约束，塑料件弹性、磨损、疲劳都不在内。仿真比实时稍慢（示例 4.5 s 仿真约 5 s 墙钟，一个 4 核容器）。

## 测试

    pytest -q

设置了 EMCOSIM_QEMU 时会跑固件联合仿真的用例，装了 ngspice 时会跑标定用例，否则跳过。
