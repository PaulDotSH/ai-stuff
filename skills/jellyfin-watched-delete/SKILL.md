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
once the users named in its rule have it marked played in Jellyfin: **all** of
them by default (AND), or **any one** of them when the rule was added with
`--any` (OR). Use OR only when the user explicitly asks for "either"/"any of";
otherwise use AND. Each item is deleted `delay_seconds` (default 3600) after it
is first seen played, so a user can still rewind or rewatch the ending.

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
   Optionally install `scripts/watched-delete-config.path` too
   (`systemctl --user enable --now watched-delete-config.path`). It starts a run
   as soon as `config.json` changes. Nothing ever needs a restart, because every
   run re-reads the config.

## Managing rules

```
watched_delete.py list
watched_delete.py add-series alice "Show Name"       # exact Jellyfin series name, validated
watched_delete.py add-series alice,bob "Show Name"   # AND: deleted only after BOTH watched
watched_delete.py add-series alice,bob "Show Name" --any          # OR: either one watching is enough
watched_delete.py add-series alice "Show Name" --seasons 1,2      # only those seasons
watched_delete.py add-movie  alice "Title" 2021 [--any]
watched_delete.py remove-series alice "Show Name" [--any]        # --any picks the OR rule
watched_delete.py remove-movie  alice "Title" 2021 [--any]
watched_delete.py            # dry run: prints the plan, changes nothing
watched_delete.py --apply    # what the timer runs
```

After adding a rule, run the dry run and show the user what it will delete.
Rules remove themselves once their media is completely deleted:
- **movie:** gone from Jellyfin and Radarr.
- **Sonarr series:** every in-scope episode has no file and is unmonitored. A
  series-wide rule also needs Sonarr status `ended`, so an airing show keeps its
  rule for future episodes.
- **unmanaged series:** no episode file is left.

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
   A torrent with one video file is deleted with its data. Small junk files
   next to it (.nfo, .txt, images, subtitles, each under 50 MB) do not count as
   videos. A torrent with several videos, such as a season pack, is removed
   from qBittorrent **without** its data as soon as its first video is deleted.
   Only the deleted episode's own downloads copy is unlinked; the other
   episodes stay on disk as hardlinks of their library files.
3. **Media no arr manages** (the title is missing from Sonarr/Radarr, e.g. a
   hand-linked torrent): the library file and its same-name sidecars
   (.srt/.ass/.nfo/thumbs) are deleted directly, the torrent rules above
   apply, and a Jellyfin library refresh is triggered.
4. Copies of the item left in `downloads_dir` that belong to no torrent (for
   example the rest of an already-removed season pack) are unlinked. A
   downloads folder left holding only junk files is removed. A removed season
   pack's folder is deleted whole, extras included, once none of its videos is
   still in the library (`state.json` `"pack_dirs"`).
5. Unmanaged media only: once the last video of a season or series is gone,
   its library folder is deleted along with any leftover extras, posters and
   .nfo files. It never goes above the folder Jellyfin reports for the
   series, and never touches a library root.

## Debugging

- Logs: `journalctl --user -u watched-delete.service`. A `skip:` line names
  the reason, and the item is retried on every run.
- A Jellyfin series name that differs from the Sonarr title is treated as
  unmanaged, so its files are deleted directly and Sonarr is not told. Both
  names must match exactly; `add-series` prints a note when Sonarr has no such
  title.
- `finished items in scope: 0` right after a deletion is normal. Jellyfin
  drops the item once its library scan sees that the file is gone.
- State lives in `state.json` next to `config.json`: `seen` (first time the
  item was seen played), `done`, `skip`, `seerr_pending`, `pack_dirs`.
- `rule complete, removing: ...` in the log means the entry was dropped from
  `config.json` on purpose. Re-add it if more media for it arrives later.
