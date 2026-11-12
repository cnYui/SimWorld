最小 ESP32 固件：一个低电平有效的按键（默认 GPIO0，即开发板 BOOT 键）每按一次，舵机就向“开”或“关”一侧摆到按压角，保持后回中；舵机信号用 LEDC 高速通道 0、50 Hz、14 位分辨率，从 GPIO18 输出。时序和 simulation/controller.py 一致（90°/s 斜坡、保持 650 ms、回位 500 ms），区别是保持时间从到达按压角后才开始计。固件没有开关状态传感器，日志里的 light=ON/OFF 只是它“以为”的状态。

可调参数不编进程序，而是放在 fw_params 分区（偏移 0x110000，结构见 main/params.h）。仿真平台每次运行前按 project.yaml 的 mcu.firmware.params 重写这一块，所以 AI 改按压角、保持时间等参数后不用重新编译；实物上也可以用 esptool 写这个分区。分区内容无效时用代码里的默认值。

以 “EVT ” 开头的串口行是给仿真器看的事件：boot（含启动次数和复位原因，欠压复位显示 BROWNOUT）、ready、press、busy_ignored、stall_guard、done。

编译（不需要本机装 ESP-IDF）：

    docker run --rm -v $PWD:/project -w /project espressif/idf:v5.4.2 idf.py set-target esp32 build

然后在 platform/examples/switch_robot 里运行 python build_example.py --fw ../../firmware/switch_robot/build 导入。仓库里已经放了一份编好的镜像（examples/switch_robot/firmware/switch_robot.bin，去掉了尾部 0xFF），开箱即可运行。

没有在真实开发板上烧录验证过；GPIO0 是启动模式引脚，上电时按住会进下载模式，实物上换别的脚要同时改 fw_params 里的 pin_button。
