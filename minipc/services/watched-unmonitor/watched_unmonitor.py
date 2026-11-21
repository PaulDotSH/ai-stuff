#!/usr/bin/env python3
"""Jellyfin -> Sonarr: unmonitor episodes the Jellyfin user has already watched.

Purpose: watched episodes must not be auto-(re)downloaded by Sonarr — no RSS
re-grabs if the file gets deleted, no quality upgrades. Runs from a systemd
timer every 10 minutes (watched-unmonitor.timer).

Usage:
  python3 watched_unmonitor.py            # dry-run: print what would change
  python3 watched_unmonitor.py --apply    # apply (timer uses this)

State: state.json records every watched episode seen (per Sonarr series).
Already-recorded episodes are never touched again, so manually re-monitoring
an episode in the Sonarr UI is never auto-reverted. Delete state.json to
re-process the whole backlog.
"""
import json, os, re, sys, urllib.request

JELLYFIN = 'http://127.0.0.1:8096'
JELLYFIN_USER = 'f70827ef2cbf4219b303cdea7b1a1001'   # Jellyfin user 'admin'
SONARR = 'http://127.0.0.1:8989/sonarr/api/v3'
SONARR_KEY = re.search('<ApiKey>([^<]+)', open('/var/lib/sonarr/config.xml').read()).group(1)
HERE = os.path.dirname(os.path.abspath(__file__))   # abspath, not realpath: the live path is a symlink into ai-stuff
# API key lives next to the script (not in git): env JELLYFIN_TOKEN wins
JELLYFIN_TOKEN = os.environ.get('JELLYFIN_TOKEN') or open(os.path.join(HERE, 'jellyfin.token')).read().strip()
STATE_FILE = os.path.join(HERE, 'state.json')


def jget(path):
    # Jellyfin 12.4 rejects X-Emby-Token (401); send the Authorization scheme instead.
    req = urllib.request.Request(JELLYFIN + path, headers={'Authorization': f'MediaBrowser Token="{JELLYFIN_TOKEN}"'})
    return json.load(urllib.request.urlopen(req, timeout=60))


def sget(path):
    sep = '&' if '?' in path else '?'
    return json.load(urllib.request.urlopen(f'{SONARR}{path}{sep}apikey={SONARR_KEY}', timeout=120))


def sput(path, body):
    req = urllib.request.Request(f'{SONARR}{path}?apikey={SONARR_KEY}',
                                 data=json.dumps(body).encode(), method='PUT',
                                 headers={'Content-Type': 'application/json'})
    return urllib.request.urlopen(req, timeout=60)


def main(apply):
    # 1. all played episodes for the user (paginated)
    played, start = [], 0
    while True:
        d = jget(f'/Users/{JELLYFIN_USER}/Items?Recursive=true&IncludeItemTypes=Episode'
                 f'&Filters=IsPlayed&Fields=ProviderIds,SeriesName&Limit=500&StartIndex={start}')
        items = d.get('Items', [])
        played.extend(items)
        if len(items) < 500:
            break
        start += len(items)
    print(f'played episodes in Jellyfin: {len(played)}', flush=True)

    # 2. Sonarr series lookup (tvdb id, title fallback)
    series = sget('/series')
    by_tvdb, by_title = {}, {}
    for s in series:
        if s.get('tvdbId'):
            by_tvdb[int(s['tvdbId'])] = s
        by_title[s['title'].strip().lower()] = s

    processed = set()
    if os.path.exists(STATE_FILE):
        try:
            processed = set(json.load(open(STATE_FILE)).get('processed', []))
        except Exception as e:
            print('state read err:', e, flush=True)

    # 3. group played episodes per Sonarr series
    per_series, unmatched = {}, {}
    for it in played:
        pids = it.get('ProviderIds') or {}
        try:
            tvdb = int(pids.get('Tvdb') or 0) or None
        except Exception:
            tvdb = None
        sn = (it.get('SeriesName') or '').strip()
        s = by_tvdb.get(tvdb) if tvdb else None
        if not s and sn:
            s = by_title.get(sn.lower())
        if not s:
            unmatched[sn or '?'] = unmatched.get(sn or '?', 0) + 1
            continue
        season, epnum = it.get('ParentIndexNumber'), it.get('IndexNumber')
        if season is None or epnum is None:
            continue
        per_series.setdefault(s['id'], {'s': s, 'eps': set()})['eps'].add((season, epnum))

    print(f'series matched: {len(per_series)} | unmatched (not in Sonarr): {len(unmatched)}', flush=True)
    for k, v in sorted(unmatched.items(), key=lambda x: -x[1])[:15]:
        print(f'  [skip] {k}: {v}', flush=True)

    total = 0
    for sid, info in per_series.items():
        s = info['s']
        try:
            eps = sget(f'/episode?seriesId={sid}')
        except Exception as e:
            print(f'  ! {s["title"]}: episode fetch failed: {e}', flush=True)
            continue
        ids = []
        for e in eps:
            key = (e['seasonNumber'], e['episodeNumber'])
            if key not in info['eps']:
                continue
            ek = f'{sid}:{e["seasonNumber"]}x{e["episodeNumber"]}'
            if ek in processed:
                continue
            processed.add(ek)          # mark seen even if not currently monitored
            if e.get('monitored'):
                ids.append(e['id'])
        if ids:
            print(f'  {s["title"]}: {len(ids)} episode(s) -> unmonitor', flush=True)
            if apply:
                sput('/episode/monitor', {'episodeIds': ids, 'monitored': False})
            total += len(ids)

    print(f'episodes {"unmonitored" if apply else "to unmonitor (dry-run)"}: {total}', flush=True)
    if apply:
        tmp = STATE_FILE + '.tmp'
        json.dump({'processed': sorted(processed)}, open(tmp, 'w'))
        os.replace(tmp, STATE_FILE)
        print('state saved:', len(processed), flush=True)


if __name__ == '__main__':
    main(apply='--apply' in sys.argv)
