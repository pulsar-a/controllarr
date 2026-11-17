#!/usr/bin/env python3
"""Controllarr: a web page to see, clean up and monitor a Jellyfin + *arr media server.

Lists every Jellyfin movie and show with size on disk, quality, who watched
and requested it, and whether seeding torrents still hold hardlinked copies.
Deletes (or replaces with a smaller version) through Radarr/Sonarr when they
manage the item, otherwise straight from disk; removes the matching
qBittorrent torrents and leftover download copies; clears the Seerr request;
keeps a history with "get it back". A Live tab shows who is streaming and
what loads the server (disk, CPU, torrents, background jobs) with 30 days of history.

Python 3.9+ standard library only. Configuration: config.json next to this file
(or the path in the CONTROLLARR_CONFIG environment variable). See README.md.
"""
import hashlib
import http.cookiejar
import http.cookies
import re
import secrets
import json
import logging
import os
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.environ.get("CONTROLLARR_CONFIG") or os.path.join(HERE, "config.json")
CFG = json.load(open(CONFIG_PATH))
DATA = os.path.abspath(os.path.expanduser(CFG.get("data_dir") or HERE))   # state files live here
os.makedirs(DATA, exist_ok=True)
TITLE = CFG.get("title") or "Controllarr"
SHARED_HOST = bool(CFG.get("shared_host", True))    # seedbox shared with other customers vs. own server
CACHE_FILE = os.path.join(DATA, "cache.json")
KEEP_FILE = os.path.join(DATA, "keep.json")
HISTORY_FILE = os.path.join(DATA, "history.json")
REBUILD_EVERY = int(CFG.get("rebuild_minutes", 30)) * 60
DOWNLOADS_ROOT = os.path.normpath(os.path.expanduser(CFG.get("downloads_root", "~/downloads")))
GB = 1024 ** 3
STALE_DAYS = int(CFG.get("stale_days", 182))
VIDEO_EXT = {".mkv", ".mp4", ".avi", ".m4v", ".mov", ".wmv", ".ts", ".m2ts",
             ".webm", ".mpg", ".mpeg", ".flv", ".iso"}

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(os.path.join(DATA, "controllarr.log")), logging.StreamHandler()])
log = logging.getLogger("library")
deletions = logging.getLogger("deletions")
deletions.propagate = False
deletions.addHandler(logging.FileHandler(os.path.join(DATA, "deletions.log")))
deletions.handlers[0].setFormatter(logging.Formatter("%(asctime)s %(message)s"))
deletions.setLevel(logging.INFO)


# ---------------------------------------------------------------- HTTP helpers
def call(method, url, headers=None, body=None, timeout=180, opener=None):
    h = dict(headers or {})
    data = None
    if body is not None:
        if isinstance(body, (dict, list)):
            data = json.dumps(body).encode()
            h.setdefault("Content-Type", "application/json")
        else:
            data = body.encode()
            h.setdefault("Content-Type", "application/x-www-form-urlencoded")
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    with (opener or urllib.request.build_opener()).open(req, timeout=timeout) as r:
        raw = r.read()
        if raw and "json" in r.headers.get("Content-Type", ""):
            return json.loads(raw)
        return raw.decode(errors="replace")


def qs(q):
    return ("?" + urllib.parse.urlencode(q)) if q else ""


def http_error_text(e):
    if isinstance(e, urllib.error.HTTPError):
        try:
            body = e.read().decode(errors="replace")[:300]
        except Exception:
            body = ""
        return "HTTP %s %s" % (e.code, body)
    return str(e)


class Jellyfin:
    def __init__(self, c):
        self.url = c["url"].rstrip("/")
        self.h = {"Authorization": 'MediaBrowser Token="%s"' % c["api_key"]}

    def get(self, path, **q):
        return call("GET", self.url + path + qs(q), self.h)

    def post(self, path, body):
        return call("POST", self.url + path, self.h, body)


class Api:
    """Radarr / Sonarr (api/v3) and Seerr (api/v1): API key in X-Api-Key."""

    def __init__(self, c, version):
        self.url = c["url"].rstrip("/") + "/api/" + version
        self.h = {"X-Api-Key": c["api_key"]}

    def req(self, method, path, body=None, **q):
        return call(method, self.url + path + qs(q), self.h, body, timeout=60)


class QBit:
    def __init__(self, c):
        self.url = c["url"].rstrip("/")
        self.user = c.get("username", "")
        self.pw = c.get("password", "")
        self.op = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def _login(self):
        if not self.pw:
            raise RuntimeError("qBittorrent password not set in config.json")
        r = call("POST", self.url + "/api/v2/auth/login", {"Referer": self.url},
                 urllib.parse.urlencode({"username": self.user, "password": self.pw}),
                 opener=self.op)
        if "Fails" in str(r):
            raise RuntimeError("qBittorrent login failed (check username/password)")

    def req(self, method, path, form=None, **q):
        url = self.url + "/api/v2" + path + qs(q)
        body = urllib.parse.urlencode(form) if form is not None else None
        try:
            return call(method, url, {"Referer": self.url}, body, opener=self.op)
        except urllib.error.HTTPError as e:
            if e.code not in (401, 403):
                raise
            self._login()
            return call(method, url, {"Referer": self.url}, body, opener=self.op)


def _service(name, need_key=True):
    """A connected app's settings, or None when it is left out or not filled in."""
    c = CFG.get(name) or {}
    return c if c.get("url") and (c.get("api_key") or not need_key) else None


if not _service("jellyfin"):
    raise SystemExit("config.json: jellyfin.url and jellyfin.api_key are required (see README)")
JF = Jellyfin(_service("jellyfin"))
RADARR = Api(_service("radarr"), "v3") if _service("radarr") else None
SONARR = Api(_service("sonarr"), "v3") if _service("sonarr") else None
SEERR = Api(_service("seerr"), "v1") if _service("seerr") else None
QB = QBit(_service("qbittorrent", need_key=False)) if _service("qbittorrent", need_key=False) else None


# ---------------------------------------------------------------- small stores
STORE_LOCK = threading.Lock()


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, default=list)
    os.replace(tmp, path)


def history_add(entry):
    with STORE_LOCK:
        h = load_json(HISTORY_FILE, [])
        h.append(entry)
        save_json(HISTORY_FILE, h[-2000:])


# ---------------------------------------------------------------- filesystem
def norm(p):
    return os.path.normpath(p) if p else ""


def is_video(p):
    return os.path.splitext(p)[1].lower() in VIDEO_EXT


def inside(path, roots):
    rp = os.path.realpath(path)
    for r in roots:
        rr = os.path.realpath(r)
        if rp == rr or rp.startswith(rr + os.sep):
            return True
    return False


def sidecars(video):
    """The video file plus files named after it (subtitles, nfo, thumbs)."""
    d = os.path.dirname(video)
    stem = os.path.splitext(os.path.basename(video))[0]
    out = [video]
    try:
        for f in os.listdir(d):
            fp = os.path.join(d, f)
            if fp == video or is_video(f) or not os.path.isfile(fp):
                continue
            if f.startswith(stem) and len(f) > len(stem) and f[len(stem)] in ".-_":
                out.append(fp)
    except OSError:
        pass
    return out


_STAT_CACHE = {"on": False, "map": {}}


def _stat(fp):
    if _STAT_CACHE["on"]:
        st = _STAT_CACHE["map"].get(fp)
        if st is None:
            st = os.stat(fp)
            _STAT_CACHE["map"][fp] = st
        return st
    return os.stat(fp)


def scan(paths):
    """size, size hardlinked elsewhere, and [(inode key, size, nlink)] for all files."""
    out = {"size": 0, "linked": 0, "files": []}
    seen = set()

    def add(fp):
        try:
            st = _stat(fp)
        except OSError:
            return
        k = (st.st_dev, st.st_ino)
        if k in seen:
            return
        seen.add(k)
        out["size"] += st.st_size
        if st.st_nlink > 1:
            out["linked"] += st.st_size
        out["files"].append((k, st.st_size, st.st_nlink))

    for p in paths:
        if os.path.isdir(p):
            for dp, _, fns in os.walk(p):
                for f in fns:
                    add(os.path.join(dp, f))
        else:
            add(p)
    return out


def freed_bytes(files, tpaths, qbit_ok, upaths=None):
    """Bytes actually released when these files, their torrents and leftover download copies go."""
    n = 0
    for k, size, nlink in files:
        others = nlink - 1 - (upaths or {}).get(k, 0) - (tpaths.get(k, 0) if qbit_ok else 0)
        if others <= 0:
            n += size
    return n


def untracked_entries(tinfo, extra_roots=()):
    """Top-level entries in the download folders that no torrent owns (leftovers)."""
    used = set()
    for t in tinfo.values():
        for p in [p for _, _, _, p in t["_files"]] + [t["content_path"]]:
            q = norm(p)
            while q and q.startswith(DOWNLOADS_ROOT + os.sep):
                used.add(q)
                q = os.path.dirname(q)
    roots = {t["save_path"] for t in tinfo.values()} | set(extra_roots)
    roots = {r for r in roots if r and r.startswith(DOWNLOADS_ROOT + os.sep)}
    roots |= {os.path.dirname(r) for r in roots if os.path.dirname(r).startswith(DOWNLOADS_ROOT + os.sep)}
    out = []
    for root in sorted(roots):
        try:
            names = os.listdir(root)
        except OSError:
            continue
        for name in names:
            p = norm(os.path.join(root, name))
            if p in used or p in roots or name.endswith(".!qB") or name.startswith("."):
                continue
            out.append(p)
    return out


def assert_deletable(path, roots):
    """Refuse anything that is not strictly inside one of the given folders."""
    rp = os.path.realpath(path)
    for root in roots:
        rr = os.path.realpath(root)
        if rp != rr and rp.startswith(rr + os.sep):
            return rp
    raise ValueError("refusing to delete outside the allowed folders: %s" % path)


def remove_path(path, roots):
    rp = assert_deletable(path, roots)
    if os.path.isdir(rp) and not os.path.islink(rp):
        shutil.rmtree(rp)
    elif os.path.lexists(rp):
        os.remove(rp)


def dir_has_video(d):
    for _, _, fns in os.walk(d):
        if any(is_video(f) for f in fns):
            return True
    return False


# ---------------------------------------------------------------- quality
def video_stream(item):
    for s in item.get("MediaStreams") or []:
        if s.get("Type") == "Video":
            return s
    return None


def res_of(s):
    if not s:
        return "?"
    w, h = s.get("Width") or 0, s.get("Height") or 0
    if w >= 3200 or h >= 1900:
        return "4K"
    if w >= 1800 or h >= 1000:
        return "1080p"
    if w >= 1200 or h >= 700:
        return "720p"
    return "SD" if w else "?"


def codec_of(s):
    c = ((s or {}).get("Codec") or "").lower()
    return {"hevc": "HEVC", "h265": "HEVC", "h264": "H.264", "avc": "H.264", "av1": "AV1",
            "mpeg4": "MPEG-4", "vc1": "VC-1", "mpeg2video": "MPEG-2", "vp9": "VP9",
            "msmpeg4v3": "DivX"}.get(c, c.upper() or "?")


def hdr_of(s):
    r = ((s or {}).get("VideoRangeType") or (s or {}).get("VideoRange") or "SDR").upper()
    if r in ("SDR", "UNKNOWN", ""):
        return ""
    return "DV" if "DOVI" in r else "HDR"


def quality(items, size):
    res, cod, hdr = Counter(), Counter(), Counter()
    ticks = 0
    for it in items:
        s = video_stream(it)
        res[res_of(s)] += 1
        cod[codec_of(s)] += 1
        hdr[hdr_of(s)] += 1
        ticks += it.get("RunTimeTicks") or 0
    hours = ticks / 36e9
    return {"res": res.most_common(1)[0][0] if res else "?",
            "codec": cod.most_common(1)[0][0] if cod else "?",
            "hdr": hdr.most_common(1)[0][0] if hdr else "",
            "hours": round(hours, 2),
            "gbh": round(size / GB / hours, 1) if hours > 0.05 else None}


# ---------------------------------------------------------------- qBittorrent
def torrent_index(on_progress=None):
    """info by hash (with _files), hashes by inode, torrent paths per inode."""
    info, by_inode, tpaths = {}, {}, {}
    tlist = QB.req("GET", "/torrents/info")
    for ti, t in enumerate(tlist):
        if on_progress:
            on_progress(ti + 1, len(tlist), t["name"])
        h = t["hash"].lower()
        files = []
        try:
            fl = QB.req("GET", "/torrents/files", hash=h)
        except Exception:
            fl = []
        for f in fl:
            p = os.path.join(t["save_path"], f["name"])
            try:
                st = os.stat(p)
            except OSError:
                continue
            k = (st.st_dev, st.st_ino)
            files.append((k, st.st_size, st.st_nlink, p))
            by_inode.setdefault(k, set()).add(h)
            tpaths[k] = tpaths.get(k, 0) + 1
        info[h] = {
            "hash": h, "name": t["name"], "size": t.get("size", 0),
            "ratio": round(t.get("ratio", 0), 2), "state": t.get("state", ""),
            "category": t.get("category", ""), "progress": round(t.get("progress", 0), 3),
            "seeding_time": t.get("seeding_time", 0), "added_on": t.get("added_on", 0),
            "completion_on": t.get("completion_on", 0),
            "tracker": urllib.parse.urlparse(t.get("tracker") or "").hostname or "",
            "content_path": norm(t.get("content_path", "")), "save_path": norm(t.get("save_path", "")),
            "_files": files,
        }
    return info, by_inode, tpaths


# ---------------------------------------------------------------- Seerr / arr lookups
def seerr_requests():
    out = {}
    if not SEERR:
        return out
    skip = 0
    while True:
        r = SEERR.req("GET", "/request", take=100, skip=skip, filter="all")
        for x in r.get("results", []):
            m = x.get("media") or {}
            u = x.get("requestedBy") or {}
            name = u.get("displayName") or u.get("jellyfinUsername") or u.get("username") or "someone"
            keys = []
            if m.get("mediaType") == "movie" and m.get("tmdbId"):
                keys.append("movie:%s" % m["tmdbId"])
            if m.get("mediaType") == "tv":
                if m.get("tmdbId"):
                    keys.append("tv:%s" % m["tmdbId"])
                if m.get("tvdbId"):
                    keys.append("tvdb:%s" % m["tvdbId"])
            for k in keys:
                e = out.setdefault(k, {"names": [], "media_id": m.get("id")})
                if name not in e["names"]:
                    e["names"].append(name)
        skip += 100
        if skip >= (r.get("pageInfo") or {}).get("results", 0):
            break
    return out


def seerr_clear(kind, ids):
    """Remove the item from Seerr so it shows as not available and can be requested again."""
    if not SEERR:
        return None
    media_id = None
    try:
        if kind == "movie" and ids.get("tmdb"):
            media_id = (SEERR.req("GET", "/movie/%s" % ids["tmdb"]).get("mediaInfo") or {}).get("id")
        elif kind == "show":
            if ids.get("tmdb"):
                media_id = (SEERR.req("GET", "/tv/%s" % ids["tmdb"]).get("mediaInfo") or {}).get("id")
            if not media_id and ids.get("tvdb"):
                media_id = (seerr_requests().get("tvdb:%s" % ids["tvdb"]) or {}).get("media_id")
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
    if not media_id:
        return None
    SEERR.req("DELETE", "/media/%s" % media_id)
    return "Cleared from Seerr"


def arr_defaults(arr):
    profiles = arr.req("GET", "/qualityprofile")
    roots = arr.req("GET", "/rootfolder")
    if not profiles or not roots:
        raise RuntimeError("no quality profile or root folder configured")
    return profiles[0]["id"], roots[0]["path"]


def radarr_get_back(tmdb, profile=None, root=None):
    if not RADARR or not tmdb:
        raise RuntimeError("needs Radarr and a TMDB id")
    existing = RADARR.req("GET", "/movie", tmdbId=tmdb)
    if existing:
        m = existing[0]
        m["monitored"] = True
        if profile:
            m["qualityProfileId"] = int(profile)
        RADARR.req("PUT", "/movie/%d" % m["id"], m)
        RADARR.req("POST", "/command", {"name": "MoviesSearch", "movieIds": [m["id"]]})
        return "Radarr is searching for it"
    dp, dr = arr_defaults(RADARR)
    m = RADARR.req("GET", "/movie/lookup/tmdb", tmdbId=tmdb)
    m.update(qualityProfileId=int(profile or dp), rootFolderPath=root or dr, monitored=True,
             addOptions={"searchForMovie": True})
    RADARR.req("POST", "/movie", m)
    return "Added to Radarr, searching"


def sonarr_get_back(tvdb, profile=None, root=None, season=None, episode_ids=None):
    if not SONARR or not tvdb:
        raise RuntimeError("needs Sonarr and a TVDB id")
    ex = SONARR.req("GET", "/series", tvdbId=tvdb)
    if ex:
        s = ex[0]
    else:
        if episode_ids:
            raise RuntimeError("the series is no longer in Sonarr")
        dp, dr = arr_defaults(SONARR)
        look = SONARR.req("GET", "/series/lookup", term="tvdb:%s" % tvdb)
        if not look:
            raise RuntimeError("Sonarr could not find TVDB id %s" % tvdb)
        s = look[0]
        s.update(qualityProfileId=int(profile or dp), rootFolderPath=root or dr, monitored=True,
                 seasonFolder=True,
                 addOptions={"monitor": "all" if season is None else "none",
                             "searchForMissingEpisodes": season is None})
        s = SONARR.req("POST", "/series", s)
        if season is None:
            return "Added to Sonarr, searching"
    if episode_ids:
        SONARR.req("PUT", "/episode/monitor", {"episodeIds": episode_ids, "monitored": True})
        SONARR.req("POST", "/command", {"name": "EpisodeSearch", "episodeIds": episode_ids})
        return "Sonarr is searching for %d episode(s)" % len(episode_ids)
    s["monitored"] = True
    if profile:
        s["qualityProfileId"] = int(profile)
    for se in s.get("seasons", []):
        if season is None and se["seasonNumber"] > 0:
            se["monitored"] = True
        elif se["seasonNumber"] == season:
            se["monitored"] = True
    SONARR.req("PUT", "/series/%d" % s["id"], s)
    if season is None:
        SONARR.req("POST", "/command", {"name": "SeriesSearch", "seriesId": s["id"]})
        return "Sonarr is searching for the series"
    SONARR.req("POST", "/command", {"name": "SeasonSearch", "seriesId": s["id"], "seasonNumber": season})
    return "Sonarr is searching for season %s" % season


# ---------------------------------------------------------------- library build
def get_quota():
    try:
        out = subprocess.run(["quota", "-w"], capture_output=True, text=True, timeout=30).stdout
        for line in out.splitlines():
            parts = line.split()
            if parts and parts[0].startswith("/dev/") and len(parts) >= 4:
                used = int(parts[1].rstrip("*")) * 1024
                limit = int(parts[3]) * 1024 or int(parts[2]) * 1024
                return {"used": used, "limit": limit}
    except Exception:
        pass
    return None


def jf_items(**q):
    return JF.get("/Items", Recursive="true", **q)["Items"]


def library_roots():
    vf = JF.get("/Library/VirtualFolders")
    movies = [norm(p) for l in vf if l.get("CollectionType") == "movies" for p in l["Locations"]]
    tv = [norm(p) for l in vf if l.get("CollectionType") == "tvshows" for p in l["Locations"]]
    return movies, tv


BUILD_STEPS = [
    ("jf_base", "Jellyfin: read users and libraries"),
    ("jf_items", "Jellyfin: read movies, shows and episodes"),
    ("jf_played", "Jellyfin: read who watched what"),
    ("radarr", "Radarr: read movies and quality profiles"),
    ("sonarr", "Sonarr: read series and quality profiles"),
    ("seerr", "Seerr: read requests"),
    ("qbit", "qBittorrent: read torrents and their files"),
    ("leftovers", "Downloads: look for leftover copies"),
    ("movies", "Disk: measure movies"),
    ("shows", "Disk: measure shows and episodes"),
    ("quota", "Disk: check space used"),
]


class BuildProgress:
    """Live step list of a library update, shown when hovering the Refresh button."""

    def __init__(self):
        self.started = time.time()
        self.finished = None
        self.steps = [{"key": k, "text": t, "state": "pending"} for k, t in BUILD_STEPS]
        self._idx = {k: i for i, (k, _) in enumerate(BUILD_STEPS)}
        self._t0 = {}

    def start(self, key, detail=None):
        st = self.steps[self._idx[key]]
        st["state"] = "running"
        if detail:
            st["detail"] = detail
        self._t0[key] = time.time()

    def detail(self, key, text):
        self.steps[self._idx[key]]["detail"] = text

    def done(self, key, detail=None, state="done"):
        st = self.steps[self._idx[key]]
        st["state"] = state
        if detail is not None:
            st["detail"] = detail
        if key in self._t0:
            st["secs"] = round(time.time() - self._t0.pop(key), 1)

    def fail_running(self, err):
        for st in self.steps:
            if st["state"] == "running":
                st["state"], st["detail"] = "error", err
            elif st["state"] == "pending":
                st["state"] = "skipped"

    def view(self):
        return {"started": self.started, "finished": self.finished,
                "steps": [dict(s) for s in self.steps]}


def build_library(prog=None):
    _STAT_CACHE["map"], _STAT_CACHE["on"] = {}, True
    try:
        return _build_library(prog)
    finally:
        _STAT_CACHE["map"], _STAT_CACHE["on"] = {}, False


def _build_library(prog=None):
    prog = prog or BuildProgress()
    t0 = time.time()
    prog.start("jf_base")
    users = [u for u in JF.get("/Users") if not u["Policy"].get("IsDisabled")]
    admin = next((u["Id"] for u in users if u["Policy"].get("IsAdministrator")), users[0]["Id"])
    server_id = JF.get("/System/Info").get("Id")
    movie_roots, tv_roots = library_roots()
    prog.done("jf_base", "%d users, %d libraries" % (len(users), len(movie_roots) + len(tv_roots)))

    prog.start("jf_items")
    base = dict(userId=admin, EnableImageTypes="Primary", ImageTypeLimit=1)
    j_movies = jf_items(IncludeItemTypes="Movie", Fields="ProviderIds,Path,DateCreated,MediaStreams", **base)
    prog.detail("jf_items", "%d movies…" % len(j_movies))
    j_series = jf_items(IncludeItemTypes="Series", Fields="ProviderIds,Path,DateCreated", **base)
    j_seasons = jf_items(IncludeItemTypes="Season", Fields="Path", userId=admin, EnableImages="false")
    prog.detail("jf_items", "%d movies, %d shows, episodes…" % (len(j_movies), len(j_series)))
    j_eps = jf_items(IncludeItemTypes="Episode", Fields="Path,MediaStreams,DateCreated", userId=admin, EnableImages="false")
    prog.done("jf_items", "%d movies, %d shows, %d episodes" % (len(j_movies), len(j_series), len(j_eps)))

    played, favs = {}, {}
    prog.start("jf_played")
    for ui, u in enumerate(users):
        prog.detail("jf_played", "%s (%d of %d)" % (u["Name"], ui + 1, len(users)))
        for flt in ("IsPlayed", "IsResumable"):
            for it in jf_items(userId=u["Id"], IncludeItemTypes="Movie,Episode", Filters=flt,
                               EnableImages="false"):
                ud = it.get("UserData") or {}
                e = played.setdefault(it["Id"], {}).setdefault(u["Name"], {})
                if flt == "IsPlayed":
                    e["p"] = 1
                else:
                    e["pct"] = round(ud.get("PlayedPercentage") or 0)
                if ud.get("LastPlayedDate"):
                    e["last"] = max(e.get("last", ""), ud["LastPlayedDate"])
        for it in jf_items(userId=u["Id"], IncludeItemTypes="Movie,Series", Filters="IsFavorite",
                           EnableImages="false"):
            favs.setdefault(it["Id"], []).append(u["Name"])
    prog.done("jf_played", "%d watched or started, %d favourites" % (len(played), sum(len(v) for v in favs.values())))

    radarr, sonarr, profiles = {}, {}, {"radarr": [], "sonarr": []}
    if RADARR:
        prog.start("radarr")
        try:
            for m in RADARR.req("GET", "/movie"):
                if m.get("hasFile"):
                    radarr[norm(m["path"])] = m
            profiles["radarr"] = [{"id": p["id"], "name": p["name"]} for p in RADARR.req("GET", "/qualityprofile")]
            prog.done("radarr", "%d movies with files" % len(radarr))
        except Exception as e:
            log.warning("Radarr unavailable: %s", e)
            prog.done("radarr", http_error_text(e), "error")
    else:
        prog.done("radarr", "not configured", "skipped")
    if SONARR:
        prog.start("sonarr")
        try:
            for s in SONARR.req("GET", "/series"):
                sonarr[norm(s["path"])] = s
            profiles["sonarr"] = [{"id": p["id"], "name": p["name"]} for p in SONARR.req("GET", "/qualityprofile")]
            prog.done("sonarr", "%d series" % len(sonarr))
        except Exception as e:
            log.warning("Sonarr unavailable: %s", e)
            prog.done("sonarr", http_error_text(e), "error")
    else:
        prog.done("sonarr", "not configured", "skipped")

    seerr_status, requests = ("not configured" if not SEERR else "ok"), {}
    if SEERR:
        prog.start("seerr")
        try:
            requests = seerr_requests()
            prog.done("seerr", "%d requested titles" % len({e["media_id"] for e in requests.values()}))
        except Exception as e:
            seerr_status = str(e)
            log.warning("Seerr unavailable: %s", e)
            prog.done("seerr", http_error_text(e), "error")
    else:
        prog.done("seerr", "not configured", "skipped")

    qbit_status, tinfo, tidx, tpaths = "not configured", {}, {}, {}
    if QB:
        prog.start("qbit")
        try:
            tinfo, tidx, tpaths = torrent_index(lambda i, n, name: prog.detail("qbit", "%d of %d — %s" % (i, n, name)))
            qbit_status = "ok"
            prog.done("qbit", "%d torrents" % len(tinfo))
        except Exception as e:
            qbit_status = str(e)
            log.warning("qBittorrent unavailable: %s", e)
            prog.done("qbit", http_error_text(e), "error")
    else:
        prog.done("qbit", "not configured", "skipped")
    qbit_ok = qbit_status == "ok"
    keep = load_json(KEEP_FILE, {})
    media_ino = {}
    stale_before = time.strftime("%Y-%m-%d", time.gmtime(time.time() - STALE_DAYS * 86400))

    # leftover copies in the download folders (only knowable when qBittorrent answers)
    untracked, upaths, uidx = [], {}, {}
    if qbit_ok:
        prog.start("leftovers")
        extra = []
        try:
            extra.append(norm(QB.req("GET", "/app/preferences").get("save_path")))
        except Exception:
            pass
        entries = untracked_entries(tinfo, extra)
        for ei, p in enumerate(entries):
            prog.detail("leftovers", "%d of %d — %s" % (ei + 1, len(entries), os.path.basename(p)))
            usc = scan([p])
            entry = {"path": p, "name": os.path.relpath(p, DOWNLOADS_ROOT), "size": usc["size"],
                     "frees": freed_bytes(usc["files"], {}, False), "_files": usc["files"]}
            untracked.append(entry)
            for k, _, _ in usc["files"]:
                upaths[k] = upaths.get(k, 0) + 1
                uidx.setdefault(k, set()).add(p)
        prog.done("leftovers", "%d leftover copies, %s" % (len(untracked), human(sum(u["size"] for u in untracked))))
    else:
        prog.done("leftovers", "needs qBittorrent", "skipped")

    def torrents_for(files):
        return sorted({h for k, _, _ in files for h in tidx.get(k, ())})

    def leftovers_for(files):
        return sorted({os.path.relpath(p, DOWNLOADS_ROOT) for k, _, _ in files for p in uidx.get(k, ())})

    def frees(files):
        return freed_bytes(files, tpaths, qbit_ok, upaths)

    def register(files, label):
        for k, _, _ in files:
            media_ino.setdefault(k, label)

    # ---- movies
    dir_count = {}
    for m in j_movies:
        if m.get("Path"):
            d = os.path.dirname(norm(m["Path"]))
            dir_count[d] = dir_count.get(d, 0) + 1
    movies = []
    prog.start("movies")
    for mi, m in enumerate(j_movies):
        if mi % 5 == 0:
            prog.detail("movies", "%d of %d — %s" % (mi + 1, len(j_movies), m.get("Name", "?")))
        path = norm(m.get("Path"))
        if not path or not os.path.exists(path):
            continue
        if os.path.isdir(path):
            target = [path]
        else:
            d = os.path.dirname(path)
            target = sidecars(path) if (d in movie_roots or dir_count.get(d, 0) > 1) else [d]
        sc = scan(target)
        register(sc["files"], m.get("Name", "?"))
        rm = None
        for rpath, cand in radarr.items():
            if path == rpath or path.startswith(rpath + os.sep):
                rm = cand
                break
        pid = m.get("ProviderIds") or {}
        ids = {"tmdb": pid.get("Tmdb") or (rm or {}).get("tmdbId"), "imdb": pid.get("Imdb")}
        movies.append({
            "id": m["Id"], "title": m.get("Name", "?"), "year": m.get("ProductionYear"),
            "added": m.get("DateCreated"), "poster": bool((m.get("ImageTags") or {}).get("Primary")),
            "path": path, "size": sc["size"], "linked": sc["linked"],
            "frees": frees(sc["files"]), "torrents": torrents_for(sc["files"]),
            "leftovers": leftovers_for(sc["files"]),
            "arr": {"app": "radarr", "id": rm["id"], "profile": rm.get("qualityProfileId"),
                    "root": rm.get("rootFolderPath")} if rm else None,
            "watched": played.get(m["Id"], {}), "favs": favs.get(m["Id"], []),
            "stale": not played.get(m["Id"]) and (m.get("DateCreated") or "9")[:10] < stale_before,
            "kept": bool(keep.get("movie:" + m["Id"])),
            "requested": (requests.get("movie:%s" % ids["tmdb"]) or {}).get("names", []),
            "quality": quality([m], sc["size"]), "ids": ids,
            "_target": target,
        })

    prog.done("movies", "%d movies, %s" % (len(movies), human(sum(x["size"] for x in movies))))

    # ---- shows
    prog.start("shows")
    season_items = {(s.get("SeriesId"), s.get("IndexNumber")): s for s in j_seasons}
    eps_by_series = {}
    for e in j_eps:
        p = norm(e.get("Path"))
        if p and os.path.exists(p):
            eps_by_series.setdefault(e.get("SeriesId"), []).append(e)

    shows = []
    for si_, s in enumerate(j_series):
        prog.detail("shows", "%d of %d — %s" % (si_ + 1, len(j_series), s.get("Name", "?")))
        path = norm(s.get("Path"))
        if not path or not os.path.isdir(path):
            continue
        eps = eps_by_series.get(s["Id"], [])
        sc = scan([path])
        register(sc["files"], s.get("Name", "?"))
        sm = sonarr.get(path)
        pid = s.get("ProviderIds") or {}
        ids = {"tvdb": pid.get("Tvdb") or (sm or {}).get("tvdbId"),
               "tmdb": pid.get("Tmdb") or (sm or {}).get("tmdbId"), "imdb": pid.get("Imdb")}
        watchers = {u for e in eps for u in played.get(e["Id"], {})}
        by_n = {}
        for e in eps:
            n = e.get("ParentIndexNumber")
            by_n.setdefault(-1 if n is None else n, []).append(e)
        seasons, agg = [], {}
        show_ew, show_ews, show_ewf = 0, 0, 0
        for n in sorted(by_n, key=lambda x: (x == 0, x)):
            seps = by_n[n]
            files = [norm(e["Path"]) for e in seps]
            ssc = scan(files)
            watched = {}
            ep_list = []
            ew = ews = ewf = 0
            stale = stale_size = 0
            for e in seps:
                by = played.get(e["Id"], {})
                for uname, w in by.items():
                    for bucket in (watched.setdefault(uname, {"w": 0, "ip": 0}),
                                   agg.setdefault(uname, {"w": 0, "ip": 0})):
                        if w.get("p"):
                            bucket["w"] += 1
                        elif w.get("pct") is not None:
                            bucket["ip"] += 1
                        if w.get("last"):
                            bucket["last"] = max(bucket.get("last", ""), w["last"])
                done_by = sorted(u for u, w in by.items() if w.get("p"))
                all_done = bool(watchers) and set(done_by) >= watchers
                esc = scan([norm(e["Path"])])
                if not by and (e.get("DateCreated") or "9")[:10] < stale_before:
                    stale += 1
                    stale_size += esc["size"]
                if all_done:
                    ew += 1
                    ews += esc["size"]
                    ewf += frees(esc["files"])
                ep_list.append({"id": e["Id"], "path": norm(e["Path"]), "all": all_done})
            si = season_items.get((s["Id"], n)) or {}
            seasons.append({
                "id": "%s:%s" % (s["Id"], n), "n": n,
                "name": si.get("Name") or ("Season %s" % n if n >= 0 else "Unknown season"),
                "episodes": len(seps), "size": ssc["size"], "linked": ssc["linked"],
                "frees": frees(ssc["files"]), "leftovers": leftovers_for(ssc["files"]),
                "torrents": torrents_for(ssc["files"]), "watched": watched,
                "quality": quality(seps, ssc["size"]),
                "ew": ew, "ews": ews, "ewf": ewf, "stale": stale, "stale_size": stale_size,
                "_files": files, "_eps": ep_list,
                "_dir": norm(si.get("Path")) if si.get("Path") else None,
            })
            show_ew += ew
            show_ews += ews
            show_ewf += ewf
        shows.append({
            "id": s["Id"], "title": s.get("Name", "?"), "year": s.get("ProductionYear"),
            "added": s.get("DateCreated"), "poster": bool((s.get("ImageTags") or {}).get("Primary")),
            "path": path, "size": sc["size"], "linked": sc["linked"],
            "frees": frees(sc["files"]), "torrents": torrents_for(sc["files"]),
            "leftovers": leftovers_for(sc["files"]),
            "episodes": len(eps), "seasons": seasons, "watched": agg, "watchers": sorted(watchers),
            "favs": favs.get(s["Id"], []), "kept": bool(keep.get("show:" + s["Id"])),
            "requested": ((requests.get("tv:%s" % ids["tmdb"]) or requests.get("tvdb:%s" % ids["tvdb"]))
                          or {}).get("names", []),
            "quality": quality(eps, sum(se["size"] for se in seasons)), "ids": ids,
            "ew": show_ew, "ews": show_ews, "ewf": show_ewf,
            "stale": sum(x["stale"] for x in seasons), "stale_size": sum(x["stale_size"] for x in seasons),
            "arr": {"app": "sonarr", "id": sm["id"], "profile": sm.get("qualityProfileId"),
                    "root": sm.get("rootFolderPath")} if sm else None,
        })

    prog.done("shows", "%d shows, %s" % (len(shows), human(sum(x["size"] for x in shows))))

    # ---- downloads: every torrent + leftover copies no torrent owns
    media_roots = movie_roots + tv_roots
    torrents = []
    for t in tinfo.values():
        labels = sorted({media_ino[k] for k, _, _, _ in t["_files"] if k in media_ino})
        pure = sum(sz for _, sz, nl, _ in t["_files"] if nl == 1)
        torrents.append(dict({k: v for k, v in t.items() if not k.startswith("_")},
                             items=labels, pure=pure,
                             in_media=inside(t["content_path"] or "/nonexistent", media_roots)))
    for u in untracked:
        u["items"] = sorted({media_ino[k] for k, _, _ in u["_files"] if k in media_ino})

    used_hashes = {h for it in movies + shows for h in it["torrents"]}
    for s in shows:
        for se in s["seasons"]:
            used_hashes.update(se["torrents"])
    data = {
        "movies": movies, "shows": shows,
        "downloads": {"torrents": torrents, "untracked": untracked},
        "torrents": {h: {"name": tinfo[h]["name"], "ratio": tinfo[h]["ratio"],
                         "pure": sum(sz for _, sz, nl, _ in tinfo[h]["_files"] if nl == 1)}
                     for h in used_hashes if h in tinfo},
        "users": sorted(u["Name"] for u in users), "server_id": server_id,
        "profiles": profiles, "qbit": qbit_status, "seerr": seerr_status, "quota": None,
        "roots": {"movies": movie_roots, "tv": tv_roots},
        "built_at": time.time(), "took": round(time.time() - t0, 1),
    }
    prog.start("quota")
    data["quota"] = get_quota()
    prog.done("quota", ("%s of %s" % (human(data["quota"]["used"]), human(data["quota"]["limit"])))
              if data["quota"] else "not available", "done" if data["quota"] else "skipped")
    data["took"] = round(time.time() - t0, 1)
    log.info("library built: %d movies, %d shows, %d torrents, %d untracked in %.1fs (qbit: %s, seerr: %s)",
             len(movies), len(shows), len(torrents), len(untracked), data["took"], qbit_status, seerr_status)
    return data


# ---------------------------------------------------------------- state
STATE = {"data": None, "building": False, "error": None, "dirty": False, "build": None, "version": ""}
_VERSION = [int(time.time()), 0]


def bump_version():
    _VERSION[1] += 1
    STATE["version"] = "%x-%d" % (_VERSION[0], _VERSION[1])
STATE_LOCK = threading.Lock()
ACTION_LOCK = threading.Lock()


def public(o):
    if isinstance(o, dict):
        return {k: public(v) for k, v in o.items() if not k.startswith("_")}
    if isinstance(o, list):
        return [public(v) for v in o]
    return o


def rebuild():
    with STATE_LOCK:
        if STATE["building"]:
            STATE["dirty"] = True      # build again once the current one ends
            return
        STATE["building"] = True
        STATE["build"] = BuildProgress()
    try:
        # Wait for a delete that is still running, and keep new ones out until the update is done
        # (they are refused while STATE["building"] is set), so every action sees consistent data.
        with ACTION_LOCK:
            while True:
                STATE["dirty"] = False
                prog = STATE["build"] if not STATE["build"].finished else BuildProgress()
                prog.started = time.time()
                STATE["build"] = prog
                try:
                    d = build_library(prog)
                except Exception as e:
                    prog.fail_running(http_error_text(e))
                    prog.finished = time.time()
                    raise
                prog.finished = time.time()
                with STATE_LOCK:
                    STATE["data"], STATE["error"] = d, None
                    bump_version()
                try:
                    save_json(CACHE_FILE, d)
                except Exception as e:
                    log.warning("could not save cache: %s", e)
                if not STATE["dirty"]:
                    break
    except Exception as e:
        log.exception("build failed")
        with STATE_LOCK:
            STATE["error"] = str(e)
    finally:
        with STATE_LOCK:
            STATE["building"] = False


_REBUILD_TIMER = {"t": None}


def rebuild_now():
    threading.Thread(target=rebuild, daemon=True).start()


def rebuild_soon(delay=8):
    """Update after a delete, but only once deletes stop coming (a bulk delete is many in a row)."""
    def fire():
        if ACTION_LOCK.locked():
            return rebuild_soon(delay)
        rebuild()
    if _REBUILD_TIMER["t"]:
        _REBUILD_TIMER["t"].cancel()
    t = threading.Timer(delay, fire)
    t.daemon = True
    _REBUILD_TIMER["t"] = t
    t.start()


def rebuild_loop():
    while True:
        rebuild()
        time.sleep(REBUILD_EVERY)


def find(kind, id_):
    d = STATE["data"] or {}
    if kind == "movie":
        for m in d.get("movies", []):
            if m["id"] == id_:
                return m, None
    elif kind == "show":
        for s in d.get("shows", []):
            if s["id"] == id_:
                return s, None
    elif kind == "season":
        sid = id_.split(":")[0]
        for s in d.get("shows", []):
            if s["id"] == sid:
                for se in s["seasons"]:
                    if se["id"] == id_:
                        return se, s
    raise KeyError("item not found, refresh the page")


def media_roots():
    d = STATE["data"]
    return d["roots"]["movies"] + d["roots"]["tv"]


def human(b):
    for u in ("B", "KB", "MB", "GB", "TB"):
        if b < 1024 or u == "TB":
            return ("%.1f %s" % (b, u)) if u in ("GB", "TB") else ("%d %s" % (b, u))
        b /= 1024


# ---------------------------------------------------------------- plans and actions
def target_files(kind, item, mode):
    """Video files (or folders) the action removes, for torrent matching."""
    if mode == "watched":
        eps = item["_eps"] if kind == "season" else [e for se in item["seasons"] for e in se["_eps"]]
        return [e["path"] for e in eps if e["all"]]
    if kind == "movie":
        return item["_target"]
    if kind == "show":
        return [item["path"]]
    return item["_files"]


def fresh_torrents(item, kind, parent, mode):
    """Torrents tied to what is removed: shared inodes now, plus Radarr/Sonarr grab history."""
    if not QB:
        return {}, {}, "qBittorrent not configured"
    try:
        tinfo, tidx, _ = torrent_index()
    except Exception as e:
        return {}, {}, str(e)
    sc = scan(target_files(kind, item, mode))
    hashes = {h for k, _, _ in sc["files"] for h in tidx.get(k, ())}
    if mode != "watched":
        try:
            if kind == "movie" and item.get("arr"):
                for h in RADARR.req("GET", "/history/movie", movieId=item["arr"]["id"]):
                    if h.get("downloadId"):
                        hashes.add(h["downloadId"].lower())
            elif kind in ("show", "season") and (parent or item).get("arr"):
                show = parent or item
                for h in SONARR.req("GET", "/history/series", seriesId=show["arr"]["id"], includeEpisode="true"):
                    if not h.get("downloadId"):
                        continue
                    if kind == "season" and (h.get("episode") or {}).get("seasonNumber") != item["n"]:
                        continue
                    hashes.add(h["downloadId"].lower())
        except Exception as e:
            log.warning("history lookup failed: %s", e)
    roots = media_roots()
    out = {}
    for h in hashes:
        if h in tinfo:
            t = tinfo[h]
            out[h] = {"name": t["name"], "size": t["size"], "ratio": t["ratio"],
                      "delete_files": not inside(t["content_path"] or "/nonexistent", roots)}
    # leftover download copies sharing files with the item, still not owned by any torrent
    keys = {tuple(k) for k, _, _ in sc["files"]}
    cands = [u for u in STATE["data"]["downloads"]["untracked"]
             if any(tuple(k) in keys for k, _, _ in u.get("_files", []))]
    if cands:
        free_now = set(untracked_entries(tinfo, {os.path.dirname(u["path"]) for u in cands}))
        out_l = {u["path"]: u["name"] for u in cands if u["path"] in free_now}
    else:
        out_l = {}
    return out, out_l, None


def profile_name(app, pid):
    for p in (STATE["data"].get("profiles") or {}).get(app, []):
        if p["id"] == pid:
            return p["name"]
    return "profile %s" % pid


def make_plan(kind, id_, mode="delete", profile=None):
    item, parent = find(kind, id_)
    show = parent or item
    torrents, leftovers, terr = fresh_torrents(item, kind, parent, mode)
    title = item["title"] if kind != "season" else "%s — %s" % (parent["title"], item["name"])
    steps, warnings = [], []
    arr = (item if kind == "movie" else show).get("arr")
    size, frees = item["size"], item["frees"]

    if mode == "watched":
        if kind == "movie":
            raise ValueError("not available for movies")
        size, frees = item["ews"], item["ewf"]
        n = item["ew"]
        if not n:
            raise ValueError("no episodes watched by everyone watching this show")
        title += " — watched episodes"
        steps.append("Delete %d episode(s) watched by %s" % (n, ", ".join(show["watchers"])))
        if arr:
            steps.append("Unmonitor them in Sonarr so they don't download again")
    elif mode == "replace":
        app = "radarr" if kind == "movie" else "sonarr"
        if not profile:
            raise ValueError("choose a quality profile")
        pname = profile_name(app, int(profile))
        if arr:
            steps.append("Delete the current file(s) via %s" % app.capitalize())
            steps.append("Switch %s to “%s” and search for a new copy" %
                         ("the whole series" if kind != "movie" else "the movie", pname))
        else:
            ids = item.get("ids") if kind != "season" else parent.get("ids")
            key = "tmdb" if kind == "movie" else "tvdb"
            if not (ids or {}).get(key):
                raise ValueError("no %s id in Jellyfin, can't add it to %s" % (key.upper(), app.capitalize()))
            steps.append("Delete the current file(s) from disk")
            steps.append("Add it to %s with “%s” and search for a new copy" % (app.capitalize(), pname))
        warnings.append("Until the new copy downloads, it won't be playable.")
    else:
        if kind == "movie":
            steps.append("Remove the movie from Radarr and delete its folder" if arr
                         else "Delete from disk: %s" % ", ".join(item["_target"]))
        elif kind == "show":
            steps.append("Remove the series from Sonarr and delete its folder" if arr
                         else "Delete folder from disk: %s" % item["path"])
        else:
            steps.append("Delete the season's files via Sonarr and unmonitor the season" if arr
                         else "Delete %d episode file(s) from disk" % item["episodes"])
        if kind in ("movie", "show") and item.get("requested"):
            steps.append("Clear the Seerr request (requested by %s)" % ", ".join(item["requested"]))

    for t in torrents.values():
        steps.append("Remove torrent “%s” (ratio %s)%s" % (
            t["name"], t["ratio"], " and its downloaded data" if t["delete_files"] else ", keeping files"))
    for name in leftovers.values():
        steps.append("Delete leftover download copy “%s”" % name)
    if terr:
        warnings.append("Torrents and leftover copies not checked: %s" % terr)
    if mode != "watched" and item.get("linked") and not torrents and not leftovers:
        warnings.append("%s is hardlinked to another copy that stays on disk." % human(item["linked"]))
    kept = []
    if show.get("kept"):
        kept.append("marked Keep")
    if show.get("favs"):
        kept.append("favourite of " + ", ".join(show["favs"]))
    return {"kind": kind, "id": id_, "mode": mode, "title": title, "size": size, "frees": frees,
            "steps": steps, "warnings": warnings, "kept": kept, "torrents": torrents,
            "leftovers": leftovers}


def jf_notify(paths):
    JF.post("/Library/Media/Updated",
            {"Updates": [{"Path": p, "UpdateType": "Deleted"} for p in paths if p]})


def sonarr_delete_files(series_id, paths=None, season=None):
    """Delete Sonarr episode files by season or by path. Returns (count, sonarr episode ids)."""
    files = SONARR.req("GET", "/episodefile", seriesId=series_id)
    if paths is not None:
        wanted = {norm(p) for p in paths}
        files = [f for f in files if norm(f.get("path")) in wanted]
    else:
        files = [f for f in files if f.get("seasonNumber") == season]
    ids = [f["id"] for f in files]
    ep_ids = []
    if ids:
        eps = SONARR.req("GET", "/episode", seriesId=series_id)
        ep_ids = [e["id"] for e in eps if e.get("episodeFileId") in set(ids)]
        SONARR.req("DELETE", "/episodefile/bulk", {"episodeFileIds": ids})
    return len(ids), ep_ids


# ---------------------------------------------------------------- jobs (live progress)
JOBS = {}
JOBS_LOCK = threading.Lock()


class Job:
    """A list of labelled steps run one after another; the page polls it for progress."""

    def __init__(self, title):
        self.id = uuid.uuid4().hex[:12]
        self.title = title
        self.steps = []
        self.finished = False
        self.ok = None
        with JOBS_LOCK:
            JOBS[self.id] = self
            for old in list(JOBS)[:-50]:
                JOBS.pop(old, None)

    def view(self):
        return {"id": self.id, "title": self.title, "steps": self.steps,
                "finished": self.finished, "ok": self.ok}

    def fail_now(self, label, err):
        self.steps = [{"text": label, "state": "error", "detail": err}]
        self.finished, self.ok = True, False

    def run(self, ops):
        """ops: (label, fn, stop). fn may return a detail string.
        stop=True: a failure skips the remaining stop-able steps. stop=None: always runs."""
        self.steps = [{"text": label, "state": "pending"} for label, _, _ in ops]
        done, errors, stopped = [], [], False
        for st, (label, fn, stop) in zip(self.steps, ops):
            if stopped and stop is not None:
                st["state"] = "skipped"
                continue
            st["state"] = "running"
            st["t0"] = time.time()
            try:
                detail = fn()
                st["state"] = "done"
                if detail:
                    st["detail"] = str(detail)
                done.append(label)
            except Exception as e:
                st["state"] = "error"
                st["detail"] = http_error_text(e)
                errors.append("%s: %s" % (label, st["detail"]))
                log.warning("step failed: %s: %s", label, st["detail"])
                if stop:
                    stopped = True
            st["secs"] = round(time.time() - st.pop("t0"), 1)
        return done, errors


def prune_state(kind=None, id_=None, hashes=(), paths=()):
    """Drop what was just deleted from the served data right away, so a page reload before
    the next library update does not bring it back."""
    with STATE_LOCK:
        d = STATE["data"]
        if not d:
            return
        if kind == "movie":
            d["movies"] = [m for m in d["movies"] if m["id"] != id_]
        elif kind == "show":
            d["shows"] = [x for x in d["shows"] if x["id"] != id_]
        elif kind == "season":
            for show in d["shows"]:
                se = next((x for x in show["seasons"] if x["id"] == id_), None)
                if se:
                    show["seasons"] = [x for x in show["seasons"] if x["id"] != id_]
                    show["size"] = max(0, show["size"] - se["size"])
                    show["frees"] = max(0, show["frees"] - se["frees"])
                    show["episodes"] -= se["episodes"]
        bump_version()
        dl = d.get("downloads") or {}
        if hashes:
            hs = set(hashes)
            dl["torrents"] = [t for t in dl.get("torrents", []) if t["hash"] not in hs]
        if paths:
            ps = set(paths)
            dl["untracked"] = [u for u in dl.get("untracked", []) if u["path"] not in ps]


def where(p):
    """Path as shown to people: relative to the folder holding media/ and downloads/."""
    base = os.path.dirname(DOWNLOADS_ROOT)
    return os.path.relpath(p, base) if p.startswith(base + os.sep) else p


def disk_delete(p, roots):
    if not os.path.lexists(p):
        return "already gone"
    size = scan([p])["size"]
    remove_path(p, roots)
    return "freed %s" % human(size) if size else None


def start_job(title, worker, *args):
    job = Job(title)
    threading.Thread(target=worker, args=(job,) + args, daemon=True).start()
    return {"job": job.id}


def execute(kind, id_, mode="delete", profile=None):
    plan = make_plan(kind, id_, mode, profile)     # validation errors surface as 400 right away
    return start_job(plan["title"], _execute, plan, kind, id_, mode, profile)


def _execute(job, plan, kind, id_, mode, profile):
    with ACTION_LOCK:
        try:
            item, parent = find(kind, id_)
        except KeyError as e:
            return job.fail_now("Find the item", str(e))
        show = parent or item
        roots = media_roots()
        owner = item if kind == "movie" else show
        arr = owner.get("arr")
        ctx = {"notify": []}
        hist = {"uid": uuid.uuid4().hex[:12], "ts": time.time(), "kind": kind, "mode": mode,
                "title": plan["title"], "year": item.get("year") or show.get("year"),
                "size": plan["size"], "frees": plan["frees"], "ids": owner.get("ids") or {},
                "arr": arr, "season": item.get("n") if kind == "season" else None}
        ops = []

        def add(label, fn, stop=True):
            ops.append((label, fn, stop))

        # 1. torrents and leftover download copies
        ctx["hashes"], ctx["paths"] = [], []
        for h, t in plan["torrents"].items():
            def rm_torrent(h=h, t=t):
                QB.req("POST", "/torrents/delete",
                       {"hashes": h, "deleteFiles": "true" if t["delete_files"] else "false"})
                ctx["hashes"].append(h)
                return "%s, ratio %s" % (human(t["size"]), t["ratio"])
            add("qBittorrent: remove torrent “%s”%s" % (t["name"], " with its downloaded data" if t["delete_files"]
                                                         else " (files stay, they are in the library)"),
                rm_torrent, False)
        for p, name in plan["leftovers"].items():
            def rm_copy(p=p):
                r = disk_delete(p, [DOWNLOADS_ROOT])
                ctx["paths"].append(p)
                return r
            add("Downloads: delete leftover copy %s" % where(p), rm_copy, False)

        def per_file(paths, what="Disk"):
            for p in paths:
                def rm(p=p):
                    files = sidecars(p) if os.path.exists(p) else []
                    if not files:
                        return "already gone"
                    size = sum(os.path.getsize(f) for f in files if os.path.exists(f))
                    for f in files:
                        remove_path(f, roots)
                    return "freed %s" % human(size)
                add("%s: delete %s" % (what, where(p)), rm, False)

        def leftover_cleanup(paths, label):
            def rm():
                n = 0
                for p in paths:
                    for f in (sidecars(p) if os.path.exists(p) else []):
                        remove_path(f, roots)
                        n += 1
                return ("removed %d leftover file(s)" % n) if n else "nothing left behind"
            add(label, rm, False)

        # 2. the media itself
        if mode == "watched":
            eps = item["_eps"] if kind == "season" else [e for se in item["seasons"] for e in se["_eps"]]
            paths = [e["path"] for e in eps if e["all"]]
            ctx["notify"] = paths
            if arr:
                def son_del():
                    n, ep_ids = sonarr_delete_files(arr["id"], paths=paths)
                    hist["episode_ids"] = ep_ids
                    return "%d file(s) deleted" % n

                def son_unmon():
                    if hist.get("episode_ids"):
                        SONARR.req("PUT", "/episode/monitor", {"episodeIds": hist["episode_ids"], "monitored": False})
                    return "%d episode(s) unmonitored" % len(hist.get("episode_ids") or [])
                add("Sonarr: delete %d watched episode file(s)" % len(paths), son_del)
                add("Sonarr: unmonitor them so they don't download again", son_unmon)
                leftover_cleanup(paths, "Disk: remove subtitles and anything Sonarr left")
            else:
                per_file(paths)

        elif kind == "movie":
            ctx["notify"] = item["_target"]
            if arr and mode == "replace":
                def rad_delfile():
                    m = RADARR.req("GET", "/movie/%d" % arr["id"])
                    if not (m.get("movieFile") or {}).get("id"):
                        return "no file in Radarr"
                    RADARR.req("DELETE", "/moviefile/%d" % m["movieFile"]["id"])
                    return where(m["movieFile"].get("path") or "")

                def rad_search():
                    m = RADARR.req("GET", "/movie/%d" % arr["id"])
                    m.update(qualityProfileId=int(profile), monitored=True)
                    RADARR.req("PUT", "/movie/%d" % arr["id"], m)
                    RADARR.req("POST", "/command", {"name": "MoviesSearch", "movieIds": [arr["id"]]})
                    return "search started"
                add("Radarr: delete the current file", rad_delfile)
                add("Radarr: switch to “%s” and search for a new copy" % profile_name("radarr", int(profile)), rad_search)
            else:
                if arr:
                    add("Radarr: remove the movie and delete its folder",
                        lambda: RADARR.req("DELETE", "/movie/%d" % arr["id"],
                                           deleteFiles="true", addImportExclusion="false") and None)
                for p in item["_target"]:
                    add("Disk: delete %s" % where(p), lambda p=p: disk_delete(p, roots))

        elif kind == "show":
            if arr and mode == "replace":
                paths = [e["path"] for se in item["seasons"] for e in se["_eps"]]
                ctx["notify"] = paths
                add("Sonarr: delete all %d episode file(s)" % len(paths),
                    lambda: "%d file(s) deleted" % sonarr_delete_files(arr["id"], paths=paths)[0])

                def son_profile():
                    s = SONARR.req("GET", "/series/%d" % arr["id"])
                    s.update(qualityProfileId=int(profile), monitored=True)
                    SONARR.req("PUT", "/series/%d" % arr["id"], s)
                    SONARR.req("POST", "/command", {"name": "SeriesSearch", "seriesId": arr["id"]})
                    return "search started"
                add("Sonarr: switch to “%s” and search for new copies" % profile_name("sonarr", int(profile)), son_profile)
            else:
                ctx["notify"] = [item["path"]]
                if arr:
                    add("Sonarr: remove the series and delete %s" % where(item["path"]),
                        lambda: SONARR.req("DELETE", "/series/%d" % arr["id"], deleteFiles="true") and None)
                    add("Disk: make sure %s is gone" % where(item["path"]), lambda: disk_delete(item["path"], roots))
                else:
                    try:
                        entries = sorted(os.listdir(item["path"]))
                    except OSError:
                        entries = []
                    for name in entries:          # one step per season folder / file, for progress
                        p = os.path.join(item["path"], name)
                        add("Disk: delete %s" % where(p), lambda p=p: disk_delete(p, roots), False)
                    add("Disk: delete the folder %s" % where(item["path"]), lambda: disk_delete(item["path"], roots))

        else:  # season
            ctx["notify"] = item["_files"] + ([item["_dir"]] if item.get("_dir") else [])
            if arr:
                add("Sonarr: delete the %d file(s) of %s" % (item["episodes"], item["name"]),
                    lambda: "%d file(s) deleted" % sonarr_delete_files(arr["id"], season=item["n"])[0])

                def son_season():
                    s = SONARR.req("GET", "/series/%d" % arr["id"])
                    for se in s.get("seasons", []):
                        if se["seasonNumber"] == item["n"]:
                            se["monitored"] = mode == "replace"
                    if mode == "replace":
                        s["qualityProfileId"] = int(profile)
                    SONARR.req("PUT", "/series/%d" % arr["id"], s)
                    if mode == "replace":
                        SONARR.req("POST", "/command", {"name": "SeasonSearch", "seriesId": arr["id"],
                                                        "seasonNumber": item["n"]})
                        return "search started"
                    return "season unmonitored"
                add("Sonarr: switch to “%s” and search for %s" % (profile_name("sonarr", int(profile)), item["name"])
                    if mode == "replace" else "Sonarr: unmonitor %s so it doesn't download again" % item["name"],
                    son_season)
                leftover_cleanup(item["_files"], "Disk: remove subtitles and anything Sonarr left")
            else:
                per_file(item["_files"])
            sdir = item.get("_dir")
            if sdir and norm(sdir) != norm(parent["path"]):
                def rm_dir():
                    if not os.path.isdir(sdir):
                        return "already gone"
                    if dir_has_video(sdir):
                        return "kept: other videos are still inside"
                    return disk_delete(sdir, roots)
                add("Disk: delete the folder %s" % where(sdir), rm_dir, False)

        # 3. hand manual items to Radarr/Sonarr when replacing
        if mode == "replace" and not arr:
            if kind == "movie":
                add("Radarr: add it with “%s” and search" % profile_name("radarr", int(profile)),
                    lambda: radarr_get_back(item["ids"].get("tmdb"), profile))
            else:
                add("Sonarr: add it with “%s” and search" % profile_name("sonarr", int(profile)),
                    lambda: sonarr_get_back(show["ids"].get("tvdb"), profile,
                                            season=item["n"] if kind == "season" else None))

        # 4. Seerr + Jellyfin
        if mode == "delete" and kind in ("movie", "show") and SEERR:
            add("Seerr: clear the request" + (" from %s" % ", ".join(item["requested"]) if item.get("requested") else ""),
                lambda: seerr_clear(kind, hist["ids"]) or "nothing to clear", False)
        add("Jellyfin: remove it from the library", lambda: jf_notify(ctx["notify"]) or "library updated", None)

        done, errors = job.run(ops)
        job.ok = not errors
        # the item leaves the list once its files are really gone from disk
        gone = False
        if mode == "delete":
            if kind == "movie":
                gone = not any(os.path.lexists(x) for x in item["_target"])
            elif kind == "show":
                gone = not os.path.lexists(item["path"])
            else:
                gone = not any(os.path.lexists(x) for x in item["_files"])
        prune_state(kind if gone else None, id_, ctx["hashes"], ctx["paths"])
        hist.update(done=done, errors=errors,
                    restorable=mode != "replace" and bool(
                        (hist["ids"].get("tmdb") if kind == "movie" else hist["ids"].get("tvdb"))
                        and (mode != "watched" or hist.get("episode_ids"))))
        history_add(hist)
        deletions.info("%s/%s %s | %s | %d steps | errors: %s", kind, mode, plan["title"], human(plan["size"]),
                       len(done), "; ".join(errors) or "none")
        job.finished = True
    rebuild_soon()


def restore(uid):
    with STORE_LOCK:
        hist = load_json(HISTORY_FILE, [])
    h = next((x for x in hist if x["uid"] == uid), None)
    if not h:
        raise KeyError("history entry not found")
    if not h.get("restorable"):
        raise ValueError("this one can't be brought back automatically")
    arr = h.get("arr") or {}
    if h["kind"] == "movie":
        msg = radarr_get_back(h["ids"].get("tmdb"), arr.get("profile"), arr.get("root"))
    else:
        msg = sonarr_get_back(h["ids"].get("tvdb"), arr.get("profile"), arr.get("root"),
                              season=h.get("season") if h["kind"] == "season" and h["mode"] != "watched" else None,
                              episode_ids=h.get("episode_ids") if h["mode"] == "watched" else None)
    with STORE_LOCK:
        hist = load_json(HISTORY_FILE, [])
        for x in hist:
            if x["uid"] == uid:
                x["restored"] = time.time()
                x["restore_msg"] = msg
        save_json(HISTORY_FILE, hist)
    deletions.info("restore %s | %s", h["title"], msg)
    return {"ok": True, "msg": msg}


def delete_downloads(hashes, paths):
    n = len(hashes) + len(paths)
    return start_job("%d download%s" % (n, "" if n == 1 else "s"), _delete_downloads, hashes, paths)


def _delete_downloads(job, hashes, paths):
    def gone():
        raise KeyError("not in qBittorrent any more")

    with ACTION_LOCK:
        try:
            tinfo, _, _ = torrent_index()
        except Exception as e:
            return job.fail_now("qBittorrent: read the torrent list", http_error_text(e))
        roots = media_roots()
        ops, hist, gone_h, gone_p = [], [], [], []
        for h in hashes:
            h = h.lower()
            t = tinfo.get(h)
            if not t:
                ops.append(("qBittorrent: torrent %s" % h[:8], gone, False))
                continue
            keep_files = inside(t["content_path"] or "/nonexistent", roots)

            def rm(h=h, t=t, keep_files=keep_files):
                QB.req("POST", "/torrents/delete", {"hashes": h, "deleteFiles": "false" if keep_files else "true"})
                gone_h.append(h)
                return "%s, ratio %s" % (human(t["size"]), t["ratio"])
            ops.append(("qBittorrent: remove “%s”%s" % (t["name"], " (files stay, they are in the library)" if keep_files
                                                         else " with its data in %s" % where(t["content_path"])), rm, False))
            hist.append(("torrent", t["name"], t["size"]))
        listed = {u["path"]: u for u in STATE["data"]["downloads"]["untracked"]}
        free_now = set(untracked_entries(tinfo, {os.path.dirname(p) for p in paths}))
        for p in paths:
            def rm_left(p=p):
                if p not in listed:
                    raise KeyError("not in the leftover list, refresh the page")
                if p not in free_now:
                    raise ValueError("a torrent uses this now")
                r = disk_delete(p, [DOWNLOADS_ROOT])
                gone_p.append(p)
                return r
            ops.append(("Downloads: delete %s" % where(p), rm_left, False))
            if p in listed:
                hist.append(("file", listed[p]["name"], listed[p]["size"]))
        done, errors = job.run(ops)
        job.ok = not errors
        prune_state(hashes=gone_h, paths=gone_p)
        for kind, title, size in hist:
            history_add({"uid": uuid.uuid4().hex[:12], "ts": time.time(), "kind": kind, "mode": "delete",
                         "title": title, "size": size, "done": ["Deleted"], "errors": errors, "restorable": False})
        deletions.info("downloads %s | errors: %s", ", ".join(t for _, t, _ in hist), "; ".join(errors) or "none")
        job.finished = True
    rebuild_soon()


def set_keep(kind, id_, value):
    find(kind, id_)
    with STORE_LOCK:
        keep = load_json(KEEP_FILE, {})
        key = "%s:%s" % (kind, id_)
        if value:
            keep[key] = True
        else:
            keep.pop(key, None)
        save_json(KEEP_FILE, keep)
    item, _ = find(kind, id_)
    item["kept"] = bool(value)
    bump_version()
    return {"ok": True}


# ---------------------------------------------------------------- live monitor
# Samples every few seconds what could make streaming slow: the shared disk, the server's CPU,
# who is streaming and how (direct or converted), torrents, and background jobs in each app.
MON_EVERY = 10                       # seconds between history points while someone has the Live tab open
RAW_KEEP = 2 * 3600                  # keep every sample this long
MIN_KEEP = 30 * 86400                # keep per-minute summaries this long
MON_IDLE_EVERY = 60                  # ... and while nobody does (keeps the app's own footprint tiny)
MON_HISTORY_FILE = os.path.join(DATA, "monitor.json")
CLK = os.sysconf("SC_CLK_TCK")
NCPU = os.cpu_count() or 1
PAGE_SIZE = os.sysconf("SC_PAGE_SIZE")
UID = os.getuid()
HIST_FIELDS = ["t", "disk_busy", "disk_r", "disk_w", "my_r", "my_w", "io_psi", "load", "my_cpu",
               "streams", "transcodes", "stream_mbps", "qb_up", "qb_down", "net_out",
               "apps",    # {app: [cpu % of one core, disk MB/s]} for apps doing something
               "who",     # {viewer: Mbit/s} for streams playing
               "disk_ops"]  # disk operations per second (reads + writes)
HEAVY_ARR = {"RefreshSeries", "RefreshMovie", "RescanSeries", "RescanMovie", "DownloadedEpisodesScan",
             "DownloadedMoviesScan", "RenameFiles", "RenameMovie", "Backup", "MoveSeries", "MoveMovie",
             "SeriesSearch", "MoviesSearch", "MissingEpisodeSearch", "MissingMoviesSearch", "CutoffUnmetEpisodeSearch",
             "RefreshAuthor", "RescanFolders", "ManualImport", "ImportListSync"}


def _home_disk():
    try:
        st = os.stat(os.path.expanduser("~"))
        real = os.path.realpath("/sys/dev/block/%d:%d" % (os.major(st.st_dev), os.minor(st.st_dev)))
        name, parent = os.path.basename(real), os.path.basename(os.path.dirname(real))
        return parent if os.path.exists("/sys/block/" + parent) else name
    except Exception:
        return None


def _main_iface():
    best, best_tx = None, -1
    try:
        for line in open("/proc/net/dev").read().splitlines()[2:]:
            name, rest = line.split(":", 1)
            name = name.strip()
            tx = int(rest.split()[8])
            if name != "lo" and tx > best_tx:
                best, best_tx = name, tx
    except Exception:
        pass
    return best


def _read(path, default=""):
    try:
        with open(path) as f:
            return f.read()
    except Exception:
        return default


def classify(comm, args):
    a = args or comm
    if comm == "jellyfin" or "/usr/bin/jellyfin" in a:
        return "Jellyfin"
    if "jellyfin-ffmpeg" in a or (comm.startswith("ffmpeg") and "transcod" in a):
        return "Jellyfin converting video"
    if "Plex Transcoder" in a:
        return "Plex converting video"
    if "plexmediaserver" in a or comm.startswith("Plex") or "EasyAudioEncode" in comm:
        return "Plex"
    for key, label in (("qbittorrent", "qBittorrent"), ("Sonarr", "Sonarr"), ("Radarr", "Radarr"),
                       ("Readarr", "Readarr"), ("Prowlarr", "Prowlarr"), ("Lidarr", "Lidarr"), ("Bazarr", "Bazarr"),
                       ("unpackerr", "Unpackerr"), ("autobrr", "autobrr"), ("qui serve", "qui"),
                       ("filebrowser", "Filebrowser"), ("flaresolverr", "FlareSolverr"), ("rtorrent", "rTorrent"),
                       ("library.py", "This library page"), ("nginx", "nginx"), ("Maintainerr", "Maintainerr")):
        if key.lower() in a.lower():
            return label
    if "node" in comm or a.startswith("node"):
        if "dist-server" in a:
            return "Audiobookshelf"
        if "dist/index.js" in a:
            return "Seerr"
        return "Node app"
    if comm in ("bash", "sh", "sshd", "ps", "sleep", "tmux", "screen", "s6-supervise", "s6-svscan", "s6-linux-init-shutdownd",
                "s6-svlisten1", "s6-notifyoncheck", "sftp-server", "systemd", "(sd-pam)", "timeout", "curl"):
        return None
    return "Other"


class Monitor:
    def __init__(self):
        self.lock = threading.Lock()
        self.disk = CFG.get("monitor_disk") or _home_disk()
        self.iface = CFG.get("monitor_interface") or _main_iface()
        self.rotational = _read("/sys/block/%s/queue/rotational" % self.disk).strip() == "1" if self.disk else None
        self.link_mbps = None
        try:
            self.link_mbps = int(_read("/sys/class/net/%s/speed" % self.iface).strip() or 0) or None
        except Exception:
            pass
        self.prev = None                  # previous raw counters
        self.prev_procs = {}
        self.meta = {}                    # (pid, start time) -> name/label, or False for other users
        self.last_view = 0                # last time someone had the Live tab open
        self.snapshot = None
        self.slow = {"t": 0, "tasks": [], "arr": [], "qb_active": None}
        self.raw = []                     # every sample, last RAW_KEEP seconds
        self.minutes = []                 # one summary row per minute, last MIN_KEEP seconds
        self.acc = []                     # samples of the minute being collected
        self.last_save = time.time()
        saved = load_json(MON_HISTORY_FILE, {})
        now = time.time()
        if "minutes" in saved:
            self.raw = [r for r in saved.get("raw", []) if r and r[0] > now - RAW_KEEP]
            self.minutes = [r for r in saved["minutes"] if r and r[0] > now - MIN_KEEP]
        elif saved.get("rows"):                                   # older format: 10 s rows for 24 h
            rows = [r for r in saved["rows"] if r]
            self.raw = [r for r in rows if r[0] > now - RAW_KEEP]
            self.minutes = summarize(rows, 60)

    # -- raw readers
    def _counters(self):
        c = {"t": time.time(), "disk": None, "net_tx": None}
        for line in _read("/proc/diskstats").splitlines():
            f = line.split()
            if len(f) > 12 and f[2] == self.disk:
                # read bytes, write bytes, busy ms, read ops, write ops
                c["disk"] = (int(f[5]) * 512, int(f[9]) * 512, int(f[12]), int(f[3]), int(f[7]))
        for line in _read("/proc/net/dev").splitlines()[2:]:
            if line.split(":", 1)[0].strip() == self.iface:
                c["net_tx"] = int(line.split(":", 1)[1].split()[8])
        return c

    def _procs(self):
        out, known = {}, {}
        for pid in os.listdir("/proc"):
            if not pid.isdigit():
                continue
            try:
                stat = _read("/proc/%s/stat" % pid)
                if not stat:
                    continue
                f = stat[stat.rindex(")") + 2:].split()
                key = (pid, f[19])                                   # pid + start time survives pid reuse
                meta = self.meta.get(key)
                if meta is None:
                    if os.stat("/proc/" + pid).st_uid != UID:
                        meta = False
                    else:
                        comm = stat[stat.index("(") + 1:stat.rindex(")")]
                        args = _read("/proc/%s/cmdline" % pid).replace("\0", " ").strip()
                        meta = {"comm": comm, "args": args, "label": classify(comm, args)}
                known[key] = meta
                if not meta or not meta["label"]:
                    continue
                rss = int(f[21]) * PAGE_SIZE
                io = _read("/proc/%s/io" % pid)
                rb = wb = 0
                for line in io.splitlines():
                    if line.startswith("read_bytes:"):
                        rb = int(line[11:])
                    elif line.startswith("write_bytes:"):
                        wb = int(line[12:])
                out[pid] = {"comm": meta["comm"], "label": meta["label"], "ticks": int(f[11]) + int(f[12]),
                            "rss": rss, "rb": rb, "wb": wb,
                            "args": meta["args"] if "converting" in meta["label"] else None}
            except Exception:
                continue
        self.meta = known
        return out

    def _sessions(self):
        streams = []
        try:
            sessions = JF.get("/Sessions", activeWithinSeconds=120)
        except Exception as e:
            return None, http_error_text(e)
        for x in sessions:
            n = x.get("NowPlayingItem")
            if not n:
                continue
            ps = x.get("PlayState") or {}
            ti = x.get("TranscodingInfo") or {}
            method = ps.get("PlayMethod") or ("Transcode" if ti else "DirectPlay")
            vids = [s for s in n.get("MediaStreams") or [] if s.get("Type") == "Video"]
            auds = [s for s in n.get("MediaStreams") or [] if s.get("Type") == "Audio"]
            v = vids[0] if vids else {}
            a = next((s for s in auds if s.get("Index") == ps.get("AudioStreamIndex")), auds[0] if auds else {})
            src_bps = (v.get("BitRate") or 0) + (a.get("BitRate") or 0)
            converting = method == "Transcode" and ti and not (ti.get("IsVideoDirect") and ti.get("IsAudioDirect"))
            out_bps = ti.get("Bitrate") if converting and ti.get("Bitrate") else src_bps
            reasons = ti.get("TranscodeReasons") or []
            if isinstance(reasons, str):
                reasons = [r for r in reasons.split(",") if r]
            if n.get("Type") == "Episode":
                title = "%s — S%02dE%02d · %s" % (n.get("SeriesName", "?"), n.get("ParentIndexNumber") or 0,
                                                    n.get("IndexNumber") or 0, n.get("Name", ""))
                poster = n.get("SeriesId") or n.get("Id")
            else:
                title = "%s%s" % (n.get("Name", "?"), " (%s)" % n["ProductionYear"] if n.get("ProductionYear") else "")
                poster = n.get("Id")
            src_fps = v.get("RealFrameRate") or v.get("AverageFrameRate")
            streams.append({
                "user": x.get("UserName"), "client": x.get("Client"), "device": x.get("DeviceName"), "_path": n.get("Path"),
                "title": title, "poster": poster, "paused": bool(ps.get("IsPaused")),
                "progress": (ps.get("PositionTicks") or 0) / n["RunTimeTicks"] if n.get("RunTimeTicks") else None,
                "method": method, "converting": bool(converting),
                "src": {"res": res_of(v) if v else "?", "codec": codec_of(v) if v else "?", "hdr": hdr_of(v) if v else "",
                        "mbps": round(src_bps / 1e6, 1) if src_bps else None, "fps": src_fps,
                        "audio": (a.get("Codec") or "").upper(), "channels": a.get("Channels")},
                "out": {"mbps": round(out_bps / 1e6, 1) if out_bps else None,
                        "video": (ti.get("VideoCodec") or "").upper(), "audio": (ti.get("AudioCodec") or "").upper(),
                        "video_direct": ti.get("IsVideoDirect"), "audio_direct": ti.get("IsAudioDirect"),
                        "width": ti.get("Width"), "height": ti.get("Height"), "fps": ti.get("Framerate"),
                        "hw": ti.get("HardwareAccelerationType"), "done": ti.get("CompletionPercentage"),
                        "reasons": reasons} if ti else None,
            })
        return streams, None

    def _slow_things(self):
        """Background jobs: checked every 30 s, they change slowly."""
        if time.time() - self.slow["t"] < 30:
            return
        tasks, arr, qb_active = [], [], None
        try:
            for t in JF.get("/ScheduledTasks", isHidden="false"):
                if t.get("State") == "Running":
                    tasks.append({"name": t["Name"], "pct": round(t.get("CurrentProgressPercentage") or 0)})
        except Exception:
            pass
        for label, api in (("Sonarr", SONARR), ("Radarr", RADARR)):
            if not api:
                continue
            try:
                for c in api.req("GET", "/command"):
                    if c.get("status") in ("started", "queued"):
                        arr.append({"app": label, "name": c.get("commandName") or c.get("name"), "status": c["status"],
                                    "heavy": c.get("name") in HEAVY_ARR, "started": c.get("started") or c.get("queued")})
            except Exception:
                pass
        if QB:
            try:
                qb_active = len(QB.req("GET", "/torrents/info", filter="active"))
            except Exception:
                pass
        self.slow = {"t": time.time(), "tasks": tasks, "arr": arr, "qb_active": qb_active}

    # -- one sample
    def sample(self):
        with self.lock:
            c = self._counters()
            procs = self._procs()
            dt = (c["t"] - self.prev["t"]) if self.prev else None
            disk = {"busy": None, "r": None, "w": None}
            net_out = None
            if dt and dt > 0.5:
                if c["disk"] and self.prev["disk"]:
                    pd = self.prev["disk"]
                    ops_r = (c["disk"][3] - pd[3]) / dt if len(pd) > 3 else None
                    ops_w = (c["disk"][4] - pd[4]) / dt if len(pd) > 4 else None
                    rb, wb = (c["disk"][0] - pd[0]) / dt, (c["disk"][1] - pd[1]) / dt
                    ops = (ops_r or 0) + (ops_w or 0)
                    disk = {"busy": min(100.0, (c["disk"][2] - pd[2]) / (dt * 10)), "r": rb, "w": wb,
                            "ops": ops if ops_r is not None else None, "ops_r": ops_r, "ops_w": ops_w,
                            "avg_kb": (rb + wb) / ops / 1024 if ops else None}
                if c["net_tx"] is not None and self.prev["net_tx"] is not None:
                    net_out = (c["net_tx"] - self.prev["net_tx"]) * 8 / dt / 1e6
            groups, pcpu = {}, {}
            for pid, p in procs.items():
                label = p["label"]
                g = groups.setdefault(label, {"name": label, "count": 0, "cpu": 0.0, "rss": 0, "r": 0.0, "w": 0.0})
                g["count"] += 1
                g["rss"] += p["rss"]
                old = self.prev_procs.get(pid)
                if dt and old and old["comm"] == p["comm"]:
                    pcpu[pid] = max(0, p["ticks"] - old["ticks"]) / CLK / dt * 100
                    g["cpu"] += pcpu[pid]
                    g["r"] += max(0, p["rb"] - old["rb"]) / dt
                    g["w"] += max(0, p["wb"] - old["wb"]) / dt
            self.prev, self.prev_procs = c, procs
            loadavg = [float(x) for x in _read("/proc/loadavg", "0 0 0").split()[:3]]
            psi = {}
            for kind in ("io", "cpu", "memory"):
                for line in _read("/proc/pressure/" + kind).splitlines():
                    if line.startswith("some"):
                        psi[kind] = float(line.split("avg10=")[1].split()[0])
            mem = {}
            for line in _read("/proc/meminfo").splitlines():
                k, v = line.split(":", 1)
                if k in ("MemTotal", "MemAvailable"):
                    mem[k] = int(v.split()[0]) * 1024
            streams, jf_err = self._sessions()
            for st in streams or []:
                path = st.pop("_path", None)
                if st["converting"] and path:
                    st["cpu"] = r1(sum(pcpu.get(pid, 0) for pid, p in procs.items() if p.get("args") and path in p["args"]))
            qb = None
            if QB:
                try:
                    ti = QB.req("GET", "/transfer/info")
                    qb = {"up": ti.get("up_info_speed", 0), "down": ti.get("dl_info_speed", 0),
                          "up_limit": ti.get("up_rate_limit", 0), "down_limit": ti.get("dl_rate_limit", 0)}
                except Exception:
                    pass
            if time.time() - self.last_view < 120:      # background jobs only matter while someone looks
                self._slow_things()
            playing = [s for s in streams or [] if not s["paused"]]
            my_r = sum(g["r"] for g in groups.values())
            my_w = sum(g["w"] for g in groups.values())
            snap = {
                "t": c["t"], "interval": round(dt, 1) if dt else None, "disk_name": self.disk,
                "disk_rotational": self.rotational, "shared": SHARED_HOST,
                "disk": disk, "my_io": {"r": my_r, "w": my_w},
                "load": {"avg": loadavg, "cores": NCPU, "pct": round(loadavg[0] / NCPU * 100, 1)},
                "psi": psi, "mem": mem, "net": {"iface": self.iface, "out_mbps": net_out, "link_mbps": self.link_mbps},
                "streams": streams, "jellyfin_error": jf_err, "qbit": qb,
                "qb_active": self.slow["qb_active"], "tasks": self.slow["tasks"], "arr": self.slow["arr"],
                "library_updating": STATE["building"],
                "apps": sorted(groups.values(), key=lambda g: -(g["cpu"] + (g["r"] + g["w"]) / 1e6)),
            }
            snap["diagnosis"] = diagnose(snap)
            self.snapshot = snap
            if dt and dt >= 1.5:
                mb = 1024 * 1024
                self._add_point([
                    round(c["t"]), r1(disk["busy"]), r1((disk["r"] or 0) / mb), r1((disk["w"] or 0) / mb),
                    r1(my_r / mb), r1(my_w / mb), psi.get("io"), r1(loadavg[0] / NCPU * 100),
                    r1(sum(g["cpu"] for g in groups.values())), len(playing),
                    sum(1 for s in playing if s["converting"]),
                    r1(sum(s["out"]["mbps"] if s["converting"] and s["out"] and s["out"]["mbps"] else (s["src"]["mbps"] or 0)
                           for s in playing)),
                    r1((qb or {}).get("up", 0) / mb), r1((qb or {}).get("down", 0) / mb), r1(net_out),
                    {g["name"]: [r1(g["cpu"]), round((g["r"] + g["w"]) / mb, 2)] for g in groups.values()
                     if g["cpu"] >= 0.5 or g["r"] + g["w"] >= 0.05 * mb},
                    who_map(playing), r1(disk.get("ops"))])
            return snap

    def _add_point(self, row):
        self.raw.append(row)
        if self.acc and self.acc[0][0] // 60 != row[0] // 60:
            self.minutes.extend(summarize(self.acc, 60))
            self.acc = []
        self.acc.append(row)
        now = time.time()
        while self.raw and self.raw[0][0] < now - RAW_KEEP:
            self.raw.pop(0)
        while self.minutes and self.minutes[0][0] < now - MIN_KEEP:
            self.minutes.pop(0)
        if now - self.last_save > 300:
            try:
                save_json(MON_HISTORY_FILE, {"fields": HIST_FIELDS, "raw": self.raw, "minutes": self.minutes})
            except Exception as e:
                log.warning("could not save monitor history: %s", e)
            self.last_save = now

    def fresh(self, max_age=2.5):
        s = self.snapshot
        if not s or time.time() - s["t"] > max_age:
            s = self.sample()
        return s

    def points(self, since=0):
        """Every sample since a time (at most the last two hours)."""
        with self.lock:
            return {"fields": HIST_FIELDS, "rows": [r for r in self.raw if r[0] > since]}

    def history(self, span):
        """Rows covering the last `span` seconds, at most ~1500 points whatever the span."""
        now = time.time()
        with self.lock:
            if span <= RAW_KEEP:
                rows = [r for r in self.raw if r[0] >= now - span]
                res = max(1, span // 1500)
            else:
                rows = [r for r in self.minutes + summarize(self.acc, 60) if r[0] >= now - span]
                res = max(60, (span // 1500) // 60 * 60)
        if span > RAW_KEEP and res > 60:
            rows = summarize(rows, res)
        elif span <= RAW_KEEP and len(rows) > 1500:
            rows = summarize(rows, res)
        return {"fields": HIST_FIELDS, "rows": rows, "res": res}


def r1(x):
    return None if x is None else round(x, 1)


def who_map(playing):
    out = {}
    for st in playing:
        name = shortname(st["user"])
        mbps = (st["out"]["mbps"] if st["converting"] and st["out"] and st["out"]["mbps"] else st["src"]["mbps"]) or 0
        out[name] = r1(out.get(name, 0) + mbps)
    return out


def _avg_maps(maps, n):
    """Average {key: number | [numbers]} dicts over n samples (a missing key counts as 0)."""
    tot = {}
    for m in maps:
        for k, v in (m or {}).items():
            if isinstance(v, list):
                acc = tot.setdefault(k, [0.0] * len(v))
                for j, x in enumerate(v):
                    acc[j] += x or 0
            else:
                tot[k] = tot.get(k, 0) + (v or 0)
    out = {}
    for k, v in tot.items():
        if isinstance(v, list):
            vals = [round(x / n, 2) for x in v]
            if any(x >= 0.05 for x in vals):
                out[k] = vals
        elif v / n >= 0.05:
            out[k] = round(v / n, 1)
    return out


def summarize(rows, seconds):
    """Group rows into time buckets: averages, except counts (streams, conversions) keep their peak."""
    buckets = {}
    for r in rows:
        buckets.setdefault(r[0] // seconds, []).append(r)
    out = []
    for k in sorted(buckets):
        b = buckets[k]
        row = [b[-1][0]]
        for i in range(1, len(HIST_FIELDS)):
            if HIST_FIELDS[i] in ("apps", "who"):
                row.append(_avg_maps([x[i] if len(x) > i else None for x in b], len(b)))
                continue
            vals = [x[i] for x in b if len(x) > i and x[i] is not None]
            if not vals:
                row.append(None)
            elif HIST_FIELDS[i] in ("streams", "transcodes"):
                row.append(max(vals))
            else:
                row.append(r1(sum(vals) / len(vals)))
        out.append(row)
    return out


def diagnose(s):
    """Plain-language list of what is most likely slowing things down right now."""
    out = []
    mb = 1024 * 1024
    d = s["disk"]
    if d["busy"] is not None:
        total = (d["r"] or 0) + (d["w"] or 0)
        mine = s["my_io"]["r"] + s["my_io"]["w"]
        share = min(100, round(mine / total * 100)) if total > mb else None
        top = max(s["apps"], key=lambda g: g["r"] + g["w"], default=None)
        who = ""
        if share is not None:
            if share < 30:
                who = " Your apps cause only about %d%% of that traffic, so it is mostly %s." % (
                    share, "other customers on this shared server" if SHARED_HOST else "other programs on this server")
            elif top and top["r"] + top["w"] > mb:
                who = " Your apps cause about %d%% of it, mostly %s (%.0f MB/s)." % (share, top["name"], (top["r"] + top["w"]) / mb)
        if d["busy"] >= 60 and total < 20 * mb:
            who += (" It is moving only %.0f MB/s, which means many small scattered reads at once:"
                    " the slowest kind of work for a spinning hard drive." % (total / mb))
        if d["busy"] >= 85:
            out.append({"level": "critical", "title": "The disk is nearly maxed out (%d%% busy)" % d["busy"],
                        "detail": "Everything that reads files has to wait: starting videos, seeking, posters, library scans." + who})
        elif d["busy"] >= 60:
            out.append({"level": "warning", "title": "The disk is busy (%d%%)" % d["busy"],
                        "detail": "Starting or seeking in videos may be slow." + who})
    if s["psi"].get("io", 0) >= 25:
        out.append({"level": "warning", "title": "Programs are waiting on the disk %d%% of the time" % s["psi"]["io"],
                    "detail": "This is measured for the whole shared server."})
    for st in s["streams"] or []:
        if st["paused"] or not st["converting"]:
            continue
        o = st["out"] or {}
        reasons = ", ".join(REASONS.get(r, r) for r in o.get("reasons") or []) or "the player asked for it"
        slow = o.get("fps") and st["src"].get("fps") and o["fps"] < st["src"]["fps"] * 0.95
        out.append({"level": "critical" if slow else "warning",
                    "title": "%s's video is being converted%s" % (shortname(st["user"]), " and can't keep up" if slow else ""),
                    "detail": "%s. Converting video is heavy work for the server. Reason: %s." % (st["title"], reasons)})
    playing = [x for x in s["streams"] or [] if not x["paused"]]
    mbps = sum(x["src"]["mbps"] or 0 for x in playing)
    if len(playing) >= 3 or mbps >= 120:
        out.append({"level": "warning", "title": "%d people are streaming (%.0f Mbit/s together)" % (len(playing), mbps),
                    "detail": "Several streams at once share the disk and the connection."})
    qb = s["qbit"] or {}
    if qb.get("up", 0) >= 15 * mb:
        out.append({"level": "warning", "title": "qBittorrent is uploading at %.0f MB/s" % (qb["up"] / mb),
                    "detail": "Seeding reads from the same disk as your videos. A seeding speed limit in qBittorrent would help."})
    if qb.get("down", 0) >= 15 * mb:
        out.append({"level": "info", "title": "qBittorrent is downloading at %.0f MB/s" % (qb["down"] / mb),
                    "detail": "Downloads write to the same disk your videos are read from."})
    for t in s["tasks"]:
        out.append({"level": "warning", "title": "Jellyfin is running “%s” (%d%%)" % (t["name"], t["pct"]),
                    "detail": "Background jobs like scans, trickplay and intro detection read lots of files."})
    for c in s["arr"]:
        if c["heavy"] and c["status"] == "started":
            out.append({"level": "info", "title": "%s is busy: %s" % (c["app"], c["name"]),
                        "detail": "This reads or moves files on the disk."})
    if s["library_updating"]:
        out.append({"level": "info", "title": "This page is re-reading the library",
                    "detail": "It measures every file once, which adds some disk load for about a minute."})
    for g in s["apps"]:
        if g["cpu"] >= 150 and "converting" not in g["name"]:
            out.append({"level": "warning", "title": "%s is using a lot of CPU (%.0f%%)" % (g["name"], g["cpu"]),
                        "detail": "100% equals one full processor core."})
    if s["load"]["pct"] >= 80:
        out.append({"level": "warning", "title": "The %sserver's processors are busy (%d%%)" % (
            "shared " if SHARED_HOST else "", s["load"]["pct"]),
                    "detail": "Other customers are using most of the CPU." if SHARED_HOST else
                              "Programs outside this page's list are using most of the CPU."})
    if not out:
        out.append({"level": "good", "title": "Nothing unusual right now",
                    "detail": "If it is slow anyway, check the charts below for the moment it happened."})
    order = {"critical": 0, "warning": 1, "info": 2, "good": 3}
    return sorted(out, key=lambda x: order[x["level"]])


REASONS = {
    "ContainerNotSupported": "the player can't open this file type",
    "VideoCodecNotSupported": "the player can't play this video format",
    "AudioCodecNotSupported": "the player can't play this audio format",
    "SubtitleCodecNotSupported": "subtitles have to be burned into the picture",
    "AudioIsExternal": "the audio track is a separate file",
    "SecondaryAudioNotSupported": "the chosen audio track isn't supported",
    "VideoProfileNotSupported": "the player doesn't support this video profile",
    "VideoLevelNotSupported": "the player doesn't support this video level",
    "VideoResolutionNotSupported": "the resolution is too high for the player",
    "VideoBitDepthNotSupported": "the player can't play 10-bit video",
    "VideoFramerateNotSupported": "the frame rate isn't supported",
    "RefFramesNotSupported": "the video's reference frames aren't supported",
    "AnamorphicVideoNotSupported": "anamorphic video isn't supported",
    "InterlacedVideoNotSupported": "interlaced video isn't supported",
    "AudioChannelsNotSupported": "too many audio channels for the player",
    "AudioProfileNotSupported": "the audio profile isn't supported",
    "AudioSampleRateNotSupported": "the audio sample rate isn't supported",
    "AudioBitDepthNotSupported": "the audio bit depth isn't supported",
    "ContainerBitrateExceedsLimit": "the video is above the player's quality/bandwidth limit",
    "VideoBitrateNotSupported": "the video bitrate is above the player's limit",
    "AudioBitrateNotSupported": "the audio bitrate is above the player's limit",
    "UnknownVideoStreamInfo": "the video details are unknown",
    "UnknownAudioStreamInfo": "the audio details are unknown",
    "DirectPlayError": "direct play failed",
    "VideoRangeTypeNotSupported": "the player can't show HDR / Dolby Vision",
    "VideoCodecTagNotSupported": "the video codec tag isn't supported",
    "StreamCountExceedsLimit": "too many streams in the file",
}


def shortname(n):
    n = str(n or "?")
    return n.split("@")[0] if "@" in n else n


MONITOR = Monitor()


def monitor_loop():
    while True:
        try:
            MONITOR.sample()
        except Exception:
            log.exception("monitor sample failed")
        watched = time.time() - MONITOR.last_view < 120
        time.sleep(MON_EVERY if watched else MON_IDLE_EVERY)


# ---------------------------------------------------------------- login
# The page has its own sign-in form; passwords are checked against the same htpasswd file the
# web area uses, by the `htpasswd -v` tool itself, so no password or hash is ever handled here.
HTPASSWD = os.path.expanduser(CFG.get("htpasswd") or os.path.join(DATA, "htpasswd"))
BASE_PATH = (CFG.get("base_path") or "/controllarr").rstrip("/") or "/"
COOKIE_SECURE = bool(CFG.get("cookie_secure", True))   # set false only for plain-http testing
SESSIONS_FILE = os.path.join(DATA, "sessions.json")
SESSION_DAYS = int(CFG.get("session_days", 30))
COOKIE = "library_session"
SESS_LOCK = threading.Lock()
SESSIONS = load_json(SESSIONS_FILE, {})          # sha256(token) -> {"user", "exp"}
FAILS = {}                                        # client -> [failure times]
FAIL_WINDOW, FAIL_MAX_IP, FAIL_MAX_ALL = 15 * 60, 8, 40


def check_password(user, password):
    if not re.fullmatch(r"[A-Za-z0-9._@-]{1,64}", user or "") or not password \
            or len(password) > 256 or "\n" in password or "\r" in password:
        return False
    try:
        r = subprocess.run(["htpasswd", "-v", "-i", HTPASSWD, user], input=password,
                           capture_output=True, text=True, timeout=10)
        return r.returncode == 0
    except Exception as e:
        log.warning("password check failed to run: %s", e)
        return False


def _tok_key(token):
    return hashlib.sha256(token.encode()).hexdigest()


def session_create(user):
    token = secrets.token_urlsafe(32)
    with SESS_LOCK:
        now = time.time()
        for k in [k for k, v in SESSIONS.items() if v["exp"] < now]:
            SESSIONS.pop(k)
        SESSIONS[_tok_key(token)] = {"user": user, "exp": now + SESSION_DAYS * 86400}
        save_json(SESSIONS_FILE, SESSIONS)
    os.chmod(SESSIONS_FILE, 0o600)
    return token


def session_user(token):
    if not token:
        return None
    with SESS_LOCK:
        s_ = SESSIONS.get(_tok_key(token))
        if not s_ or s_["exp"] < time.time():
            return None
        return s_["user"]


def session_drop(token):
    if token:
        with SESS_LOCK:
            if SESSIONS.pop(_tok_key(token), None):
                save_json(SESSIONS_FILE, SESSIONS)


def too_many_failures(client):
    now = time.time()
    for k in list(FAILS):
        FAILS[k] = [t for t in FAILS[k] if now - t < FAIL_WINDOW]
        if not FAILS[k]:
            FAILS.pop(k)
    return len(FAILS.get(client, [])) >= FAIL_MAX_IP or sum(len(v) for v in FAILS.values()) >= FAIL_MAX_ALL


# ---------------------------------------------------------------- web
def _page(name):
    html = open(os.path.join(HERE, name), encoding="utf-8").read()
    return html.replace("{{TITLE}}", TITLE.replace("&", "&amp;").replace("<", "&lt;")).encode()


PAGE = _page("index.html")
LOGIN_PAGE = _page("login.html")


MUTATING = {"/api/delete", "/api/keep", "/api/restore", "/api/downloads/delete"}


class Handler(BaseHTTPRequestHandler):
    server_version = "Library/2"

    def log_message(self, *a):
        pass

    def send(self, code, body, ctype="application/json", headers=()):
        if not isinstance(body, bytes):
            body = json.dumps(body, default=list).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "same-origin")
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def token(self):
        c = http.cookies.SimpleCookie()
        try:
            c.load(self.headers.get("Cookie") or "")
        except http.cookies.CookieError:
            return None
        return c[COOKIE].value if COOKIE in c else None

    def client(self):
        return (self.headers.get("X-Forwarded-For") or self.client_address[0]).split(",")[0].strip()

    def cookie(self, value, max_age):
        return ("Set-Cookie", "%s=%s; Path=%s; Max-Age=%d; HttpOnly;%s SameSite=Lax"
                % (COOKIE, value, BASE_PATH, max_age, " Secure;" if COOKIE_SECURE else ""))

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        user = session_user(self.token())
        if path == "/login":
            if user:
                return self.send(302, b"", "text/plain", [("Location", "./")])
            return self.send(200, LOGIN_PAGE, "text/html; charset=utf-8")
        if not user:
            if path.startswith("/api/"):
                return self.send(401, {"error": "Please sign in again", "login": True})
            return self.send(302, b"", "text/plain", [("Location", "login")])
        if path == "/api/live":
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            since = float((q.get("since") or ["0"])[0] or 0)
            MONITOR.last_view = time.time()
            snap = MONITOR.fresh()
            return self.send(200, {"now": snap, "points": MONITOR.points(since)})
        if path == "/api/detail/qbit":
            out = []
            try:
                for t in QB.req("GET", "/torrents/info", filter="active"):
                    out.append({"name": t["name"], "up": t.get("upspeed", 0), "down": t.get("dlspeed", 0),
                                "ratio": round(t.get("ratio", 0), 2), "state": t.get("state", ""),
                                "progress": round(t.get("progress", 0), 3), "peers": t.get("num_leechs", 0),
                                "category": t.get("category", "")})
            except Exception as e:
                return self.send(502, {"error": http_error_text(e)})
            return self.send(200, sorted(out, key=lambda x: -(x["up"] + x["down"])))
        if path == "/api/serverhistory":
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            span = max(60, min(MIN_KEEP, int(float((q.get("range") or ["3600"])[0] or 3600))))
            return self.send(200, MONITOR.history(span))
        if path == "/api/me":
            return self.send(200, {"user": user})
        if path in ("/", "/index.html"):
            return self.send(200, PAGE, "text/html; charset=utf-8")
        if path == "/api/library":
            have = (urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("v") or [""])[0]
            with STATE_LOCK:
                same = bool(have) and have == STATE["version"] and STATE["data"] is not None
                body = {"building": STATE["building"], "error": STATE["error"], "version": STATE["version"],
                        "same": same, "build": STATE["build"].view() if STATE["build"] else None,
                        "data": None if same or not STATE["data"] else public(STATE["data"])}
            return self.send(200, body)
        if path == "/api/build":
            b = STATE["build"]
            return self.send(200, {"building": STATE["building"], "error": STATE["error"],
                                   "build": b.view() if b else None})
        if path == "/api/job":
            job = JOBS.get((urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("id") or [""])[0])
            return self.send(200, job.view()) if job else self.send(404, {"error": "job not found"})
        if path == "/api/history":
            with STORE_LOCK:
                return self.send(200, list(reversed(load_json(HISTORY_FILE, []))))
        self.send(404, {"error": "not found"})

    def do_POST(self):
        # Custom header blocks cross-site form posts riding on the browser's session cookie.
        if self.headers.get("X-Library") != "1":
            return self.send(403, {"error": "forbidden"})
        path = urllib.parse.urlparse(self.path).path
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n > 1_000_000:
                return self.send(413, {"error": "too large"})
            req = json.loads(self.rfile.read(n) or b"{}")
            if path == "/api/login":
                client = self.client()
                if too_many_failures(client):
                    return self.send(429, {"error": "Too many wrong attempts. Wait 15 minutes and try again."})
                user = str(req.get("user") or "").strip()
                if not check_password(user, str(req.get("password") or "")):
                    FAILS.setdefault(client, []).append(time.time())
                    log.warning("failed sign-in for %r from %s", user[:64], client)
                    time.sleep(1)
                    return self.send(401, {"error": "Wrong username or password"})
                FAILS.pop(client, None)
                log.info("signed in: %s from %s", user, client)
                return self.send(200, {"ok": True}, headers=[self.cookie(session_create(user), SESSION_DAYS * 86400)])
            if path == "/api/logout":
                session_drop(self.token())
                return self.send(200, {"ok": True}, headers=[self.cookie("", 0)])
            if not session_user(self.token()):
                return self.send(401, {"error": "Please sign in again", "login": True})
            if path == "/api/refresh":
                rebuild_now()
                return self.send(200, {"ok": True})
            if path in MUTATING and STATE["building"]:
                return self.send(409, {"error": "The library is updating. Try again when it finishes.", "updating": True})
            if path == "/api/plan":
                return self.send(200, make_plan(req["kind"], req["id"], req.get("mode", "delete"), req.get("profile")))
            if path == "/api/delete":
                return self.send(200, execute(req["kind"], req["id"], req.get("mode", "delete"), req.get("profile")))
            if path == "/api/keep":
                return self.send(200, set_keep(req["kind"], req["id"], bool(req.get("keep"))))
            if path == "/api/restore":
                return self.send(200, restore(req["uid"]))
            if path == "/api/downloads/delete":
                return self.send(200, delete_downloads(req.get("hashes") or [], req.get("paths") or []))
            self.send(404, {"error": "not found"})
        except KeyError as e:
            self.send(404, {"error": str(e).strip("'\"")})
        except ValueError as e:
            self.send(400, {"error": str(e)})
        except Exception as e:
            log.exception("request failed")
            self.send(500, {"error": http_error_text(e)})


def main():
    STATE["data"] = load_json(CACHE_FILE, None)
    bump_version()
    if STATE["data"] and "downloads" not in STATE["data"]:
        STATE["data"] = None       # cache from an older version
    threading.Thread(target=rebuild_loop, daemon=True).start()
    if CFG.get("monitor", True):
        threading.Thread(target=monitor_loop, daemon=True).start()
    if not os.path.exists(HTPASSWD):
        log.warning("password file %s does not exist yet: nobody can sign in until you create it "
                    "(see README: htpasswd -c)", HTPASSWD)
    port = int(CFG.get("port", 8790))
    host = CFG.get("listen", "127.0.0.1")
    log.info("%s listening on %s:%d (path %s)", TITLE, host, port, BASE_PATH)
    ThreadingHTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
    main()
