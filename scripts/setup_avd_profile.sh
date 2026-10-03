#!/usr/bin/env bash
#
# scripts/setup_avd_profile.sh — apply this machine's AVD profile in one shot.
#
# What it changes (all outside git; backups are taken first):
#   GPU    config.ini                hw.gpu.mode = host        (Intel HW rendering,
#                                                              not SwiftShader)
#   GPS    AVD.conf                  loc\latitude/longitude/altitude
#   SIM    iccprofile_for_*.xml      IMSI  -> 452010000000000 (Viettel, PLMN 45201)
#          numeric_operator.xml      adds  45201/452010 -> Viettel
#          (system-image template + AVD copy + the running guest's copy)
#   WIFI   guest settings            wifi_on=1 + report the auto-connected SSID
#   GEO    live fix                  adb emu geo fix <lon> <lat> for a running AVD
#
# Usage:
#   ./scripts/setup_avd_profile.sh                          # apply default profile
#   ./scripts/setup_avd_profile.sh --status                 # show current values only
#   ./scripts/setup_avd_profile.sh --restore                # restore newest backups
#   ./scripts/setup_avd_profile.sh --avd Pixel_8_API_34 --lat 21.0285 --lon 105.8542
#
# Why some steps are skipped while the AVD runs:
#   AVD.conf and the AVD's modem_simulator copy are (re)written by the emulator
#   itself, so a change made under a running instance is lost on exit. GPU
#   (config.ini) and the system-image template are read at boot → always safe.
#   When a device is online the script still applies everything live: geo fix,
#   wifi, and the guest's own SIM profile (needs adb root).
#
# Rollback: every touched file gets <file>.bak-<YYYYMMDD-HHMMSS>; --restore
# copies the newest backup of each file back.

set -euo pipefail

AVD_NAME="Pixel_8_API_34"
LAT="10.8231"        # Ho Chi Minh City
LON="106.6297"
ALT="10"
IMSI="452010000000000"          # MCC 452 + MNC 01 = Viettel (Vietnam)
PLMN="45201"
PLMN_ITEM='<item numeric="45201">Viettel=VIETTEL</item>'
PLMN_ITEM6='<item numeric="452010">Viettel=VIETTEL</item>'

MODE="apply"
RUN_TS="$(date +%Y%m%d-%H%M%S)"

die() { echo "✖ $*" >&2; exit 1; }
log() { echo "▸ $*"; }
ok()  { echo "  ✓ $*"; }
warn(){ echo "  ! $*"; }

usage() {
  sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'
  exit 0
}

while [ $# -gt 0 ]; do
  case "$1" in
    --status)   MODE="status" ;;
    --restore)  MODE="restore" ;;
    --avd)      AVD_NAME="${2:?--avd needs a name}"; shift ;;
    --lat)      LAT="${2:?--lat needs a value}"; shift ;;
    --lon)      LON="${2:?--lon needs a value}"; shift ;;
    --help|-h)  usage ;;
    *) die "unknown option: $1 (try --help)" ;;
  esac
  shift
done

SDK_ROOT="${ANDROID_SDK_ROOT:-$HOME/Android/Sdk}"
AVD_DIR="$HOME/.android/avd/$AVD_NAME.avd"
CONFIG_INI="$AVD_DIR/config.ini"
AVD_CONF="$AVD_DIR/AVD.conf"
SIM_TPL_DIR="$SDK_ROOT/system-images/android-34/google_apis/x86_64/data/misc/modem_simulator"
SIM_AVD_DIR="$AVD_DIR/modem_simulator"
ADB="${ADB_BIN:-$(command -v adb || echo "$SDK_ROOT/platform-tools/adb")}"

[ -d "$AVD_DIR" ] || die "AVD not found: $AVD_DIR (use --avd NAME)"

emulator_running() { pgrep -f "qemu-system.*-avd $AVD_NAME" >/dev/null 2>&1; }

device_serial() {  # the emulator's adb serial, if any
  "$ADB" devices 2>/dev/null | awk '/^emulator-/{print $1; exit}'
}

backup() {  # $1 = file — keep the first (original-state) backup only
  local f="$1" existing
  [ -f "$f" ] || return 0
  existing=$(ls -1 "$f".bak-* 2>/dev/null | head -1 || true)
  if [ -n "$existing" ]; then
    return 0
  fi
  cp -a "$f" "$f.bak-$RUN_TS"
  ok "backup $(basename "$f") -> $(basename "$f").bak-$RUN_TS"
}

restore_all() {
  local restored=0 f latest
  for f in "$CONFIG_INI" "$AVD_CONF" \
           "$SIM_TPL_DIR/iccprofile_for_sim0.xml" \
           "$SIM_TPL_DIR/iccprofile_for_carrierapitests.xml" \
           "$SIM_TPL_DIR/etc/modem_simulator/files/numeric_operator.xml" \
           "$SIM_AVD_DIR/iccprofile_for_sim0.xml" \
           "$SIM_AVD_DIR/iccprofile_for_carrierapitests.xml" \
           "$SIM_AVD_DIR/etc/modem_simulator/files/numeric_operator.xml"; do
    [ -f "$f" ] || continue
    latest=$(ls -1t "$f".bak-* 2>/dev/null | head -1 || true)
    if [ -z "$latest" ]; then
      # also look for directory-style backups (modem_simulator.bak-<ts>/<file>)
      local dir base candidate
      dir=$(dirname "$f"); base=$(basename "$f")
      candidate=$(ls -1dt "$dir".bak-*/ 2>/dev/null | head -1 || true)
      if [ -n "$candidate" ] && [ -f "$candidate$base" ]; then
        latest="$candidate$base"
      fi
    fi
    [ -n "$latest" ] || continue
    cp -a "$latest" "$f"
    ok "restored $(basename "$f") from $(basename "$latest")"
    restored=$((restored + 1))
  done
  [ "$restored" -gt 0 ] || warn "no backups found"
  echo
  echo "Restart the AVD for config changes to take effect:"
  echo "  curl -s -X POST http://127.0.0.1:8001/api/system/emulator/stop; sleep 10"
  echo "  curl -s -X POST -H 'Content-Type: application/json' -d '{\"avd_name\":\"$AVD_NAME\"}' http://127.0.0.1:8001/api/system/emulator/launch"
}

# ---------------------------------------------------------------- GPU -------
apply_gpu() {
  [ -f "$CONFIG_INI" ] || { warn "config.ini missing, skipping GPU"; return 0; }
  backup "$CONFIG_INI"
  if grep -q '^hw.gpu.mode=' "$CONFIG_INI"; then
    sed -i 's/^hw.gpu.mode=.*/hw.gpu.mode=host/' "$CONFIG_INI"
  else
    printf 'hw.gpu.mode=host\n' >> "$CONFIG_INI"
  fi
  ok "hw.gpu.mode=$(grep '^hw.gpu.mode=' "$CONFIG_INI" | cut -d= -f2)"
}

# ---------------------------------------------------------------- GPS -------
apply_location() {
  [ -f "$AVD_CONF" ] || { warn "AVD.conf missing, skipping location"; return 0; }
  if emulator_running; then
    warn "AVD is running: AVD.conf is rewritten on exit, skipping persistent location"
    warn "applying a live geo fix instead (re-run this script after stopping for persistence)"
    local serial; serial=$(device_serial || true)
    if [ -n "${serial:-}" ]; then
      "$ADB" emu geo fix "$LON" "$LAT" >/dev/null 2>&1 \
        && ok "live geo fix $LON $LAT on $serial" \
        || warn "geo fix failed (needs a running emulator)"
    else
      warn "no emulator serial, cannot push a live fix"
    fi
    return 0
  fi
  backup "$AVD_CONF"
  sed -i "s|^loc\\\\latitude=.*|loc\\\\latitude=$LAT|;s|^loc\\\\longitude=.*|loc\\\\longitude=$LON|;s|^loc\\\\altitude=.*|loc\\\\altitude=$ALT|" "$AVD_CONF"
  ok "location = $LAT, $LON (alt $ALT)"
}

# ---------------------------------------------------------------- SIM -------
patch_imsi() {  # $1 = iccprofile file
  local f="$1"
  [ -f "$f" ] || return 0
  backup "$f"
  sed -i "s|<CIMI>[0-9][0-9]*</CIMI>|<CIMI>$IMSI</CIMI>|" "$f"
  ok "IMSI in $(basename "$f") = $(grep -o '<CIMI>[0-9]*' "$f" | head -1 | sed 's/<CIMI>//')"
}

patch_plmn_map() {  # $1 = numeric_operator.xml
  local f="$1"
  [ -f "$f" ] || return 0
  backup "$f"
  if ! grep -q "numeric=\"$PLMN\"" "$f"; then
    if grep -q 'numeric="310260"' "$f"; then
      sed -i "s|<item numeric=\"310260\">[^<]*</item>|&\\n        $PLMN_ITEM|" "$f"
    else
      sed -i "s|    </string-array>|        $PLMN_ITEM\\n    </string-array>|" "$f"
    fi
  fi
  # the emulator reports a 6-digit numeric (MNC parsed as 3 digits) too
  grep -q "numeric=\"${PLMN}0\"" "$f" \
    || sed -i "s|$PLMN_ITEM|$PLMN_ITEM\\n        $PLMN_ITEM6|" "$f"
  ok "operator map: $(grep -o 'numeric="[0-9]*">[^<]*' "$f" | tr '\n' ' ')"
}

apply_sim_template() {
  [ -d "$SIM_TPL_DIR" ] || { warn "system image template missing, skipping"; return 0; }
  patch_imsi "$SIM_TPL_DIR/iccprofile_for_sim0.xml"
  patch_imsi "$SIM_TPL_DIR/iccprofile_for_carrierapitests.xml"
  patch_plmn_map "$SIM_TPL_DIR/etc/modem_simulator/files/numeric_operator.xml"
}

apply_sim_avd() {
  [ -d "$SIM_AVD_DIR" ] || { warn "AVD modem_simulator missing, skipping"; return 0; }
  if emulator_running; then
    warn "AVD is running: its modem_simulator copy is rewritten on boot; template already patched"
    return 0
  fi
  patch_imsi "$SIM_AVD_DIR/iccprofile_for_sim0.xml"
  patch_imsi "$SIM_AVD_DIR/iccprofile_for_carrierapitests.xml"
  patch_plmn_map "$SIM_AVD_DIR/etc/modem_simulator/files/numeric_operator.xml"
}

apply_sim_guest() {  # live AVD keeps its own copy inside userdata → patch it too
  local serial="$1" shell out
  echo "  → guest SIM profile on $serial (needs adb root)"
  if ! "$ADB" -s "$serial" root 2>&1 | grep -qiE 'root|already'; then
    warn "adb root unavailable on $serial, guest SIM copy left as-is"
    return 0
  fi
  sleep 3
  "$ADB" -s "$serial" wait-for-device >/dev/null 2>&1 || true
  shell='sed -i "s|<CIMI>[0-9][0-9]*</CIMI>|<CIMI>'"$IMSI"'</CIMI>|" /data/misc/modem_simulator/iccprofile_for_sim0.xml /data/misc/modem_simulator/iccprofile_for_carrierapitests.xml; grep -o "<CIMI>[0-9]*" /data/misc/modem_simulator/iccprofile_for_sim0.xml | head -1'
  out=$("$ADB" -s "$serial" shell "$shell" 2>/dev/null | tr -d '\r' || true)
  ok "guest IMSI = ${out:-patched}"
}

# ------------------------------------------------------------ WIFI / GEO ----
apply_wifi() {
  local serial="$1"
  "$ADB" -s "$serial" shell 'svc wifi enable; settings put global wifi_on 1' >/dev/null 2>&1 || true
  sleep 3
  local ssid ip
  ssid=$("$ADB" -s "$serial" shell 'dumpsys wifi 2>/dev/null | grep -m1 -o "SSID: \"[^\"]*\""' 2>/dev/null | tr -d '\r' | sed 's/SSID: //')
  ip=$("$ADB" -s "$serial" shell 'ip -4 addr show wlan0 2>/dev/null | grep -o "inet [0-9.]*[0-9]" | awk "{print \$2}"' 2>/dev/null | tr -d '\r')
  ok "wifi_on=1 ssid=${ssid:-<none>} ip=${ip:-<none>}"
}

apply_geo_live() {
  local serial="$1"
  "$ADB" emu geo fix "$LON" "$LAT" >/dev/null 2>&1 \
    && ok "live geo fix $LON $LAT on $serial" \
    || warn "geo fix failed"
}

# --------------------------------------------------------------- status -----
show_status() {
  echo "── GPU ────────────────────────────────────────────────"
  grep '^hw.gpu' "$CONFIG_INI" 2>/dev/null || echo "  (no config.ini)"
  echo "── GPS (AVD.conf) ─────────────────────────────────────"
  grep '^loc\\' "$AVD_CONF" 2>/dev/null || echo "  (no AVD.conf)"
  echo "── SIM (template / AVD copy) ──────────────────────────"
  for f in "$SIM_TPL_DIR/iccprofile_for_sim0.xml" "$SIM_AVD_DIR/iccprofile_for_sim0.xml"; do
    if [ -f "$f" ]; then
      echo "  ${f#"$SDK_ROOT/system-images/"}: $(grep -o '<CIMI>[0-9]*' "$f" | head -1 | sed 's/<CIMI>//')"
    fi
  done
  if [ -f "$SIM_TPL_DIR/etc/modem_simulator/files/numeric_operator.xml" ]; then
    echo "  map: $(grep -o 'numeric="[0-9]*">[^<]*' "$SIM_TPL_DIR/etc/modem_simulator/files/numeric_operator.xml" | tr '\n' ' ')"
  fi
  echo "── device (if connected) ──────────────────────────────"
  local serial; serial=$(device_serial || true)
  if [ -n "${serial:-}" ]; then
    echo "  serial: $serial"
    "$ADB" -s "$serial" shell 'echo "  sim: $(getprop gsm.sim.operator.numeric) / $(getprop gsm.operator.alpha) / $(getprop gsm.sim.operator.iso-country)"; echo "  wifi: $(settings get global wifi_on) $(dumpsys wifi 2>/dev/null | grep -m1 -o "SSID: \"[^\"]*\"")"; echo "  loc_mode: $(settings get secure location_mode)"; dumpsys SurfaceFlinger 2>/dev/null | grep -i "GLES:" | head -1 | sed "s/^/  /"' 2>/dev/null
  else
    echo "  (AVD not running)"
  fi
  echo "── backups ────────────────────────────────────────────"
  ls -1td "$AVD_DIR"/*.bak-* "$SIM_TPL_DIR"/../modem_simulator.bak-* "$SIM_TPL_DIR"/*.bak-* 2>/dev/null | head -8 || echo "  (none)"
}

# ----------------------------------------------------------------- main -----
case "$MODE" in
  status)  show_status; exit 0 ;;
  restore) restore_all; exit 0 ;;
esac

echo "Applying AVD profile '$AVD_NAME' (GPS $LAT,$LON · SIM Viettel $PLMN · GPU host)"
if emulator_running; then
  warn "AVD is running — persistent AVD.conf/SIM-copy edits are skipped (see header)"
fi

echo "── GPU ────────────────────────────────────────────────"; apply_gpu
echo "── GPS ────────────────────────────────────────────────"; apply_location
echo "── SIM (system image template) ────────────────────────"; apply_sim_template
echo "── SIM (AVD copy) ────────────────────────────────────"; apply_sim_avd

SERIAL=$(device_serial || true)
if [ -n "${SERIAL:-}" ]; then
  echo "── SIM (running guest) on $SERIAL ────────────────────"; apply_sim_guest "$SERIAL"
  echo "── WIFI / GEO on $SERIAL ─────────────────────────────"; apply_wifi "$SERIAL"; apply_geo_live "$SERIAL"
else
  warn "AVD not connected: guest SIM/wifi/geo steps skipped (re-run once it is up)"
fi

echo
echo "Done. Restart the AVD to pick up config-level changes:"
echo "  curl -s -X POST http://127.0.0.1:8001/api/system/emulator/stop; sleep 10"
echo "  curl -s -X POST -H 'Content-Type: application/json' -d '{\"avd_name\":\"$AVD_NAME\"}' http://127.0.0.1:8001/api/system/emulator/launch"
echo "Check with: $0 --status"
