"""Regenerate pcb/carrier_v04.kicad_pcb from the EasyEDA Pro sources in
electronics/v04 (stop-gap until EasyEDA's own KiCad export is used)."""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE.parents[1]))
from emcosim.importers import easyeda_pro  # noqa: E402

V04 = HERE.parents[2] / 'electronics' / 'v04'
print(easyeda_pro.convert(V04 / 'carrier_v04.epcb', V04 / 'carrier_v04.dsn', HERE / 'pcb' / 'carrier_v04.kicad_pcb',
                          V04 / 'carrier_v04_bom.csv'))
