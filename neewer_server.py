#!/usr/bin/env python3
"""
Neewer GL1 Pro WiFi HTTP API Server

A lightweight HTTP server that controls a Neewer GL1 Pro over WiFi
using the reverse-engineered UDP protocol from braintapper/neewer-gl1.

No Node.js required — pure Python, zero dependencies.

Usage:
    python3 neewer-gl1-server.py --light-ip 192.168.1.XXX [--port 8182] [--client-ip 192.168.1.251]

API endpoints (all GET):
    /api/on                          - Turn light on
    /api/off                         - Turn light off
    /api/set?bri=50&temp=56          - Set brightness (1-100) and temp (29-70)
    /api/preset/daylight             - Full bright daylight (5600K, 100%)
    /api/preset/warm                 - Warm dim (3200K, 30%)
    /api/preset/zoom                 - Zoom call (4500K, 70%)
    /api/preset/recording            - Recording (4800K, 90%)
    /api/status                      - Show current state (local only, not from light)
"""

import argparse
import json
import socket
import time
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

LIGHT_PORT = 5052
HANDSHAKE_REPEAT = 3
DEFAULT_DELAY = 0.5  # seconds between UDP commands

PRESETS = {
    "daylight":  {"bri": 100, "temp": 56, "power": "on"},
    "warm":      {"bri": 30,  "temp": 32, "power": "on"},
    "zoom":      {"bri": 70,  "temp": 45, "power": "on"},
    "recording": {"bri": 90,  "temp": 48, "power": "on"},
    "dim":       {"bri": 10,  "temp": 56, "power": "on"},
    "off":       {"power": "off"},
}


class NeewerGL1:
    """Controls a Neewer GL1 Pro over UDP."""

    def __init__(self, light_ip, client_ip=None, delay=DEFAULT_DELAY):
        self.light_ip = light_ip
        self.client_ip = client_ip or self._guess_ip()
        self.delay = delay
        self.lock = threading.Lock()
        self.state = {"power": "unknown", "brightness": None, "temperature": None}

    def _guess_ip(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
        finally:
            s.close()
        return ip

    def _ip_to_handshake_hex(self):
        ip_hex = self.client_ip.encode("ascii").hex()
        length = len(self.client_ip)
        return f"80021000000{length:x}{ip_hex}2e"

    def _send_udp(self, hex_data):
        data = bytes.fromhex(hex_data)
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.sendto(data, (self.light_ip, LIGHT_PORT))
        finally:
            sock.close()

    def _handshake(self):
        hex_cmd = self._ip_to_handshake_hex()
        for _ in range(HANDSHAKE_REPEAT):
            self._send_udp(hex_cmd)
            time.sleep(self.delay)

    def _build_brightness_temp_hex(self, brightness, temperature):
        prefix = [0x80, 0x05, 0x03, 0x02]
        bri = max(1, min(100, brightness))
        temp = max(29, min(70, temperature))
        payload = prefix + [bri, temp]
        checksum = sum(payload) & 0xFF
        return "".join(f"{b:02x}" for b in payload) + f"{checksum:02x}"

    def power_on(self):
        with self.lock:
            self._handshake()
            self._send_udp("800502010189")
            self.state["power"] = "on"
            time.sleep(self.delay)

    def power_off(self):
        with self.lock:
            self._handshake()
            self._send_udp("800502010088")
            self.state["power"] = "off"
            self.state["brightness"] = None
            self.state["temperature"] = None
            time.sleep(self.delay)

    def set_brightness_temp(self, brightness, temperature):
        with self.lock:
            self._handshake()
            hex_cmd = self._build_brightness_temp_hex(brightness, temperature)
            self._send_udp(hex_cmd)
            self.state["brightness"] = brightness
            self.state["temperature"] = temperature * 100  # display as Kelvin
            time.sleep(self.delay)

    def apply_preset(self, name):
        preset = PRESETS.get(name)
        if not preset:
            return False
        if preset.get("power") == "off":
            self.power_off()
        else:
            self.power_on()
            if "bri" in preset and "temp" in preset:
                self.set_brightness_temp(preset["bri"], preset["temp"])
        return True


class GL1Handler(BaseHTTPRequestHandler):
    """HTTP request handler for the GL1 API."""

    light = None  # set after class creation

    def _respond(self, status, data):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data, indent=2).encode())

    def log_message(self, format, *args):
        print(f"[{time.strftime('%H:%M:%S')}] {args[0]}")

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        params = parse_qs(parsed.query)

        if path == "/api/on":
            self.light.power_on()
            self._respond(200, {"action": "on", "state": self.light.state})

        elif path == "/api/off":
            self.light.power_off()
            self._respond(200, {"action": "off", "state": self.light.state})

        elif path == "/api/set":
            bri = int(params.get("bri", [100])[0])
            temp = int(params.get("temp", [56])[0])
            self.light.power_on()
            self.light.set_brightness_temp(bri, temp)
            self._respond(200, {
                "action": "set",
                "brightness": bri,
                "temperature": temp * 100,
                "state": self.light.state,
            })

        elif path.startswith("/api/preset/"):
            name = path.split("/")[-1]
            if self.light.apply_preset(name):
                self._respond(200, {
                    "action": "preset",
                    "preset": name,
                    "config": PRESETS[name],
                    "state": self.light.state,
                })
            else:
                self._respond(404, {
                    "error": f"Unknown preset: {name}",
                    "available": list(PRESETS.keys()),
                })

        elif path == "/api/status":
            self._respond(200, {
                "light_ip": self.light.light_ip,
                "client_ip": self.light.client_ip,
                "state": self.light.state,
                "presets": list(PRESETS.keys()),
            })

        elif path == "/api/presets":
            self._respond(200, {"presets": PRESETS})

        else:
            self._respond(404, {
                "error": "Unknown endpoint",
                "endpoints": [
                    "GET /api/on",
                    "GET /api/off",
                    "GET /api/set?bri=50&temp=56",
                    "GET /api/preset/{name}",
                    "GET /api/presets",
                    "GET /api/status",
                ],
            })


def main():
    parser = argparse.ArgumentParser(description="Neewer GL1 Pro WiFi HTTP API Server")
    parser.add_argument("--light-ip", required=True, help="IP address of the GL1 Pro")
    parser.add_argument("--client-ip", default=None, help="This machine's IP (auto-detected if omitted)")
    parser.add_argument("--port", type=int, default=8182, help="HTTP server port (default: 8182)")
    parser.add_argument("--delay", type=float, default=0.5, help="Delay between UDP commands in seconds")
    args = parser.parse_args()

    light = NeewerGL1(args.light_ip, client_ip=args.client_ip, delay=args.delay)
    GL1Handler.light = light

    server = HTTPServer(("", args.port), GL1Handler)
    print(f"Neewer GL1 Pro API server")
    print(f"  Light IP:  {light.light_ip}")
    print(f"  Client IP: {light.client_ip}")
    print(f"  Listening: http://0.0.0.0:{args.port}")
    print(f"  Endpoints: /api/on /api/off /api/set?bri=&temp= /api/preset/{{name}} /api/status")
    print()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.server_close()


if __name__ == "__main__":
    main()
