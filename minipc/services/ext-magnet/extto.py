# VERSION: 1.0
# AUTHORS: local (minipc)
# Search engine for qBittorrent: ext.to, with real magnet links taken from the
# detail page's own "Get magnet link" flow.
#
# How it works (self-contained, stdlib only, imports only novaprinter from nova3):
#   search()  -> runs  ~/invisible-playwright/.venv/bin/python
#                      ~/services/ext-magnet/ext_magnet.py search-magnets <query> --limit 10
#                as a subprocess. That helper fetches the search page via trawl
#                (FlareSolverr API on 127.0.0.1:8191) and opens the first 10 detail
#                pages in ONE invisible-playwright session, clicking the magnet button
#                and capturing the /ajax/getTorrentMagnet.php JSON reply.
#                Rows 1-10 get the real magnet as `link`. Rows 11+ (and any of the
#                first 10 whose extraction failed) get the detail page as `link`;
#                qBittorrent then calls download_torrent() for them on demand.
#   download_torrent(url) -> runs `ext_magnet.py magnet <url>` and prints
#                "<magnet> <url>" (the nova3 convention for magnet results).
# The subprocess is used because invisible-playwright lives in its own venv, which
# qBittorrent's python3 can't import.

import json
import os
import subprocess
import sys
import urllib.parse
from datetime import datetime, timezone

from novaprinter import prettyPrinter

HOME = os.path.expanduser('~')
VENV_PY = os.path.join(HOME, 'invisible-playwright', '.venv', 'bin', 'python')
HELPER = os.path.join(HOME, 'services', 'ext-magnet', 'ext_magnet.py')
MAGNET_LIMIT = 10          # magnets extracted per search (keeps a search < ~3 min)
SEARCH_TIMEOUT = 240       # subprocess cap (helper: soft 165 s budget, hard backstop 225 s)
MAGNET_TIMEOUT = 75


def _run(args, timeout):
    proc = subprocess.run([VENV_PY, HELPER] + args, capture_output=True, text=True,
                          timeout=timeout, stdin=subprocess.DEVNULL)
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr[-2000:])
        return None
    return json.loads(proc.stdout)


def _int(value):
    try:
        return int(str(value).replace(',', '').strip())
    except ValueError:
        return -1


def _pub(date_str):
    try:
        return int(datetime.strptime(date_str, '%d %B %Y')
                   .replace(tzinfo=timezone.utc).timestamp())
    except (TypeError, ValueError):
        return -1


class extto:
    url = 'https://ext.to'
    name = 'EXT (ext.to)'
    supported_categories = {'all': ''}

    def download_torrent(self, url: str) -> None:
        if url.startswith('magnet:'):
            print(url + ' ' + url)
            return
        res = _run(['magnet', url], MAGNET_TIMEOUT)
        if res and res.get('magnet', '').startswith('magnet:?'):
            print(res['magnet'] + ' ' + url)
        else:
            raise RuntimeError('ext.to: could not extract magnet for ' + url)

    def search(self, what: str, cat: str = 'all') -> None:
        query = urllib.parse.unquote_plus(what)
        try:
            results = _run(['search-magnets', query, '--limit', str(MAGNET_LIMIT)],
                           SEARCH_TIMEOUT) or []
        except subprocess.TimeoutExpired:
            sys.stderr.write('ext.to: helper timed out\n')
            return
        for item in results:
            magnet = item.get('magnet') or ''
            link = magnet if magnet.startswith('magnet:?') else item['url']
            prettyPrinter({
                # novaprinter rejects '|' (its field delimiter) in link/desc_link
                'link': link.replace('|', '%7C'),
                'name': (item.get('title') or '').replace('|', ' '),
                'size': item.get('size') or '-1',
                'seeds': _int(item.get('seeds')),
                'leech': _int(item.get('leechers')),
                'engine_url': self.url,
                'desc_link': item['url'],
                'pub_date': _pub(item.get('date')),
            })
