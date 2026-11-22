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
- **Spoolio 0.11.6** is the OrcaSlicer plugin, installed by hand in
  `%APPDATA%\OrcaSlicer\orca_plugins\spoolio\`, pointed at that server. Its
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
