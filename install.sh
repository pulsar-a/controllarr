#!/usr/bin/env bash
# Controllarr installer.
#
#   bash install.sh                 create config.json (if missing), install the service and the web route
#   bash install.sh --no-service    don't create/restart the systemd user service
#   bash install.sh --no-nginx      don't touch nginx (configure your reverse proxy yourself)
#   bash install.sh --config-only   only create config.json
#
# Safe to run again: an existing config.json is never overwritten.
set -euo pipefail
cd "$(dirname "$0")"
DIR="$(pwd)"
SERVICE=1; NGINX=1
for a in "$@"; do
  case "$a" in
    --no-service) SERVICE=0 ;;
    --no-nginx) NGINX=0 ;;
    --config-only) SERVICE=0; NGINX=0 ;;
    -h|--help) sed -n '2,9p' "$0"; exit 0 ;;
    *) echo "unknown option: $a" >&2; exit 2 ;;
  esac
done

say()  { printf '\033[1m%s\033[0m\n' "$*"; }
warn() { printf '\033[33m! %s\033[0m\n' "$*"; }

# ---------------------------------------------------------------- requirements
command -v python3 >/dev/null || { echo "python3 is required" >&2; exit 1; }
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' || { echo "python 3.9 or newer is required" >&2; exit 1; }
command -v htpasswd >/dev/null || warn "the 'htpasswd' tool is missing (package apache2-utils / httpd-tools); sign-in needs it"

# ---------------------------------------------------------------- config.json
if [ -f config.json ]; then
  say "config.json exists, leaving it as it is"
else
  say "Creating config.json"
  # Fill in what can be discovered on this machine; everything else stays at the example defaults.
  python3 - <<'PY'
import json, os, re, shutil, subprocess

home = os.path.expanduser("~")
cfg = json.load(open("config.example.json"))
found = []

def first(*paths):
    for p in paths:
        p = os.path.expanduser(p)
        if os.path.exists(p):
            return p
    return None

def ultra_port(app):
    """Ultra.cc and similar seedboxes list each app's port with `app-ports show`."""
    if not shutil.which("app-ports"):
        return None
    try:
        out = subprocess.run(["app-ports", "show"], capture_output=True, text=True, timeout=20).stdout
    except Exception:
        return None
    for line in out.splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2 and parts[0].isdigit() and parts[1].strip().lower() == app.lower():
            return int(parts[0])
    return None

def arr(name, default_port):
    xml = first(f"~/.apps/{name}/config.xml", f"~/.config/{name.capitalize()}/config.xml", f"/var/lib/{name}/config.xml")
    if not xml:
        return
    text = open(xml).read()
    get = lambda tag: (re.search(f"<{tag}>([^<]*)</{tag}>", text) or [None, ""])[1]
    port = ultra_port(name.capitalize()) or int(get("Port") or default_port)
    cfg[name] = {"url": f"http://127.0.0.1:{port}{get('UrlBase')}", "api_key": get("ApiKey")}
    found.append(name.capitalize())

arr("radarr", 7878)
arr("sonarr", 8989)

for app in ("seerr", "jellyseerr", "overseerr"):
    st = first(f"~/.apps/{app}/settings.json", f"~/.config/{app}/settings.json")
    if st:
        key = json.load(open(st)).get("main", {}).get("apiKey", "")
        port = ultra_port({"seerr": "Seerr", "jellyseerr": "Jellyseerr", "overseerr": "Overseerr"}[app]) or 5055
        cfg["seerr"] = {"url": f"http://127.0.0.1:{port}", "api_key": key}
        found.append(app.capitalize())
        break

jf_port = ultra_port("Jellyfin")
if jf_port:
    # on Ultra.cc Jellyfin answers under /jellyfin
    cfg["jellyfin"]["url"] = f"http://127.0.0.1:{jf_port}/jellyfin"
qb_port = ultra_port("qBittorrent")
if qb_port:
    cfg["qbittorrent"]["url"] = f"http://127.0.0.1:{qb_port}"
    cfg["qbittorrent"]["username"] = os.environ.get("USER", "admin")

if shutil.which("app-ports"):                                   # Ultra.cc-style seedbox
    try:
        free = subprocess.run(["app-ports", "free"], capture_output=True, text=True, timeout=20).stdout
        import socket
        for cand in re.findall(r"\b(\d{4,5})\b", free):          # first listed port nothing listens on yet
            with socket.socket() as sock:
                try:
                    sock.bind(("127.0.0.1", int(cand)))
                except OSError:
                    continue
            cfg["port"] = int(cand)
            break
    except Exception:
        pass
    if os.path.exists(os.path.join(home, "www/.htpasswd")):
        cfg["htpasswd"] = "~/www/.htpasswd"                       # same login as the slot's web area
    cfg["shared_host"] = True

old = os.umask(0o077)
with open("config.json", "w") as f:
    json.dump(cfg, f, indent=2)
os.umask(old)
print("  found and connected: " + (", ".join(found) or "nothing automatically"))
print("  web port: %s, path: %s" % (cfg["port"], cfg["base_path"]))
PY
fi
chmod 600 config.json

# ---------------------------------------------------------------- systemd user service
if [ "$SERVICE" = 1 ]; then
  if command -v systemctl >/dev/null && systemctl --user show-environment >/dev/null 2>&1; then
    say "Installing the systemd user service 'controllarr'"
    mkdir -p "$HOME/.config/systemd/user"
    cat > "$HOME/.config/systemd/user/controllarr.service" <<EOF
[Unit]
Description=Controllarr media library and server monitor
After=network-online.target

[Service]
Type=simple
WorkingDirectory=$DIR
ExecStart=$(command -v python3) $DIR/library.py
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
EOF
    systemctl --user daemon-reload
    systemctl --user enable controllarr.service >/dev/null 2>&1 || true
    systemctl --user restart controllarr.service
    loginctl enable-linger "$USER" >/dev/null 2>&1 || true   # keep running after you log out (may need admin rights)
  else
    warn "systemd user services are not available here; start it yourself:  python3 $DIR/library.py"
  fi
fi

# ---------------------------------------------------------------- web route
PORT=$(python3 -c 'import json; print(json.load(open("config.json")).get("port", 8790))')
BASE=$(python3 -c 'import json; print((json.load(open("config.json")).get("base_path") or "/controllarr").rstrip("/"))')
if [ "$NGINX" = 1 ] && [ -d "$HOME/.apps/nginx/proxy.d" ]; then
  say "Adding the web route $BASE/ to your nginx (Ultra.cc layout)"
  cat > "$HOME/.apps/nginx/proxy.d/controllarr.conf" <<EOF
location = $BASE { return 301 $BASE/; }
location $BASE/ {
    # sign-in is handled by Controllarr itself
    proxy_pass           http://127.0.0.1:$PORT/;
    proxy_set_header     Host \$host;
    proxy_set_header     X-Forwarded-For \$proxy_add_x_forwarded_for;
    proxy_read_timeout   600;
    client_max_body_size 1m;
}
EOF
  if command -v app-nginx >/dev/null; then app-nginx restart >/dev/null 2>&1 || warn "restart nginx yourself"; fi
elif [ "$NGINX" = 1 ]; then
  warn "no ~/.apps/nginx found: add a reverse-proxy route for $BASE/ -> http://127.0.0.1:$PORT/ yourself (examples in README.md)"
fi

# ---------------------------------------------------------------- what is left to do
echo
say "Almost done. Still to do:"
python3 - <<'PY'
import json, os
c = json.load(open("config.json"))
todo = []
if not c.get("jellyfin", {}).get("api_key"):
    todo.append("Jellyfin API key: Jellyfin → Dashboard → API Keys → +, then put it in config.json → jellyfin.api_key")
qb = c.get("qbittorrent") or {}
if qb.get("url") and not qb.get("password"):
    todo.append("qBittorrent password: put it in config.json → qbittorrent.password (needed to remove torrents)")
ht = os.path.expanduser(c.get("htpasswd") or os.path.join(os.getcwd(), "htpasswd"))
if not os.path.exists(ht):
    todo.append("Create a login:  htpasswd -c -B %s yourname" % ht)
todo.append("Then restart:  systemctl --user restart controllarr   and open  %s/" % (c.get("base_path") or "/controllarr").rstrip("/"))
for i, t in enumerate(todo, 1):
    print("  %d. %s" % (i, t))
PY
