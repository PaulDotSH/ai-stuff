# minipc

Scripts that run on the home mini PC (Arch, user `minipc`). This folder is
the source of truth for the code. On the mini PC it is checked out at
`~/ai-stuff`, and every live path is a symlink into that checkout, so an edit
on either side changes the same file.

| Repo path                | Live path on the mini PC          |
| ------------------------ | --------------------------------- |
| `stack-backup.sh`        | `~/.local/bin/stack-backup.sh`    |
| `bin/<name>`             | `~/.local/bin/<name>`             |
| `services/<dir>/<file>`  | `~/services/<dir>/<file>`         |
| `../skills/jellyfin-watched-delete/scripts/watched_delete.py` | `~/services/watched-delete/watched_delete.py` |

Code that already ships as a skill script is linked to that copy (the `EXTRA`
list in `link.sh`), so the repo holds one copy of it, not two.

Only code lives here. State, configs and secrets stay next to the code on the
mini PC (for example `~/services/watched-delete/config.json` and
`jellyfin.token`), and `stack-backup.sh` copies them into the backup folder.
Paths in these scripts are host-specific on purpose.

## Setup / repair

```bash
git clone https://github.com/PaulDotSH/ai-stuff ~/ai-stuff
bash ~/ai-stuff/minipc/link.sh          # creates the symlinks
bash ~/ai-stuff/minipc/link.sh --check  # reports anything unlinked or drifted
```

`link.sh` replaces a live file only when it is identical to the repo copy. A
live file that differs is reported as `DRIFTED` and left alone. This usually
means an editor saved it with write-then-rename, which turns the symlink back
into a plain file. Copy the live file over the repo one, commit it, then run
`link.sh` again.

Syncing with other machines is plain git: `git -C ~/ai-stuff pull` on the
mini PC after pushing from elsewhere, and commit/push from wherever an edit
was made.

## stack-backup.sh

This builds a single migration-ready folder at `~/backup/stack-migration`
containing configs, secrets, databases and state for the self-hosted stack.
The root-only groups need sudo, so run it from an interactive SSH session:

```bash
ssh -t minipc@192.168.0.226 stack-backup.sh
```

`latest-backup.txt` records when it last ran and whether the run was
complete. `inventory.txt` lists listening ports, containers, enabled units,
the ai-stuff commit and any script that has drifted from its symlink.

## Browser tooling (invisible-playwright)

- `~/invisible-playwright` is the stealth Firefox with the Playwright API (venv `.venv`, wrapper `invpw` from `minipc/bin`). It is not in git; reinstall with `uv pip install --python ~/invisible-playwright/.venv/bin/python -U invisible-playwright`, then `~/.local/bin/invisible-playwright fetch` to restore the engine.
- Host-side browser steps need headless mode (`headless=True`) and run as user `minipc`.
- uBlock Origin is shipped in the trawl container at `/opt/camoufox/addons/UBO`. `~/services/ext-magnet/ublock` is a host copy. It loads with `--ublock` (profile + xpi). It does not filter page traffic; the request allowlist does the blocking.

## ext.to magnet engine (ext-magnet)

- Code: `~/services/ext-magnet/` (`ext_magnet.py` CLI, `extto.py` qBittorrent engine, `README.md`). Run the CLI with `~/invisible-playwright/.venv/bin/python`.
- Search goes through trawl (`127.0.0.1:8191`). Magnets come from the detail page's button, which posts to `/ajax/getTorrentMagnet.php`.
- Installed: `~/.local/share/qBittorrent/nova3/engines/extto.py` is a symlink to the file above.
