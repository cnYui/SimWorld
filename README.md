# emcosim：电子 + 物理联合仿真平台

把 PCB（KiCad）、3D 模型（STL/STEP）和 ESP32 固件放进同一个仿真里：固件在 Espressif 的 QEMU 里真实运行，引脚输出驱动舵机/直流电机的电气模型，电机力矩进 MuJoCo，电源用等效电路模型，四层按同一个虚拟时钟每 1 ms 交换一次。工程是文本（YAML + MJCF），可以 diff；自带 MCP 服务，AI 可以导入、标注、跑场景、改参数、再跑。主体 Apache-2.0，QEMU 部分见 qemu/README.md（GPL，独立进程）。

第一个示例是本仓库的开关机器人（examples/switch_robot），能复现“舵机堵转 → 5 V 掉压 → ESP32 欠压重启”，并通过 MCP 跑完一轮“测试 → AI 调参 → 重测”，记录在 examples/switch_robot/DEMO.md。

本平台的所有电机、电源、开关参数默认值都是按公开规格估的假设值，没有实物标定；仿真结论只能说明“在这些参数下会怎样”。

## Windows 应用

v0.2 起有桌面版：一个窗口里看工程（PCB、机构、固件、引脚绑定、电源网络）、改参数、编辑和运行场景（带进度和实时曲线、可取消）、回放结果（3D + 曲线 + 串口）、对比两次运行、扫参，右侧是 AI 聊天面板。安装包由 GitHub Actions 的 emcosim-windows 工作流构建（Actions 页面 → 最新一次运行 → Artifacts → emcosim-windows-installer），里面带了打过补丁的 Windows 版 QEMU 和开关机器人示例，装好即可运行固件联合仿真，不需要 Docker 或 WSL。第一次打开示例时会把它复制到“文档\emcosim”下，运行结果写在那里。

聊天面板的做法和 ChatCut 一样：不自己接 Claude 账号，而是调用你电脑上安装的 Claude Code，并把 emcosim 的工具（get_project、set_param、run_scenario、sweep 等）通过 MCP 挂给它。登录用的是 Claude Code 自己的登录（设置页的“登录 Claude”按钮会运行 claude auth login），费用计入你自己的 Claude 套餐，本软件不保存 Claude 账号凭证；Anthropic 的条款不允许第三方软件自己提供 claude.ai 登录。没有装 Claude Code 时，可以在设置里填 Anthropic API Key（Windows 上用 DPAPI 加密保存），直接调用 Claude API，默认模型 claude-opus-5-5。AI 改的参数会写回 project.yaml，它启动的运行会自动出现在结果页。

不装安装包也能用同一个界面：pip install -e "platform[step,app]" 之后，emcosim app 打开桌面窗口，emcosim serve 在浏览器里打开（地址里带一次性令牌，只监听 127.0.0.1）。

## 快速开始

Docker（QEMU、ngspice、OpenGL 都装好了），在仓库根目录：

    docker build -f platform/Dockerfile -t emcosim .
    docker run --rm emcosim run toggle_nominal          # 按两次键，开关先开后关
    docker run --rm emcosim run stall_brownout          # 9 N 硬开关，ESP32 欠压重启
    docker run --rm -p 8765:8765 -e EMCOSIM_APP_TOKEN=local emcosim serve --host 0.0.0.0 --no-browser
    # 浏览器打开 http://127.0.0.1:8765/#token=local

pip：

    pip install -e "platform[step]"          # step 可选，STEP 导入需要 cascadio
    platform/qemu/build.sh                   # 编译打过补丁的 QEMU，按提示设置 EMCOSIM_QEMU / EMCOSIM_QEMU_ROMS
    cd platform/examples/switch_robot
    emcosim run toggle_nominal
    emcosim serve      # 或 emcosim app（桌面窗口，需要 pywebview）

不装 QEMU 也能跑 mcu_backend: scripted 的场景（例如 servo_sweep_scripted），这时引脚波形由场景文件直接给出，用来快速扫机械和电气参数。ngspice 只在离线标定时需要（apt install ngspice）。

## 接到 AI（MCP）

    claude mcp add emcosim -- emcosim --project /path/to/platform/examples/switch_robot mcp
    # Docker：claude mcp add emcosim -- docker run --rm -i emcosim mcp

工具：get_project、import_pcb、import_model、import_firmware、define_joint、bind_pin、set_param、write_scenario、run_scenario、sweep、get_results、render_frames、calibrate_power、fit_measurements、compare_trace、send_to_bambu_studio。参数一律用 project.yaml 里的点路径，例如 power.sources.adapter.r_ohm、actuators.servo1.preset、mechanics.elements.rocker_detent.peak_force_n；firmware.press_angle_deg 这类写法指固件 fw_params 分区里的字段，改了不用重新编译。set_param 会写回 project.yaml，run_scenario 和 sweep 的 overrides 只对那一次运行生效。命令行有同名子命令（emcosim --help）。

## 架构与时钟

每 1 ms（mcu.step_ms）QEMU 停一次：把引脚输出电平和经 GPIO 矩阵路由到引脚的 LEDC 频率/占空比发给仿真器，收到按键等输入电平和“电源是否正常”后再跑下一毫秒。QEMU 用 -icount shift=3,sleep=off，虚拟时间只取决于执行的指令，和主机快慢无关，同一场景重复运行的信号和指标逐位一致（串口日志里个别 ESP-IDF 时间戳在欠压重启后偶尔差 1 个 tick）。QEMU 和仿真器之间在 Windows 上走本机 TCP，其他系统默认 UNIX socket，可以用环境变量 EMCOSIM_COSIM_TRANSPORT=tcp|unix 指定；串口输出写到临时文件，按帧读取，所以每行的时间戳和主机系统无关。

在这 1 ms 内，机械按 MuJoCo 步长（示例里 0.125 ms，即 8 个子步）推进。每个子步里，执行器先根据引脚信号和关节状态算出一个随电压变化的等效负载（诺顿等效），电源网络用后向欧拉把这些负载和节点电容一起隐式求解出各节点电压，执行器再用解出的电压算电流和力矩，最后 MuJoCo 走一步。1 ms 结束时，欠压检测器看 ESP32 供电轨这 1 ms 内的最低电压，决定下一次交换时是否让芯片保持复位。所以电气和机械之间按 0.125 ms 耦合，固件看到的输入最多晚 1 ms，这是联合仿真本身引入的唯一近似。

电源模型是电压源（内阻、可选限流，例如 USB 口 500 mA）、电阻支路（保险丝、导线、PCB 铜皮）、节点电容和 LDO（压差、输出电容只能充不能放）。没有 ESR 和电感；calibrate_power 用 ngspice 跑带 ESR 和引线电感的瞬态，再反推等效模型的有效电容和内阻，示例里把最大误差从 29 mV 降到 5 mV。

舵机模型是：脉宽解码（40~500 Hz、500~2500 µs 有效，断信号 50 ms 后停止驱动）→ 位置环（P + D、死区）→ H 桥平均值模型 → 直流电机（R、L、反电动势、减速比、效率）→ 关节力矩。直流电机支持低边开关（单引脚，续流二极管）和 H 桥（两个引脚）。sg90 预设在 5 V 下的堵转力矩和空载转速与 simulation/parameters.json 一致，堵转电流约 0.71 A 是假设。

## 用实测数据校准

仿真的主要误差来自参数，不是求解器。examples/switch_robot/measurements/HOWTO.md 说明怎么用 USB 电流表、万用表、手机慢动作和厨房秤测舵机堵转电流和掉压、转 90° 的时间、开关翻转力、USB 口内阻和限流；填进 template.yaml 后，emcosim fit 表格.yaml（或 MCP 工具 fit_measurements）用同一套模型反推舵机电阻、反电动势常数、齿轮效率、摩擦、电源内阻和限流、开关峰值力，给出每个参数的 ±1σ、拟合前后的残差和 project.yaml 的改动，加 --apply 才写回。测不出来的参数（例如只能看到 ke×减速比，减速比本身看不到）保持原值并说明原因。示波器或记录型万用表的波形可以用 emcosim compare 波形.csv node.usb5v.v 和某次运行对比，给出均方根误差。舵机模型新增了齿轮间隙 backlash_deg、输出端扭转刚度 output_stiffness_nm_per_rad 和位置环限速 max_speed_dps，默认都关闭，旧结果不变。measurements/example_sg90_usb.yaml 是编的示例数据，只用来演示流程。

## 发送到拓竹 Bambu Studio

project.yaml 的 printing.parts 列出可打印零件（开关机器人示例引用了 mechanical/ 下的 5 个 PRINT_*.stl）。emcosim print [零件名] 或界面里的“打印”页会把选中的零件打成一个 3MF（保持 STL 导出时的打印朝向，平铺在热床上），然后交给 Bambu Studio 打开：先找 bambu-studio.exe（注册表和默认安装目录）用命令行参数打开，找不到时用 MakerWorld 用的 bambustudio://open?file= 链接，文件由本机 127.0.0.1 临时提供一次。--with-settings 会把推荐的层高、外墙、填充写进 3MF 的 Metadata/model_settings.config，这个格式拓竹没有公开，是按 Bambu Studio 自己保存的格式写的，没在真机上验证，默认关闭。拓竹 wiki 公开的 bambu-connect://import-file 协议导入的是切好片的 .gcode.3mf，用于直接下发打印机，不适合导入模型。

## 3D 打印件强度校核

`pip install -e ".[strength]"` 后运行 `python examples/switch_robot/strength/run_strength.py`（4 核约 13 分钟，`--report-only` 只重出报告）。用仿真里的接触力峰值和舵机堵转扭矩作载荷，对打印件做线弹性有限元，材料按 PLA/PETG 的横观各向同性处理，层间强度单独折减，可比较平放、侧放、竖放三种打印方向，并扫描喷嘴直径、层高、壁数、填充率；另有直角内角圆角半径的应力集中系数研究。结果见 `examples/switch_robot/strength/REPORT.md`。材料与打印参数关系取自文献和数据手册，未经实测，结论只能用来比较方案。

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

v0.1 只支持 ESP32（不含 S3/C3）、舵机和直流电机、KiCad。电机和电源都是平均值模型，不解析 PWM 纹波、EMI 和热；保险丝没有跳闸模型；LDO 用固定压差；ESP32 的电流只分运行和复位两档，没有 Wi-Fi 发射尖峰。MuJoCo 碰撞是刚体加软约束，塑料件弹性、磨损、疲劳都不在内。仿真比实时稍慢（示例 4.5 s 仿真约 5 s 墙钟，一个 4 核容器）。

## 测试

    cd platform && pytest -q

设置了 EMCOSIM_QEMU 时会跑固件联合仿真的用例，装了 ngspice 时会跑标定用例，否则跳过。
