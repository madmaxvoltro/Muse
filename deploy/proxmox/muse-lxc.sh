#!/usr/bin/env bash
# Muse in a new Proxmox LXC container. Run on the Proxmox host as root:
#
#   bash -c "$(curl -fsSL https://raw.githubusercontent.com/madmaxvoltro/Muse/main/deploy/proxmox/muse-lxc.sh)"
#
# Makes an unprivileged Debian 12 container with nesting (Docker needs it), installs Docker and
# Muse inside, and starts the browser setup: the URL and a QR code for your phone appear here.
# Override defaults with environment variables: CTID CT_NAME CORES MEMORY DISK STORAGE BRIDGE BRANCH.
set -euo pipefail

die() { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }
ok() { printf '  \033[32mOK  \033[0m %s\n' "$*"; }
command -v pct >/dev/null 2>&1 || die "run this on a Proxmox VE host"
[ "$(id -u)" -eq 0 ] || die "run as root"

CTID="${CTID:-$(pvesh get /cluster/nextid)}"
CT_NAME="${CT_NAME:-muse}"
CORES="${CORES:-4}"
MEMORY="${MEMORY:-8192}"   # MB; 16384 for the larger embedding model
DISK="${DISK:-32}"         # GB, for images and models (your media stays where it is)
BRIDGE="${BRIDGE:-vmbr0}"
BRANCH="${BRANCH:-main}"
STORAGE="${STORAGE:-$(pvesm status -content rootdir 2>/dev/null | awk 'NR>1 && $3=="active" {print $1; exit}')}"
TSTORE="${TEMPLATE_STORAGE:-$(pvesm status -content vztmpl 2>/dev/null | awk 'NR>1 && $3=="active" {print $1; exit}')}"
[ -n "$STORAGE" ] || die "no storage for containers found; set STORAGE="
[ -n "$TSTORE" ] || die "no storage for templates found; set TEMPLATE_STORAGE="

printf '\033[1mMuse in a Proxmox container\033[0m\n'
echo "  CT $CTID '$CT_NAME': $CORES cores, $MEMORY MB, $DISK GB on $STORAGE, network $BRIDGE (DHCP)"
read -r -p "  Create it? [Y/n] " ans </dev/tty || ans=y
case "${ans:-y}" in y|Y|yes) ;; *) exit 0 ;; esac

pveam update >/dev/null
TEMPLATE="$(pveam available --section system | awk '/debian-12-standard/ {print $2}' | sort -V | tail -1)"
[ -n "$TEMPLATE" ] || die "no Debian 12 template available"
pveam list "$TSTORE" | grep -q "$TEMPLATE" || pveam download "$TSTORE" "$TEMPLATE" >/dev/null
ok "template $TEMPLATE"

pct create "$CTID" "$TSTORE:vztmpl/$TEMPLATE" \
  --hostname "$CT_NAME" --cores "$CORES" --memory "$MEMORY" --swap 1024 \
  --rootfs "$STORAGE:$DISK" --net0 "name=eth0,bridge=$BRIDGE,ip=dhcp,firewall=1" \
  --features nesting=1,keyctl=1 --unprivileged 1 --onboot 1 --ostype debian --tags muse >/dev/null
pct start "$CTID"
ok "container $CTID started"

for _ in $(seq 1 60); do pct exec "$CTID" -- getent hosts github.com >/dev/null 2>&1 && break; sleep 2; done
pct exec "$CTID" -- getent hosts github.com >/dev/null 2>&1 || die "the container has no network (check $BRIDGE and DHCP)"
pct exec "$CTID" -- bash -c "apt-get update -qq && apt-get install -y -qq curl ca-certificates >/dev/null"
ok "network up: $(pct exec "$CTID" -- hostname -I | awk '{print $1}')"

echo
echo "  Installing Docker and Muse in the container, then the setup opens in your browser."
pct exec "$CTID" -- bash -c "curl -fsSL https://raw.githubusercontent.com/madmaxvoltro/Muse/$BRANCH/install.sh | MUSE_SETUP_PHONE=1 bash -s -- --branch $BRANCH --web --yes"
echo
ok "Muse runs in CT $CTID. Later: pct enter $CTID, then cd /opt/muse && ./muse status"
