"""ESP32 firmware in Espressif's QEMU (patched with the lock-step link in
platform/qemu/). QEMU is GPL and runs as a separate process; this module only
talks to it over a socket (TCP on localhost on Windows, a UNIX socket elsewhere)
and reads its serial console from a file.

Every exchange advances the emulator by `step_ms` of virtual time
(-icount, so virtual time does not depend on host speed).
"""
from __future__ import annotations

import os
import re
import shutil
import socket
import subprocess
import tempfile
import time
from pathlib import Path

from .. import firmware as fwmod
from ..toolpaths import local_qemu

EVT = re.compile(r'^EVT\s+(\S+)\s*(.*)$')


def _bundled_qemu():
    """QEMU shipped next to the frozen Windows app (installer layout: qemu/)."""
    import sys
    if getattr(sys, 'frozen', False):
        for name in ('qemu-system-xtensa.exe', 'qemu-system-xtensa'):
            cand = Path(sys.executable).parent / 'qemu' / name
            if cand.exists():
                return str(cand)
    return None


def find_qemu():
    exe = os.environ.get('EMCOSIM_QEMU') or _bundled_qemu() or local_qemu() \
        or shutil.which('qemu-system-xtensa-cosim') or shutil.which('qemu-system-xtensa')
    if not exe:
        raise RuntimeError('No patched qemu-system-xtensa found. Run scripts/setup.sh (or scripts\\setup.ps1 on '
                           'Windows), build it with qemu/build.sh, or set EMCOSIM_QEMU to the binary.')
    roms = os.environ.get('EMCOSIM_QEMU_ROMS')
    if not roms:
        for cand in (Path(exe).resolve().parent / 'share' / 'qemu', Path(exe).resolve().parent.parent / 'share' / 'qemu',
                     Path(exe).resolve().parent / 'pc-bios', Path(exe).resolve().parent.parent / 'pc-bios'):
            if (cand / 'esp32-v3-rom.bin').exists():
                roms = str(cand)
                break
    return exe, roms


class QemuMcu:
    backend = 'qemu'

    def __init__(self, flash_image, params=None, step_ms=1, icount_shift=3, connect_timeout_s=30,
                 workdir=None, transport=None):
        self.step_ms = step_ms
        self._tmp = tempfile.TemporaryDirectory(prefix='emcosim-')
        work = Path(workdir or self._tmp.name)
        self.flash = fwmod.build_flash(flash_image, params or {}, work / 'flash.bin')
        tmp = Path(self._tmp.name)
        exe, roms = find_qemu()
        transport = transport or os.environ.get('EMCOSIM_COSIM_TRANSPORT') or (
            'tcp' if os.name == 'nt' or not hasattr(socket, 'AF_UNIX') else 'unix')
        if transport == 'tcp':
            self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.listener.bind(('127.0.0.1', 0))
            address = 'tcp:127.0.0.1:%d' % self.listener.getsockname()[1]
        else:
            address = str(tmp / 'cosim.sock')
            self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.listener.bind(address)
        self.listener.listen(1)
        self.listener.settimeout(connect_timeout_s)
        # The serial console goes to a file: QEMU writes it synchronously before
        # it sends the next lock-step frame, so reading the file up to that frame
        # timestamps every line deterministically on every host OS.
        self.uart_path = tmp / 'uart.txt'
        self.uart_path.write_bytes(b'')
        self._uart_file = open(self.uart_path, 'rb')
        self._log = open(tmp / 'qemu.log', 'wb')
        cmd = [exe, '-display', 'none', '-monitor', 'none', '-serial', f'file:{self.uart_path}',
               '-machine', 'esp32', '-icount', f'shift={icount_shift},sleep=off',
               '-drive', f'file={self.flash},if=mtd,format=raw']
        if roms:
            cmd += ['-L', str(roms)]
        env = dict(os.environ, ESP32_COSIM_SOCKET=address,
                   ESP32_COSIM_PERIOD_US=str(int(step_ms * 1000)))
        flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
        self.proc = subprocess.Popen(cmd, env=env, stdin=subprocess.DEVNULL, stdout=self._log,
                                     stderr=subprocess.STDOUT, creationflags=flags)
        try:
            self.conn, _ = self.listener.accept()
        except socket.timeout:
            out = self._qemu_log()
            self.close()
            raise RuntimeError('QEMU did not connect to the co-simulation socket; is it the patched '
                               f'build (platform/qemu)? Output:\n{out[-2000:]}') from None
        self.conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1) if transport == 'tcp' else None
        self.conn.settimeout(connect_timeout_s)
        self.rfile = self.conn.makefile('rb')
        self.uart = []          # (t, line)
        self.events = []        # (t, name, rest)
        self._partial = ''
        self.resets = 0
        self.vtime_ns = 0

    def _qemu_log(self):
        try:
            self._log.flush()
            return (Path(self._tmp.name) / 'qemu.log').read_bytes().decode(errors='replace')
        except (OSError, ValueError):
            return ''

    def _drain_raw(self):
        try:
            data = self._uart_file.read() or b''
        except (OSError, ValueError):
            data = b''
        return data.decode(errors='replace')

    def _collect_uart(self, t):
        text = self._drain_raw()
        if not text:
            return
        text = self._partial + text
        lines = text.split('\n')
        self._partial = lines.pop()
        for line in lines:
            line = line.rstrip('\r')
            if not line:
                continue
            self.uart.append((round(t, 4), line))
            m = EVT.match(line)
            if m:
                self.events.append((round(t, 4), m.group(1), m.group(2)))

    def exchange(self, t, inputs_mask, power_good):
        """Run one step. Returns {'out': mask, 'enable': mask, 'pwm': {pin: (freq, duty)}}."""
        line = self.rfile.readline()
        if not line:
            raise RuntimeError('QEMU closed the co-simulation link:\n' + self._qemu_log()[-2000:])
        parts = line.split()
        # F seq vtime out en resets npwm pin:freq:duty...
        self.vtime_ns = int(parts[2])
        out, en, resets, npwm = int(parts[3], 16), int(parts[4], 16), int(parts[5]), int(parts[6])
        self.resets = resets
        pwm = {}
        for item in parts[7:7 + npwm]:
            pin, freq, duty = item.decode().split(':')
            pwm[int(pin)] = (float(freq), float(duty))
        self._collect_uart(t)       # everything printed before this frame belongs to <= t
        self.conn.sendall(f'I {inputs_mask:x} {1 if power_good else 0}\n'.encode())
        return {'out': out, 'enable': en, 'pwm': pwm}

    def close(self):
        try:
            if getattr(self, 'conn', None):
                try:
                    self.rfile.readline()
                    self.conn.sendall(b'Q\n')
                except OSError:
                    pass
                self.conn.close()
        finally:
            if self.proc.poll() is None:
                try:
                    self.proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
            time.sleep(0)
            self._collect_uart(self.uart[-1][0] if self.uart else 0)
            self.listener.close()
            for f in (self._uart_file, self._log):
                try:
                    f.close()
                except (OSError, AttributeError):
                    pass
            self._tmp.cleanup()


class ScriptedMcu:
    """Stand-in MCU for fast tests and sweeps that do not need the firmware: pin
    outputs come from the scenario's `pin_script` events."""

    backend = 'scripted'

    def __init__(self, script):
        self.script = sorted(script or [], key=lambda e: e['t'])
        self.state = {}
        self.uart, self.events = [], []
        self.resets = 0
        self._was_good = True
        self._i = 0

    def exchange(self, t, inputs_mask, power_good):
        while self._i < len(self.script) and self.script[self._i]['t'] <= t + 1e-9:
            e = self.script[self._i]
            if 'pulse_us' in e:
                freq = e.get('freq', 50.0)
                self.state[e['pin']] = (freq, e['pulse_us'] * 1e-6 * freq)
            elif 'duty' in e:
                self.state[e['pin']] = (e.get('freq', 1000.0), e['duty'])
            elif 'level' in e:
                self.state[e['pin']] = (0.0, float(e['level']))
            elif e.get('off'):
                self.state.pop(e['pin'], None)
            self._i += 1
        if not power_good:
            if self._was_good:
                self.resets += 1
                self.events.append((round(t, 4), 'reset', 'brownout'))
            self._was_good = False
            return {'out': 0, 'enable': 0, 'pwm': {}}
        self._was_good = True
        pwm = {p: v for p, v in self.state.items() if v[0] > 0}
        out = sum(1 << p for p, v in self.state.items() if v[0] == 0 and v[1] >= 0.5)
        en = sum(1 << p for p in self.state)
        return {'out': out, 'enable': en, 'pwm': pwm}

    def close(self):
        pass
