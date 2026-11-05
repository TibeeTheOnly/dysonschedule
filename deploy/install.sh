#!/usr/bin/env bash
# Dyson Pi installer for Raspberry Pi OS Lite (Bookworm, 64-bit).
# Idempotent: safe to re-run (e.g. after changing relay polarity in config.yaml).
#
#   sudo ./deploy/install.sh                 interactive
#   sudo ./deploy/install.sh --reconfigure-wifi
#
# Non-interactive: set DYSON_WIFI_SSID, DYSON_WIFI_PSK, DYSON_WIFI_COUNTRY,
# DYSON_PASSWORD, DYSON_HOSTNAME (and optionally DYSON_SKIP_WIFI=1).
set -euo pipefail

APP_DIR=/opt/dyson-pi
ETC_DIR=/etc/dyson-pi
STATE_DIR=/var/lib/dyson-pi
LOG_DIR=/var/log/dyson-pi
CONFIG=$ETC_DIR/config.yaml
VENV=$APP_DIR/.venv
SVC_USER=dyson-pi
SRC_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
RECONFIGURE_WIFI=0
[[ "${1:-}" == "--reconfigure-wifi" ]] && RECONFIGURE_WIFI=1

c_bold=$'\e[1m'; c_red=$'\e[31m'; c_yel=$'\e[33m'; c_grn=$'\e[32m'; c_off=$'\e[0m'
step() { echo; echo "${c_bold}==> $*${c_off}"; }
warn() { echo "${c_yel}WARNING: $*${c_off}"; }
die()  { echo "${c_red}ERROR: $*${c_off}" >&2; exit 1; }
ask()  { local p=$1 d=${2:-} v; read -r -p "$p${d:+ [$d]}: " v </dev/tty; echo "${v:-$d}"; }

[[ $EUID -eq 0 ]] || die "run as root: sudo $0"
grep -qi "bookworm" /etc/os-release || warn "this installer targets Raspberry Pi OS Bookworm"
grep -qi "raspberry pi" /proc/device-tree/model 2>/dev/null || warn "this does not look like a Raspberry Pi"

# ---------------------------------------------------------------- packages
step "Installing system packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip python3-gpiozero python3-lgpio \
    avahi-daemon network-manager git rsync sqlite3 iputils-ping raspi-utils >/dev/null \
    || apt-get install -y -qq python3 python3-venv python3-pip python3-gpiozero python3-lgpio \
       avahi-daemon network-manager git rsync sqlite3 iputils-ping >/dev/null
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' || die "Python 3.11+ required"
command -v pinctrl >/dev/null || warn "pinctrl not found: the ExecStopPost relay latch will fall back to raspi-gpio"

# ---------------------------------------------------------------- user
step "Creating service user '$SVC_USER'"
if ! id "$SVC_USER" >/dev/null 2>&1; then
    useradd --system --home-dir "$STATE_DIR" --no-create-home --shell /usr/sbin/nologin "$SVC_USER"
fi
getent group gpio >/dev/null || groupadd --system gpio
usermod -aG gpio "$SVC_USER"

# ---------------------------------------------------------------- code + venv
step "Installing application to $APP_DIR"
if [[ "$SRC_DIR" != "$APP_DIR" ]]; then
    mkdir -p "$APP_DIR"
    rsync -a --delete --exclude '.venv' --exclude '.dev-state' --exclude '__pycache__' \
        --exclude '*.egg-info' --exclude '.pytest_cache' "$SRC_DIR"/ "$APP_DIR"/
fi
chown -R root:root "$APP_DIR"
chmod 755 "$APP_DIR/deploy/"*.sh
if [[ ! -x "$VENV/bin/python" ]]; then
    # system site packages: use apt's gpiozero + lgpio (most reliable on Bookworm)
    python3 -m venv --system-site-packages "$VENV"
fi
"$VENV/bin/pip" install -q --upgrade pip
"$VENV/bin/pip" install -q "$APP_DIR"
ln -sf "$VENV/bin/dysonctl" /usr/local/bin/dysonctl

# ---------------------------------------------------------------- dirs + config
step "Config and state directories"
install -d -o root -g "$SVC_USER" -m 0750 "$ETC_DIR"
install -d -o "$SVC_USER" -g "$SVC_USER" -m 0750 "$STATE_DIR" "$LOG_DIR"
if [[ ! -f "$CONFIG" ]]; then
    install -o root -g "$SVC_USER" -m 0640 "$APP_DIR/config.example.yaml" "$CONFIG"
    echo "Created $CONFIG from the example. Review it (relay polarity!) then re-run this script."
else
    echo "Keeping existing $CONFIG"
fi
"$VENV/bin/dysonctl" --config "$CONFIG" config check || die "fix $CONFIG and re-run"
cfg() { "$VENV/bin/dysonctl" --config "$CONFIG" config get "$1"; }

RELAY_PIN=$(cfg hardware.relay_pin)
PRESENCE_PIN=$(cfg hardware.presence_pin)
if [[ "$(cfg hardware.relay_active_low)" == "true" ]]; then OFF_LEVEL=dh; else OFF_LEVEL=dl; fi
printf 'RELAY_PIN=%s\nRELAY_OFF_LEVEL=%s\n' "$RELAY_PIN" "$OFF_LEVEL" > "$ETC_DIR/relay-off.env"
chmod 0644 "$ETC_DIR/relay-off.env"
"$APP_DIR/deploy/relay-off.sh" || true

TZ_NAME=$(cfg scheduler.timezone)
timedatectl set-timezone "$TZ_NAME" || warn "could not set timezone $TZ_NAME"
systemctl enable --now systemd-timesyncd >/dev/null 2>&1 || warn "systemd-timesyncd not available (NTP)"

# ---------------------------------------------------------------- dashboard password
step "Dashboard password"
if [[ -n "${DYSON_PASSWORD:-}" ]]; then
    printf '%s\n' "$DYSON_PASSWORD" | runuser -u "$SVC_USER" -- "$VENV/bin/dysonctl" --config "$CONFIG" set-password --stdin
else
    runuser -u "$SVC_USER" -- "$VENV/bin/dysonctl" --config "$CONFIG" set-password --if-unset </dev/tty
fi

# ---------------------------------------------------------------- hotspot Wi-Fi
CONN=$(cfg network.connection_name)
IFACE=$(cfg network.interface)
if [[ "${DYSON_SKIP_WIFI:-0}" != "1" ]]; then
    step "Phone hotspot Wi-Fi ($CONN)"
    echo "${c_bold}The Pi 3 Model B only supports 2.4 GHz Wi-Fi.${c_off}"
    echo "  iPhone:  Settings > Personal Hotspot > turn ON 'Maximize Compatibility'"
    echo "  Android: Hotspot > AP band > 2.4 GHz"
    COUNTRY=${DYSON_WIFI_COUNTRY:-}
    [[ -z "$COUNTRY" ]] && COUNTRY=$(ask "Wi-Fi country code" "HU")
    if command -v raspi-config >/dev/null; then
        raspi-config nonint do_wifi_country "$COUNTRY" || warn "could not set Wi-Fi country"
    fi
    rfkill unblock wifi 2>/dev/null || true
    if nmcli -t -f NAME connection show | grep -qx "$CONN" && [[ $RECONFIGURE_WIFI -eq 0 && -z "${DYSON_WIFI_SSID:-}" ]]; then
        echo "Connection '$CONN' exists; keeping it (use --reconfigure-wifi to change SSID/password)."
    else
        SSID=${DYSON_WIFI_SSID:-}
        PSK=${DYSON_WIFI_PSK:-}
        [[ -z "$SSID" ]] && SSID=$(ask "Hotspot SSID (exact name shown on the phone)")
        if [[ -z "$PSK" ]]; then read -r -s -p "Hotspot password: " PSK </dev/tty; echo; fi
        [[ -n "$SSID" && ${#PSK} -ge 8 ]] || die "SSID required and WPA password must be 8+ chars"
        nmcli connection delete "$CONN" >/dev/null 2>&1 || true
        nmcli connection add type wifi ifname "$IFACE" con-name "$CONN" ssid "$SSID" >/dev/null
        nmcli connection modify "$CONN" \
            wifi-sec.key-mgmt wpa-psk wifi-sec.psk "$PSK" \
            802-11-wireless.band bg \
            802-11-wireless.powersave 2 \
            connection.autoconnect yes \
            connection.autoconnect-priority 100 \
            connection.autoconnect-retries 0
        echo "Created '$CONN': autoconnect, priority 100, retries forever, 2.4 GHz only, power-save off."
        echo "It connects automatically whenever the hotspot is in range."
        warn "If you are connected over another Wi-Fi right now, the Pi may switch to the hotspot later."
    fi
fi

# ---------------------------------------------------------------- hostname + mDNS
step "Hostname and mDNS"
HOST=${DYSON_HOSTNAME:-dyson-pi}
if [[ "$(hostname)" != "$HOST" ]]; then
    OLD=$(hostname)
    hostnamectl set-hostname "$HOST"
    if grep -q '^127\.0\.1\.1' /etc/hosts; then
        sed -i "s/^127\.0\.1\.1.*/127.0.1.1\t$HOST/" /etc/hosts
    else
        printf '127.0.1.1\t%s\n' "$HOST" >> /etc/hosts
    fi
    echo "Hostname changed: $OLD -> $HOST"
fi
install -m 0644 "$APP_DIR/deploy/avahi/dyson-pi.service" /etc/avahi/services/dyson-pi.service
systemctl enable avahi-daemon >/dev/null
systemctl restart avahi-daemon

# ---------------------------------------------------------------- boot config
step "Boot config (hardware watchdog, GPIO power-on states)"
BOOTCFG=/boot/firmware/config.txt
[[ -f $BOOTCFG ]] || BOOTCFG=/boot/config.txt
REBOOT_NEEDED=0
if [[ -f $BOOTCFG ]]; then
    BLOCK=$(cat <<EOF
# BEGIN dyson-pi (managed by /opt/dyson-pi/deploy/install.sh - do not edit inside)
[all]
dtparam=watchdog=on
# Hold the relay OFF from power-on until the service starts
gpio=${RELAY_PIN}=op,${OFF_LEVEL}
# Presence contact: input with pull-up
gpio=${PRESENCE_PIN}=ip,pu
# END dyson-pi
EOF
)
    TMP=$(mktemp)
    sed '/^# BEGIN dyson-pi/,/^# END dyson-pi/d' "$BOOTCFG" > "$TMP"
    printf '%s\n' "$BLOCK" >> "$TMP"
    if ! cmp -s "$TMP" "$BOOTCFG"; then
        cp "$BOOTCFG" "$BOOTCFG.dyson-pi.bak"
        cat "$TMP" > "$BOOTCFG"
        REBOOT_NEEDED=1
        echo "Updated $BOOTCFG (backup: $BOOTCFG.dyson-pi.bak)"
    fi
    rm -f "$TMP"
else
    warn "boot config.txt not found; add dtparam=watchdog=on manually"
fi
install -d /etc/systemd/system.conf.d
install -m 0644 "$APP_DIR/deploy/systemd/watchdog.conf" /etc/systemd/system.conf.d/dyson-pi-watchdog.conf

# ---------------------------------------------------------------- systemd
step "systemd units"
for u in dyson-pi.service dyson-pi-netwatch.service dyson-pi-netwatch.timer; do
    install -m 0644 "$APP_DIR/deploy/systemd/$u" "/etc/systemd/system/$u"
done
systemctl daemon-reload
systemctl daemon-reexec   # pick up RuntimeWatchdogSec
systemctl enable dyson-pi.service dyson-pi-netwatch.timer >/dev/null
systemctl restart dyson-pi.service
systemctl restart dyson-pi-netwatch.timer

# ---------------------------------------------------------------- verify
step "Checking the service"
ok=0
for _ in $(seq 1 30); do
    if "$VENV/bin/dysonctl" --config "$CONFIG" health >/dev/null 2>&1; then ok=1; break; fi
    sleep 1
done
IP=$(hostname -I 2>/dev/null | awk '{print $1}')
if [[ $ok -eq 1 ]]; then
    echo "${c_grn}Service is up.${c_off}"
else
    warn "service did not answer yet: journalctl -u dyson-pi -e"
fi
echo
echo "Dashboard:  http://$HOST.local/   or   http://${IP:-<pi-ip>}/"
echo "CLI:        sudo dysonctl status | sudo dysonctl test sensor | sudo dysonctl test relay"
echo "API token:  sudo dysonctl token create iphone"
if [[ $REBOOT_NEEDED -eq 1 ]]; then
    echo
    echo "${c_yel}${c_bold}Reboot required${c_off}${c_yel} to apply the hardware watchdog and GPIO power-on states: sudo reboot${c_off}"
fi
