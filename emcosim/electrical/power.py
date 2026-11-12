"""Equivalent power network: voltage sources with internal resistance, resistive
branches (fuse, wires, PCB copper), node capacitances (bulk caps), linear
regulators and current-sink loads. Integrated with backward Euler, which is
unconditionally stable for the stiff RC time constants (tens of microseconds)
that appear when a 0.1 ohm source meets a few hundred microfarads.

The model deliberately has no ESR or wiring inductance; `calibrate.py` fits the
effective R/C of this model against an ngspice transient that has them.
"""
from __future__ import annotations

import math

import numpy as np


class Ldo:
    """Linear regulator with dropout and an output capacitor that the regulator
    can charge but not discharge (a real LDO cannot sink current)."""

    def __init__(self, name, cfg):
        self.name = name
        self.input = cfg['input']
        self.v_out = float(cfg.get('v_out', 3.3))
        self.dropout = float(cfg.get('dropout_v', 1.1))
        self.c_out = float(cfg.get('c_out_f', 22e-6))
        self.i_q = float(cfg.get('i_q_a', 0.005))
        self.i_max = float(cfg.get('i_max_a', 1.0))
        self.v = 0.0
        self.i_in = 0.0

    def step(self, v_in, i_load, dt):
        target = min(self.v_out, max(0.0, v_in - self.dropout))
        # current needed to reach the target in one step, limited by the pass device
        need = i_load + self.c_out * (target - self.v) / dt
        i_pass = min(max(need, 0.0), self.i_max)
        self.v = max(0.0, self.v + (i_pass - i_load) * dt / self.c_out)
        if self.v > target + 1e-9 and i_pass > 0:
            self.v = target
        self.i_in = i_pass + (self.i_q if v_in > 0.5 else 0.0)
        return self.v


class PowerNetwork:
    def __init__(self, cfg):
        nodes = cfg.get('nodes', {})
        self.names = list(nodes)
        self.index = {n: i for i, n in enumerate(self.names)}
        n = len(self.names)
        self.C = np.array([float(nodes[k].get('c_f', 0.0) or 0.0) for k in self.names])
        self.G = np.zeros((n, n))
        self.leak = np.zeros(n)
        self.sources = {}
        for name, s in (cfg.get('sources') or {}).items():
            self.sources[name] = {'node': self.index[s['node']], 'v': float(s['v']),
                                  'r': max(float(s.get('r_ohm', 0.1)), 1e-6),
                                  'enabled': bool(s.get('enabled', True)),
                                  'i_limit': float(s['i_limit_a']) if s.get('i_limit_a') else None,
                                  'limited': False}
        self.branches = {}
        for name, b in (cfg.get('branches') or {}).items():
            a, c, r = self.index[b['from']], self.index[b['to']], max(float(b['r_ohm']), 1e-6)
            self.branches[name] = (a, c, r)
            g = 1.0 / r
            self.G[a, a] += g
            self.G[c, c] += g
            self.G[a, c] -= g
            self.G[c, a] -= g
        for k in self.names:
            self.leak[self.index[k]] = 1.0 / float(nodes[k].get('r_leak_ohm', 1e6))
        self.regulators = {name: Ldo(name, r) for name, r in (cfg.get('regulators') or {}).items()}
        self.v = np.zeros(n)
        self._dt = None
        self._ainv = {}
        self.load = np.zeros(n)
        self.reg_load = {k: 0.0 for k in self.regulators}

    # -- configuration that may change during a run (scenario events) ----------
    def set_source(self, name, v=None, enabled=None, r_ohm=None):
        s = self.sources[name]
        if v is not None:
            s['v'] = float(v)
        if enabled is not None:
            s['enabled'] = bool(enabled)
        if r_ohm is not None:
            s['r'] = max(float(r_ohm), 1e-6)
        self._dt = None
        self._ainv = {}

    def node_voltage(self, node):
        if node in self.regulators:
            return self.regulators[node].v
        return float(self.v[self.index[node]])

    def _A(self, dt, limited):
        A = np.diag(self.C / dt) + self.G + np.diag(self.leak)
        for name, s in self.sources.items():
            if s['enabled'] and name not in limited:
                A[s['node'], s['node']] += 1.0 / s['r']
        return A

    def _matrix(self, dt, limited):
        if self._dt != dt:
            self._ainv = {}
            self._dt = dt
        if limited not in self._ainv:
            A = np.diag(self.C / dt) + self.G + np.diag(self.leak)
            for name, s in self.sources.items():
                if s['enabled'] and name not in limited:
                    A[s['node'], s['node']] += 1.0 / s['r']
            self._ainv[limited] = np.linalg.inv(A)
        return self._ainv[limited]

    def initialise(self):
        """Settle to the DC operating point with no load."""
        for _ in range(200):
            self.step(1e-3, {}, {})

    def step(self, dt, node_loads, reg_loads, node_g=None):
        """node_loads: {node: current drawn [A]}; node_g: {node: extra conductance to ground [S]}
        (Norton part of a load that depends on the node voltage, solved implicitly);
        reg_loads: {regulator: current drawn from its output}."""
        load = np.zeros(len(self.names))
        for node, i in node_loads.items():
            load[self.index[node]] += i
        gdiag = np.zeros(len(self.names))
        for node, g in (node_g or {}).items():
            gdiag[self.index[node]] += g
        for name, reg in self.regulators.items():
            v_in = float(self.v[self.index[reg.input]])
            reg.step(v_in, reg_loads.get(name, 0.0), dt)
            load[self.index[reg.input]] += reg.i_in
        base = self.C / dt * self.v - load
        # Sources with a current limit (USB port switches, bench supplies) turn into
        # current sources when the load asks for more; solve, check, re-solve.
        limited = frozenset(k for k, s in self.sources.items() if s['limited'])
        for _ in range(3):
            rhs = base.copy()
            for name, s in self.sources.items():
                if not s['enabled']:
                    continue
                if name in limited:
                    rhs[s['node']] += s['i_limit']
                else:
                    rhs[s['node']] += s['v'] / s['r']
            if gdiag.any():
                v = np.linalg.solve(self._A(dt, limited) + np.diag(gdiag), rhs)
            else:
                v = self._matrix(dt, limited) @ rhs
            new = set(limited)
            for name, s in self.sources.items():
                if not s['enabled'] or s['i_limit'] is None:
                    continue
                if name in limited:
                    if v[s['node']] >= s['v'] - s['i_limit'] * s['r']:
                        new.discard(name)          # load fell below the limit again
                elif (s['v'] - v[s['node']]) / s['r'] > s['i_limit']:
                    new.add(name)
            if frozenset(new) == limited:
                break
            limited = frozenset(new)
        for name, s in self.sources.items():
            s['limited'] = name in limited
        self.v = v
        self.load = load + gdiag * v
        np.maximum(self.v, -0.7, out=self.v)       # body diodes clamp reverse voltage
        return self.v

    def source_current(self, name):
        s = self.sources[name]
        if not s['enabled']:
            return 0.0
        if s['limited']:
            return s['i_limit']
        return (s['v'] - self.v[s['node']]) / s['r']

    def branch_current(self, name):
        a, c, r = self.branches[name]
        return (self.v[a] - self.v[c]) / r

    def signals(self):
        out = {f'node.{k}.v': float(self.v[i]) for k, i in self.index.items()}
        for k, reg in self.regulators.items():
            out[f'node.{k}.v'] = reg.v
            out[f'reg.{k}.i_in'] = reg.i_in
        for k, s in self.sources.items():
            out[f'source.{k}.i'] = float(self.source_current(k))
            if s['i_limit'] is not None:
                out[f'source.{k}.limited'] = float(s['limited'])
        for k in self.branches:
            out[f'branch.{k}.i'] = float(self.branch_current(k))
        return out


class BrownoutDetector:
    """Holds the MCU in reset while its supply is below v_trip; releases above
    v_release (hysteresis), after the supply has been good for t_release."""

    def __init__(self, cfg):
        self.v_trip = float(cfg.get('v_trip', 2.43))
        self.v_release = float(cfg.get('v_release', self.v_trip + 0.1))
        self.t_release = float(cfg.get('t_release_s', 0.0))
        self.good = False
        self._good_since = None
        self.events = []

    def update(self, t, v):
        if self.good and v < self.v_trip:
            self.good = False
            self._good_since = None
            self.events.append({'t': t, 'event': 'brownout', 'v': round(v, 4)})
        elif not self.good and v >= self.v_release:
            if self._good_since is None:
                self._good_since = t
            if t - self._good_since >= self.t_release:
                self.good = True
                self.events.append({'t': t, 'event': 'power_good', 'v': round(v, 4)})
        elif not self.good:
            self._good_since = None
        return self.good


def rc_time_constant(r, c):
    return r * c if r > 0 and c > 0 else 0.0


def ldo_input_needed(v_out, dropout):
    return v_out + dropout


def describe(cfg):
    """One-line human summary used in reports."""
    parts = []
    for k, s in (cfg.get('sources') or {}).items():
        parts.append(f"{k}: {s['v']} V / {s.get('r_ohm', 0.1)} ohm -> {s['node']}")
    for k, b in (cfg.get('branches') or {}).items():
        parts.append(f"{k}: {b['from']}->{b['to']} {b['r_ohm']} ohm")
    for k, n in (cfg.get('nodes') or {}).items():
        if n.get('c_f'):
            parts.append(f"{k}: {n['c_f'] * 1e6:.1f} uF")
    return '; '.join(parts)


__all__ = ['PowerNetwork', 'Ldo', 'BrownoutDetector', 'describe', 'math']
