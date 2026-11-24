# ext-magnet — ext.to search + real magnet links for qBittorrent

Host-side (no Docker), user `minipc`. Code lives in this repo; `link.sh` links it to
`~/services/ext-magnet/`. Built 2026-11-24.

Files in the repo:

- `ext_magnet.py` — CLI. Searches ext.to and extracts magnet links. Each magnet comes from the
  detail page's own "Get magnet link" flow.
- `extto.py` — qBittorrent nova3 search engine that calls `ext_magnet.py`.
- `README.md` — this file.

Not in the repo (state/large, rebuild per the setup below): `~/services/ext-magnet/ublock/`
(uBO unpacked, 18 MB) and `~/services/ext-magnet/.cache/` (scratch, safe to delete).

## Setup on a fresh host

Needs: `~/invisible-playwright` (stealth Firefox, Playwright API, see `minipc/README.md`), trawl
running on `127.0.0.1:8191`, and qBittorrent with nova3 engines.

```bash
# 1. code (after cloning ai-stuff)
bash ~/ai-stuff/minipc/link.sh            # creates ~/services/ext-magnet/*.py + README
bash ~/ai-stuff/minipc/link.sh --check    # must exit 0

# 2. uBO (optional; only for --ublock)
mkdir -p ~/services/ext-magnet
docker cp trawl:/opt/camoufox/addons/UBO ~/services/ext-magnet/ublock

# 3. qBittorrent engine
ln -sfn ~/services/ext-magnet/extto.py ~/.local/share/qBittorrent/nova3/engines/extto.py
```

Step 3 is manual and not in `link.sh`. Then reload the search plugins in qBittorrent.

Quick check of the engine without qBittorrent:

```bash
cd ~/.local/share/qBittorrent/nova3/engines && python3 -c "import sys; sys.path.insert(0,'..'); \
  sys.path.insert(0,'$HOME/services/ext-magnet'); from extto import extto; extto().search('Breaking Bad S01E01')"
```

## How it works

| step | how |
|---|---|
| search | `POST http://127.0.0.1:8191/v1` (trawl, FlareSolverr API) → HTML of `https://ext.to/browse/?q=…` → parsed (`a.torrent-title-link` rows; Size/Age/Seeds/Leechs cells). |
| magnet | invisible-playwright (headless, Xvfb) opens the detail page, gets past the Cloudflare challenge on its own, **clicks `a.download-btn-magnet`**, and captures the JSON reply of the XHR this click fires. |

The click's behaviour comes from ext.to's `static/js/main.min.js`: `handleTorrentDownload(btn,'magnet')`
sends `POST /ajax/getTorrentMagnet.php` (`torrent_id`, `download_type=magnet`, `timestamp`,
`hmac` = HMAC over `window.pageToken` via CryptoJS, `sessid`). The reply is
`{"success":true,"url":"magnet:?xt=urn:btih:…","type":"magnet"}`. The page then clicks a hidden
`<a href="magnet:…">`. We read `url` from the XHR response and check it against
`^magnet:\?xt=urn:btih:([0-9a-f]{40}|[a-z2-7]{32})` (case-insensitive). If the server returns
only `hash`, the page's own JS builds `magnet:?xt=urn:btih:<hash>`. We do the same and set
`"source": "xhr.hash (page fallback)"` so you can tell. We never build a magnet from the
torrent id.

Search uses trawl because invisible-playwright does **not** get past the Cloudflare
challenge on `/browse/`. It stalls on the 403 challenge page, even after clearing the
homepage first. Detail pages do pass. `--via browser` is still available, but it currently fails.

## Usage

```bash
PY=~/invisible-playwright/.venv/bin/python
cd ~/services/ext-magnet
$PY ext_magnet.py search "Breaking Bad" [--limit N]          # JSON list
$PY ext_magnet.py magnet "https://ext.to/<slug>-<id>/"         # JSON {magnet, id, source, ...}
$PY ext_magnet.py magnets URL1 URL2 ...                        # one browser, N pages
$PY ext_magnet.py search-magnets "Breaking Bad" --limit 10     # what the engine calls
# add --ublock to magnet/magnets/search-magnets to also load uBO (see below)
```

Run the CLI with the invisible-playwright venv Python, not the system `python3` (system
`python3` fails with `ModuleNotFoundError: invisible_playwright`).

JSON goes to stdout. Progress and errors go to stderr. The exit code is non-zero on error
(`magnets` fails only if every URL failed).
Typical timings: search about 1–10 s (depends on whether trawl has a warm session). One
`magnet` call takes about 6–12 s, including browser launch and the CF challenge. The engine
search with 10 magnets took about 29–31 s.

## qBittorrent engine

`extto.py` is stdlib-only and imports only `novaprinter` from nova3. It runs
`~/invisible-playwright/.venv/bin/python ~/services/ext-magnet/ext_magnet.py` as a
**subprocess**, because qBittorrent's `python3` can't import the venv's packages.

- Rows 1–10 get the real magnet as `link`.
- Rows 11+, and any of the first 10 whose extraction failed, get the detail URL as `link`.
  When you add one of those rows, qBittorrent calls `download_torrent(url)`. That method
  extracts the magnet on demand and prints `"<magnet> <url>"`.

## Trust / security notes

- **Network allowlist**: a context-level `route` (it covers popups too) allows only `ext.to`
  (+ subdomains, incl. its `/cdn-cgi` Cloudflare paths), `challenges.cloudflare.com`
  (Turnstile) and `cdnjs.cloudflare.com` (crypto-js, which the page needs for the HMAC). Everything else
  is aborted. That includes ad and decoy hosts seen on the page (rapidadexchange.com,
  usrpubtrck.com, random `*.website` hosts), Google Fonts, and TMDB images.
- **No downloads**: any `*.torrent` URL is aborted. Calls to `getTorrentMagnet.php` are aborted
  unless they are `download_type=magnet`. Download events are cancelled. The pref
  `network.protocol-handler.external.magnet=false` stops Firefox handing `magnet:` to an OS
  handler.
- **No persistence**: every run uses a fresh temp profile (`/tmp/ext-magnet-profile-*`),
  removed in `finally`. There are no logins and no credentials, and cookies (incl. `cf_clearance`) live only for
  that session.
- **Hard timeouts**: `magnet` ≤ 60 s total (SIGALRM, browser launch included). Each page in a
  batch gets ≤ 60 s. The browser is closed through context managers on every path.
- **Fingerprint**: the User-Agent and fingerprint are invisible-playwright's defaults. Nothing
  is overridden.
- The magnet comes from ext.to's server. It names whatever the uploader posted, so check
  what you download.

## uBlock Origin: loaded, but not proven to filter

invisible-playwright supports `profile_dir=` (persistent context) and `extra_prefs=`.
`--ublock` packs `ublock/` into `.cache/ublock.xpi` and drops it into a *throw-away*
profile's `extensions/` dir, with `extensions.autoDisableScopes=0` / `enabledScopes=15`.
Evidence from the probe runs:

- `extensions.json` shows `active: True, signedState: 2` (signature valid).
- uBO's background storage `moz-extension+++<uuid>` gets created.
- A top-level navigation to `pagead2.googlesyndication.com` is aborted (`NS_BINDING_ABORTED`)
  with uBO, and not aborted without it.
- **But** subresource `fetch()` and `<script>` loads of that same ad URL from example.com
  went through, with or without uBO (checked over 12 s). uBO probably treats Juggler-driven
  tabs as "behind-the-scene" and leaves them unfiltered.

So uBO is **off by default**. The route allowlist is what actually enforces the policy.
`--ublock` works (magnet extraction verified with it), but it adds about 3 s, and uBO's own
filter-list update requests are not covered by the page allowlist.
