这里是 QEMU 补丁，许可证是 GPL-2.0-or-later，与被修改的 QEMU 文件一致。emcosim 主体是 Apache-2.0，它把 QEMU 当独立进程启动，只通过 socket（Windows 上是本机 TCP，其他系统默认 UNIX socket）和串口文件通信，不链接 QEMU 代码。Windows 安装包里带的 qemu-system-xtensa.exe 也是这样，安装目录 licenses/ 下放了补丁和本说明，源码就是下面的标签加补丁。Docker 镜像里带了编译好的 QEMU 可执行文件，对应源码就是下面的 Espressif 标签加这个补丁，镜像内 /opt/emcosim/qemu 下也放了一份补丁和 build.sh。

基线是 Espressif 的 QEMU 分支 esp-develop-9.0.0-20240606（ESP-IDF v5.4.2 官方 Docker 镜像自带的同一版本）。Linux/macOS 用 build.sh 编译，或者直接用 platform/Dockerfile；Windows 在 MSYS2 MINGW64 里用 build-windows.sh（GitHub Actions 的 emcosim-windows 工作流就是这样编的）。

为什么要打补丁：2026-11-12 用原版实测（同一份固件，-icount shift=3），GPIO 只实现了 strap 寄存器，GPIO_IN 读出来恒为 0，按键无法注入，固件一开机就把 GPIO0 当成“按下”；GPIO_OUT/ENABLE 的写入被丢弃，也没有 GPIO 矩阵路由。LEDC 只在写占空比寄存器时把占空比换算成 0~100 的整数百分比喂给一个 LED 设备（-trace led_set_intensity 可见），没有频率、没有分辨率之外的信息；50 Hz 舵机信号 1500 µs 显示为 7%，±17° 对应的 1311~1689 µs 只能落在 6%/7%/8% 三档，相当于 200 µs、约 18° 的分辨率，不能用来驱动舵机模型。原版也没有欠压复位的入口。

补丁做了三件事。GPIO：保存 OUT/ENABLE（含 W1TS/W1TC 和 32~39 脚）、FUNCn_OUT_SEL_CFG，GPIO_IN 返回外部输入和输出回读；没有联合仿真连接时输入默认全高。LEDC：保存完整寄存器，在 DUTY_START（高速通道）或 PARA_UP（低速通道）时锁存占空比，按定时器分频、分辨率和时钟源（APB 80 MHz / REF_TICK / 8 MHz）算出频率和占空比；随外设复位一起复位（原版不复位）。锁步接口（hw/xtensa/esp32_cosim.c）：设置环境变量 ESP32_COSIM_SOCKET（"tcp:127.0.0.1:端口" 或 UNIX socket 路径）后，每隔 ESP32_COSIM_PERIOD_US（默认 1000）微秒虚拟时间，QEMU 停下来，把各引脚的输出电平和经 GPIO 矩阵路由到引脚上的 LEDC 频率/占空比发出去，阻塞等待一行输入（引脚输入电平、电源是否正常）。电源不正常时芯片保持复位，复位原因置为 RTCWDT_BROWN_OUT_RESET（15），固件里 esp_reset_reason() 读到 ESP_RST_BROWNOUT。

协议见 esp32_cosim.c 文件头。没有实现的：GPIO 中断（固件要轮询）、IO_MUX 的上下拉（由仿真器一侧决定输入电平）、LEDC 渐变（fade）和中断、hpoint 相位、RMT/MCPWM/ADC、Wi-Fi。没做的外设在 QEMU 里仍是原版行为。
