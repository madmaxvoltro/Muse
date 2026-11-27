#!/usr/bin/env bash
# Muse in a new Proxmox VM (Debian 12 cloud image). Run on the Proxmox host as root:
#
#   bash -c "$(curl -fsSL https://raw.githubusercontent.com/madmaxvoltro/Muse/main/deploy/proxmox/muse-vm.sh)"
#
# A VM is the choice for GPU passthrough or if you'd rather not run Docker in a container.
# Cloud-init installs Docker and Muse; this script waits until the browser setup is up and
# prints its URL and a QR code. Defaults via environment: VMID NAME CORES MEMORY DISK STORAGE
# BRIDGE BRANCH SSH_KEY (a public key file, so you can log in as `muse`).
set -euo pipefail

die() { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }
ok() { printf '  \033[32mOK  \033[0m %s\n' "$*"; }
command -v qm >/dev/null 2>&1 || die "run this on a Proxmox VE host"
[ "$(id -u)" -eq 0 ] || die "run as root"

VMID="${VMID:-$(pvesh get /cluster/nextid)}"
NAME="${NAME:-muse}"
CORES="${CORES:-4}"
MEMORY="${MEMORY:-8192}"
DISK="${DISK:-40}"
BRIDGE="${BRIDGE:-vmbr0}"
BRANCH="${BRANCH:-main}"
STORAGE="${STORAGE:-$(pvesm status -content images 2>/dev/null | awk 'NR>1 && $3=="active" {print $1; exit}')}"
SNIPPETS="${SNIPPET_STORAGE:-$(pvesm status -content snippets 2>/dev/null | awk 'NR>1 && $3=="active" {print $1; exit}')}"
SSH_KEY="${SSH_KEY:-$( [ -f /root/.ssh/id_ed25519.pub ] && echo /root/.ssh/id_ed25519.pub || true)}"
IMG_URL="https://cloud.debian.org/images/cloud/bookworm/latest/debian-12-genericcloud-amd64.qcow2"
IMG="/var/lib/vz/template/iso/debian-12-genericcloud-amd64.qcow2"
[ -n "$STORAGE" ] || die "no storage for VM disks found; set STORAGE="
if [ -z "$SNIPPETS" ]; then
  echo "  Cloud-init needs a storage that holds snippets. 'local' can, once it is allowed to:"
  echo "    pvesm set local --content \$(pvesm config local | awk '/content/ {print \$2}'),snippets"
  die "no snippets storage; run the line above (or set SNIPPET_STORAGE=) and try again"
fi
SNIPDIR="$(pvesm path "$SNIPPETS:snippets/x" 2>/dev/null | xargs dirname)"
[ -d "$SNIPDIR" ] || mkdir -p "$SNIPDIR"

printf '\033[1mMuse in a Proxmox VM\033[0m\n'
echo "  VM $VMID '$NAME': $CORES cores, $MEMORY MB, $DISK GB on $STORAGE, network $BRIDGE (DHCP)"
read -r -p "  Create it? [Y/n] " ans </dev/tty || ans=y
case "${ans:-y}" in y|Y|yes) ;; *) exit 0 ;; esac

mkdir -p "$(dirname "$IMG")"
if [ ! -f "$IMG" ] || [ -n "$(find "$IMG" -mtime +30)" ]; then
  curl -fsSL -o "$IMG.part" "$IMG_URL" && mv "$IMG.part" "$IMG"
fi
ok "Debian 12 cloud image"

KEYS=""
[ -n "$SSH_KEY" ] && [ -f "$SSH_KEY" ] && KEYS="    ssh_authorized_keys: [\"$(cat "$SSH_KEY")\"]"
cat >"$SNIPDIR/muse-$VMID.yaml" <<YAML
#cloud-config
hostname: $NAME
users:
  - name: muse
    groups: [sudo, docker]
    shell: /bin/bash
    sudo: "ALL=(ALL) NOPASSWD:ALL"
    lock_passwd: true
$KEYS
package_update: true
packages: [qemu-guest-agent, curl, ca-certificates, git, python3, openssl]
runcmd:
  - systemctl enable --now qemu-guest-agent
  - systemd-run --unit=muse-setup --setenv=MUSE_SETUP_URL_FILE=/root/muse-setup-url.txt --setenv=MUSE_YES=1 bash -c 'curl -fsSL https://raw.githubusercontent.com/madmaxvoltro/Muse/$BRANCH/install.sh | bash -s -- --branch $BRANCH --web --yes'
YAML

qm create "$VMID" --name "$NAME" --cores "$CORES" --memory "$MEMORY" --cpu host --ostype l26 \
  --net0 "virtio,bridge=$BRIDGE,firewall=1" --scsihw virtio-scsi-single --agent enabled=1 \
  --serial0 socket --vga serial0 --onboot 1 --tags muse >/dev/null
qm set "$VMID" --scsi0 "$STORAGE:0,import-from=$IMG,discard=on,ssd=1" >/dev/null
qm resize "$VMID" scsi0 "${DISK}G" >/dev/null
qm set "$VMID" --ide2 "$STORAGE:cloudinit" --boot order=scsi0 --ipconfig0 ip=dhcp \
  --cicustom "user=$SNIPPETS:snippets/muse-$VMID.yaml" >/dev/null
qm start "$VMID"
ok "VM $VMID started; installing Docker and Muse (5 to 15 minutes)"

guest() { qm guest exec "$VMID" -- "$@" 2>/dev/null | python3 -c 'import json,sys; d=json.load(sys.stdin); sys.stdout.write(d.get("out-data",""))' 2>/dev/null; }
URL=""
for _ in $(seq 1 180); do
  URL="$(guest cat /root/muse-setup-url.txt | tr -d '\n')"
  [ -n "$URL" ] && break
  printf '.'; sleep 10
done
echo
[ -n "$URL" ] || die "the setup didn't come up in 30 minutes: qm terminal $VMID, then journalctl -u muse-setup"
ok "the setup is ready"
echo
echo "  Open this in a browser on your network (accept the one-time certificate warning):"
echo "    $URL"
QR="$(mktemp)"
if curl -fsSL -o "$QR" "https://raw.githubusercontent.com/madmaxvoltro/Muse/$BRANCH/installer/qr.py"; then
  python3 - "$QR" "$URL" <<'PY'
import importlib.util, sys
spec = importlib.util.spec_from_file_location("qr", sys.argv[1]); qr = importlib.util.module_from_spec(spec); spec.loader.exec_module(qr)
print("\n  Or scan it with your phone:\n")
print("\n".join("    " + l for l in qr.to_terminal(qr.encode(sys.argv[2], "M")).splitlines()))
PY
fi
rm -f "$QR"
echo
ok "Muse runs in VM $VMID. Later: ssh muse@<its address>, then cd /opt/muse && sudo ./muse status"
