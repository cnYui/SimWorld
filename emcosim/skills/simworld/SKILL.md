---
name: simworld
description: Rules for designing a product with SimWorld through its emcosim MCP tools — which page each piece of work belongs to, product parts versus test fixtures, and filling project data instead of writing page code. Use whenever you call emcosim tools to model, lay out a PCB, or simulate.
---

# Designing in SimWorld

SimWorld is a page in the user's browser plus a local backend. You change the project through the
emcosim MCP tools; the page shows whatever the project data says and refreshes by itself. You never
write or edit page code (HTML, JS, CSS) to show something: if the page cannot show a result, tell the
user what is missing instead.

## What is fixed and what you fill in

Fixed by SimWorld (do not work around it): the backend calculations (MuJoCo physics, ngspice,
KiCad checks, QEMU firmware, FEA strength), the project data format (project.yaml and the files it
points to), the MCP tools, and the page's display components: the 3D view, curves, tables and
pictures.

Yours to fill in: the project data, through the tools: parts, joints, materials, print settings,
the schematic and board, firmware or a behaviour model, tests (scenarios), and parameters.

## Three pages, one job each

The left menu is PCB 元件 → 建模 → 仿真, the order a beginner works in. Put each piece of work on
the page that owns it, and nowhere else.

- PCB 元件: everything electrical. Components (with 嘉立创 C numbers when known), schematic, board,
  KiCad DRC/ERC, the ngspice circuit check, power and MCU parameters, pin wiring. Tools:
  schematic_create, schematic_edit, board_from_schematic, board_autoroute, board_drc, board_erc,
  board_edit, circuit_check, parts_search / part_info, bind_pin, import_pcb, set_param on power.*
  and mcu.*.
- 建模: the product's mechanical design only. Its parts, how they fit, joints inside the product,
  materials and print settings, motors and gears, 3MF / Bambu export. No PCB geometry and no test
  fixtures appear here. Tools: add_shape, import_model, part_edit, part_delete, define_joint,
  model_info, render_model, set_param on printing.* and actuators.*.
- 仿真: the product (PCB + model) together with its test fixtures and environment, running real
  tests: motion replay, strength and stress (strength_check, gear_check), the stress-coloured 3D
  picture, history and comparison of runs. Tools: write_scenario, define_probe, write_behavior or
  import_firmware, run_scenario, get_results, render_frames, strength_check, gear_check, sweep. When you run a test the page switches here by itself.

When the user asks to "put the PCB and the model together and simulate", that is 仿真 work: do not
add fixtures or test props while the user is only modelling.

## Product parts and test fixtures

Every mechanical part is one of two kinds:

- product (default): what the user is designing and will build: case, lid, bracket, strap, knob,
  arm, gear. Shown on 建模.
- fixture: what the product is tested on or with, and is not part of what the user builds: a
  barbell or bar, weight plates, a table, a floor, a wall, an existing wall switch, a hand, a
  person. Simulated on 仿真, hidden on 建模.

Make fixtures with role="fixture": add_shape(..., role="fixture"), import_model(..., role="fixture"),
or change an existing part with part_edit(name, role="fixture"). In a project with an MJCF base
model, list fixture bodies or geom-name prefixes under mechanics.fixtures with set_param, for
example set_param("mechanics.fixtures", ["wall", "rocker"]). A product part may hang on a fixture
(a strap clamped on a bar keeps role product).

Add fixtures only when a test needs them, and say which parts you treated as fixtures.

## Parameters the user did not ask about

Leave them empty or at the tool's default; do not invent values to make a page look complete.
When a value has to be assumed for a calculation (mass without a material, a motor constant), the
tools flag it as assumed; tell the user which values are assumed and how to measure them. Never
present an assumed value as a measurement.

## Working loop

1. current_project (open_project / new_project if needed).
2. 建模: build or import the product parts; render_model to look at them; tell the user what you
   made.
3. PCB 元件: components, schematic, board, circuit_check.
4. 仿真: add fixtures and a test (write_scenario), run_scenario, get_results; strength_check / gear_check when
   loads matter.
5. Change parameters with set_param and run again until the test passes; report what changed and
   what the result depends on.

Before a change the user did not ask for, say what you are going to change.
