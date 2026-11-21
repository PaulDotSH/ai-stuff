---
name: jellyfin-watched-delete
description: >
  Delete episodes or movies automatically after specific Jellyfin users have
  watched them. A systemd user timer runs a stdlib-only Python script that
  removes the file through Sonarr/Radarr (unmonitoring it so it is not grabbed
  again), deletes the qBittorrent torrent with its data, and deletes the Seerr
  row for movies. Rules ("delete <show> after <user> watches it") are one CLI
  command each, so no agent is needed per episode. Use when asked to auto-delete
  watched media, add or remove a show/movie from that list, or debug why a
  watched item was not deleted.
---

# Jellyfin watched-delete

`scripts/watched_delete.py` (Python 3 stdlib only) deletes allowlisted media
once **every** user named in its rule has it marked played in Jellyfin. Each
item is deleted `delay_seconds` (default 3600) after it is first seen played,
so a user can still rewind or rewatch the ending.

## Install

1. Copy `scripts/watched_delete.py` and `scripts/config.example.json` into a
   directory, e.g. `~/watched-delete/`. Rename the example to `config.json`
   and set `settings` to match the stack. Every key is optional; see
   `DEFAULTS` in the script. Sonarr/Radarr API keys are read from their
   `config.xml` and the Seerr key from its `settings.json`, so those files must
   be readable by the user that runs the script.
2. Put a Jellyfin API key (Dashboard → API Keys) in `jellyfin.token` next to
   `config.json`, then run `chmod 600 config.json jellyfin.token`. The env var
   `JELLYFIN_TOKEN` overrides the file. Never commit either file.
3. The qBittorrent WebUI must accept the script's requests without a login:
   enable "Bypass authentication for clients on localhost", or add
   `127.0.0.1/32` to the IP subnet whitelist.
4. Run a dry run (no arguments) and read the plan.
5. Install `scripts/watched-delete.{service,timer}` into
   `~/.config/systemd/user/`, fix the `ExecStart` path, then run
   `systemctl --user daemon-reload && systemctl --user enable --now watched-delete.timer`.
   Run `loginctl enable-linger <user>` so the timer runs without an open session.

## Managing rules

```
watched_delete.py list
watched_delete.py add-series alice "Show Name"       # exact Jellyfin series name, validated
watched_delete.py add-series alice,bob "Show Name"   # deleted only after BOTH watched
watched_delete.py add-movie  alice "Title" 2021
watched_delete.py remove-series alice "Show Name"
watched_delete.py remove-movie  alice "Title" 2021
watched_delete.py            # dry run: prints the plan, changes nothing
watched_delete.py --apply    # what the timer runs
```

After adding a rule, run the dry run and show the user what it will delete.
**Adding a series also deletes episodes the users had already watched.** To
protect specific items, put their Jellyfin item IDs in `state.json` `"skip"`.

## What one deletion does

1. **Episode:** Sonarr unmonitors the episode (so RSS will not grab it again)
   and deletes its episode file. **Movie:** Radarr deletes the movie with its
   files, and the Seerr media row is deleted once Jellyfin has dropped the item.
2. **Torrents:** the qBittorrent entry is deleted together with its data, so
   nothing is left behind as missing or errored, and nothing re-downloads.
   Torrents are found from two sources:
   - the arr's grab/import history (`downloadId` = torrent hash). This still
     works when the library file is already gone or is a copy, not a hardlink.
   - torrents whose files share an inode with the library file (hardlinks).
   A torrent with one video file goes immediately. Small junk files next to it
   (.nfo, .txt, images, subtitles, each under 50 MB) do not count as videos. A
   torrent with several videos, such as a season pack, is saved under
   `state.json` `"packs"` and deleted once none of its videos is still
   hardlinked into the library (`st_nlink == 1`).
3. Copies of the same file left in `downloads_dir` that belong to no torrent
   are unlinked.

## Debugging

- Logs: `journalctl --user -u watched-delete.service`. A `skip:` line names
  the reason, and the item is retried on every run.
- `Sonarr series not found`: the Jellyfin series name differs from the Sonarr
  title. Both must match exactly.
- `finished items in scope: 0` right after a deletion is normal. Jellyfin
  drops the item once its library scan sees that the file is gone.
- `pack: waiting, N video(s) still in the library`: other episodes of the
  pack have not been watched or deleted yet.
- State lives in `state.json` next to `config.json`: `seen` (first time the
  item was seen played), `done`, `skip`, `packs`, `seerr_pending`.
