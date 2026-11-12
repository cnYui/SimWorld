// 开始：打开最近工程 / 示例 / 新建工程，环境状态（QEMU、Claude Code）。
import { post } from '../api.js';
import { store } from '../store.js';
import { h, icon, clear, modal, pathField, toast, toastError, nativePick } from '../ui.js';

export function createStart(ctx) {
  const body = h('div', { class: 'screen-body' });
  const el = h('section', { class: 'screen', 'aria-label': '开始' },
    h('div', { class: 'screen-head' }, h('h1', {}, '开始'), h('span', { class: 'sub' }, '打开或新建一个仿真工程')),
    body);

  async function openPath(path, example = false) {
    try {
      const r = await post(example ? 'project/open_example' : 'project/open', { path });
      await afterOpen();
      if (r.copied_to) toast(`示例已复制到 ${r.copied_to}，在副本上修改不会影响安装目录`, { timeout: 8000 });
      ctx.nav('overview');
    } catch (e) { toastError(e); }
  }

  async function afterOpen() {
    store.seenRuns.clear(); store.newRuns.clear();
    await store.loadApp();
    await store.loadProject();
    await store.loadRuns();
  }

  async function openDialog() {
    const picked = await nativePick('folder');
    if (picked) return openPath(picked);
    const f = pathField({ label: '工程目录（包含 project.yaml）', kind: 'folder', placeholder: 'D:\\robots\\switch_robot' });
    const ok = await modal({
      title: '打开工程', body: f.el,
      actions: [{ label: '取消', value: false }, { label: '打开', kind: 'primary', handler: () => { if (!f.value) throw new Error('请填写路径'); return true; } }],
    });
    if (ok) openPath(f.value);
  }

  async function newDialog() {
    const f = pathField({ label: '新工程目录（不存在会自动创建）', kind: 'folder', placeholder: 'D:\\robots\\my_robot' });
    const name = h('input', { class: 'input', placeholder: '例如 my_robot' });
    const ok = await modal({
      title: '新建工程',
      body: [f.el, h('label', { class: 'field' }, '工程名称', name),
        h('div', { class: 'hint' }, '新工程是空的：之后在“工程总览”里导入 PCB（KiCad）、3D 模型和 ESP32 固件，或者直接让右侧的 AI 助手帮你搭。')],
      actions: [{ label: '取消', value: false }, {
        label: '创建', kind: 'primary', handler: async () => {
          if (!f.value) throw new Error('请填写目录');
          await post('project/new', { path: f.value, name: name.value.trim() || undefined });
          return true;
        },
      }],
    });
    if (ok) { await afterOpen(); ctx.nav('overview'); }
  }

  function projRow(path, { current, example } = {}) {
    const name = path.split(/[\\/]/).filter(Boolean).pop();
    return h('button', { class: 'proj' + (current ? ' current' : ''), onclick: () => (current ? ctx.nav('overview') : openPath(path, example)) },
      h('span', { class: 'ico' }, icon(example ? 'cube' : 'folder')),
      h('span', { class: 'grow' }, h('div', { class: 't' }, name, current ? h('span', { class: 'chip accent', style: { marginLeft: '8px' } }, '当前') : null),
        h('div', { class: 'p' }, path)),
      icon('chevron', 'sm'));
  }

  function render() {
    const app = store.app || {};
    const recent = app.recent_projects || [];
    const cs = store.chatStatus;
    const q = app.qemu || {};
    const cc = cs?.claude_code;
    clear(body).append(h('div', { class: 'start-grid' },
      h('div', { class: 'hero' },
        h('div', { style: { color: 'var(--accent)' } }, icon('logo', '')),
        h('div', {}, h('h1', {}, 'SimWorld 电子 + 物理联合仿真'),
          h('p', {}, 'ESP32 固件在 QEMU 里真实运行，舵机/电机电气模型、电源网络和 MuJoCo 机械按 1 ms 锁步联合仿真。'))),
      h('div', { class: 'col' },
        h('div', { class: 'card' },
          h('header', {}, h('h2', {}, icon('folder'), '工程'),
            h('div', { class: 'row', style: { marginLeft: 'auto' } },
              h('button', { class: 'btn', onclick: openDialog }, icon('folder'), '打开文件夹…'),
              h('button', { class: 'btn primary', onclick: newDialog }, icon('plus'), '新建工程'))),
          h('div', { class: 'body flush' },
            recent.length ? recent.map((p) => projRow(p, { current: p === app.project }))
              : h('div', { class: 'empty' }, '还没有最近打开的工程'))),
        h('div', { class: 'card' },
          h('header', {}, h('h2', {}, icon('cube'), '示例工程'), h('span', { class: 'meta' }, '打开时复制一份到“文档/emcosim”')),
          h('div', { class: 'body flush' },
            (app.examples || []).length ? app.examples.map((p) => projRow(p, { example: true, current: p === app.project }))
              : h('div', { class: 'empty' }, '没有找到自带示例')))),
      h('div', { class: 'col' },
        h('div', { class: 'card' },
          h('header', {}, h('h2', {}, icon('gear'), '运行环境')),
          h('div', { class: 'body' },
            h('div', { class: 'env-row' }, h('span', { class: 'led ' + (q.found ? 'ok' : 'warn') }),
              h('div', { class: 'grow' }, h('div', {}, h('b', {}, 'QEMU（ESP32 固件仿真）'), ' ', q.found ? '已找到' : '未找到'),
                q.found ? h('div', { class: 'mono dim', style: { overflowWrap: 'anywhere' } }, q.path)
                  : h('div', { class: 'note', style: { marginTop: '6px' } }, icon('alert', 'sm'),
                    h('span', {}, '运行固件场景（mcu_backend: qemu，例如 toggle_nominal）需要打过补丁的 QEMU。没有 QEMU 也能跑 scripted 场景（例如 servo_sweep_scripted），用来快速扫机械和电气参数。',
                      q.error ? h('div', { class: 'mono', style: { marginTop: '4px' } }, q.error) : null)))),
            h('div', { class: 'env-row' }, h('span', { class: 'led ok' }),
              h('div', { class: 'grow' }, h('b', {}, '版本'), ' ', h('span', { class: 'mono' }, app.version || ''))))),
        h('div', { class: 'card' },
          h('header', {}, h('h2', {}, icon('info'), '工作流程')),
          h('div', { class: 'body' },
            h('ol', { style: { margin: 0, paddingLeft: '18px', lineHeight: 1.8 } },
              h('li', {}, '在“工程总览”检查 PCB、机械、电源网络和引脚绑定'),
              h('li', {}, '在“参数”里修改电源/舵机/开关参数（写回 project.yaml）'),
              h('li', {}, '在“场景”里运行，或加临时覆盖只改这一次'),
              h('li', {}, '在“结果”里看 3D 回放、曲线、串口日志，并对比两次运行'),
              h('li', {}, '用“扫参”批量试不同参数，找最稳的一组'),
              h('li', {}, '在“打印”里把零件发到 Bambu Studio 切片'))))),
    ));
  }

  store.on('app', render);
  store.on('chatStatus', render);
  return { el, show: render };
}
