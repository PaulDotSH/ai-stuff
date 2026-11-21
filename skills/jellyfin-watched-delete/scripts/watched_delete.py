#!/usr/bin/env python3
"""Delete media after specific Jellyfin users have finished it (DELAY after they finish).

Scope is an allowlist in config.json (next to this script):
  {"delay_seconds": 3600,
   "rules": [{"users": ["alice"], "series": ["Example Show"],
              "movies": [{"title": "Example Movie", "year": 2021}]}]}
An item is deleted once EVERY user listed in its rule has it marked played ("match": "all",
the default) or once ANY of them has ("match": "any", set with --any). A series entry is a
name or {"name": ..., "seasons": [1, 2]} to limit it to some seasons. Nothing outside the
rules is ever touched.

For each finished item, once DELAY seconds have passed since it was first seen as finished:
  1. Episodes: Sonarr stops monitoring the episode and deletes its library file.
     Movie: qBittorrent torrent first, then Radarr deletes the movie with its files.
  2. Every qBittorrent torrent for the item is removed with its data (the entry, not just the
     files, so nothing shows as missing/broken or re-downloads). Torrents are found from the
     arr's grab/import history (downloadId) and from the library file's hardlink inode.
     Single-video torrents (plus .nfo/.txt/subs junk < 50 MB) are deleted with their data. A
     torrent with several videos (season pack) is removed from qBittorrent WITHOUT its data as
     soon as its first video is deleted; only the deleted item's own downloads copy is unlinked,
     the other episodes' files stay on disk (they are hardlinks of the library files).
  3. Any leftover /media/downloads hardlink to the same inode is unlinked.
  4. Movies: the Seerr media row is deleted after Jellyfin has dropped the item.
  5. A removed season pack's downloads folder is deleted whole (extras included) once none
     of its videos is still in the library.
  6. A series/movie entry is dropped from config.json once it is completely deleted: movie
     gone from Jellyfin and Radarr; Sonarr series with every in-scope episode fileless and
     unmonitored (series-wide rules also need status "ended"); unmanaged series with no
     episode file left. Empty rules are dropped too.
  Media that no arr manages (title absent from Sonarr/Radarr, e.g. a hand-linked torrent):
  the library file and its same-name sidecars are deleted directly, steps 2-3 apply, and a
  Jellyfin library refresh is triggered.

Items whose Jellyfin IDs are listed in state.json "skip" are never deleted.
Already-watched episodes of a newly added series ARE deleted (after DELAY).

Settings (service URLs, file paths) live under "settings" in config.json; see DEFAULTS.
The Jellyfin API key comes from env JELLYFIN_TOKEN or the file named by jellyfin_token_file.

Usage:
  watched_delete.py                      dry run: print the plan, change nothing
  watched_delete.py --apply              act (the systemd user timer uses this)
  watched_delete.py list                 show rules
  watched_delete.py add-series USER "Series Name" [--any] [--seasons 1,2]
                       USER may be "a,b": all of them must watch it, or any one with --any
  watched_delete.py add-movie  USER "Title" YEAR [--any]
  watched_delete.py remove-series USER "Series Name"
  watched_delete.py remove-movie  USER "Title" YEAR
"""
import json, os, re, shutil, sqlite3, sys, time, urllib.parse, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.environ.get('WATCHED_DELETE_CONFIG', os.path.join(HERE, 'config.json'))
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(CONFIG_FILE)), 'state.json')

# Defaults for a single-host stack; override any of them under "settings" in config.json.
DEFAULTS = {
    'jellyfin_url': 'http://127.0.0.1:8096',
    'jellyfin_token_file': 'jellyfin.token',          # relative = next to config.json; env JELLYFIN_TOKEN wins
    'jellyfin_db': '/var/lib/jellyfin/data/data/jellyfin.db',
    'sonarr_url': 'http://127.0.0.1:8989/api/v3',
    'sonarr_config': '/var/lib/sonarr/config.xml',     # API key is read from here
    'radarr_url': 'http://127.0.0.1:7878/api/v3',
    'radarr_config': '/var/lib/radarr/config.xml',
    'seerr_url': 'http://127.0.0.1:5055/api/v1',      # set to "" to skip Seerr cleanup
    'seerr_settings': '/etc/seerr/settings.json',
    'qbittorrent_url': 'http://127.0.0.1:8080/api/v2', # WebUI must allow localhost without auth
    'downloads_dir': '/media/downloads',
}
S = dict(DEFAULTS)
JUNK_EXT = {'.nfo', '.txt', '.url', '.jpg', '.jpeg', '.png', '.sfv', '.md5', '.srt', '.sub',
            '.idx', '.ass', '.ssa', '.website', '.html', '.htm'}
JUNK_MAX = 50 * 1024 * 1024


def http(method, url, headers=None, body=None, form=None):
    hdrs = dict(headers or {})
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        hdrs['Content-Type'] = 'application/json'
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        hdrs['Content-Type'] = 'application/x-www-form-urlencoded'
    req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
    with urllib.request.urlopen(req, timeout=120) as r:
        raw = r.read().decode()
    if raw.strip().startswith(('{', '[')):
        return json.loads(raw)
    return raw


def jf(path, method='GET'):
    return http(method, S['jellyfin_url'] + path, headers={'Authorization': f'MediaBrowser Token="{K["jellyfin"]}"'})


def load_settings():
    if os.path.exists(CONFIG_FILE):
        S.update(json.load(open(CONFIG_FILE)).get('settings', {}))


def keys():
    tok = os.environ.get('JELLYFIN_TOKEN')
    if not tok:
        f = S['jellyfin_token_file']
        f = f if os.path.isabs(f) else os.path.join(os.path.dirname(os.path.abspath(CONFIG_FILE)), f)
        tok = open(f).read().strip()
    k = {'jellyfin': tok,
         'sonarr': re.search('<ApiKey>([^<]+)', open(S['sonarr_config']).read()).group(1)}
    if os.path.exists(S['radarr_config']):
        k['radarr'] = re.search('<ApiKey>([^<]+)', open(S['radarr_config']).read()).group(1)
    if S['seerr_url'] and os.path.exists(S['seerr_settings']):
        k['seerr'] = json.load(open(S['seerr_settings']))['main']['apiKey']
    return k


K = {}


def sonarr(method, path, body=None):
    sep = '&' if '?' in path else '?'
    return http(method, f"{S['sonarr_url']}{path}{sep}apikey={K['sonarr']}", body=body)


def radarr(method, path, body=None):
    return http(method, S['radarr_url'] + path, headers={'X-Api-Key': K['radarr']}, body=body)


def seerr(method, path):
    return http(method, S['seerr_url'] + path, headers={'X-Api-Key': K['seerr']})


def qbt_get(path):
    return http('GET', S['qbittorrent_url'] + path)


def qbt_post(path, form):
    return http('POST', S['qbittorrent_url'] + path, form=form)


# ---------- config ----------

def load_config():
    if os.path.exists(CONFIG_FILE):
        cfg = json.load(open(CONFIG_FILE))
    else:
        cfg = {}
    cfg.setdefault('delay_seconds', 3600)
    cfg.setdefault('rules', [])
    return cfg


def save_config(cfg):
    tmp = CONFIG_FILE + '.tmp'
    with open(tmp, 'w') as fh:
        json.dump(cfg, fh, indent=1)
    os.replace(tmp, CONFIG_FILE)


def jf_users():
    return {u['Name'].lower(): u['Id'] for u in jf('/Users')}


def rule_for(cfg, users, match='all', create=False):
    key = sorted(u.lower() for u in users)
    for r in cfg['rules']:
        if sorted(u.lower() for u in r['users']) == key and r.get('match', 'all') == match:
            return r
    if not create:
        return None
    r = {'users': key, 'match': match, 'series': [], 'movies': []}
    cfg['rules'].append(r)
    return r


def series_name(e):
    return e if isinstance(e, str) else e['name']


def rule_title(r):
    j = ' OR ' if r.get('match', 'all') == 'any' else ' + '
    return j.join(r['users'])


def parse_users(s):
    users = [u.strip() for u in s.split(',') if u.strip()]
    known = jf_users()
    bad = [u for u in users if u.lower() not in known]
    if bad:
        sys.exit(f'unknown Jellyfin user(s): {bad}; known: {sorted(known)}')
    return users


def cmd_list(cfg):
    print(f"delay: {cfg['delay_seconds']} s")
    for r in cfg['rules']:
        print(f"users {rule_title(r)}:")
        for s in r['series']:
            seasons = '' if isinstance(s, str) else f" (seasons {', '.join(map(str, s['seasons']))})"
            print(f'   series: {series_name(s)}{seasons}')
        for m in r['movies']:
            print(f"   movie:  {m['title']} ({m['year']})")


def cmd_add_series(cfg, users, name, match='all', seasons=None):
    hits = jf(f'/Items?Recursive=true&IncludeItemTypes=Series&SearchTerm={urllib.parse.quote(name)}')['Items']
    exact = [h['Name'] for h in hits if h['Name'].lower() == name.lower()]
    if not exact:
        sys.exit(f'no Jellyfin series named {name!r}; close matches: {[h["Name"] for h in hits]}')
    name = exact[0]
    if not [s for s in sonarr('GET', '/series') if s['title'] == name]:
        print(f'note: Sonarr has no series titled {name!r}; its files will be deleted directly (not arr-managed)')
    r = rule_for(cfg, users, match, create=True)
    r['series'] = [e for e in r['series'] if series_name(e) != name]
    r['series'].append({'name': name, 'seasons': seasons} if seasons else name)
    save_config(cfg)
    print(f"added series {name!r}{f' seasons {seasons}' if seasons else ''} for {rule_title(r)}")


def cmd_add_movie(cfg, users, title, year, match='all'):
    hits = jf(f'/Items?Recursive=true&IncludeItemTypes=Movie&Fields=ProductionYear&SearchTerm={urllib.parse.quote(title)}')['Items']
    exact = [h for h in hits if h['Name'].lower() == title.lower() and h.get('ProductionYear') == year]
    if not exact:
        sys.exit(f'no Jellyfin movie {title!r} ({year}); matches: {[(h["Name"], h.get("ProductionYear")) for h in hits]}')
    title = exact[0]['Name']
    r = rule_for(cfg, users, match, create=True)
    if not any(m['title'] == title and m['year'] == year for m in r['movies']):
        r['movies'].append({'title': title, 'year': year})
    save_config(cfg)
    print(f"added movie {title!r} ({year}) for {rule_title(r)}")


def cmd_remove(cfg, users, kind, name, year=None, match='all'):
    r = rule_for(cfg, users, match)
    if not r:
        sys.exit(f"no {'--any ' if match == 'any' else ''}rule for those users")
    if kind == 'series':
        r['series'] = [s for s in r['series'] if series_name(s).lower() != name.lower()]
    else:
        r['movies'] = [m for m in r['movies'] if not (m['title'].lower() == name.lower() and m['year'] == year)]
    if not r['series'] and not r['movies']:
        cfg['rules'].remove(r)
    save_config(cfg)
    print('removed')


# ---------- scan ----------

def played(uid):
    out, start = [], 0
    while True:
        d = jf(f'/Users/{uid}/Items?Recursive=true&IncludeItemTypes=Episode,Movie'
               f'&Filters=IsPlayed&Fields=Path,SeriesName,ProductionYear&Limit=500&StartIndex={start}')
        items = d.get('Items', [])
        out += items
        if len(items) < 500:
            break
        start += len(items)
    return {it['Id']: it for it in out}


def in_rule(it, r):
    if it.get('Type') == 'Episode':
        for e in r['series']:
            if series_name(e) == it.get('SeriesName'):
                if isinstance(e, str) or it.get('ParentIndexNumber') in e.get('seasons', []):
                    return True
        return False
    if it.get('Type') == 'Movie':
        return any(it.get('Name') == m['title'] and it.get('ProductionYear') == m['year'] for m in r['movies'])
    return False


def finished_items(cfg):
    ids = jf_users()
    cache, keep = {}, {}
    for r in cfg['rules']:
        sets = []
        for u in r['users']:
            uid = ids.get(u.lower())
            if uid is None:
                print(f'warning: Jellyfin user {u!r} not found; rule skipped')
                sets = None
                break
            if uid not in cache:
                cache[uid] = played(uid)
            sets.append(cache[uid])
        if not sets:
            continue
        if r.get('match', 'all') == 'any':
            pool = {}
            for st_ in sets:
                pool.update(st_)
        else:
            common = set(sets[0]).intersection(*sets[1:])
            pool = {i: sets[0][i] for i in common}
        for iid, it in pool.items():
            if in_rule(it, r):
                keep[iid] = it
    return list(keep.values())


def label(it):
    if it['Type'] == 'Episode':
        return f"{it['SeriesName']} S{it.get('ParentIndexNumber', 0):02d}E{it.get('IndexNumber', 0):02d}"
    return f"{it['Name']} ({it.get('ProductionYear')})"


def downloads_by_inode():
    idx = {}
    for root, _, files in os.walk(S['downloads_dir']):
        for f in files:
            p = os.path.join(root, f)
            try:
                idx.setdefault(os.stat(p).st_ino, []).append(p)
            except OSError:
                pass
    return idx


def torrent_index():
    """hash -> {'name', 'files': [(abs path, size)]}"""
    idx = {}
    for t in qbt_get('/torrents/info'):
        files = qbt_get('/torrents/files?hash=' + t['hash'])
        idx[t['hash'].lower()] = {'name': t['name'], 'content_path': t.get('content_path', ''),
                                  'files': [(os.path.join(t['save_path'], f['name']), f['size']) for f in files]}
    return idx


def is_junk(name, size):
    return os.path.splitext(name)[1].lower() in JUNK_EXT and size < JUNK_MAX


def media_files(t):
    return [p for p, sz in t['files'] if not is_junk(p, sz)]


def plan_for(lib_path, history_hashes, idx_ino, idx_t):
    """Which torrents hold this item, from two sources: the arr's grab/import history
    (works even after the library file is gone or when it is a copy, not a hardlink)
    and the inode of the library file (hardlinked downloads copy)."""
    cands = {h.lower() for h in history_hashes if h and h.lower() in idx_t}
    ino = None
    if lib_path and os.path.exists(lib_path):
        ino = os.stat(lib_path).st_ino
    by_path = {p: h for h, t in idx_t.items() for p, _ in t['files']}
    unlink = []
    if ino is not None:
        for c in idx_ino.get(ino, []):
            if c == lib_path:
                continue
            h = by_path.get(c)
            if h is None or len(media_files(idx_t[h])) > 1:
                unlink.append(c)   # orphan copy, or this item's copy inside a season pack
            if h is not None:
                cands.add(h)
    remove, packs = [], []
    for h in sorted(cands):
        t = idx_t[h]
        (remove if len(media_files(t)) <= 1 else packs).append((h, t['name']))
    return {'torrents': remove, 'packs': packs, 'unlink': unlink}


def cleanup_dir(d):
    """Remove a downloads subfolder left with only junk files (or nothing)."""
    root = os.path.abspath(S['downloads_dir'])
    while d and os.path.abspath(d).startswith(root + os.sep) and os.path.isdir(d):
        entries = [os.path.join(d, e) for e in os.listdir(d)]
        if any(os.path.isdir(e) or not is_junk(e, os.path.getsize(e)) for e in entries):
            return
        for e in entries:
            print(f'    downloads junk: unlink {e}')
            os.remove(e)
        print(f'    downloads dir: rmdir {d}')
        os.rmdir(d)
        d = os.path.dirname(d)


def remove_torrents_and_copies(plan, idx_t, apply, st=None):
    for h, name in plan['torrents']:
        print(f'    torrent: remove + delete data: {name}')
        if apply:
            qbt_post('/torrents/delete', {'hashes': h, 'deleteFiles': 'true'})
        idx_t.pop(h, None)
    for h, name in plan['packs']:
        print(f'    season-pack torrent: remove from qBittorrent, keep files on disk: {name}')
        cp = idx_t.get(h, {}).get('content_path', '')
        if apply:
            qbt_post('/torrents/delete', {'hashes': h, 'deleteFiles': 'false'})
            if st is not None and cp and os.path.isdir(cp) and in_downloads(cp):
                st['pack_dirs'][cp] = name
        idx_t.pop(h, None)
    for c in plan['unlink']:
        print(f'    downloads copy: unlink {c}')
        if apply:
            os.remove(c)
            cleanup_dir(os.path.dirname(c))


def delete_unmanaged(it, idx_ino, idx_t, st, apply):
    """Media no arr manages (e.g. a hand-linked torrent): delete the library file and its
    same-name sidecars (.srt/.ass/.nfo/-thumb.jpg...) directly, then let Jellyfin rescan."""
    lib = it['Path']
    plan = plan_for(lib, [], idx_ino, idx_t)
    if os.path.exists(lib):
        d, stem = os.path.dirname(lib), os.path.splitext(os.path.basename(lib))[0]
        side = [os.path.join(d, f) for f in os.listdir(d)
                if f != os.path.basename(lib) and f.startswith(stem) and is_junk(f, os.path.getsize(os.path.join(d, f)))]
        for f in [lib] + side:
            print(f'    not managed by an arr: unlink {f}')
            if apply:
                os.remove(f)
    else:
        print('    library file already gone')
    remove_torrents_and_copies(plan, idx_t, apply, st)
    if apply:
        cleanup_library(it, lib)
    return 'done', 'ok (unmanaged)', None, None, True


def has_video(d):
    for root, _, files in os.walk(d):
        for f in files:
            fp = os.path.join(root, f)
            try:
                if not is_junk(f, os.path.getsize(fp)):
                    return True
            except OSError:
                pass
    return False


def cleanup_library(it, lib):
    """Unmanaged media only: once its last video is gone, delete the season folder and then
    the series/movie folder with whatever extras are left (posters, nfo, Extras/...).
    Never goes above the folder Jellyfin reports for the series/movie."""
    top = None
    if it['Type'] == 'Episode' and it.get('SeriesId'):
        try:
            top = jf(f"/Items?Ids={it['SeriesId']}&Fields=Path")['Items'][0].get('Path')
        except Exception:
            top = None
    elif it['Type'] == 'Movie':
        top = os.path.dirname(lib)
    if not top or not os.path.isdir(top):
        return
    top = os.path.abspath(top)
    roots = library_roots()
    if not roots or any(top == r or r.startswith(top + os.sep) for r in roots):
        return  # never delete a library root (or anything above one)
    d = os.path.abspath(os.path.dirname(lib))
    if not (d == top or d.startswith(top + os.sep)):
        return
    while True:
        if os.path.isdir(d) and not has_video(d):
            print(f'    library: last video gone, remove folder {d}')
            shutil.rmtree(d)
        if d == top:
            return
        d = os.path.dirname(d)


def library_roots():
    try:
        return {os.path.abspath(l) for v in jf('/Library/VirtualFolders') for l in v.get('Locations', [])}
    except Exception:
        return set()


def in_downloads(p):
    root = os.path.abspath(S['downloads_dir'])
    return os.path.abspath(p).startswith(root + os.sep)


def sweep_pack_dirs(st, idx_t, apply):
    """A removed season pack's downloads folder goes (extras included) once none of its
    videos is still hardlinked into the library and no torrent uses it any more."""
    for d, name in list(st['pack_dirs'].items()):
        if not os.path.isdir(d):
            del st['pack_dirs'][d]
            continue
        if any(p.startswith(d + os.sep) for t in idx_t.values() for p, _ in t['files']):
            print(f'pack folder: in use by a torrent again, keep: {d}')
            continue
        linked = []
        for root, _, files in os.walk(d):
            for f in files:
                fp = os.path.join(root, f)
                if not is_junk(f, os.path.getsize(fp)) and os.stat(fp).st_nlink > 1:
                    linked.append(fp)
        if linked:
            print(f'pack folder: waiting, {len(linked)} video(s) still in the library: {name}')
            continue
        print(f'pack folder: last episode gone, remove {d}')
        if apply:
            shutil.rmtree(d)
            del st['pack_dirs'][d]


def act_episode(it, idx_ino, idx_t, st, apply):
    lib = it['Path']
    series = [s for s in sonarr('GET', '/series') if s['title'] == it['SeriesName']]
    if not series:
        return delete_unmanaged(it, idx_ino, idx_t, st, apply)
    if len(series) != 1:
        return 'skip', f"several Sonarr series titled {it['SeriesName']}"
    sid = series[0]['id']
    eps = [e for e in sonarr('GET', f'/episode?seriesId={sid}')
           if e['seasonNumber'] == it.get('ParentIndexNumber') and e['episodeNumber'] == it.get('IndexNumber')]
    if not eps:
        return 'skip', 'no Sonarr episode record matches'
    ep = eps[0]
    hist = sonarr('GET', f"/history?episodeId={ep['id']}&pageSize=200").get('records', [])
    plan = plan_for(lib, [r.get('downloadId') for r in hist], idx_ino, idx_t)
    if ep.get('monitored'):
        print(f"    sonarr: unmonitor episode {ep['id']}")
        if apply:
            sonarr('PUT', '/episode/monitor', {'episodeIds': [ep['id']], 'monitored': False})
    if os.path.exists(lib):
        files = [f for f in sonarr('GET', f'/episodefile?seriesId={sid}') if f['path'] == lib]
        if not files:
            return 'skip', 'no Sonarr episode file matches this path'
        print(f"    sonarr: delete episodefile {files[0]['id']} ({lib})")
        if apply:
            sonarr('DELETE', f"/episodefile/{files[0]['id']}")
    else:
        print('    library file already gone')
    remove_torrents_and_copies(plan, idx_t, apply, st)
    return 'done', 'ok'


def act_movie(it, idx_ino, idx_t, st, apply):
    lib = it['Path']
    if 'radarr' not in K:
        return 'skip', 'Radarr not configured'
    movies = [m for m in radarr('GET', '/movie') if m['title'] == it['Name'] and m['year'] == it.get('ProductionYear')]
    if not movies:
        return delete_unmanaged(it, idx_ino, idx_t, st, apply)
    if len(movies) != 1:
        return 'skip', 'several Radarr movies match'
    m = movies[0]
    hist = radarr('GET', f"/history/movie?movieId={m['id']}")
    plan = plan_for(lib, [r.get('downloadId') for r in hist], idx_ino, idx_t)
    remove_torrents_and_copies(plan, idx_t, apply, st)
    print(f"    radarr: delete movie {m['id']} with files ({lib})")
    if apply:
        radarr('DELETE', f"/movie/{m['id']}?deleteFiles=true&addImportExclusion=false")
    return 'done', 'ok', m.get('tmdbId'), os.path.dirname(lib) + '/'


def jellyfin_has_path(prefix):
    con = sqlite3.connect(f"file:{S['jellyfin_db']}?mode=ro", uri=True)
    try:
        return con.execute('select count(*) from BaseItems where Path like ?', (prefix + '%',)).fetchone()[0] > 0
    finally:
        con.close()


def entry_complete(kind, e, sonarr_series, radarr_movies):
    """True once nothing is left to delete for this rule entry and nothing more will arrive."""
    if kind == 'movie':
        hits = jf('/Items?Recursive=true&IncludeItemTypes=Movie&Fields=Path,ProductionYear&SearchTerm='
                  + urllib.parse.quote(e['title']))['Items']
        alive = [h for h in hits if h['Name'] == e['title'] and h.get('ProductionYear') == e['year']
                 and h.get('Path') and os.path.exists(h['Path'])]
        in_radarr = [m for m in radarr_movies if m['title'] == e['title'] and m['year'] == e['year']]
        return not alive and not in_radarr
    name = series_name(e)
    seasons = None if isinstance(e, str) else set(e.get('seasons', []))
    ss = [x for x in sonarr_series if x['title'] == name]
    if len(ss) == 1:
        eps = [x for x in sonarr('GET', f"/episode?seriesId={ss[0]['id']}")
               if seasons is None or x['seasonNumber'] in seasons]
        if not eps or any(x['hasFile'] or x['monitored'] for x in eps):
            return False
        return seasons is not None or ss[0].get('status') == 'ended'
    if ss:
        return False
    hits = jf('/Items?Recursive=true&IncludeItemTypes=Series&SearchTerm=' + urllib.parse.quote(name))['Items']
    for h in hits:
        if h['Name'] != name:
            continue
        for ep in jf(f"/Shows/{h['Id']}/Episodes?Fields=Path").get('Items', []):
            if (seasons is None or ep.get('ParentIndexNumber') in seasons) and ep.get('Path') and os.path.exists(ep['Path']):
                return False
    return True


def prune_rules(cfg, apply):
    """Drop series/movie entries that are completely deleted; drop rules left empty."""
    sonarr_series = sonarr('GET', '/series')
    radarr_movies = radarr('GET', '/movie') if 'radarr' in K else []
    changed = False
    for r in list(cfg['rules']):
        for kind, key in (('series', 'series'), ('movie', 'movies')):
            keep = []
            for e in r[key]:
                try:
                    done_ = entry_complete(kind, e, sonarr_series, radarr_movies)
                except Exception as ex:
                    print(f'rule check error ({e}): {type(ex).__name__}: {ex}')
                    done_ = False
                if done_:
                    label_ = series_name(e) if kind == 'series' else f"{e['title']} ({e['year']})"
                    print(f'rule complete, removing: {label_} for {rule_title(r)}')
                    changed = True
                else:
                    keep.append(e)
            r[key] = keep
        if not r['series'] and not r['movies']:
            cfg['rules'].remove(r)
    if changed and apply:
        save_config(cfg)


def load_state():
    if os.path.exists(STATE_FILE):
        st = json.load(open(STATE_FILE))
    else:
        st = {}
    st.pop('packs', None)  # old format: season packs used to wait; now removed right away
    for k, v in (('skip', []), ('seen', {}), ('done', {}), ('seerr_pending', {}), ('pack_dirs', {})):
        st.setdefault(k, v)
    return st


def save_state(st):
    tmp = STATE_FILE + '.tmp'
    with open(tmp, 'w') as fh:
        json.dump(st, fh, indent=1)
    os.replace(tmp, STATE_FILE)


def run(apply):
    cfg = load_config()
    delay = cfg.get('delay_seconds', 3600)
    st = load_state()
    now = int(time.time())
    print(f"mode={'APPLY' if apply else 'DRY-RUN'}")
    items = finished_items(cfg)
    print(f'finished items in scope: {len(items)}')

    skip, done, seen = set(st['skip']), st['done'], st['seen']
    for it in items:
        if it['Id'] not in skip and it['Id'] not in done:
            seen.setdefault(it['Id'], now)

    pending = [it for it in items if it['Id'] not in skip and it['Id'] not in done]
    idx_t = torrent_index()
    idx_ino = downloads_by_inode() if pending else {}
    rescan = False
    for it in sorted(pending, key=label):
        iid = it['Id']
        age = now - seen[iid]
        if age < delay:
            print(f'waiting {int((delay - age) / 60)} min: {label(it)}')
            continue
        print(f'due: {label(it)}')
        try:
            if it['Type'] == 'Episode':
                res = act_episode(it, idx_ino, idx_t, st, apply)
            else:
                res = act_movie(it, idx_ino, idx_t, st, apply)
        except Exception as e:  # keep going; retried next run
            print(f'    error: {type(e).__name__}: {e}')
            continue
        print(f'    -> {res[0]}: {res[1]}')
        if res[0] == 'done':
            done[iid] = now
            if len(res) > 4 and res[4]:
                rescan = True
            if it['Type'] == 'Movie' and len(res) > 3 and res[2] and res[3]:
                st['seerr_pending'][str(res[2])] = {'t': now, 'dir': res[3]}

    if rescan and apply:
        print('jellyfin: library refresh')
        jf('/Library/Refresh', 'POST')

    sweep_pack_dirs(st, torrent_index() if apply else idx_t, apply)
    prune_rules(cfg, apply)

    for tmdb, info in list(st['seerr_pending'].items()):
        if 'seerr' not in K:
            del st['seerr_pending'][tmdb]
            continue
        d = info['dir']
        if jellyfin_has_path(d):
            print(f'seerr: waiting for Jellyfin to drop {d} (tmdb {tmdb})')
            continue
        rows = [m for m in seerr('GET', '/media?take=500&skip=0').get('results', []) if m.get('tmdbId') == int(tmdb)]
        for m in rows:
            print(f"seerr: delete media row {m['id']} (tmdb {tmdb})")
            if apply:
                seerr('DELETE', f"/media/{m['id']}")
        if apply:
            del st['seerr_pending'][tmdb]

    if apply:
        save_state(st)


def main():
    load_settings()
    K.update(keys())
    a = [x for x in sys.argv[1:] if x != '--apply']
    if not a:
        return run('--apply' in sys.argv)
    cfg = load_config()
    match = 'any' if '--any' in a else 'all'
    seasons = None
    if '--seasons' in a:
        i = a.index('--seasons')
        seasons = sorted({int(x) for x in a[i + 1].split(',')})
        del a[i:i + 2]
    a = [x for x in a if x != '--any']
    cmd = a[0]
    if cmd == 'list':
        return cmd_list(cfg)
    if cmd == 'add-series' and len(a) == 3:
        return cmd_add_series(cfg, parse_users(a[1]), a[2], match, seasons)
    if cmd == 'add-movie' and len(a) == 4:
        return cmd_add_movie(cfg, parse_users(a[1]), a[2], int(a[3]), match)
    if cmd == 'remove-series' and len(a) == 3:
        return cmd_remove(cfg, parse_users(a[1]), 'series', a[2], match=match)
    if cmd == 'remove-movie' and len(a) == 4:
        return cmd_remove(cfg, parse_users(a[1]), 'movie', a[2], int(a[3]), match)
    sys.exit(__doc__)


if __name__ == '__main__':
    main()
