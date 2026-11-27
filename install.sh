#!/usr/bin/env bash
# Install Muse with one line:
#
#   curl -fsSL https://raw.githubusercontent.com/madmaxvoltro/Muse/main/install.sh | bash
#
# Checks for Docker (offers to install it), Python 3 and git, puts Muse in /opt/muse (root)
# or ~/muse, and starts the setup: in your browser by default (with a QR code for your phone),
# or in the terminal with --terminal. Safe to run again: it updates the checkout and keeps
# your keys and data.
#
# Options: --dir DIR  --branch NAME  --terminal  --web  --yes (install missing packages without asking)
set -euo pipefail

REPO="${MUSE_REPO:-https://github.com/madmaxvoltro/Muse.git}"
BRANCH="${MUSE_BRANCH:-main}"
MODE="ask"
ASSUME_YES="${MUSE_YES:-0}"
if [ "$(id -u)" -eq 0 ]; then DIR="${MUSE_DIR:-/opt/muse}"; else DIR="${MUSE_DIR:-$HOME/muse}"; fi

while [ $# -gt 0 ]; do
  case "$1" in
    --dir) DIR="$2"; shift 2 ;;
    --branch) BRANCH="$2"; shift 2 ;;
    --terminal) MODE="terminal"; shift ;;
    --web) MODE="web"; shift ;;
    --yes|-y) ASSUME_YES=1; shift ;;
    -h|--help) sed -n '2,13p' "$0" 2>/dev/null || true; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
ok() { printf '  \033[32mOK  \033[0m %s\n' "$*"; }
warn() { printf '  \033[33mWARN\033[0m %s\n' "$*"; }
die() { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }

# Questions go to the terminal even when this script arrives through a pipe.
TTY=/dev/tty
have_tty() { [ -r "$TTY" ] && [ -w "$TTY" ] && { : <"$TTY"; } 2>/dev/null; }
confirm() {
  [ "$ASSUME_YES" = 1 ] && return 0
  have_tty || return 1
  local ans
  printf '  %s [Y/n]: ' "$1" >"$TTY"
  read -r ans <"$TTY" || return 1
  case "${ans:-y}" in y|Y|yes|Yes) return 0 ;; *) return 1 ;; esac
}

SUDO=""
if [ "$(id -u)" -ne 0 ]; then
  command -v sudo >/dev/null 2>&1 && SUDO="sudo" || warn "not root and no sudo: missing packages can't be installed for you"
fi

pkg_install() {
  if command -v apt-get >/dev/null 2>&1; then $SUDO apt-get update -qq && $SUDO apt-get install -y -qq "$@"
  elif command -v dnf >/dev/null 2>&1; then $SUDO dnf install -y -q "$@"
  elif command -v pacman >/dev/null 2>&1; then $SUDO pacman -Sy --noconfirm "$@"
  elif command -v apk >/dev/null 2>&1; then $SUDO apk add --no-cache "$@"
  else die "can't install $* here: install it yourself and run this again"; fi
}

bold "Muse installer"
[ "$(uname -s)" = "Linux" ] || die "Muse runs on Linux (a VM or an LXC container is fine)"

# --- prerequisites ------------------------------------------------------------------------------
for tool in git python3 curl; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    confirm "$tool is missing. Install it?" || die "$tool is needed"
    pkg_install "$tool"
  fi
done
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' || die "Python 3.9 or newer is needed"
ok "git, Python $(python3 -c 'import platform; print(platform.python_version())')"

if ! command -v docker >/dev/null 2>&1; then
  confirm "Docker isn't installed. Install it now with Docker's official script (get.docker.com)?" \
    || die "install Docker (https://docs.docker.com/engine/install/) and run this again"
  curl -fsSL https://get.docker.com | $SUDO sh
  [ -n "$SUDO" ] && $SUDO usermod -aG docker "$USER" && warn "added $USER to the docker group (takes effect at your next login)"
fi
if ! docker compose version >/dev/null 2>&1 && ! $SUDO docker compose version >/dev/null 2>&1; then
  confirm "The Docker Compose plugin is missing. Install it?" || die "Docker Compose v2 is needed"
  pkg_install docker-compose-plugin
fi
ok "Docker $(docker --version 2>/dev/null | sed 's/Docker version //; s/,.*//' || echo '(via sudo)')"
if ! docker info >/dev/null 2>&1; then
  if $SUDO docker info >/dev/null 2>&1; then DOCKER_SUDO="$SUDO"; else
    $SUDO systemctl enable --now docker >/dev/null 2>&1 || true
    docker info >/dev/null 2>&1 || $SUDO docker info >/dev/null 2>&1 || die "Docker isn't running: start it (systemctl start docker)"
    DOCKER_SUDO="$SUDO"
  fi
else DOCKER_SUDO=""; fi

# --- get or update Muse ------------------------------------------------------------------------
if [ -d "$DIR/.git" ]; then
  git -C "$DIR" fetch -q origin "$BRANCH" && git -C "$DIR" checkout -q "$BRANCH" && git -C "$DIR" pull -q --ff-only origin "$BRANCH"
  ok "updated $DIR"
else
  [ -e "$DIR" ] && [ -n "$(ls -A "$DIR" 2>/dev/null)" ] && die "$DIR exists and isn't a Muse checkout: pick another with --dir"
  parent="$(dirname "$DIR")"
  if [ -w "$parent" ]; then git clone -q --depth 50 --branch "$BRANCH" "$REPO" "$DIR"
  else $SUDO git clone -q --depth 50 --branch "$BRANCH" "$REPO" "$DIR" && $SUDO chown -R "$(id -u):$(id -g)" "$DIR"; fi
  ok "Muse in $DIR"
fi
cd "$DIR"

# --- setup -------------------------------------------------------------------------------------
if [ "$MODE" = "ask" ]; then
  MODE="web"
  if have_tty; then
    printf '\n  Set up in your browser (also works on your phone) or here in the terminal? [browser/terminal]: ' >"$TTY"
    read -r ans <"$TTY" || ans=""
    case "$ans" in t|terminal|T) MODE="terminal" ;; esac
  fi
fi

run() { if [ -n "$DOCKER_SUDO" ]; then $DOCKER_SUDO --preserve-env=MUSE_SETUP_PHONE,MUSE_SETUP_URL_FILE "$@"; else "$@"; fi; }
if have_tty; then
  if [ "$MODE" = "web" ]; then run ./muse setup --web <"$TTY"; else run ./muse setup <"$TTY"; fi
else
  # No terminal (e.g. cloud-init): the browser setup, reachable on the network.
  MUSE_SETUP_PHONE=1 run ./muse setup --web </dev/null
fi
