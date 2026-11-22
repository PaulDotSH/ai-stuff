# 3D printer

What agents need to know before slicing for, or talking to, the printer.
Last checked against the printer on 2026-11-18. It was switched off on
2026-11-22, when this note was written, so nothing here was re-read from it.

## The printer

- **Creality CR-10 SE**, borrowed from a friend. The owner calls it "Ender 3".
  Model code F003, Creality OS V1.1.0.28, which is Klipper underneath.
- Bed 220 x 220 mm, Z 265 mm, direct drive, 0.4 mm nozzle, textured PEI sheet.
- LAN only, at `192.168.0.77`.
- Filament used so far: PLA+ at 220 C nozzle, 60 C bed, 0.8 mm retraction,
  100% fan (Creality's own PLA values).

## Access

- Rooted on 2026-11-18. `ssh -i ~/.ssh/ender3_claude root@192.168.0.77`.
  The key is ECDSA because the printer's dropbear (2019.78) cannot do ed25519.
- There is **no Moonraker, Fluidd or Mainsail**. Control goes through the
  Klipper unix socket `/tmp/klippy_uds` over SSH. `printer.py` in the
  `dragon-bookmark` project wraps it (status, files, upload, gcode, print).
- G-code folder on the printer: `/usr/data/printer_data/gcodes`.
- Camera: `http://192.168.0.77:8080/?action=snapshot` returns a JPEG. It shows
  only part of the bed. No timelapse frames are kept on the printer.

## Known faults and quirks

- **Accelerometer (ADXL345) on the print head does not respond**: Klipper
  reports `Invalid adxl345 id (got ff vs e5)`. Because of that, the on-screen
  calibration fails at the vibration compensation step, while it shakes X.
  Do not run input shaping or the full self-check until this is fixed.
- The owner reported X overshooting and crashing during that calibration.
- X and Y home **without endstop switches** (sensorless, TMC2209). X uses
  SGTHRS 78 and 0.2 A homing current, which are Creality's defaults.
- `G28` is Creality's own homing macro: it heats the nozzle to 140 C, probes,
  and **clears the bed mesh**. A saved mesh named `default` exists.
- Details under 1 mm come out messy with the 0.4 mm nozzle, and thin tips curl
  with the fan at 100% from layer 2.
- Removing a part hot tore its bed face once. Let the bed cool, then flex the
  sheet. No scraper.

## Rules

- Read-only queries are fine at any time.
- Heating, moving or printing needs the owner's go-ahead **each time**, and
  never while someone is working on the machine.

## Slicing

- **PrusaSlicer 2.9.7** runs from the command line for scripted prints. It has
  no CR-10 SE profile, so `dragon-bookmark/make_printer_config.py` builds
  `slicer_config/cr10_se.ini` from the Ender-3 profile plus values read from
  the printer. `slice.ps1` wraps the slicer.
- **OrcaSlicer 2.5.2** is installed for interactive use (winget
  `SoftFever.OrcaSlicer`). It ships a Creality CR-10 SE profile. It cannot
  send to this printer, because there is no Moonraker to send to: export the
  G-code and upload it with `printer.py`.
- New print projects go in `repos/3d-printer-models`. Older ones are still in
  `repos/other` (dragon-bookmark, conifer-sleeve, can-cap, subscara, streasina).

## Filament tracking

- **Spoolman 0.27.1** runs on the mini PC in docker: `http://192.168.0.226:7912`.
  LAN only, no login, no public route. `minipc/services/spoolman/run.sh`
  recreates it. Data is `~/services/spoolman/data/spoolman.db`, and
  `stack-backup.sh` snapshots it to `databases/spoolman/`.
- **Spoolio 0.11.6** is the OrcaSlicer plugin, subscribed from Orca Cloud and
  pointed at that server (settings in `%APPDATA%\OrcaSlicer\spoolio\`). Its
  Slice Sync check warns after a slice when the spools cannot cover the plate.
- Spoolio can only subtract filament by itself for printers it can watch
  (Moonraker, OctoPrint, PrusaLink, Bambu). This printer has none of those, so
  **subtracting is manual**: `repos/3d-printer-models/spool.py`.
  - `spool.py load <id>` marks the spool on the printer (Spoolman location
    `CR-10 SE`).
  - `spool.py check <file.gcode>` before a print. Exit code 1 means not enough.
  - `spool.py use <file.gcode>` after a print, with `--percent N` for a print
    that failed partway.
- The numbers are only as good as the starting weight. Weigh a part-used
  spool and enter that, minus the empty spool.

## OrcaSlicer plugins

All ten come from Orca Cloud (File > Plugins) and were active on 2026-11-22.
They live under `%APPDATA%\OrcaSlicer\orca_plugins\_subscribed\`, and
OrcaSlicer updates them from there.

| Plugin | Version | What it is for |
| --- | --- | --- |
| Spoolio | 0.11.6 | Spoolman spools inside OrcaSlicer, see Filament tracking |
| OrcaSlicer MCP | 0.2.4 | Lets an AI assistant read and change the open OrcaSlicer, see below |
| ModelFinder | 1.2.0 | Search Printables, MakerWorld and Thingiverse in a tab and load a file |
| Arc Overhang | 0.4.2 | Prints steep overhangs as concentric arcs, without supports |
| Support Fins | 0.2.0 | Adds breakaway fins and a bed pad to a part, baked into the mesh |
| FilamentHub | 0.2.10 | Community filament profiles. It has its own spool inventory: do not use it, Spoolman is the one inventory |
| BumpMesh Textures (`rt`) | 1.0.1 | Texture pack for texture displacement |
| FHSL Textiles & Materials | 1.0.2 | Height maps for texture displacement |
| Print Calculator | 0.9.2 | Prices a plate: filament, energy, labor, fees |
| U1 Control | 0.17.0 | Controls a Snapmaker U1. No use on the CR-10 SE |

## OrcaSlicer MCP

Two halves, both by Max Ellis (`github.com/maxellis/orcaslicer-mcp`):

- The **plugin** runs inside OrcaSlicer and serves HTTP on `127.0.0.1:13131`.
  Requests need the `X-Api-Token` header. The token is in `token.txt` in the
  plugin's data folder and never belongs in this repo.
- The **connector** is the MCP server an agent talks to: `uvx orcaslicer-mcp`
  (PyPI `orcaslicer-mcp`). It finds the running OrcaSlicer, port and token by
  itself. Register it once per harness, for Claude Code:
  `claude mcp add --scope user orcaslicer -- uvx orcaslicer-mcp`

On stock OrcaSlicer the plugin can read settings, presets, the objects on the
plate and the project, change settings (as unsaved edits), switch presets, and
read the slice the user makes with Slice plate, including its G-code. It
cannot load or move models, start a slice, render the plate, manage plates,
open or save projects, or save presets. Those need the author's OrcaSlicer
build, which is not installed.

It only answers once OrcaSlicer's **Prepare** tab has been shown, because the
plugin's panel is created there. Before that every request returns
`503 panel_not_open`.

State on 2026-11-22: with Prepare open, all seven read routes answered 200
when called directly over HTTP (`/api/v1/status`, `presets`, `project`,
`objects`, `slice/status`, `user_changes`, `config?keys=...`), and a request
without the token got 401. The writes (`PUT /api/v1/config`, `PUT
/api/v1/preset`) were not tried. The connector was not registered with any
agent and has not been run, so the MCP tools themselves are untested.

OrcaSlicer is set to `Creality CR-10 SE 0.4 nozzle`, process `0.20mm Standard
@Creality CR10SE 0.4`, filament `Generic PLA @CR-10 SE-all`. That filament
preset prints at 230 C. The PLA+ used so far went at 220 C.
