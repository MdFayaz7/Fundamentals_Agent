#!/usr/bin/env bash
# Join Wi-Fi with a fixed IP (Ubuntu / netplan). Asks for the Wi-Fi name and password; the
# password is never shown or logged. Ethernet keeps working, so you can unplug it afterwards.
#   ./deploy/wifi-static.sh [IP] [GATEWAY]      defaults: 192.168.25.50 192.168.25.1
set -euo pipefail
IP="${1:-192.168.25.50}"
GW="${2:-192.168.25.1}"

read -rp  "Wi-Fi name (SSID): " SSID
read -rsp "Wi-Fi password: " PASS; echo
yaml_quote() { printf "'%s'" "${1//\'/\'\'}"; }   # YAML single-quoted string

sudo apt-get install -y -q iw >/dev/null
sudo iw reg set IN || true
sudo tee /etc/netplan/60-wifi.yaml >/dev/null <<EOF
network:
  version: 2
  wifis:
    wlan0:
      regulatory-domain: "IN"
      dhcp4: false
      addresses: [${IP}/24]
      routes: [{to: default, via: ${GW}, metric: 50}]
      nameservers: {addresses: [${GW}, 1.1.1.1, 8.8.8.8]}
      access-points:
        $(yaml_quote "$SSID"):
          auth:
            key-management: psk     # plain WPA2; the Pi's Broadcom chip is unreliable with WPA3/SAE
            password: $(yaml_quote "$PASS")
EOF
sudo chmod 600 /etc/netplan/60-wifi.yaml
unset PASS

# Keep Wi-Fi awake 24/7 (power saving causes drop-outs on the Pi).
sudo tee /etc/systemd/system/wifi-powersave-off.service >/dev/null <<'EOF'
[Unit]
Description=Disable Wi-Fi power saving
After=sys-subsystem-net-devices-wlan0.device
[Service]
Type=oneshot
ExecStart=/usr/sbin/iw dev wlan0 set power_save off
[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable -q wifi-powersave-off.service
sudo netplan apply
sleep 8
sudo systemctl start wifi-powersave-off.service || true
ip -4 -br addr show wlan0
echo "If wlan0 shows ${IP}, test from the Mac:  ssh $(id -un)@${IP}  — then unplug Ethernet."
