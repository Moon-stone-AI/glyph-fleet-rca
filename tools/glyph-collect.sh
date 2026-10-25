#!/usr/bin/env bash
# glyph-collect: gather the logs GlyphAI Fleet RCA reads from ONE Linux GPU server.
#
# Read-only. It installs nothing, changes nothing and sends nothing anywhere: it writes one
# .tar.gz in the current directory, and you decide whether that file leaves the building.
#
#   sudo ./glyph-collect.sh                 # last 30 days of kernel log, redacted
#   sudo ./glyph-collect.sh --days 90
#   sudo ./glyph-collect.sh --diag 2        # ALSO run DCGM diagnostics (loads the GPUs: idle nodes only)
#   sudo ./glyph-collect.sh --bmc ./exports # ALSO add vendor BMC exports from a folder (redacted too)
#   sudo ./glyph-collect.sh --no-redact     # keep hostnames, IPs, MACs and serial numbers
#
# What it reads (each step is skipped if the tool is missing):
#   kernel log           journalctl -k (or dmesg -T)            -> kernel.log   (NVIDIA Xid, PCIe AER, MCE)
#   BMC event log        ipmitool sel elist                     -> sel.txt
#   GPU state            nvidia-smi -q                          -> nvidia-smi-q.txt (identity, ECC, row remapping)
#   PCIe tree            /sys/bus/pci (widths and speeds)       -> pci-sysfs.tsv
#   DCGM GPU ids         dcgmi discovery -l                     -> dcgm-discovery.txt
#   DCGM diagnostics     dcgmi diag -r N -j, only with --diag   -> dcgm-diag.json
# Vendor BMC exports (iDRAC Lifecycle Log, Lenovo XCC event log, HPE IML) cannot be read from the
# host without vendor tools: export them from the BMC web page into a folder and pass it with
# --bmc FOLDER. The files are copied into bmc/ and redacted with everything else.
#
# Redaction (default ON), applied to every text file in the archive, bmc/ included: the hostname
# becomes node-<hash>; IPv4, IPv6 and MAC addresses are masked; serial numbers, board part numbers
# and Dell service tags are masked ("Key: value", "Key=value" and <Key>value</Key> forms).
# GPU UUIDs and PCIe addresses are kept: they are how a GPU's history follows the card.
#
# (c) 2026 Vitali de Mille. PolyForm Noncommercial 1.0.0. https://github.com/Moon-stone-AI/glyph-fleet-rca
set -u
VERSION="0.5.1"
DAYS=30
DIAG=""
REDACT=1
BMC_DIR=""

while [ $# -gt 0 ]; do
  case "$1" in
    --days) DAYS="$2"; shift 2 ;;
    --diag) DIAG="$2"; shift 2 ;;
    --bmc) BMC_DIR="${2:-}"; shift 2 ;;
    --no-redact) REDACT=0; shift ;;
    -h|--help) sed -n '2,29p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done
case "$DAYS" in ''|*[!0-9]*) echo "--days needs a number" >&2; exit 2 ;; esac
case "$DIAG" in ''|1|2|3|4) ;; *) echo "--diag must be 1, 2, 3 or 4" >&2; exit 2 ;; esac
if [ -n "$BMC_DIR" ] && [ ! -d "$BMC_DIR" ]; then echo "--bmc needs a folder: $BMC_DIR" >&2; exit 2; fi

have() { command -v "$1" >/dev/null 2>&1; }
log() { echo "[glyph-collect] $*" >&2; }

HOST_REAL="$(hostname -f 2>/dev/null || hostname)"
HOST_SHORT="$(hostname -s 2>/dev/null || hostname)"
if [ "$REDACT" = 1 ]; then
  NODE="node-$(printf '%s' "$HOST_REAL" | sha256sum | cut -c1-10)"
else
  NODE="$HOST_SHORT"
fi
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
WORK="$(mktemp -d)"
OUT="$WORK/$NODE"
mkdir -p "$OUT/bmc"
trap 'rm -rf "$WORK"' EXIT

[ "$(id -u)" = 0 ] || log "not root: the kernel log and the BMC log may be incomplete"

# ---- meta
{
  echo "collector_version=$VERSION"
  echo "node=$NODE"
  echo "collected_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "kernel=$(uname -r)"
  [ -r /etc/os-release ] && echo "os=$(. /etc/os-release; echo "$PRETTY_NAME")"
  [ -r /sys/class/dmi/id/sys_vendor ] && echo "system_vendor=$(cat /sys/class/dmi/id/sys_vendor)"
  [ -r /sys/class/dmi/id/product_name ] && echo "system_model=$(cat /sys/class/dmi/id/product_name)"
  [ -r /sys/class/dmi/id/bios_version ] && echo "bios=$(cat /sys/class/dmi/id/bios_version)"
  have nvidia-smi && echo "nvidia_driver=$(nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>/dev/null | head -1)"
  echo "days=$DAYS"
  echo "redacted=$REDACT"
} > "$OUT/meta.txt"

# ---- kernel log (all boots kept by journald, else the current ring buffer)
if have journalctl; then
  journalctl -k -o short-iso --no-pager --since "-${DAYS} days" > "$OUT/kernel.log" 2>/dev/null
fi
if [ ! -s "$OUT/kernel.log" ] && have dmesg; then
  log "journald has no kernel log; falling back to dmesg (current boot only)"
  dmesg -T > "$OUT/kernel.log" 2>/dev/null
fi
log "kernel.log: $(wc -l < "$OUT/kernel.log" 2>/dev/null || echo 0) lines"

# ---- BMC SEL through the in-band IPMI interface
if have ipmitool; then
  if ipmitool sel elist > "$OUT/sel.txt" 2>/dev/null; then
    log "sel.txt: $(wc -l < "$OUT/sel.txt") records"
  else
    rm -f "$OUT/sel.txt"; log "ipmitool present but the SEL could not be read (no /dev/ipmi0 or not root)"
  fi
else
  log "ipmitool not installed: export the BMC event log from the BMC page and pass it with --bmc FOLDER"
fi

# ---- GPU state
if have nvidia-smi; then
  nvidia-smi -q > "$OUT/nvidia-smi-q.txt" 2>&1
  log "nvidia-smi-q.txt: $(grep -c '^GPU ' "$OUT/nvidia-smi-q.txt") GPUs"
else
  log "nvidia-smi not found: no GPU state"
fi
have dcgmi && dcgmi discovery -l > "$OUT/dcgm-discovery.txt" 2>&1

if [ -n "$DIAG" ]; then
  if have dcgmi; then
    log "running dcgmi diag -r $DIAG: this loads every GPU; run it only on an idle, drained node"
    dcgmi diag -r "$DIAG" -j > "$OUT/dcgm-diag.json" 2>"$OUT/dcgm-diag.err"
    [ -s "$OUT/dcgm-diag.err" ] || rm -f "$OUT/dcgm-diag.err"
  else
    log "--diag asked but dcgmi is not installed"
  fi
fi

# ---- PCIe: one line per device, read straight from sysfs
{
  printf 'bdf\tparent\tclass\tvendor\tcur_width\tmax_width\tcur_speed\tmax_speed\tslot\n'
  declare -A SLOT
  for s in /sys/bus/pci/slots/*; do
    [ -r "$s/address" ] && SLOT["$(cat "$s/address")"]="$(basename "$s")"
  done
  for d in /sys/bus/pci/devices/*; do
    bdf="$(basename "$d")"
    up="$(basename "$(dirname "$(readlink -f "$d")")")"
    case "$up" in [0-9a-f][0-9a-f][0-9a-f][0-9a-f]:[0-9a-f][0-9a-f]:[0-9a-f][0-9a-f].[0-7]) ;; *) up="-" ;; esac
    rd() { [ -r "$d/$1" ] && tr -d '\n' < "$d/$1" 2>/dev/null || printf '?'; }
    printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$bdf" "$up" "$(rd class)" "$(rd vendor)" \
      "$(rd current_link_width)" "$(rd max_link_width)" "$(rd current_link_speed)" "$(rd max_link_speed)" \
      "${SLOT[${bdf%.*}]:--}"
  done
} > "$OUT/pci-sysfs.tsv"

cat > "$OUT/bmc/README.txt" <<'EOF'
Vendor BMC event-log exports, as plain text, CSV or XML. Export them from the BMC page into a
folder and run the collector with --bmc FOLDER, so that they are redacted before archiving.
Files added here by hand after the archive was built are NOT redacted.
  Dell iDRAC:   Maintenance > Lifecycle Log > Export  (or: racadm lclog export)
  Lenovo XCC:   Events > Event Log > Export
  HPE iLO:      Information > Integrated Management Log > Save
GlyphAI reads Dell message IDs, Lenovo FQX event IDs and HPE IML texts from these files.
EOF

# ---- vendor BMC exports given with --bmc (top-level regular files only; copied, never moved)
if [ -n "$BMC_DIR" ]; then
  n=0
  for f in "$BMC_DIR"/*; do
    [ -f "$f" ] || continue
    case "$(basename "$f")" in README.txt) continue ;; esac
    cp -- "$f" "$OUT/bmc/" && n=$((n + 1))
  done
  log "bmc/: $n file(s) copied from $BMC_DIR"
fi

# ---- redaction: every text file in the archive, bmc/ included
# IPv6: only full 8-group addresses, or forms with "::" and at least one hex group, bounded by
# characters that cannot belong to an address. PCIe addresses (0000:1b:00.0) and clock times
# (10:23:45) are left alone. The IPv6 pass runs twice so that adjacent addresses are caught.
H='[0-9A-Fa-f]{1,4}'
V6="(($H:){7}$H|$H(:$H){0,6}::($H(:$H){0,6})?|::$H(:$H){0,6})(:x\.x\.x\.x)?"
P6='[^0-9A-Za-z_:.]'     # what may come before an address
S6='[^0-9A-Za-z_:]'      # what may follow it (a scope like %ib0, a full stop, a space)
KEYS='Serial Number|Serial No\.?|SerialNumber|Serial_Number|Serial|Board Part Number|Service Tag|ServiceTag|SVCTAG'
if [ "$REDACT" = 1 ]; then
  find "$OUT" -type f ! -name README.txt | while IFS= read -r f; do
    grep -Iq . "$f" 2>/dev/null || continue          # skip empty and binary files
    sed -E -i \
      -e "s/${HOST_REAL//./\\.}/$NODE/g" -e "s/\\b${HOST_SHORT}\\b/$NODE/g" \
      -e 's/\b([0-9]{1,3}\.){3}[0-9]{1,3}\b/x.x.x.x/g' \
      -e 's/\b([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}\b/xx:xx:xx:xx:xx:xx/g' \
      -e "s/(^|$P6)$V6($S6|\$)/\1x:x:x:x\9/g" \
      -e "s/(^|$P6)$V6($S6|\$)/\1x:x:x:x\9/g" \
      -e "s/(($KEYS)[[:space:]]*[:=][[:space:]]*).*/\1REDACTED/I" \
      -e "s/(<($KEYS)>)[^<]*(<)/\1REDACTED\3/Ig" \
      "$f"
  done
fi

ARCHIVE="glyph-collect-$NODE-$STAMP.tar.gz"
tar -C "$WORK" -czf "$ARCHIVE" "$NODE"
log "done: $ARCHIVE ($(du -h "$ARCHIVE" | cut -f1))"
log "nothing was sent anywhere. Look inside before you share it: tar -tzf $ARCHIVE"
