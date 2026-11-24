#!/home/minipc/invisible-playwright/.venv/bin/python
"""ext.to search + magnet extraction (host-side, invisible-playwright).

Usage (run with ~/invisible-playwright/.venv/bin/python):
  ext_magnet.py search "<query>" [--via trawl|browser] [--limit N]
  ext_magnet.py magnet "<detail-url>" [--ublock]
  ext_magnet.py magnets "<detail-url>" ["<detail-url>" ...] [--ublock]
  ext_magnet.py search-magnets "<query>" [--limit 10] [--via trawl|browser] [--ublock]

All commands print JSON on stdout; errors go to stderr with a non-zero exit code.

Magnet flow (verified against ext.to's own static/js/main.min.js):
  click a.download-btn-magnet -> handleTorrentDownload(btn, 'magnet')
  -> POST /ajax/getTorrentMagnet.php {torrent_id, download_type=magnet, timestamp,
     hmac=HMAC(pageToken), sessid}  -> JSON {success, url:"magnet:?xt=...", type}
  -> the page then clicks a hidden <a href="magnet:..."> with that url.
We click the real button and read the JSON response of that XHR.
"""
from __future__ import annotations

import argparse
import contextlib
import html
import json
import re
import shutil
import signal
import sys
import tempfile
import time
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
UBO_DIR = HERE / "ublock"
UBO_ID = "uBlock0@raymondhill.net"
CACHE_DIR = HERE / ".cache"

BASE = "https://ext.to"
TRAWL_URL = "http://127.0.0.1:8191/v1"
MAGNET_ENDPOINT = "/ajax/getTorrentMagnet.php"
MAGNET_RE = re.compile(r"^magnet:\?xt=urn:btih:([0-9a-fA-F]{40}|[A-Za-z2-7]{32})(?:&|$)")

# Network allowlist: ext.to itself (+ its Cloudflare /cdn-cgi paths), the Turnstile
# challenge host, and cdnjs (serves crypto-js, needed by the page's HMAC for the
# magnet XHR). Everything else is aborted.
ALLOW_HOSTS = {"ext.to", "challenges.cloudflare.com", "cdnjs.cloudflare.com"}

MAGNET_TIMEOUT = 60          # hard cap for one `magnet` call (s), browser launch included
PER_URL_TIMEOUT = 60         # cap per detail page inside a batch (s)
BATCH_EXTRA = 25             # budget added per extra URL in a batch (s)
SEARCH_MAGNETS_BUDGET = 165  # soft cap for search + magnet batch (engine search < ~3 min)

# Never let Firefox hand a magnet: URL to an OS handler (the page "clicks" one).
SAFE_PREFS = {
    "network.protocol-handler.external.magnet": False,
    "network.protocol-handler.warn-external.magnet": False,
    "browser.download.useDownloadDir": False,
}


class ExtError(Exception):
    pass


def log(*a):
    print("[ext_magnet]", *a, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- uBO
def _ubo_xpi() -> Path:
    """Pack the unpacked (signed, META-INF present) uBO build into an .xpi, cached."""
    xpi = CACHE_DIR / "ublock.xpi"
    src_mtime = max(p.stat().st_mtime for p in UBO_DIR.rglob("*"))
    if xpi.exists() and xpi.stat().st_mtime >= src_mtime:
        return xpi
    CACHE_DIR.mkdir(exist_ok=True)
    tmp = xpi.with_suffix(".tmp")
    # docker cp keeps epoch-0 mtimes from the image -> allow pre-1980 stamps
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, strict_timestamps=False) as z:
        for p in sorted(UBO_DIR.rglob("*")):
            if p.is_file():
                z.write(p, p.relative_to(UBO_DIR).as_posix())
    tmp.replace(xpi)
    return xpi


@contextlib.contextmanager
def temp_profile(use_ublock: bool = True):
    """Fresh throw-away Firefox profile dir; optionally pre-seeded with uBO."""
    d = Path(tempfile.mkdtemp(prefix="ext-magnet-profile-"))
    prefs = dict(SAFE_PREFS)
    try:
        if use_ublock:
            if not UBO_DIR.is_dir():
                raise ExtError(f"uBO requested but {UBO_DIR} is missing")
            (d / "extensions").mkdir()
            shutil.copy2(_ubo_xpi(), d / "extensions" / f"{UBO_ID}.xpi")
            prefs.update({"extensions.autoDisableScopes": 0, "extensions.enabledScopes": 15})
        yield d, prefs
    finally:
        shutil.rmtree(d, ignore_errors=True)


# --------------------------------------------------------------------------- timeouts
class _Alarm(BaseException):
    """BaseException so library code catching Exception can't swallow it."""


@contextlib.contextmanager
def hard_timeout(seconds: float, what: str):
    def _h(signum, frame):
        raise _Alarm(f"{what}: hard timeout after {seconds:.0f}s")
    old = signal.signal(signal.SIGALRM, _h)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old)


# --------------------------------------------------------------------------- parsing
def _text(fragment: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", fragment))).strip()


def parse_search(page_html: str) -> list[dict]:
    out = []
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", page_html, re.S):
        m = re.search(r'<a href="([^"]+)" class="torrent-title-link"[^>]*>(.*?)</a>', row, re.S)
        if not m:
            continue
        item = {"title": _text(m.group(2)),
                "url": urllib.parse.urljoin(BASE + "/", html.unescape(m.group(1)))}
        tid = re.search(r'data-id="(\d+)"', row)
        item["id"] = tid.group(1) if tid else None
        for key, label in (("size", "Size"), ("files", "Files"), ("age", "Age"),
                           ("seeds", "Seeds"), ("leechers", "Leechs")):
            c = re.search(r'<span class="add-block">' + label + r'</span>\s*<span([^>]*)>(.*?)</span>',
                          row, re.S)
            item[key] = _text(c.group(2)) if c else ""
            if key == "age" and c:
                t = re.search(r'title="([^"]+)"', c.group(1))
                item["date"] = t.group(1) if t else ""
        out.append(item)
    return out


def date_to_unix(s: str) -> int:
    try:
        return int(datetime.strptime(s, "%d %B %Y").replace(tzinfo=timezone.utc).timestamp())
    except (ValueError, TypeError):
        return 0


def search_url(query: str) -> str:
    return f"{BASE}/browse/?" + urllib.parse.urlencode({"q": query})


def fetch_via_trawl(url: str, timeout: int = 55) -> str:
    body = json.dumps({"cmd": "request.get", "url": url, "maxTimeout": timeout * 1000}).encode()
    req = urllib.request.Request(TRAWL_URL, data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout + 15) as r:
            d = json.loads(r.read())
    except Exception as e:  # noqa: BLE001
        raise ExtError(f"trawl request failed: {e}") from e
    sol = d.get("solution") or {}
    if d.get("status") != "ok" or int(sol.get("status") or 0) != 200:
        raise ExtError(f"trawl: status={d.get('status')} http={sol.get('status')} msg={d.get('message')}")
    return sol.get("response") or ""


# --------------------------------------------------------------------------- browser
def _host_allowed(url: str) -> bool:
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    return host in ALLOW_HOSTS or host.endswith(".ext.to")


def _route(route):
    req = route.request
    url = req.url
    path = urllib.parse.urlsplit(url).path.lower()
    if not _host_allowed(url):
        return route.abort()
    if path.endswith(".torrent"):                       # never fetch .torrent files
        return route.abort()
    if path == MAGNET_ENDPOINT.lower():                 # only the magnet variant
        form = urllib.parse.parse_qs(req.post_data or "")
        if form.get("download_type", [""])[0] != "magnet" and form.get("action", [""])[0] != "get_hash":
            return route.abort()
    return route.continue_()


class Browser:
    """One invisible-playwright session with a fresh temp profile and the allowlist."""

    def __init__(self, use_ublock: bool = False):
        self.use_ublock = use_ublock
        self._stack = contextlib.ExitStack()
        self.ctx = None
        self.stats = {"aborted": 0}

    def __enter__(self):
        from invisible_playwright import InvisiblePlaywright
        try:
            profile, prefs = self._stack.enter_context(temp_profile(self.use_ublock))
            if self.use_ublock:
                # persistent context on a throw-away dir: only way to pre-install an addon
                self.ctx = self._stack.enter_context(
                    InvisiblePlaywright(headless=True, profile_dir=profile, extra_prefs=prefs))
                time.sleep(2)  # let uBO start
            else:
                browser = self._stack.enter_context(
                    InvisiblePlaywright(headless=True, extra_prefs=prefs))
                self.ctx = browser.new_context()
                self._stack.callback(self.ctx.close)
            # context-level route: also covers popups / new tabs opened by page JS
            self.ctx.route("**/*", self._counting_route)
            return self
        except BaseException:
            self._stack.close()
            raise

    def __exit__(self, *exc):
        self._stack.close()

    def _counting_route(self, route):
        if not _host_allowed(route.request.url):
            self.stats["aborted"] += 1
        _route(route)

    def open(self, url: str, deadline: float):
        """Open url, get through the Cloudflare challenge if any, return a usable page.

        After a challenge the original page's JS handles go stale (Juggler keeps the
        pre-redirect execution context), so we reopen the URL in a fresh tab of the
        same context, which now holds cf_clearance.
        """
        for attempt in range(3):
            page = self.ctx.new_page()
            page.on("download", lambda d: d.cancel())
            docs = []

            def on_resp(r, page=page, docs=docs):
                try:
                    if r.request.resource_type == "document" and r.frame == page.main_frame:
                        docs.append(r.status)
                except Exception:  # noqa: BLE001
                    pass
            page.on("response", on_resp)
            remaining = deadline - time.time()
            if remaining <= 1:
                break
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=remaining * 1000)
            except Exception as e:  # noqa: BLE001  (navigations interrupted by CF)
                log(f"goto note: {str(e).splitlines()[0][:120]}")
            while not docs and time.time() < deadline:
                page.wait_for_timeout(200)
            if docs and docs[0] == 200:
                return page
            log(f"cloudflare challenge (doc status {docs[:1]}), waiting...")
            while 200 not in docs and time.time() < deadline:
                page.wait_for_timeout(300)
            page.close()
            if 200 not in docs:
                log(f"document statuses seen: {docs}")
                break
            log(f"challenge passed after attempt {attempt + 1}; reopening in a fresh tab")
        raise ExtError(f"could not load {url} (Cloudflare challenge not passed in time)")

    def html(self, url: str, timeout: float = 55, warmup: str | None = None) -> str:
        deadline = time.time() + timeout
        if warmup:  # get cf_clearance on a page whose challenge auto-solves first
            self.open(warmup, deadline).close()
        page = self.open(url, deadline)
        try:
            return page.content()
        finally:
            page.close()

    def magnet(self, url: str, timeout: float = PER_URL_TIMEOUT) -> dict:
        deadline = time.time() + timeout
        if urllib.parse.urlsplit(url).hostname not in ("ext.to", "www.ext.to"):
            raise ExtError(f"not an ext.to URL: {url}")
        page = self.open(url, deadline)
        try:
            def ms():
                return max(1000, (deadline - time.time()) * 1000)
            btn = page.locator("a.download-btn-magnet").first
            btn.wait_for(state="attached", timeout=ms())
            # handler deps: pageToken + CryptoJS (cdnjs) + page helpers + jQuery binding
            page.wait_for_function(
                "() => window.pageToken && window.CryptoJS && typeof computeHMAC === 'function'"
                " && typeof getMagnetSessid === 'function' && document.readyState !== 'loading'",
                timeout=ms())
            data_id = btn.get_attribute("data-id")
            with page.expect_response(
                    lambda r: urllib.parse.urlsplit(r.url).path == MAGNET_ENDPOINT
                    and r.request.method == "POST", timeout=ms()) as info:
                btn.click(timeout=ms())
            resp = info.value
            try:
                data = resp.json()
            except Exception as e:  # noqa: BLE001
                raise ExtError(f"magnet XHR returned non-JSON (HTTP {resp.status})") from e
            if not data.get("success"):
                raise ExtError(f"magnet XHR error: {data.get('error') or data}")
            source = "xhr.url"
            magnet = data.get("url") or ""
            if not magnet and data.get("hash"):
                # the page's own JS falls back to this exact construction (main.min.js)
                magnet = "magnet:?xt=urn:btih:" + data["hash"]
                source = "xhr.hash (page fallback)"
            if not MAGNET_RE.match(magnet):
                raise ExtError(f"magnet XHR returned no valid magnet: {str(data)[:200]}")
            return {"url": url, "id": data_id, "magnet": magnet, "source": source,
                    "type": data.get("type")}
        finally:
            with contextlib.suppress(Exception):
                page.close()


# --------------------------------------------------------------------------- commands
def cmd_search(query: str, via: str = "trawl", limit: int = 0, browser: Browser | None = None) -> list[dict]:
    url = search_url(query)
    if via == "trawl":
        page_html = fetch_via_trawl(url)
    elif browser is not None:
        page_html = browser.html(url, 90, warmup=BASE + "/")
    else:
        with Browser() as b:
            page_html = b.html(url, 90, warmup=BASE + "/")
    results = parse_search(page_html)
    if not results and "torrent-title-link" not in page_html and "No results" not in page_html:
        raise ExtError("search page did not contain a results table (blocked?)")
    return results[:limit] if limit else results


def cmd_magnets(urls: list[str], use_ublock: bool, browser: Browser | None = None,
                budget: float | None = None) -> list[dict]:
    """Extract magnets for urls in one browser. `budget` (s) is a soft cap for the whole
    batch: URLs that no longer fit are skipped (partial results instead of a hard kill)."""
    out = []
    end = time.time() + budget if budget else None
    own = browser is None
    b = Browser(use_ublock) if own else browser
    with (b if own else contextlib.nullcontext(b)):
        for u in urls:
            t = time.time()
            left = (end - t) if end else PER_URL_TIMEOUT
            if left < 12:
                out.append({"url": u, "error": "skipped: batch time budget used up", "seconds": 0})
                continue
            try:
                r = b.magnet(u, timeout=min(PER_URL_TIMEOUT, left))
            except _Alarm:
                raise
            except Exception as e:  # noqa: BLE001
                r = {"url": u, "error": str(e).splitlines()[0][:300]}
            r["seconds"] = round(time.time() - t, 1)
            log(f"{'ok ' if 'magnet' in r else 'ERR'} {r['seconds']:5.1f}s {u}")
            out.append(r)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("search")
    s.add_argument("query")
    s.add_argument("--via", choices=["trawl", "browser"], default="trawl")
    s.add_argument("--limit", type=int, default=0)
    m = sub.add_parser("magnet")
    m.add_argument("url")
    m.add_argument("--ublock", action="store_true", help="also load uBO (see README)")
    ms = sub.add_parser("magnets")
    ms.add_argument("urls", nargs="+")
    ms.add_argument("--ublock", action="store_true")
    sm = sub.add_parser("search-magnets")
    sm.add_argument("query")
    sm.add_argument("--limit", type=int, default=10)
    sm.add_argument("--via", choices=["trawl", "browser"], default="trawl")
    sm.add_argument("--ublock", action="store_true")
    a = ap.parse_args(argv)

    t0 = time.time()
    try:
        if a.cmd == "search":
            with hard_timeout(120, "search"):
                res = cmd_search(a.query, a.via, a.limit)
        elif a.cmd == "magnet":
            with hard_timeout(MAGNET_TIMEOUT, "magnet"):
                r = cmd_magnets([a.url], a.ublock)[0]
            if "error" in r:
                raise ExtError(r["error"])
            res = r
        elif a.cmd == "magnets":
            with hard_timeout(MAGNET_TIMEOUT + BATCH_EXTRA * (len(a.urls) - 1), "magnets"):
                res = cmd_magnets(a.urls, a.ublock)
            if not any("magnet" in r for r in res):
                print(json.dumps(res, indent=2))
                raise ExtError("no magnet extracted")
        else:  # search-magnets: one browser for the (optional) search and all magnets
            # search first (trawl), then one browser for all magnets with a soft budget
            # so the whole call stays under ~3 min; the alarm is only a backstop.
            with hard_timeout(SEARCH_MAGNETS_BUDGET + 60, "search-magnets"):
                res = cmd_search(a.query, a.via)
                left = SEARCH_MAGNETS_BUDGET - (time.time() - t0)
                mags = {r["url"]: r for r in cmd_magnets([x["url"] for x in res[:a.limit]],
                                                         a.ublock, budget=left)}
                for x in res:
                    if x["url"] in mags:
                        x["magnet"] = mags[x["url"]].get("magnet")
                        if "error" in mags[x["url"]]:
                            x["magnet_error"] = mags[x["url"]]["error"]
    except (ExtError, _Alarm) as e:
        log(f"error: {e}")
        return 1
    except KeyboardInterrupt:
        return 130
    except Exception as e:  # noqa: BLE001
        log(f"unexpected error: {type(e).__name__}: {str(e).splitlines()[0] if str(e) else ''}")
        return 1
    log(f"{a.cmd} done in {time.time() - t0:.1f}s")
    print(json.dumps(res, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
