# Controllarr

**One page to see, clean up and keep an eye on a Jellyfin + \*arr media server.**

Controllarr lists everything in your Jellyfin library with its real size on disk, who watched it, who
requested it and how heavy the file is, and lets you delete or shrink things safely: through Radarr and
Sonarr when they manage the title, together with the matching torrents and leftover download copies, so
the space really comes back. A **Live** tab shows who is streaming right now and what is loading your
server, with 30 days of history to find out why things were slow.

It is a single small Python program (standard library only, no database, no Docker needed) that runs
next to your apps and stays out of the way: about 0.05% of one CPU core when nobody is looking.

---

## What you get

**Movies and TV Shows**
- Every title with poster, size on disk, quality (resolution, codec, GB per hour of video) and how much
  space deleting it actually frees.
- Who watched it (per Jellyfin user, finished or partly), who requested it in Seerr / Jellyseerr / Overseerr.
- Filters and sorting: watched, unwatched for 6+ months, kept, requested, largest, heaviest per hour…
- List or poster view; seasons expand inside shows.
- **Delete** with a preview of every step, then a live progress list while it runs.
- **Replace with a smaller copy**: switch the quality profile in Radarr/Sonarr and download again.
- **Delete watched episodes**: episodes everyone watching the show has finished.
- **Keep**: protect titles from cleanup (Jellyfin favourites count too).

**Downloads**
- Every torrent in qBittorrent, whether it is still part of your library, ratio and seeding time.
- Leftover copies: files in the download folders no torrent uses any more, the classic "why is my disk
  full" culprit. Suspicious files (programs or archives posing as videos) are flagged.

**History**
- Everything deleted, with **Get it back** (asks Radarr/Sonarr to download it again).

**Live**
- A steady server-load bar with the main cause, and a popup that explains it.
- Who is watching, on what, whether the video is played directly or converted (and why), and how heavy
  each stream is compared with the others.
- Disk, waiting-on-disk, streaming, qBittorrent and CPU tiles. Click any of them for what is behind the
  number right now and its history, with a breakdown by app and viewer at every moment.
- On a shared seedbox it separates *your* disk use from other customers', so you can tell whether a
  slowdown is yours to fix.

## How deleting works (and why it is safe)

Nothing is removed without a preview that lists every step. For a movie or show managed by Radarr or
Sonarr, Controllarr asks them to remove it with its files, so it won't be downloaded again. Titles that
were added by hand are deleted from disk, and only from inside your Jellyfin library folders. Matching
torrents are removed from qBittorrent (with their downloaded data, unless that data *is* the library copy),
leftover copies in the download folders are deleted, and the Seerr request is cleared so it can be asked
for again. Every deletion is written to `deletions.log` and to the History tab.

Space is only freed when no other name of a file remains. Controllarr follows **hardlinks**, which is how
Radarr/Sonarr normally import downloads (see the [TRaSH guides on hardlinks](https://trash-guides.info/File-and-Folder-Structure/Hardlinks-and-Instant-Moves/)),
and shows how much each deletion really frees.

## Requirements

- Linux with **Python 3.9+** (nothing else to install with pip)
- **Jellyfin** (required); optional: **Radarr**, **Sonarr**, **Seerr / Jellyseerr / Overseerr**, **qBittorrent**
- The **`htpasswd`** tool for the login (`apache2-utils` on Debian/Ubuntu, `httpd-tools` on Fedora)
- Controllarr must see the same files as your apps, at the same paths (it reads sizes and hardlinks itself)
- A reverse proxy with HTTPS in front of it (nginx, Caddy, Traefik…). It listens on localhost only.

---

## Install on an Ultra.cc seedbox (or a similar slot)

```bash
git clone https://github.com/pulsar-a/controllarr.git ~/.apps/controllarr
cd ~/.apps/controllarr
bash install.sh
```

The installer finds Radarr, Sonarr, Seerr, Jellyfin and qBittorrent with `app-ports`, reads their API
keys from their own config files, picks a free port, installs a user service and adds the route
`/controllarr/` to your slot's nginx. It then prints what is left:

1. **Jellyfin API key**: Jellyfin → Dashboard → API Keys → **+**, and put it in `config.json` → `jellyfin.api_key`.
2. **qBittorrent password** in `config.json` → `qbittorrent.password` (only needed to remove torrents).
3. **Login**: by default the same login as your slot's web area (`~/www/.htpasswd`).
4. `systemctl --user restart controllarr`, then open `https://<you>.<server>.usbx.me/controllarr/`.

## Install anywhere else

```bash
git clone https://github.com/pulsar-a/controllarr.git ~/controllarr
cd ~/controllarr
bash install.sh --no-nginx            # creates config.json and a systemd user service
nano config.json                      # fill in URLs and API keys (see the table below)
htpasswd -c -B htpasswd yourname      # create your login (add more people without -c)
systemctl --user restart controllarr
```

No systemd? Run it any way you like: `python3 library.py` (it stays in the foreground).

Then point your reverse proxy at it. Controllarr does its own sign-in, so **don't** add basic auth in front.

**nginx**
```nginx
location = /controllarr { return 301 /controllarr/; }
location /controllarr/ {
    proxy_pass         http://127.0.0.1:8790/;
    proxy_set_header   Host $host;
    proxy_set_header   X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_read_timeout 600;
}
```

**Caddy**
```caddy
media.example.com {
    redir /controllarr /controllarr/
    handle_path /controllarr/* {
        reverse_proxy 127.0.0.1:8790
    }
}
```

The `base_path` in `config.json` must match the path you publish it under (it is used for the login cookie).
Posters are loaded from `/jellyfin/…` on the same site, so Jellyfin should be reachable at `/jellyfin` on
the same domain (as on Ultra.cc); otherwise the page shows letters instead of posters.

---

## Configuration

Everything lives in `config.json` (created from [`config.example.json`](config.example.json); it is
git-ignored because it holds your keys). Empty values mean "default" or "not connected".

| Setting | Default | What it does |
|---|---|---|
| `title` | `Controllarr` | Name shown in the header and browser tab |
| `listen` / `port` | `127.0.0.1` / `8790` | Where it listens (keep it on localhost behind your proxy) |
| `base_path` | `/controllarr` | URL path you publish it under; also the login cookie's path |
| `htpasswd` | `htpasswd` next to the app | Password file (Apache format) used for sign-in |
| `cookie_secure` | `true` | Login cookie only over HTTPS. Set `false` only for plain-http testing |
| `session_days` | `30` | How long a sign-in lasts per browser |
| `data_dir` | the app folder | Where state and logs are written |
| `downloads_root` | `~/downloads` | Download folder; leftover copies are only looked for and deleted inside it |
| `rebuild_minutes` | `30` | How often the library is re-read (also after deletes, and with the Refresh button) |
| `stale_days` | `182` | "Unwatched for N days" marker |
| `shared_host` | `true` | `true` on a shared seedbox (talks about "other customers"), `false` on your own server |
| `monitor` | `true` | Run the Live tab's monitor |
| `monitor_disk` | auto | Disk to watch, e.g. `sda` (auto = the disk holding your home folder) |
| `monitor_interface` | auto | Network interface to watch |
| `jellyfin.url` / `api_key` | — | **Required.** Include the base path if Jellyfin has one, e.g. `http://127.0.0.1:8096/jellyfin` |
| `radarr` / `sonarr` | — | `url` (with URL base, e.g. `/radarr`) and `api_key` (Settings → General) |
| `seerr` | — | Seerr, Jellyseerr or Overseerr: `url` and `api_key` (Settings → General) |
| `qbittorrent` | — | `url`, `username`, `password` of the Web UI |

Leave an app's `api_key` empty (or remove the block) to run without it; the related features hide or
say what is missing.

## Updating

```bash
cd ~/.apps/controllarr        # or wherever you cloned it
git pull
systemctl --user restart controllarr
```

Your `config.json` and history are untouched by updates.

## Uninstall

```bash
systemctl --user disable --now controllarr
rm ~/.config/systemd/user/controllarr.service
rm ~/.apps/nginx/proxy.d/controllarr.conf && app-nginx restart     # Ultra.cc only
rm -rf ~/.apps/controllarr
```

Nothing in your media apps is changed by uninstalling.

---

## Security

- The page has its own sign-in form. Passwords are checked with the `htpasswd` tool against your
  password file; Controllarr never stores or reads passwords itself.
- Sessions are random tokens in an HttpOnly, Secure, SameSite cookie scoped to `base_path`; only a hash is
  kept on disk. Repeated wrong passwords lock sign-in for 15 minutes.
- Every action that changes something needs a custom header, so other sites can't trigger deletes.
- Deletes are limited to your Jellyfin library folders and `downloads_root`, after a preview.
- `config.json` contains API keys and passwords: it is created with `chmod 600` and is git-ignored.
- It listens on localhost; put HTTPS in front of it.

## Troubleshooting

| Symptom | What to check |
|---|---|
| Sign-in always fails | Does the `htpasswd` file exist and does the `htpasswd` tool work? See `controllarr.log`. |
| Logged out right after signing in | `base_path` must match the URL path; `cookie_secure` needs HTTPS. |
| "qBittorrent not connected" | URL, username and password in `config.json`; "Bypass authentication for localhost" also works. |
| Deleting doesn't free space | Another copy still exists. Open the Downloads tab: leftover copies show there. |
| Posters are letters | Jellyfin isn't reachable at `/jellyfin` on the same domain. |
| Live tab numbers look wrong | Set `monitor_disk` / `monitor_interface` explicitly. |

Logs: `controllarr.log` (app) and `deletions.log` (what was deleted), in `data_dir`.

## How light is it?

Measured on a busy shared seedbox: about **0.05% of one CPU core** while nobody has the Live tab open
(it samples once a minute), about 1% while you watch the Live tab (every 3 seconds), about 1 CPU-second
per library re-read, and around 100 MB of memory. Re-loading the page when nothing changed costs about 1 KB.

## License

[MIT](LICENSE)
