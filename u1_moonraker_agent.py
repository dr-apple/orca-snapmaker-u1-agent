# /// script
# requires-python = ">=3.12"
# dependencies = []
#
# [tool.orcaslicer.plugin]
# name = "Snapmaker U1 Agent"
# description = "LAN printing for the Snapmaker U1 via Moonraker, with print options (timelapse, flow calibration, bed leveling), filament slot sync, printer status, camera and a U1 device tab."
# author = "dr-apple"
# version = "0.6.0"
# ///
"""Printer agent for the Snapmaker U1 (OrcaSlicer 2.5 printer-agent API).

The agent speaks Moonraker and translates it to the Bambu-style JSON the host expects:
- status: polls Moonraker and pushes `push_status` messages (state, temperatures, progress,
  storage, capability flags and the four filament slots as one AMS unit);
- jobs: extracts the plate G-code from the sliced 3MF, uploads it and starts it with the U1's
  own `server.files.start_local_print`, which carries the print options and the
  filament-to-slot table (the same request Snapmaker Orca sends to firmware 1.3.0 and later);
- commands: translates the host's `gcode_line`, pause/resume/stop, temperature and homing
  commands to Moonraker.

All network and file access runs on the plugin's own threads; the host only declares one
threading permission up front.
"""
import base64
import concurrent.futures
import http.client
import json
import os
import queue
import re
import socket
import ssl
import struct
import sys
import threading
import time
import urllib.parse
import uuid
import zipfile

import orca

# Writing stdlib bytecode caches into the app bundle trips the plugin audit hook
sys.dont_write_bytecode = True

pa = orca.printer_agent

AGENT_ID = "u1_moonraker"
AGENT_NAME = "Snapmaker U1 (Moonraker)"
AGENT_VERSION = "0.6.0"
# model_id of the Snapmaker U1 in Orca's Snapmaker vendor profile
MODEL_ID = "797581801"
DEFAULT_PORT = 7125
# The host passes this access code when the preset has no API key
PLACEHOLDER_ACCESS_CODE = "88888888"

POLL_INTERVAL = 2.0
SYNC_SETTLE = 1.5
RECONNECT_INTERVAL = 5.0
HTTP_TIMEOUT = 10
GCODE_TIMEOUT = 120
UPLOAD_TIMEOUT = 600
UPLOAD_CHUNK = 256 * 1024

OK = 0
FAILED = -1
NOT_SUPPORTED = -7010

# SendingPrintJobStage (bambu_networking.hpp)
STAGE_CREATE = 0
STAGE_UPLOAD = 1
STAGE_SENDING = 3
STAGE_FINISHED = 6

# home_flag bits read by MachineObject::parse_home_flag
HOME_X, HOME_Y, HOME_Z = 1 << 0, 1 << 1, 1 << 2
STORAGE_NORMAL = 1 << 8
FLOW_CALI_SUPPORTED = 1 << 15
PA_CALI_SUPPORTED = 1 << 16

TOOL_HEATERS = ("extruder", "extruder1", "extruder2", "extruder3")
# Nozzle material of the U1 0.4 presets (Moonraker does not report it)
NOZZLE_TYPE = "hardened_steel"

# Highest targets the U1 tab sets: 10 / 5 °C below the U1's Klipper max_temp shutdown (300 / 100 °C)
TEMP_LIMITS = {**{name: 290 for name in TOOL_HEATERS}, "heater_bed": 95}
JOG_STEPS = {0.1, 1.0, 10.0}
EXTRUDE_STEPS = {1.0, 10.0}

# The live agent instance, read by the U1 tab
_active_agent = None

STATUS_OBJECTS = ("print_stats", "virtual_sdcard", "heater_bed", "fan", "toolhead", *TOOL_HEATERS, "print_task_config")


def log(msg):
    # OrcaSlicer tees stderr (not stdout) into log/python_*.log
    print(f"[{AGENT_ID}] {msg}", file=sys.stderr, flush=True)


class Cancelled(Exception):
    pass


# ---------------------------------------------------------------------------
# Moonraker transport
# ---------------------------------------------------------------------------

class WebSocket:
    """Minimal RFC 6455 client: enough for one JSON-RPC exchange with Moonraker."""

    def __init__(self, sock):
        self.sock = sock
        self.buf = b""

    def _fill(self):
        chunk = self.sock.recv(65536)
        if not chunk:
            raise ConnectionError("websocket closed")
        self.buf += chunk

    def _read(self, n):
        while len(self.buf) < n:
            self._fill()
        data, self.buf = self.buf[:n], self.buf[n:]
        return data

    def handshake(self, host, port, headers):
        key = base64.b64encode(os.urandom(16)).decode()
        extra = "".join(f"{k}: {v}\r\n" for k, v in headers.items())
        self.sock.sendall(
            (
                f"GET /websocket HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\n"
                f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n"
                f"{extra}\r\n"
            ).encode()
        )
        while b"\r\n\r\n" not in self.buf:
            self._fill()
        head, self.buf = self.buf.split(b"\r\n\r\n", 1)
        status_line = head.split(b"\r\n", 1)[0]
        if b" 101 " not in status_line:
            raise ConnectionError(f"websocket handshake failed: {status_line.decode(errors='replace')}")

    def send(self, payload, opcode=0x1):
        mask = os.urandom(4)
        n = len(payload)
        if n < 126:
            header = struct.pack("!BB", 0x80 | opcode, 0x80 | n)
        elif n < 1 << 16:
            header = struct.pack("!BBH", 0x80 | opcode, 0x80 | 126, n)
        else:
            header = struct.pack("!BBQ", 0x80 | opcode, 0x80 | 127, n)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(header + mask + masked)

    def recv_text(self):
        """Return the next complete text message, answering pings on the way."""
        parts = []
        while True:
            b1, b2 = self._read(2)
            opcode = b1 & 0x0F
            n = b2 & 0x7F
            if n == 126:
                n = struct.unpack("!H", self._read(2))[0]
            elif n == 127:
                n = struct.unpack("!Q", self._read(8))[0]
            mask = self._read(4) if b2 & 0x80 else None
            data = self._read(n)
            if mask:
                data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
            if opcode == 0x8:
                raise ConnectionError("websocket closed by printer")
            if opcode == 0x9:
                self.send(data, 0xA)
                continue
            if opcode == 0xA:
                continue
            parts.append(data)
            if b1 & 0x80:
                return b"".join(parts).decode()


class Moonraker:
    def __init__(self, host, port, use_ssl, api_key):
        self.host = host
        self.port = int(port) if str(port or "").isdigit() else DEFAULT_PORT
        self.use_ssl = use_ssl
        # An unknown X-Api-Key is rejected even when Moonraker needs no auth, so only send a real key
        self.headers = {"X-Api-Key": api_key} if api_key and api_key != PLACEHOLDER_ACCESS_CODE else {}

    def _connection(self, timeout):
        if self.use_ssl:
            return http.client.HTTPSConnection(self.host, self.port, timeout=timeout,
                                               context=ssl.create_default_context())
        return http.client.HTTPConnection(self.host, self.port, timeout=timeout)

    def request(self, method, path, payload=None, timeout=HTTP_TIMEOUT):
        headers = dict(self.headers)
        body = None
        if payload is not None:
            body = json.dumps(payload).encode()
            headers["Content-Type"] = "application/json"
        conn = self._connection(timeout)
        try:
            conn.request(method, path, body=body, headers=headers)
            resp = conn.getresponse()
            data = resp.read()
        finally:
            conn.close()
        if resp.status >= 300:
            # Moonraker explains failures (e.g. Klipper's "Must home axis first") in error.message
            try:
                message = json.loads(data)["error"]["message"]
            except (ValueError, KeyError, TypeError):
                message = data[:200].decode(errors="replace")
            raise RuntimeError(f"HTTP {resp.status} for {path}: {message}")
        return json.loads(data).get("result", {}) if data else {}

    def query(self, objects):
        return self.request("GET", "/printer/objects/query?" + "&".join(objects)).get("status", {})

    def gcode(self, script):
        return self.request("POST", "/printer/gcode/script", {"script": script}, timeout=GCODE_TIMEOUT)

    def upload(self, filename, payload, progress=None, cancelled=None):
        """Upload G-code to the gcodes root and return the path Moonraker stored it under."""
        boundary = uuid.uuid4().hex
        head = (
            f'--{boundary}\r\nContent-Disposition: form-data; name="root"\r\n\r\ngcodes\r\n'
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
            f'filename="{filename}"\r\nContent-Type: application/octet-stream\r\n\r\n'
        ).encode()
        tail = f"\r\n--{boundary}--\r\n".encode()
        view = memoryview(payload)
        conn = self._connection(UPLOAD_TIMEOUT)
        try:
            conn.putrequest("POST", "/server/files/upload")
            for key, value in self.headers.items():
                conn.putheader(key, value)
            conn.putheader("Content-Type", f"multipart/form-data; boundary={boundary}")
            conn.putheader("Content-Length", str(len(head) + len(view) + len(tail)))
            conn.endheaders()
            conn.send(head)
            for offset in range(0, len(view), UPLOAD_CHUNK):
                if cancelled and cancelled():
                    raise Cancelled()
                conn.send(view[offset:offset + UPLOAD_CHUNK])
                if progress:
                    progress(min(100, (offset + UPLOAD_CHUNK) * 100 // max(1, len(view))))
            conn.send(tail)
            resp = conn.getresponse()
            data = resp.read()
        finally:
            conn.close()
        if resp.status >= 300:
            raise RuntimeError(f"upload failed with HTTP {resp.status}: {data[:200].decode(errors='replace')}")
        result = json.loads(data)
        return result.get("result", result).get("item", {}).get("path", filename)

    def rpc(self, method, params, timeout=30):
        """One JSON-RPC call over the websocket; some U1 methods exist only there."""
        sock = socket.create_connection((self.host, self.port), timeout=timeout)
        if self.use_ssl:
            sock = ssl.create_default_context().wrap_socket(sock, server_hostname=self.host)
        ws = WebSocket(sock)
        try:
            ws.handshake(self.host, self.port, self.headers)
            req_id = uuid.uuid4().int % 1_000_000_000
            ws.send(json.dumps({"jsonrpc": "2.0", "method": method, "params": params, "id": req_id}).encode())
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                msg = json.loads(ws.recv_text())
                if msg.get("id") != req_id:
                    continue  # status notifications
                if "error" in msg:
                    error = msg["error"]
                    raise RuntimeError(error.get("message", error) if isinstance(error, dict) else error)
                result = msg.get("result")
                # Snapmaker's methods report failures in the result: {"state": "error", "message": ...}
                if isinstance(result, dict) and result.get("state") == "error":
                    raise RuntimeError(result.get("message", "printer reported an error"))
                return result
            raise TimeoutError(f"{method}: no answer within {timeout}s")
        finally:
            try:
                ws.send(b"", 0x8)
            except OSError:
                pass
            sock.close()


# ---------------------------------------------------------------------------
# Moonraker -> Bambu-style JSON
# ---------------------------------------------------------------------------

def combine_filament_type(ftype, sub_type):
    """The slot's material as Orca's filament_type names it.

    The send dialog only lets a filament map to a slot of exactly its filament_type, so variants
    without a type of their own (SnapSpeed, Silk, Matte, ...) report their base material; only
    fibre fills are separate types (PLA-CF, PETG-GF, ...).
    """
    base = (ftype or "").strip().upper()
    sub = (sub_type or "").strip().upper()
    if not base:
        return "PLA"
    return base + {"CF": "-CF", "GF": "-GF"}.get(sub, "")


def normalize_color(rgba):
    color = (rgba or "").lstrip("#").upper()
    if len(color) == 6:
        color += "FF"
    return color if len(color) == 8 else "FFFFFFFF"


def build_trays(task_config):
    """Slots from print_task_config as dicts with slot, exists, type, vendor and color."""
    exist = task_config.get("filament_exist") or []

    def at(key, i, default=""):
        values = task_config.get(key) or []
        return values[i] if i < len(values) else default

    return [
        {
            "slot": i,
            "exists": bool(present),
            "type": combine_filament_type(at("filament_type", i), at("filament_sub_type", i)),
            "sub_type": at("filament_sub_type", i),
            "vendor": at("filament_vendor", i),
            "color": normalize_color(at("filament_color_rgba", i)),
        }
        for i, present in enumerate(exist)
    ]


def build_ams(trays, known_filaments):
    """The slots as one AMS lite unit, as MoonrakerPrinterAgent::build_ams_payload builds it."""
    tray_json = []
    tray_exist_bits = 0
    for tray in trays[:4]:
        entry = {"id": str(tray["slot"]), "tag_uid": "0000000000000000"}
        if tray["exists"]:
            tray_exist_bits |= 1 << tray["slot"]
            entry.update(
                tray_info_idx=match_filament_id(tray, known_filaments),
                tray_type=tray["type"],
                tray_color=tray["color"],
            )
        else:
            entry.update(tray_info_idx="", tray_type="", tray_color="00000000", tray_slot_placeholder="1")
        tray_json.append(entry)
    return {
        "ams": [{"id": "0", "info": "0002", "tray": tray_json}],
        "ams_exist_bits": "1",
        "tray_exist_bits": format(tray_exist_bits, "X"),
    }


GCODE_STATES = {"printing": "RUNNING", "paused": "PAUSE", "complete": "FINISH",
                "error": "FAILED", "cancelled": "FAILED"}
PRINT_STAGES = {"printing": 1, "paused": 2, "complete": 3, "error": 4}


def build_status(status, known_filaments):
    stats = status.get("print_stats") or {}
    state = str(stats.get("state") or "standby").lower()
    toolhead = status.get("toolhead") or {}
    homed = str(toolhead.get("homed_axes") or "").upper()

    home_flag = STORAGE_NORMAL | FLOW_CALI_SUPPORTED | PA_CALI_SUPPORTED
    home_flag |= (HOME_X if "X" in homed else 0) | (HOME_Y if "Y" in homed else 0) | (HOME_Z if "Z" in homed else 0)

    report = {
        "command": "push_status",
        "msg": 0,
        "support_mqtt_alive": True,
        "gcode_state": GCODE_STATES.get(state, "IDLE"),
        "mc_print_stage": PRINT_STAGES.get(state, 0),
        "mc_print_error_code": 0,
        "print_error": 0,
        "home_flag": home_flag,
        "sdcard": True,
        "nozzle_temp_range": [100, 300],
        "bed_temp_range": [0, 100],
        "support_send_to_sd": True,
        "support_bed_leveling": 1,
        "support_flow_calibration": True,
        "support_timelapse": True,
    }

    extruder = status.get(toolhead.get("extruder") or "extruder") or status.get("extruder") or {}
    if isinstance(extruder.get("temperature"), (int, float)):
        report["nozzle_temper"] = extruder["temperature"]
    if isinstance(extruder.get("target"), (int, float)):
        report["nozzle_target_temper"] = extruder["target"]
    bed = status.get("heater_bed") or {}
    if isinstance(bed.get("temperature"), (int, float)):
        report["bed_temper"] = bed["temperature"]
    if isinstance(bed.get("target"), (int, float)):
        report["bed_target_temper"] = bed["target"]
    fan = status.get("fan") or {}
    if isinstance(fan.get("speed"), (int, float)):
        report["fan_gear"] = max(0, min(255, round(fan["speed"] * 255)))

    if stats.get("filename"):
        report["subtask_name"] = stats["filename"]
        report["gcode_file"] = stats["filename"]
    sdcard = status.get("virtual_sdcard") or {}
    if isinstance(sdcard.get("progress"), (int, float)):
        progress = sdcard["progress"]
        report["mc_percent"] = max(0, min(100, round(progress * 100)))
        elapsed = stats.get("print_duration")
        if state == "printing" and 0 < progress < 1 and isinstance(elapsed, (int, float)) and elapsed > 0:
            report["mc_remaining_time"] = int(elapsed / progress * (1 - progress) / 60)

    # One extruder only: the host's device tab draws at most two (more crash it), so the U1's four
    # toolheads are not reported. Tool 1's nozzle goes in the classic fields: the printer sync
    # button then reads 0.4 for its first nozzle and nothing for the second, so it leaves the
    # printer preset alone instead of switching to the system preset for an empty diameter.
    tool1 = status.get("extruder") or {}
    if isinstance(tool1.get("nozzle_diameter"), (int, float)):
        report["nozzle_diameter"] = float(tool1["nozzle_diameter"])
        report["nozzle_type"] = NOZZLE_TYPE

    trays = build_trays(status.get("print_task_config") or {})
    if trays:
        report["ams"] = build_ams(trays, known_filaments)

    return {"print": report, "t_utc": int(time.time() * 1000)}


# ---------------------------------------------------------------------------
# Presets (host thread only)
# ---------------------------------------------------------------------------

def parse_config_strings(value):
    """Split a serialized string list as Orca writes it (escape_strings_cstyle): `a;"b c";d`."""
    out, i, n = [], 0, len(value)
    while i < n:
        while i < n and value[i] in " \t":
            i += 1
        word = []
        if i < n and value[i] == '"':
            i += 1
            while i < n and value[i] != '"':
                c = value[i]
                if c == "\\" and i + 1 < n:
                    i += 1
                    c = {"r": "\r", "n": "\n"}.get(value[i], value[i])
                word.append(c)
                i += 1
            i += 1  # closing quote
        else:
            while i < n and value[i] != ";":
                word.append(value[i])
                i += 1
        out.append("".join(word))
        while i < n and value[i] != ";":
            i += 1
        i += 1  # separator
    return out


def _preset_value(preset, key):
    """First value of a preset option; the bindings return every option serialized as a string."""
    value = preset.config_value(key)
    if value is None:
        return ""
    values = parse_config_strings(str(value))
    return values[0] if values else ""


def printer_cameras(client):
    """The enabled printer cameras in Moonraker's webcam list as {"name", "url"} snapshot entries.

    The U1 streams over WebRTC, which the host cannot play, but its camera-streamer also serves
    JPEG snapshots. Relative URLs are served by the printer's web server, not by Moonraker's port.
    """
    try:
        webcams = client.request("GET", "/server/webcams/list").get("webcams", [])
    except Exception as e:
        log(f"no webcam list: {e}")
        return []
    cameras = []
    for cam in webcams:
        url = cam.get("snapshot_url") or ""
        # "iframe" entries are web pages (the U1's touchscreen mirror), not cameras
        if cam.get("enabled", True) and url and cam.get("service") != "iframe":
            if "://" not in url:
                scheme = "https" if client.use_ssl else "http"
                url = f"{scheme}://{client.host}/{url.lstrip('/')}"
            cameras.append({"name": cam.get("name") or f"Camera {len(cameras) + 1}", "url": url})
    log(f"cameras: {cameras}")
    return cameras


def job_thumbnail_url(client, filename):
    """URL of the largest thumbnail Moonraker extracted from a G-code file, or ""."""
    if not filename:
        return ""
    try:
        meta = client.request("GET", "/server/files/metadata?filename=" + urllib.parse.quote(filename))
    except Exception as e:
        log(f"no metadata for {filename!r}: {e}")
        return ""
    thumbs = [t for t in meta.get("thumbnails") or [] if t.get("relative_path")]
    if not thumbs:
        return ""
    best = max(thumbs, key=lambda t: t.get("width", 0) * t.get("height", 0))
    # Thumbnail paths are relative to the G-code file's folder
    path = os.path.join(os.path.dirname(filename), best["relative_path"])
    scheme = "https" if client.use_ssl else "http"
    return f"{scheme}://{client.host}:{client.port}/server/files/gcodes/{urllib.parse.quote(path)}"


def preset_print_host():
    """The edited printer preset's print host as a bare host name or IP."""
    try:
        preset = orca.host.preset_bundle().current_printer_preset()
        value = _preset_value(preset, "print_host")
    except Exception as e:
        log(f"cannot read print host from preset: {e}")
        return ""
    if "://" not in value:
        value = "http://" + value
    return urllib.parse.urlsplit(value.strip()).hostname or ""


def _rgb(color):
    try:
        value = int(color.lstrip("#")[:6], 16)
    except ValueError:
        return 0, 0, 0
    return value >> 16, (value >> 8) & 0xFF, value & 0xFF


def selected_filaments():
    """(vendor, type, rgb, filament_id) of the project's selected filaments. Host thread only.

    The plugin API does not expose a preset's filament_id, so real ids are known only for the
    selected filaments (the bundle's "filament_ids").
    """
    try:
        bundle = orca.host.preset_bundle()
        presets = bundle.current_filament_presets()
        ids = parse_config_strings(bundle.full_config_value("filament_ids") or "")
    except Exception as e:
        log(f"cannot read the selected filaments: {e}")
        return []
    return [
        (_preset_value(preset, "filament_vendor"), _preset_value(preset, "filament_type").upper(),
         _rgb(_preset_value(preset, "default_filament_colour")), filament_id)
        for preset, filament_id in zip(presets, ids)
        if preset is not None and filament_id
    ]


def match_filament_id(tray, known):
    """The filament preset id a slot reports for the host's filament sync.

    A selected filament of the same vendor and type, or else of the same type, gives its id, the
    closest colour winning. Otherwise a placeholder: the sync skips slots without an id, but for
    an unknown id it falls back to the "Generic <type>" preset.
    """
    target = _rgb(tray["color"])
    matches = [k for k in known if k[0] == tray["vendor"] and k[1] == tray["type"]] or \
              [k for k in known if k[1] == tray["type"]]
    if not matches:
        return f"U1_{tray['type']}"
    return min(matches, key=lambda k: sum((a - b) ** 2 for a, b in zip(k[2], target)))[3]


# Sub types the U1 reports for a spool without one
NO_SUB_TYPES = {"", "NONE", "GENERAL", "NORMAL"}


def _words(text):
    return set(re.findall(r"[A-Z0-9+]+", (text or "").upper()))


def _vendor_matches(tray_vendor, preset_vendor):
    """Equal, or one names the other ("Snapmaker" / "Snapmaker PLA Basic"), ignoring case."""
    a, b = _words(tray_vendor), _words(preset_vendor)
    return bool(a) and bool(b) and (a <= b or b <= a)


def filament_catalog():
    """Visible filament presets, as dicts with name, vendor, type, words, system, compatible and rgb.

    Host thread only. words are the name's words before "@", which carry the product line and
    finish ("Snapmaker PLA SnapSpeed @U1"); rgb is None for a preset without a colour.
    """
    try:
        filaments = orca.host.preset_bundle().filaments
        presets = [filaments.preset(i) for i in range(filaments.size())]
    except Exception as e:
        log(f"cannot read the filament presets: {e}")
        return []
    catalog = []
    for preset in presets:
        if not preset.is_visible or preset.is_default:
            continue
        color = _preset_value(preset, "default_filament_colour")
        catalog.append({
            "name": preset.name,
            "vendor": _preset_value(preset, "filament_vendor"),
            "type": _preset_value(preset, "filament_type").upper(),
            "words": _words(preset.name.split("@")[0]),
            "system": preset.is_system,
            "compatible": preset.is_compatible,
            "rgb": _rgb(color) if color else None,
        })
    return catalog


def match_slot_preset(tray, catalog):
    """Name of the compatible preset that fits a slot best, or "" when no preset has its type.

    The spool's vendor first, then Generic, then any other vendor; within that a name carrying
    the spool's sub type (SnapSpeed, Matte, HF, ...), then the plainest name (Snapmaker PLA
    before Snapmaker PLA Glow), a system preset, one made for a printer ("... @U1"), and finally
    the closest colour.
    """
    sub = (tray.get("sub_type") or "").strip().upper()
    sub_words = set() if sub in NO_SUB_TYPES or sub in ("CF", "GF") else _words(sub)
    type_words = _words(tray["type"])
    target = _rgb(tray["color"])

    def rank(preset):
        if _vendor_matches(tray["vendor"], preset["vendor"]):
            tier = 0
        elif preset["vendor"].strip().upper() == "GENERIC":
            tier = 1
        else:
            tier = 2
        extra = preset["words"] - _words(preset["vendor"]) - _words(tray["vendor"]) - type_words - sub_words
        distance = (sum((a - b) ** 2 for a, b in zip(preset["rgb"], target))
                    if preset["rgb"] is not None else 3 * 255 ** 2 + 1)
        return (tier, not sub_words <= preset["words"], len(extra), not preset["system"],
                "@" not in preset["name"], distance)

    candidates = [p for p in catalog if p["compatible"] and p["type"] == tray["type"]]
    return min(candidates, key=rank)["name"] if candidates else ""


def can_assign_presets():
    """Whether the host can select a preset for a filament slot (OrcaSlicer 2.5 beta and later)."""
    editor = getattr(getattr(orca.host, "preset", None), "filament", None)
    return hasattr(editor, "begin_update")


def plan_slot_presets(trays, catalog, assigned):
    """[(1-based slot, preset name)] for the slots whose assignment should change.

    assigned is the project's filament preset per slot. The host's sync gives the project either
    one filament per printer slot or one per loaded slot; any other count means the project's
    filaments do not mirror the printer, and nothing is planned. A slot is only reassigned to a
    preset of the type it already has, so a slot that does not hold that spool stays as it is.
    """
    loaded = [t for t in trays if t["exists"]]
    if len(assigned) == len(trays):
        pairs = [(t["slot"] + 1, t) for t in loaded]
    elif len(assigned) == len(loaded):
        pairs = [(i + 1, t) for i, t in enumerate(loaded)]
    else:
        log(f"preset assignment skipped: {len(assigned)} project filaments for {len(trays)} slots")
        return []
    types = {p["name"]: p["type"] for p in catalog}
    plan = []
    for slot, tray in pairs:
        current = assigned[slot - 1]
        if types.get(current) != tray["type"]:
            log(f"slot {slot}: project has {current!r}, printer {tray['type']}; left alone")
            continue
        name = match_slot_preset(tray, catalog)
        if name and name != current:
            plan.append((slot, name))
    return plan


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------

def remote_filename(params):
    base = params.project_name or params.task_name or os.path.splitext(os.path.basename(params.filename))[0]
    # The host's project name can carry the export suffixes, e.g. "Würfel.gcode.3mf"
    while base.lower().endswith((".gcode", ".3mf")):
        base = os.path.splitext(base)[0]
    base = "".join(c if (c.isalnum() or c in " -_.+()") else "_" for c in base).strip() or "job"
    plate = int(params.plate_index or 1)
    return f"{base}_plate{plate}.gcode" if plate > 1 else f"{base}.gcode"


def read_plate_gcode(path, plate_index):
    """The plate's G-code from a sliced 3MF (Metadata/plate_<n>.gcode, n from 1), or the file itself."""
    if not zipfile.is_zipfile(path):
        with open(path, "rb") as f:
            return f.read()
    plate = int(plate_index or 1)
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        wanted = f"Metadata/plate_{plate}.gcode"
        if wanted in names:
            return z.read(wanted)
        plates = sorted(n for n in names if n.startswith("Metadata/plate_") and n.endswith(".gcode"))
        if len(plates) == 1:
            return z.read(plates[0])
    raise RuntimeError(f"no G-code for plate {plate} in {os.path.basename(path)}")


def print_options(params, slot_count=4):
    """The options of server.files.start_local_print, as Snapmaker Orca sends them."""
    options = {
        "bed_level": int(bool(params.task_bed_leveling)),
        "flow_calibrate": int(bool(params.task_flow_cali)),
        "time_lapse_camera": int(bool(params.task_record_timelapse)),
    }
    try:
        mapping = json.loads(params.ams_mapping or "[]")
    except ValueError:
        mapping = []
    # ams_mapping[i] is the tray for filament i (T<i> in the G-code); the slots are AMS 0, trays 0-3
    table = [[i, int(tray)] for i, tray in enumerate(mapping)
             if isinstance(tray, (int, str)) and str(tray).lstrip("-").isdigit() and 0 <= int(tray) < slot_count]
    if table:
        options["map_table"] = table
    return options


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------

class U1Agent(pa.PrinterAgentBase):
    def __init__(self):
        super().__init__()
        self.callbacks = {}
        self.selected = ""
        self.lock = threading.Lock()
        self.client = None
        self.generation = 0
        self.dev_id = ""
        self.dev_ip = ""
        self.dev_name = "Snapmaker U1"
        self.version = ""
        self.status = {}
        # Read on host-thread calls, matched against the slots in every status push: the host's
        # filament sync reads the slots from the push before the one its own request triggers
        self.known_filaments = []
        self.camera_url = ""
        self.cameras = []
        self.thumbnail_for = None
        self.thumbnail_url = ""
        self.page_message = ""
        self.pool = None
        # The U1 tab reads the live agent; the host keeps one instance per agent id
        global _active_agent
        _active_agent = self

    def get_name(self):
        return AGENT_NAME

    def get_agent_info(self):
        return pa.AgentInfo(AGENT_ID, AGENT_NAME, AGENT_VERSION,
                            "Snapmaker U1 via Moonraker: print options, filament sync, status")

    # Host callbacks: store them, they may be None
    def _store(self, key, fn):
        self.callbacks[key] = fn
        return OK

    def _emit(self, key, *args):
        fn = self.callbacks.get(key)
        if fn is None:
            return
        try:
            fn(*args)
        except Exception as e:
            log(f"callback {key} failed: {e}")

    def set_on_ssdp_msg_fn(self, fn): return self._store("ssdp", fn)
    def set_on_local_connect_fn(self, fn): return self._store("local_connect", fn)
    def set_on_local_message_fn(self, fn): return self._store("local_message", fn)
    def set_on_message_fn(self, fn): return self._store("message", fn)
    def set_on_user_message_fn(self, fn): return self._store("user_message", fn)
    def set_on_printer_connected_fn(self, fn): return self._store("printer_connected", fn)
    def set_on_subscribe_failure_fn(self, fn): return self._store("subscribe_failure", fn)
    def set_server_callback(self, fn): return self._store("server_error", fn)
    def set_queue_on_main_fn(self, fn): return self._store("queue_on_main", fn)

    def _executor(self):
        if self.pool is None:
            self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix=AGENT_ID)
        return self.pool

    def _run(self, fn, *args, timeout=HTTP_TIMEOUT + 5):
        """Run fn on the plugin's worker threads and wait for it."""
        return self._executor().submit(fn, *args).result(timeout)

    def _send_local(self, payload):
        self._emit("local_message", self.dev_id, json.dumps(payload))

    def _push_status(self):
        with self.lock:
            payload = build_status(self.status, self.known_filaments)
        self._send_local(payload)

    def _announce(self):
        if not self.dev_id:
            # Before the first connect the host only knows the machine from our announcement,
            # so take it from the preset's print host
            host = preset_print_host()
            if not host:
                log("announce skipped: no print host in the printer preset")
                return
            self.dev_id = self.dev_ip = host
        log(f"announce {self.dev_id}")
        self._emit("ssdp", json.dumps({
            "dev_name": self.dev_name, "dev_id": self.dev_id, "dev_ip": self.dev_ip,
            "dev_type": MODEL_ID, "dev_signal": "0", "connect_type": "lan",
            "bind_state": "free", "sec_link": "secure", "ssdp_version": "v1",
        }))
        if not self.selected:
            self.selected = self.dev_id

    # Discovery: the printer comes from the preset's print host
    def start_discovery(self, start, sending):
        log(f"start_discovery start={start} sending={sending}")
        if start:
            self._announce()
        return True

    def get_user_selected_machine(self):
        return self.selected

    def set_user_selected_machine(self, dev_id):
        self.selected = dev_id or ""
        return OK

    # Connection
    def connect_printer(self, params):
        log(f"connect_printer dev_id={params.dev_id!r} host={params.host!r} port={params.port!r}")
        if not params.host:
            return FAILED
        client = Moonraker(params.host, params.port, params.use_ssl, params.password)
        known = selected_filaments()
        with self.lock:
            self.known_filaments = known
            self.generation += 1
            generation = self.generation
            self.client = client
            self.dev_id = params.dev_id or params.host
            self.dev_ip = params.host
            self.status = {}
        try:
            threading.Thread(target=self._connection_loop, args=(generation, client),
                             name=f"{AGENT_ID}-status", daemon=True).start()
        except PermissionError:
            log("thread permission denied or still pending; allow it and reconnect")
            return FAILED
        return OK

    def disconnect_printer(self):
        with self.lock:
            self.generation += 1
            self.client = None
        return OK

    def on_unload(self):
        global _active_agent
        if _active_agent is self:
            _active_agent = None
        self.disconnect_printer()
        if self.pool is not None:
            self.pool.shutdown(wait=False, cancel_futures=True)
            self.pool = None

    def _current(self, generation):
        return generation == self.generation

    def _connection_loop(self, generation, client):
        connected = False
        last_error = None
        while self._current(generation):
            try:
                if not connected:
                    info = client.request("GET", "/printer/info")
                    self.dev_name = info.get("hostname") or self.dev_name
                    self.version = info.get("software_version", "")
                    self.cameras = printer_cameras(client)
                    self.camera_url = self.cameras[0]["url"] if self.cameras else ""
                status = client.query(STATUS_OBJECTS)
                with self.lock:
                    if not self._current(generation):
                        return
                    self.status = status
                filename = (status.get("print_stats") or {}).get("filename") or ""
                if filename != self.thumbnail_for:
                    self.thumbnail_url = job_thumbnail_url(client, filename)
                    self.thumbnail_for = filename
                if not connected:
                    connected = True
                    log(f"connected to {client.host}:{client.port} ({self.version})")
                    self._announce()
                    self._emit("local_connect", 0, self.dev_id, "0")
                    self._emit("printer_connected", self.dev_id)
                self._push_status()
                last_error = None
                time.sleep(POLL_INTERVAL)
            except Exception as e:
                if str(e) != last_error:
                    log(f"status poll failed: {e}")
                    last_error = str(e)
                time.sleep(RECONNECT_INTERVAL)

    # Camera: the host shows the snapshot URL in a web view and reloads it several times a second
    def get_camera_stream_mode(self):
        return pa.CameraStreamMode.HTTP_SNAPSHOT if self.camera_url else pa.CameraStreamMode.None_

    def get_camera_url(self):
        return self.camera_url

    # Filament sync: the host asks, the slots arrive with the status push
    def get_filament_sync_mode(self):
        return pa.FilamentSyncMode.Pull

    def fetch_filament_info(self, dev_id, sync_mode):
        client = self.client
        if client is None:
            return False
        try:
            status = self._run(client.query, ("print_task_config",))
        except Exception as e:
            log(f"filament sync failed: {e}")
            return False
        task_config = status.get("print_task_config") or {}
        trays = build_trays(task_config)
        if not trays:
            return False
        known = selected_filaments()
        with self.lock:
            self.status["print_task_config"] = task_config
            self.known_filaments = known
        log(f"filament sync: {[(t['slot'], t['type'], match_filament_id(t, known)) for t in trays if t['exists']]}")
        self._push_status()
        if can_assign_presets():
            self._assign_after_sync(trays, filament_catalog())
        return True

    # Preset assignment (OrcaSlicer 2.5 beta): the host's sync can only pick the project's
    # filaments or "Generic <type>", so each synced slot then gets its best preset by name
    def _assign_after_sync(self, trays, catalog):
        def run():
            # Let the host finish applying its own sync first
            time.sleep(SYNC_SETTLE)
            try:
                # Off the host thread, but the host is done changing the list by now
                assigned = list(orca.host.preset_bundle().current_filament_preset_names())
            except Exception as e:
                log(f"preset assignment skipped: {e}")
                return
            self._assign_presets(plan_slot_presets(trays, catalog, assigned))

        self._async(run)

    def _assign_presets(self, plan):
        """Select each planned preset for its slot. Plugin thread only: the call blocks until the
        host has switched the slot, including any prompt about unsaved filament edits."""
        if not plan:
            self.page_message = "Filament-Profile passen bereits"
            return
        done = []
        for slot, name in plan:
            try:
                orca.host.preset.filament.begin_update(slot_number=slot, preset_name=name)
                done.append(f"{slot}: {name}")
                log(f"slot {slot} -> {name}")
            except Exception as e:
                log(f"slot {slot} -> {name} failed: {e}")
        self.page_message = "Profile zugewiesen: " + ", ".join(done) if done else "Profile nicht zugewiesen, siehe Log"

    # Commands from the device tab (Bambu JSON)
    def send_message(self, dev_id, json_str, qos, flag):
        return self._handle(json_str)

    def send_message_to_printer(self, dev_id, json_str, qos, flag):
        return self._handle(json_str)

    def _handle(self, json_str):
        try:
            msg = json.loads(json_str)
        except ValueError:
            return FAILED
        info = msg.get("info") or {}
        system = msg.get("system") or {}
        pushing = msg.get("pushing") or {}
        command = msg.get("print") or {}

        if info.get("command") == "get_version":
            self._send_local({"info": {
                "command": "get_version", "result": "success",
                "module": [{"name": "ota", "sw_ver": self.version, "product_name": "Snapmaker U1"}],
            }})
            return OK
        if system.get("command") == "get_access_code":
            self._send_local({"system": {"command": "get_access_code", "access_code": PLACEHOLDER_ACCESS_CODE}})
            return OK
        if pushing.get("command") == "pushall":
            self._push_status()
            return OK

        client = self.client
        cmd = command.get("command")
        if client is None or not cmd:
            return OK
        if cmd == "gcode_line" and isinstance(command.get("param"), str):
            self._gcode_async(command["param"], command.get("sequence_id"))
            return OK
        if cmd in ("pause", "resume", "stop"):
            path = {"pause": "pause", "resume": "resume", "stop": "cancel"}[cmd]
            self._async(lambda: client.request("POST", f"/printer/print/{path}"))
            return OK
        if cmd == "set_bed_temp" and isinstance(command.get("temp"), (int, float)):
            self._async(lambda: client.gcode(f"SET_HEATER_TEMPERATURE HEATER=heater_bed TARGET={command['temp']}"))
            return OK
        if cmd == "set_nozzle_temp" and isinstance(command.get("target_temp"), (int, float)):
            index = command.get("extruder_index") or 0
            heater = "extruder" if index == 0 else f"extruder{index}"
            self._async(lambda: client.gcode(f"SET_HEATER_TEMPERATURE HEATER={heater} TARGET={command['target_temp']}"))
            return OK
        if cmd == "home":
            self._async(lambda: client.gcode("G28"))
            return OK
        return OK

    def _async(self, fn):
        future = self._executor().submit(fn)
        future.add_done_callback(lambda f: f.exception() and log(f"command failed: {f.exception()}"))
        return future

    def _gcode_async(self, script, sequence_id):
        client = self.client

        def run():
            try:
                client.gcode(script)
                result = "success"
            except Exception as e:
                log(f"gcode {script!r} failed: {e}")
                result = "failed"
            reply = {"command": "gcode_line", "param": script, "result": result}
            if sequence_id:
                reply["sequence_id"] = sequence_id
            self._send_local({"print": reply})

        self._async(run)

    # U1 tab: the page polls page_state() and sends page_command() requests
    def page_state(self):
        with self.lock:
            status = self.status
            connected = self.client is not None and bool(status)
            message = self.page_message
        stats = status.get("print_stats") or {}
        info = stats.get("info") or {}
        progress = (status.get("virtual_sdcard") or {}).get("progress") or 0
        elapsed = stats.get("print_duration") or 0
        state = str(stats.get("state") or "standby")
        remaining = elapsed / progress * (1 - progress) if state == "printing" and 0 < progress < 1 else None
        toolhead = status.get("toolhead") or {}
        tools = [
            {"heater": name, "temp": status[name].get("temperature"), "target": status[name].get("target")}
            for name in TOOL_HEATERS if name in status
        ]
        bed = status.get("heater_bed") or {}
        task_config = status.get("print_task_config") or {}

        def at(key, i, default=""):
            values = task_config.get(key) or []
            return values[i] if i < len(values) else default

        slots = [
            {
                "exists": bool(present),
                "type": at("filament_type", i),
                "sub_type": at("filament_sub_type", i),
                "vendor": at("filament_vendor", i),
                "color": normalize_color(at("filament_color_rgba", i)),
            }
            for i, present in enumerate(task_config.get("filament_exist") or [])
        ]
        return {
            "agent": True,
            "connected": connected,
            "name": self.dev_name,
            "host": self.dev_ip,
            "version": self.version,
            "state": state,
            "filename": stats.get("filename") or "",
            "progress": progress,
            "layer": info.get("current_layer"),
            "layers": info.get("total_layer"),
            "elapsed": elapsed,
            "remaining": remaining,
            "tools": tools,
            "active_tool": toolhead.get("extruder") or "",
            "homed": toolhead.get("homed_axes") or "",
            "bed": {"temp": bed.get("temperature"), "target": bed.get("target")},
            "slots": slots,
            "thumbnail": self.thumbnail_url,
            "cameras": self.cameras,
            "message": message,
        }

    def page_command(self, data):
        """Run one whitelisted request from the U1 tab; the result shows up in page_state()."""
        if data.get("cmd") == "assign_presets":
            self._page_assign_presets()
            return
        client = self.client
        if client is None:
            self.page_message = "Drucker nicht verbunden"
            return
        with self.lock:
            busy = str((self.status.get("print_stats") or {}).get("state")) in ("printing", "paused")
        cmd = data.get("cmd")
        try:
            if cmd in ("pause", "resume", "cancel"):
                label = {"pause": "Pause", "resume": "Fortsetzen", "cancel": "Abbrechen"}[cmd]
                action = lambda: client.request("POST", f"/printer/print/{cmd}")
            elif cmd == "temp":
                heater = data.get("heater")
                limit = TEMP_LIMITS.get(heater)
                if limit is None:
                    raise ValueError("unbekannte Heizung")
                target = max(0, min(limit, int(data.get("target", 0))))
                label = f"{heater} auf {target} °C"
                action = lambda: client.gcode(f"SET_HEATER_TEMPERATURE HEATER={heater} TARGET={target}")
            elif busy:
                raise ValueError("Während des Drucks gesperrt")
            elif cmd == "home":
                label = "Referenzfahrt"
                action = lambda: client.gcode("G28")
            elif cmd == "tool":
                index = int(data.get("index", -1))
                if not 0 <= index < len(TOOL_HEATERS):
                    raise ValueError("unbekanntes Tool")
                label = f"Tool {index + 1}"
                action = lambda: client.gcode(f"T{index}")
            elif cmd == "jog":
                axis = data.get("axis")
                dist = float(data.get("dist", 0))
                if axis not in ("X", "Y", "Z") or abs(dist) not in JOG_STEPS:
                    raise ValueError("ungültige Bewegung")
                feed = 6000 if axis in "XY" else 600
                label = f"{axis} {dist:+g} mm"
                action = lambda: client.gcode(f"G91\nG1 {axis}{dist:g} F{feed}\nG90")
            elif cmd == "extrude":
                dist = float(data.get("dist", 0))
                if abs(dist) not in EXTRUDE_STEPS:
                    raise ValueError("ungültige Extrusion")
                label = f"Extruder {dist:+g} mm"
                action = lambda: client.gcode(f"M83\nG1 E{dist:g} F300")
            else:
                raise ValueError(f"unbekannter Befehl {cmd!r}")
        except (ValueError, TypeError) as e:
            self.page_message = f"Abgelehnt: {e}"
            return

        self.page_message = f"{label} …"

        def run():
            try:
                action()
                self.page_message = f"{label}: OK"
            except Exception as e:
                self.page_message = f"{label}: {e}"

        self._async(run)

    def _page_assign_presets(self):
        """The tab's "Profile zuweisen": plan on the UI thread, select on a plugin thread."""
        if not can_assign_presets():
            self.page_message = "Profile zuweisen braucht OrcaSlicer 2.5 Beta"
            return
        with self.lock:
            trays = build_trays(self.status.get("print_task_config") or {})
        if not trays:
            self.page_message = "Keine Filament-Slots vom Drucker"
            return
        try:
            assigned = list(orca.host.preset_bundle().current_filament_preset_names())
        except Exception as e:
            self.page_message = f"Profile zuweisen: {e}"
            return
        plan = plan_slot_presets(trays, filament_catalog(), assigned)
        self.page_message = "Profile zuweisen …"
        self._async(lambda: self._assign_presets(plan))

    # Jobs
    def _upload_job(self, params, update_fn, cancel_fn):
        """Upload on a worker thread; host callbacks stay on the calling job thread."""
        client = self.client
        if client is None:
            raise RuntimeError("printer not connected")
        if update_fn:
            update_fn(STAGE_CREATE, 0, "Preparing...")
        name = remote_filename(params)
        source, plate = params.filename, params.plate_index
        progress = queue.SimpleQueue()
        cancel = threading.Event()

        def upload():
            return client.upload(name, read_plate_gcode(source, plate), progress.put, cancel.is_set)

        future = self._executor().submit(upload)
        deadline = time.monotonic() + UPLOAD_TIMEOUT
        while True:
            try:
                percent = progress.get(timeout=0.2)
                if update_fn:
                    update_fn(STAGE_UPLOAD, percent, "Uploading G-code...")
            except queue.Empty:
                pass
            if cancel_fn and cancel_fn():
                cancel.set()
            if future.done() and progress.empty():
                return future.result()
            if time.monotonic() > deadline:
                cancel.set()
                raise TimeoutError("upload timed out")

    def _start(self, path, params, update_fn):
        client = self.client
        if client is None:
            raise RuntimeError("printer not connected")
        if update_fn:
            update_fn(STAGE_SENDING, 0, "Starting print...")
        with self.lock:
            slots = len((self.status.get("print_task_config") or {}).get("filament_exist") or []) or 4
        options = print_options(params, slots)
        log(f"start_local_print {path} options={options}")
        self._run(lambda: client.rpc("server.files.start_local_print",
                                     {"type": "gcode", "path": path, "options": options}), timeout=45)

    def start_send_gcode_to_sdcard(self, params, update_fn, cancel_fn, wait_fn):
        # The host also calls this with project_name "verify_job" to test the connection
        if params.project_name == "verify_job":
            client = self.client
            try:
                self._run(client.request, "GET", "/server/info")
                return OK
            except Exception as e:
                log(f"connection check failed: {e}")
                return FAILED
        try:
            path = self._upload_job(params, update_fn, cancel_fn)
        except Cancelled:
            return FAILED
        except Exception as e:
            log(f"send failed: {e}")
            return FAILED
        if update_fn:
            update_fn(STAGE_FINISHED, 100, "File uploaded")
        return OK

    def start_local_print(self, params, update_fn, cancel_fn):
        try:
            path = self._upload_job(params, update_fn, cancel_fn)
            if cancel_fn and cancel_fn():
                return FAILED
            self._start(path, params, update_fn)
        except Cancelled:
            return FAILED
        except Exception as e:
            log(f"print failed: {e}")
            return FAILED
        if update_fn:
            update_fn(STAGE_FINISHED, 100, "Print started")
        return OK

    def start_local_print_with_record(self, params, update_fn, cancel_fn, wait_fn):
        return self.start_local_print(params, update_fn, cancel_fn)

    def start_sdcard_print(self, params, update_fn, cancel_fn):
        path = params.dst_file or os.path.basename(params.filename)
        try:
            self._start(path, params, update_fn)
        except Exception as e:
            log(f"print from storage failed: {e}")
            return FAILED
        return OK

    def start_print(self, params, update_fn, cancel_fn, wait_fn):
        return NOT_SUPPORTED

    # No binding step on the U1
    def bind_detect(self, dev_ip, sec_link, detect):
        detect.dev_id = self.dev_id or dev_ip
        detect.dev_name = self.dev_name
        detect.model_id = MODEL_ID
        detect.version = self.version
        detect.connect_type = "lan"
        detect.bind_state = "free"
        return OK

    def check_cert(self): return OK
    def ping_bind(self, ping_code): return OK
    def bind(self, dev_ip, dev_id, dev_model, sec_link, timezone, improved, update_fn): return OK
    def unbind(self, dev_id): return OK
    def request_bind_ticket(self): return (OK, "")


# ---------------------------------------------------------------------------
# U1 tab
# ---------------------------------------------------------------------------

PAGE_HTML = """<!doctype html>
<html><head><meta charset="utf-8">
<style>
  * { box-sizing: border-box; }
  body { margin: 0; padding: 12px; font-family: var(--orca-font, system-ui); font-size: 13px;
         background: var(--orca-bg); color: var(--orca-fg); }
  .grid { display: grid; grid-template-columns: minmax(0, 1.5fr) minmax(330px, 1fr); gap: 12px;
          height: calc(100vh - 24px); }
  .col { display: flex; flex-direction: column; gap: 12px; min-height: 0; }
  .card { border: 1px solid var(--orca-border); border-radius: 8px; padding: 12px; }
  .head { display: flex; align-items: center; gap: 8px; margin-bottom: 10px; }
  .head h3 { margin: 0; font-size: 13px; font-weight: 600; }
  .grow { flex: 1; }
  .muted { color: var(--orca-muted); }
  button { cursor: pointer; padding: 5px 10px; border-radius: 6px; }
  button:disabled { opacity: .4; cursor: default; }
  .seg { display: inline-flex; border: 1px solid var(--orca-border); border-radius: 6px; overflow: hidden; }
  .seg button { border: 0; border-radius: 0; background: transparent; color: inherit; padding: 4px 10px; }
  .seg button.on { background: var(--orca-accent); color: var(--orca-accent-fg); }
  .camcard { flex: 1; min-height: 0; display: flex; flex-direction: column; }
  .camview { flex: 1; min-height: 160px; background: #000; border-radius: 6px; overflow: hidden;
             display: flex; align-items: center; justify-content: center; color: #888; }
  .camview img { width: 100%; height: 100%; object-fit: contain; }
  .job { display: flex; gap: 12px; align-items: center; }
  .thumb { width: 96px; height: 96px; border-radius: 6px; border: 1px solid var(--orca-border);
           object-fit: contain; flex: none; }
  .badge { padding: 2px 8px; border-radius: 10px; font-size: 11px; font-weight: 600;
           border: 1px solid var(--orca-border); }
  .badge.active { background: var(--orca-accent); color: var(--orca-accent-fg); border-color: transparent; }
  .bar { height: 6px; border-radius: 3px; background: var(--orca-border); overflow: hidden; margin: 8px 0 4px; }
  .bar div { height: 100%; width: 0; background: var(--orca-accent); }
  .row { display: flex; align-items: center; gap: 8px; }
  .temps { display: grid; grid-template-columns: auto 1fr auto; gap: 6px 10px; align-items: center; }
  .temps input { width: 64px; }
  .tlabel.active { color: var(--orca-accent); font-weight: 600; }
  .controls { display: flex; gap: 16px; align-items: center; justify-content: center; margin-top: 12px; }
  .pad { display: grid; grid-template-columns: repeat(3, 40px); grid-template-rows: repeat(3, 40px); gap: 4px; }
  .pad button, .stack button { padding: 0; width: 40px; height: 40px; }
  .stack { display: flex; flex-direction: column; gap: 4px; align-items: center; }
  .slots { display: grid; grid-template-columns: repeat(4, 1fr); gap: 8px; }
  .slot { border: 1px solid var(--orca-border); border-radius: 8px; padding: 10px 4px; text-align: center; }
  .slot.active { border-color: var(--orca-accent); box-shadow: 0 0 0 1px var(--orca-accent); }
  .swatch { width: 40px; height: 40px; border-radius: 50%; margin: 0 auto 6px; display: flex;
            align-items: center; justify-content: center; font-weight: 700; border: 1px solid var(--orca-border); }
  .slot .type { font-weight: 600; }
  .slot .sub { font-size: 11px; }
  .notice { padding: 40px 20px; text-align: center; }
  #message { min-height: 1.2em; }
</style></head>
<body>
<div id="notice" class="card notice" hidden></div>
<div id="main" class="grid" hidden>
  <div class="col">
    <div class="card camcard">
      <div class="head"><h3>Kamera</h3><span class="grow"></span><span id="cams" class="seg"></span>
        <button id="camtoggle">Stopp</button></div>
      <div class="camview" id="camview"><img id="cam" alt=""></div>
    </div>
    <div class="card">
      <div class="head"><h3>Druckauftrag</h3><span id="badge" class="badge"></span><span class="grow"></span>
        <button id="pause" data-cmd="pause">Pause</button>
        <button id="resume" data-cmd="resume">Fortsetzen</button>
        <button id="cancel">Abbrechen</button></div>
      <div class="job">
        <img id="thumb" class="thumb" alt="" hidden>
        <div class="grow">
          <div id="filename" style="font-weight:600"></div>
          <div class="bar"><div id="bar"></div></div>
          <div class="row muted"><span id="pct"></span><span class="grow"></span><span id="layer"></span>
            <span id="times"></span></div>
        </div>
      </div>
    </div>
  </div>
  <div class="col">
    <div class="card">
      <div class="head"><h3 id="title">Steuerung</h3><span class="grow"></span><span id="tools" class="seg"></span></div>
      <div class="temps" id="temps"></div>
      <div class="controls">
        <div class="stack"><span class="muted">Extruder</span>
          <button data-extrude="-1" title="Zurückziehen">E−</button>
          <button data-extrude="1" title="Extrudieren">E+</button></div>
        <div class="pad">
          <span></span><button data-axis="Y" data-dir="1">Y+</button><span></span>
          <button data-axis="X" data-dir="-1">X−</button><button id="home" title="Referenzfahrt">⌂</button>
          <button data-axis="X" data-dir="1">X+</button>
          <span></span><button data-axis="Y" data-dir="-1">Y−</button><span></span>
        </div>
        <div class="stack"><span class="muted">Z</span>
          <button data-axis="Z" data-dir="1" title="Abstand vergrößern">Z+</button>
          <button data-axis="Z" data-dir="-1" title="Abstand verkleinern">Z−</button></div>
      </div>
      <div class="row" style="justify-content:center;margin-top:10px">
        <span class="muted">Schritt</span><span id="steps" class="seg"></span></div>
      <div id="message" class="muted" style="margin-top:10px"></div>
    </div>
    <div class="card">
      <div class="head"><h3>Filament</h3><span class="grow"></span><span id="homed" class="muted"></span>
        <button data-cmd="assign_presets" title="Jedem Slot im Projekt das passende Filament-Profil zuweisen">Profile zuweisen</button></div>
      <div class="slots" id="slots"></div>
    </div>
  </div>
</div>
<script>
  const $ = id => document.getElementById(id);
  const STATES = { standby: "Bereit", printing: "Druckt", paused: "Pausiert", complete: "Fertig",
                   cancelled: "Abgebrochen", error: "Fehler" };
  let state = null, camIndex = 0, camOn = true, camLoading = false, step = 10, cancelArmed = 0;

  const send = obj => orca.postMessage(obj);
  const command = obj => send(Object.assign({ type: "command" }, obj));
  const poll = () => send({ type: "poll" });
  orca.onMessage(d => { if (d && d.type === "state") render(d.state); });
  setInterval(poll, 1000);
  poll();

  function fmtTime(s) {
    if (s == null) return "";
    const m = Math.round(s / 60);
    return m >= 60 ? Math.floor(m / 60) + " h " + (m % 60) + " min" : m + " min";
  }
  function deg(v) { return v == null ? "–" : Math.round(v); }
  function textColor(rgba) {
    const v = parseInt(rgba.slice(0, 6), 16);
    const lum = 0.299 * (v >> 16) + 0.587 * ((v >> 8) & 255) + 0.114 * (v & 255);
    return lum > 140 ? "#000" : "#fff";
  }
  function segment(el, labels, active, onClick) {
    if (el.childElementCount !== labels.length) {
      el.innerHTML = "";
      labels.forEach((label, i) => {
        const b = document.createElement("button");
        b.textContent = label;
        b.onclick = () => onClick(i);
        el.appendChild(b);
      });
    }
    [...el.children].forEach((b, i) => b.classList.toggle("on", i === active));
  }

  function render(s) {
    state = s;
    const ready = s.agent && s.connected;
    $("notice").hidden = ready;
    $("main").hidden = !ready;
    if (!ready) {
      $("notice").textContent = s.agent
        ? "Verbinde mit dem Drucker …"
        : "Der Drucker-Agent „Snapmaker U1 (Moonraker)“ ist nicht aktiv. Wähle ihn im Drucker-Preset unter Drucker-Agent aus.";
      return;
    }
    const busy = s.state === "printing" || s.state === "paused";
    $("title").textContent = s.name + " · " + s.host;

    // Job
    $("badge").textContent = STATES[s.state] || s.state;
    $("badge").classList.toggle("active", busy);
    $("filename").textContent = s.filename || "Kein Druckauftrag";
    $("bar").style.width = Math.round(s.progress * 100) + "%";
    $("pct").textContent = Math.round(s.progress * 100) + " %";
    $("layer").textContent = s.layers ? "Schicht " + (s.layer || 0) + "/" + s.layers : "";
    $("times").textContent = busy ? fmtTime(s.elapsed) + (s.remaining != null ? " · noch " + fmtTime(s.remaining) : "") : "";
    if (s.thumbnail && $("thumb").dataset.src !== s.thumbnail) {
      $("thumb").dataset.src = s.thumbnail;
      $("thumb").src = s.thumbnail;
    }
    $("thumb").hidden = !s.thumbnail;
    $("pause").hidden = s.state !== "printing";
    $("resume").hidden = s.state !== "paused";
    $("cancel").hidden = !busy;

    // Tools and temperatures
    const activeTool = s.tools.findIndex(t => t.heater === s.active_tool);
    segment($("tools"), s.tools.map((t, i) => "Tool " + (i + 1)), activeTool,
            i => command({ cmd: "tool", index: i }));
    [...$("tools").children].forEach(b => b.disabled = busy);
    const rows = s.tools.map((t, i) => ({ label: "Tool " + (i + 1), heater: t.heater, t, active: i === activeTool }))
      .concat([{ label: "Bett", heater: "heater_bed", t: s.bed, active: false }]);
    const temps = $("temps");
    if (temps.childElementCount !== rows.length * 3) {
      temps.innerHTML = "";
      rows.forEach(r => {
        const label = document.createElement("span");
        label.className = "tlabel";
        const value = document.createElement("span");
        const input = document.createElement("input");
        input.type = "number"; input.min = 0; input.step = 5; input.title = "Zieltemperatur, Enter zum Setzen";
        input.dataset.heater = r.heater;
        input.onkeydown = e => { if (e.key === "Enter") { command({ cmd: "temp", heater: r.heater, target: Number(input.value) }); input.blur(); } };
        temps.append(label, value, input);
      });
    }
    rows.forEach((r, i) => {
      const [label, value, input] = [...temps.children].slice(i * 3, i * 3 + 3);
      label.textContent = r.label;
      label.classList.toggle("active", r.active);
      value.textContent = deg(r.t.temp) + " / " + deg(r.t.target) + " °C";
      if (document.activeElement !== input) input.value = r.t.target == null ? "" : Math.round(r.t.target);
    });

    // Motion
    document.querySelectorAll("[data-axis], [data-extrude], #home").forEach(b => b.disabled = busy);
    $("homed").textContent = s.homed ? "Referenziert: " + s.homed.toUpperCase() : "Nicht referenziert";
    segment($("steps"), ["10 mm", "1 mm", "0,1 mm"], [10, 1, 0.1].indexOf(step), i => { step = [10, 1, 0.1][i]; render(state); });

    // Filament slots
    const slots = $("slots");
    if (slots.childElementCount !== s.slots.length) {
      slots.innerHTML = "";
      s.slots.forEach(() => {
        const d = document.createElement("div");
        d.className = "slot";
        d.innerHTML = '<div class="swatch"></div><div class="type"></div><div class="sub muted"></div>';
        slots.appendChild(d);
      });
    }
    s.slots.forEach((slot, i) => {
      const d = slots.children[i];
      const sw = d.querySelector(".swatch");
      d.classList.toggle("active", i === activeTool);
      sw.textContent = i + 1;
      sw.style.background = slot.exists ? "#" + slot.color.slice(0, 6) : "transparent";
      sw.style.color = slot.exists ? textColor(slot.color) : "inherit";
      d.querySelector(".type").textContent = slot.exists ? slot.type : "leer";
      const sub = slot.sub_type && slot.sub_type !== "NONE" ? slot.sub_type : "";
      d.querySelector(".sub").textContent = [slot.vendor, sub].filter(Boolean).join(" · ");
    });

    // Cameras
    segment($("cams"), s.cameras.map(c => c.name), camIndex, i => { camIndex = i; nextFrame(); });
    $("cams").hidden = s.cameras.length < 2;
    if (!s.cameras.length) $("camview").textContent = "Keine Kamera gefunden";
    else if (camOn && !camLoading) nextFrame();

    $("message").textContent = s.message || "";
  }

  // Camera: load the next snapshot once the previous one has arrived
  const cam = $("cam");
  function nextFrame() {
    const cams = state ? state.cameras : [];
    if (!camOn || !cams.length) return;
    const url = cams[camIndex % cams.length].url;
    camLoading = true;
    cam.src = url + (url.includes("?") ? "&" : "?") + "t=" + Date.now();
  }
  cam.onload = () => { camLoading = false; setTimeout(nextFrame, 150); };
  cam.onerror = () => { camLoading = false; setTimeout(nextFrame, 2000); };
  $("camtoggle").onclick = () => {
    camOn = !camOn;
    $("camtoggle").textContent = camOn ? "Stopp" : "Start";
    if (camOn) nextFrame();
  };

  // Buttons
  document.querySelectorAll("[data-cmd]").forEach(b => b.onclick = () => command({ cmd: b.dataset.cmd }));
  $("cancel").onclick = () => {
    if (Date.now() - cancelArmed < 3000) {
      cancelArmed = 0;
      $("cancel").textContent = "Abbrechen";
      command({ cmd: "cancel" });
    } else {
      cancelArmed = Date.now();
      $("cancel").textContent = "Wirklich abbrechen?";
      setTimeout(() => { $("cancel").textContent = "Abbrechen"; }, 3000);
    }
  };
  $("home").onclick = () => command({ cmd: "home" });
  document.querySelectorAll("[data-axis]").forEach(b =>
    b.onclick = () => command({ cmd: "jog", axis: b.dataset.axis, dist: Number(b.dataset.dir) * step }));
  document.querySelectorAll("[data-extrude]").forEach(b =>
    b.onclick = () => command({ cmd: "extrude", dist: Number(b.dataset.extrude) * (step >= 10 ? 10 : 1) }));
</script>
</body></html>
"""


class U1Page(orca.pages.PagesPluginCapabilityBase):
    """Main-window tab with the U1's camera, job, temperatures, motion and filament slots."""

    def get_name(self):
        return "U1"

    def get_ui(self):
        return PAGE_HTML

    def on_message(self, data):
        # Runs on the UI thread: answer from the agent's cached status, never block on the network
        if not isinstance(data, dict):
            return
        agent = _active_agent
        if data.get("type") == "command" and agent is not None:
            agent.page_command(data)
        state = agent.page_state() if agent is not None else {"agent": False, "connected": False}
        self.post_message({"type": "state", "state": state})


@orca.plugin
class U1Plugin(orca.base):
    def register_capabilities(self):
        # OrcaSlicer 2.5.0-alpha's request_permissions() only takes fs_read, so the threading
        # permission is asked for on first use (the first connect) instead
        orca.register_capability(U1Agent)
        orca.register_capability(U1Page)
