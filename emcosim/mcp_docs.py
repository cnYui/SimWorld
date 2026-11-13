"""Plain-language guide to the emcosim MCP tools, for the "MCP 使用说明" page: what each tool does
and when to use it, grouped by the page it works for. The tool list itself comes from the running
MCP server (mcp_guide), so a tool added to the server shows up even before it is described here;
tests/test_mcp_docs.py fails until it is.
"""
from __future__ import annotations

# (group title, which page it is for, one line about the group, [(tool, 能做什么, 什么时候用)])
GROUPS = [
    ('开始和工程', '全部', '先让 AI 连上、找到要做的工程。', [
        ('design_rules', '读 SimWorld 的使用规则：三个页面各管什么，产品零件和测试夹具怎么分，参数没提到就别乱填。',
         'AI 第一次用 SimWorld、开始设计之前读一遍。'),
        ('current_project', '看 AI 现在操作的是哪个工程，浏览器页面连没连上。', '刚连上 AI，或者不确定它在改哪个工程时。'),
        ('list_examples', '列出自带的示例工程。', '想先看看别人做好的例子时。'),
        ('open_example', '复制一份示例工程并在页面里打开。', '想拿示例来改、或者照着学的时候。'),
        ('open_project', '打开电脑上已有的工程文件夹，页面跟着切过去。', '“打开 D:\\xxx 这个工程”。'),
        ('new_project', '新建一个空工程并打开。', '从零开始做一个新东西时。'),
        ('get_project', '看工程概况：有哪些测试、板子、固件、关节、电机、电源、最近的结果；也能查某一个参数的值。',
         '动手改之前先了解现状，或者查一个参数现在是多少。'),
        ('set_param', '改一个参数并保存，返回改之前和改之后的值。', '调电压、电容、舵机型号、材料、开关手感等任何参数时。'),
    ]),
    ('建模', '建模', '产品本身的零件、怎么装在一起、用什么材料。测试用的桌子、杠铃、墙不在这里。', [
        ('add_shape', '不用 CAD 文件，直接用方块、圆柱、圆管、球、胶囊、圆角方块做一个零件，并给出一张带新零件的图。'
                      '测试用的东西（杠铃、桌面）加 role=fixture 标成夹具。',
         '还没有 3D 文件，想先用简单形状搭出产品，或者给测试加一根杠铃、一张桌子时。'),
        ('import_model', '导入 STL、STEP、OBJ 零件（或者一个 MuJoCo 的 .xml 整体模型），可以指定装在哪个零件上、位置、质量。',
         '已经有 3D 文件时。'),
        ('part_edit', '移动、换挂载位置、改质量、改颜色、改名字，或者把零件标成夹具 / 产品。', '零件位置不对、质量要按实测改、或者分错了产品和夹具时。'),
        ('part_delete', '删掉一个零件，连同依赖它的关节、测量点和电机。', '零件不要了。'),
        ('define_joint', '让零件能转（铰链）或能滑（滑轨），设定转动范围、阻尼、弹簧。', '做按键、翘板、转轴、滑块这类会动的结构时。'),
        ('model_info', '列出模型里所有零件、关节、碰撞体和位置、质量（质量是给定的、按密度算的还是估计的）。',
         '检查装配对不对、零件是否互相穿插、质量是不是估计值。'),
        ('model_edit', '直接改 MuJoCo 模型文件里某个元素的属性，比如摩擦系数、位置；改完会检查能不能编译。',
         '工程用的是 .xml 整体模型、要改里面的细节时（用简单形状做的零件请用 part_edit）。'),
        ('render_model', '给现在的模型拍一张图，可以对准某个零件、把某些零件涂成橙色。', '想看看模型长什么样，不用跑仿真。'),
        ('send_to_bambu_studio', '把要打印的零件导出成一个 3MF，在拓竹 Bambu Studio 里打开。', '准备打印时。'),
    ]),
    ('PCB 元件', 'PCB 元件', '电路相关的都在这里：元件、原理图、板子、检查、买料。', [
        ('board_status', '看工程用的是哪些板子文件，电脑上有没有装 KiCad 和 ngspice。', '电路相关的工具报错、或者刚开始做板子时。'),
        ('import_pcb', '导入一块 KiCad 板子（.kicad_pcb），得到网络、每个网络上的电容和铜线电阻。', '已经有画好的 KiCad 板子时。'),
        ('board_import', '用 KiCad 的导入功能导入嘉立创EDA（.epro）、Altium、Eagle 的板子。', '板子是用别的 EDA 画的。'),
        ('symbol_search', '在 KiCad 元件库里搜原理图符号，比如“ESP32-PICO”“LDO 3.3”。', '画原理图前找元件。'),
        ('symbol_info', '查一个符号有哪些引脚、默认封装。', '要写连线（哪个引脚接哪个网络）之前。'),
        ('footprint_search', '在 KiCad 封装库里搜封装，比如“R_0402”“SOT-23-5”。', '给元件选封装时。'),
        ('schematic_create', '按元件清单和连线直接生成 KiCad 原理图。', '从零开始画电路。'),
        ('schematic_edit', '改原理图里某个元件的字段：值、嘉立创料号、封装等。', '换元件型号、补料号时。'),
        ('schematic_render', '给原理图拍一张图，可以放大某个元件。', '想看看原理图对不对。'),
        ('board_erc', '用 KiCad 做电气规则检查（引脚悬空、电源没接等）。', '原理图画完、做板子之前。'),
        ('board_from_schematic', '按原理图生成 PCB，或者像 KiCad 的“从原理图更新 PCB”那样同步改动。', '原理图定了、要出板子，或者原理图改了。'),
        ('board_autoroute', '自动布线（装了 freerouting 用它，没有就用内置的简单布线）。', '元件摆好了、要连线时。'),
        ('board_edit', '用 KiCad 改板子：移动元件、改线宽、加过孔等，改完自动重跑 DRC。', '修 DRC 报错、调整摆放时。'),
        ('board_drc', '按嘉立创双层板工艺做设计规则检查，列出问题和位置。', '下单前，或者改完板子后。'),
        ('board_render', '给板子拍图（2D 铜层或 3D），可以把 DRC 问题标出来。', '想看看板子长什么样、问题在哪。'),
        ('board_export_3d', '把带元件的板子导出成 3D 文件（STEP、GLB 等）。', '要把板子放进外壳里看装不装得下时。'),
        ('board_netlist', '列出所有元件（值、封装、嘉立创料号）和网络。', '核对元件和连线时。'),
        ('circuit_check', '用 ngspice 按真实网表算电源通路：各点电压、电流、稳压芯片够不够用。', '电路改完、担心电压掉太多或芯片过热时。'),
        ('calibrate_power', '用 ngspice 校准仿真里的简化电源模型（电容、内阻），可以直接写回工程。', '仿真电压和电路仿真对不上时。'),
        ('bind_pin', '把 ESP32 的引脚接到电机、舵机、按键、电位器上。', '告诉仿真“哪个引脚控制什么、读什么”。'),
        ('parts_search', '用大白话搜嘉立创有货的元件，比如“10k 0603 1%”“3.3V LDO 1A”。', '选型、找便宜有货的料时。'),
        ('part_info', '查一个嘉立创料号的实时库存、价格、是不是基础库、数据手册。', '确认某个料能不能买、贵不贵。'),
        ('parts_alternatives', '给一个料找能替换的有货元件，便宜的排前面。', '料缺货或者太贵时。'),
        ('board_parts_status', '把板子上所有带料号的元件一次查完：哪些缺货、哪些要加扩展库费用。', '下单前最后核对一遍。'),
    ]),
    ('仿真', '仿真', '把产品、板子和测试夹具放在一起跑真实的测试。', [
        ('import_firmware', '导入 ESP32 固件（ESP-IDF 编译目录或合并好的 .bin），列出能调的固件参数。', '有真实固件、想在仿真里原样跑时。'),
        ('write_behavior', '用一小段 Python 写固件的行为模型（读引脚、算、输出），代替真实固件。', '还没写固件、想先验证思路时。'),
        ('define_probe', '加测量点：加速度计/陀螺仪（IMU）、接触力、距离等。', '测试要看某个量时，比如计数器要读加速度。'),
        ('write_scenario', '写一个测试：多长时间、什么时候按键、怎么动、通过的标准是什么。', '要验证一个具体用法时，比如“举 5 次要数出 5 次”。'),
        ('run_scenario', '跑一个测试（固件、电源、电机、物理一起算），返回是否通过和关键数据。页面会切到仿真页回放。',
         '模型、板子、测试都准备好了。'),
        ('get_results', '看某次测试的结果，或取出某些曲线的数据。', '测试跑完要分析原因时。'),
        ('render_frames', '给某次测试的某几个时刻拍图。', '想看看某一瞬间零件是什么姿势。'),
        ('sweep', '把几个参数的各种组合都跑一遍，列出哪些组合通过。', '想找出参数在什么范围内都能用时。'),
        ('strength_check', '用有限元算打印件够不够结实：安全系数、最大应力、变形，页面上出一张按应力着色的 3D 图。',
         '担心零件会断、要选材料或打印设置时。'),
        ('gear_check', '算舵机输出齿轮的齿根应力和安全系数（齿轮尺寸默认是估计值）。', '舵机负载大、担心打齿时。'),
        ('fit_measurements', '用实测数据反推舵机、电源、开关手感的参数。', '量过真实的东西，想让仿真更准时。'),
        ('compare_trace', '把仿真曲线和实测曲线对比，给出误差。', '检查仿真和真实差多少。'),
    ]),
]


def described():
    return {t for _, _, _, rows in GROUPS for t, _, _ in rows}


def mcp_guide(tools):
    """tools: [(name, english description)] from the MCP server. Returns the page's sections, with
    tools not described here yet listed last under 其他 with their own (English) description."""
    names = {n for n, _ in tools}
    sections = [{'title': title, 'page': page, 'intro': intro,
                 'tools': [{'name': t, 'what': what, 'when': when} for t, what, when in rows if t in names]}
                for title, page, intro, rows in GROUPS]
    extra = [{'name': n, 'what': ' '.join((d or '').split()), 'when': ''} for n, d in tools if n not in described()]
    if extra:
        sections.append({'title': '其他', 'page': '', 'intro': '新加的工具，还没有中文说明。', 'tools': extra})
    return [s for s in sections if s['tools']]


def guide_markdown(sections):
    """The guide as Markdown, to paste to an AI or keep."""
    out = ['# SimWorld MCP 使用说明', '',
           'SimWorld 通过 MCP 服务 emcosim 把建模、PCB、仿真的能力交给你的 AI（Claude Code、Codex）。'
           'AI 操作的是浏览器里开着的 SimWorld 页面：它改了什么，页面会自己刷新。开始设计前先调用 design_rules 读规则。', '']
    for s in sections:
        out += [f"## {s['title']}" + (f"（{s['page']}页）" if s['page'] and s['page'] not in ('全部', s['title']) else ''), '']
        if s['intro']:
            out += [s['intro'], '']
        for t in s['tools']:
            out.append(f"- `{t['name']}`：{t['what']}" + (f" 什么时候用：{t['when']}" if t['when'] else ''))
        out.append('')
    return '\n'.join(out)
