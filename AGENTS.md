# Agent Notes for Trident_config

Klipper configuration for a Voron Trident with klipper-toolchanger, Beacon,
Maxwell tool detection, Orbiter v2.5, and Panda Breath. The printer deploys this
repository by Git pull, so every committed change can reach the machine.

Full history, rationale, and failure shields live in the owner's shared memory
repository (`llmContext`, `projects/voron-trident.md`). Read it when available.
This file holds only the rules that must never be missed.

## Before editing

- Check the current branch, working-tree status, the active include graph, and
  saved `#*#` sections at the end of `printer.cfg`.
- Inspect the installed plugin and extra source on the printer host rather than
  adapting upstream or Micron examples.
- Dock coordinates, pins, tool enablement, and tuning values are machine- and
  branch-specific. Never copy them from old examples.

## Owner decisions — ask before changing

- T0 stays on its existing Nitehawk mapping and is the Beacon
  contact-calibration reference.
- Beacon owns Klipper's probe commands. Use `detection_pin` on `[tool T0]` for
  Maxwell detection, not `[tool_probe T0]`.
- Keep classic `z_tilt`, not `z_tilt_ng`.
- Toolchanges require only X and Y homed (`uses_axis: xy`).
- Ask before changing board mapping, homing behavior, detection method, probing
  ownership, or enabled tool count.

## Known failures

- Orbiter v2.5 `rotation_distance: 4.69` already includes its reduction. Adding
  `gear_ratio: 7.5:1` caused severe over-extrusion.
- `[tool_probe T0]` with Beacon fails with `gcode command PROBE already
  registered`.
- The toolchanger plugin registers `T<n>`, `SELECT_TOOL`, `UNSELECT_TOOL`,
  `M106`, and `M107`. Do not redefine them locally.
- Later duplicate sections override earlier includes; an inline `[beacon]` in
  `printer.cfg` once overrode `beacon.cfg`. Saved `#*#` sections still parse
  when an include is commented out.
- Beacon and MadMax must never be active together.
- Klipper extruder numbering is contiguous: `[extruder1]` requires `[extruder]`.
- Adjacent docks are close. A combined `G0 X{detach_x} Y{safe_toolhead_y}`
  nearly hit a docked tool; split dropoff into X then Y moves.
- In `PRINT_START`, read `printer.beacon.last_z_result`, not `printer.probe`,
  and remove disabled tools from the offset loop.
- Use `QUERY_TOOLCHANGER` for read-only status; `VERIFY_TOOL_DETECTED` errored.

## Done means physically verified

Local validation, commit, push, host installation of Klipper extras, MCU
flashing, Klipper restart, and visible behavior on the printer are separate
milestones. Report which ones actually happened. A pushed branch is not a
deployment, and a static patch check is not a printer fix.
