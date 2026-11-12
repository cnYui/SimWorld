#!/usr/bin/env bash
# Opens emcosim in the browser with the example project. Ctrl+C stops it.
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
exec "$ROOT/.venv/bin/python" -m emcosim --project "${1:-$ROOT/examples/switch_robot}" serve
