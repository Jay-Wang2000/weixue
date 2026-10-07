#!/usr/bin/env python3
"""Writes Wi-Fi credentials and the OTA URL override into the device's NVS partition over USB.

Run with the project .venv python (esptool pin from vendor.lock). Inputs come only from the
environment: WIFI_SSID + WIFI_PASSWORD (saved-SSID list in NVS namespace "wifi", new SSID first)
and OTA_URL (wifi/ota_url; unset keeps the stored value, empty clears the override). All other
NVS entries are kept except the Wi-Fi driver cache namespace nvs.net80211.

Steps: partition table from the device, NVS backup (0600 + sha256), read-modify-write image via
nvs_partition_gen, round-trip check; without --dry-run, write-flash of the NVS region only, reset
and boot-log check. Values of WIFI_SSID, WIFI_PASSWORD and OTA_URL are never printed.
"""
from __future__ import annotations

import argparse
import base64
import csv
import dataclasses
import datetime
import glob
import hashlib
import importlib
import io
import os
import re
import subprocess
import sys
import tempfile
import time
import zlib
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
VENDOR = Path(os.environ.get("WEIXUE_VENDOR", str(Path.home() / "Projects/weixue-vendor")))
IDF_PATH = Path(os.environ.get("IDF_PATH", str(VENDOR / "esp-idf")))
IDF_TOOLS_PATH = Path(os.environ.get("IDF_TOOLS_PATH", str(VENDOR / ".espressif")))
BACKUP_DIR = Path(os.environ.get("WEIXUE_BACKUP_DIR", str(Path.home() / "Projects/weixue/backup")))

PT_OFFSET = 0x8000
PT_SIZE = 0xC00
NVS_LABEL = "nvs"  # nvs_flash_init() default partition

# esp-wifi-connect 3.3.1 ssid_manager.cc: namespace "wifi", MAX_WIFI_SSID_COUNT 10,
# char ssid[33] / password[65] read buffers.
WIFI_NS = "wifi"
MAX_SSIDS = 10
SSID_MAX_BYTES = 32
PASSWORD_MAX_BYTES = 64
# xiaozhi ota.cc Settings("wifi").GetString("ota_url"); the portal reads it into char[256].
OTA_URL_KEY = "ota_url"
OTA_URL_MIN = 10
OTA_URL_MAX = 255

DROP_NAMESPACES = ("nvs.net80211",)
# nvs_partition_gen.write_entry() injects extra entries for these keys.
GENERATOR_SPECIAL_KEYS = frozenset({"sta.ssid", "sta.pswd", "ap.ssid", "ap.passwd", "ap.authmode"})
PRIMITIVES = ("u8", "i8", "u16", "i16", "u32", "i32", "u64", "i64")
_PARSER_KIND = {
    "uint8_t": "u8", "int8_t": "i8", "uint16_t": "u16", "int16_t": "i16",
    "uint32_t": "u32", "int32_t": "i32", "uint64_t": "u64", "int64_t": "i64",
}
_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_BSSID = re.compile(r"(BSSID: )([0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}")


class ProvisionError(Exception):
    pass


@dataclasses.dataclass(frozen=True)
class Entry:
    namespace: str
    key: str
    kind: str  # one of PRIMITIVES, "string", "blob"
    value: int | str | bytes


@dataclasses.dataclass(frozen=True)
class Inputs:
    ssid: str | None
    password: str | None
    ota_url: str | None


def read_inputs(env: dict[str, str]) -> tuple[Inputs, list[str]]:
    """Validates WIFI_SSID / WIFI_PASSWORD / OTA_URL; returns inputs and warnings (no values)."""
    warnings: list[str] = []
    ssid = env.get("WIFI_SSID")
    password = env.get("WIFI_PASSWORD")
    ota_url = env.get("OTA_URL")
    if ssid is not None:
        if not 1 <= len(ssid.encode()) <= SSID_MAX_BYTES:
            raise ProvisionError(f"WIFI_SSID must be 1..{SSID_MAX_BYTES} bytes")
        if password is None:
            raise ProvisionError("WIFI_PASSWORD must be set with WIFI_SSID (empty for an open network)")
        if len(password.encode()) > PASSWORD_MAX_BYTES:
            raise ProvisionError(f"WIFI_PASSWORD must be at most {PASSWORD_MAX_BYTES} bytes")
        if 0 < len(password) < 8:
            warnings.append("WIFI_PASSWORD is shorter than 8 characters; WPA2/WPA3 networks reject it")
        if len(password) == 64 and not re.fullmatch(r"[0-9a-fA-F]{64}", password):
            warnings.append("a 64-character WIFI_PASSWORD is used as a hex PSK but is not hex")
    elif password is not None:
        raise ProvisionError("WIFI_PASSWORD is set without WIFI_SSID")
    if ota_url:
        parts = urlsplit(ota_url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ProvisionError("OTA_URL must be an http:// or https:// URL with a host")
        if not OTA_URL_MIN <= len(ota_url.encode()) <= OTA_URL_MAX:
            raise ProvisionError(f"OTA_URL must be {OTA_URL_MIN}..{OTA_URL_MAX} bytes")
    return Inputs(ssid, password, ota_url), warnings


def _import_idf(subdir: str, module: str):
    path = str(IDF_PATH / subdir)
    if not Path(path).is_dir():
        raise ProvisionError(f"ESP-IDF tool directory missing: {path} (set IDF_PATH)")
    if path not in sys.path:
        sys.path.insert(0, path)
    return importlib.import_module(module)


# --- partition table -------------------------------------------------------------------------

def _gen_esp32part():
    mod = _import_idf("components/partition_table", "gen_esp32part")
    mod.quiet = True
    mod.offset_part_table = PT_OFFSET
    return mod


def parse_partition_table(data: bytes):
    """Parses a binary partition table (as read from flash) or a partitions CSV."""
    mod = _gen_esp32part()
    if data[:2] == b"\xaa\x50":
        return mod.PartitionTable.from_binary(data)
    if data[:4] == b"\xff\xff\xff\xff":
        raise ProvisionError("no partition table at 0x8000")
    try:
        text = data.decode()
    except UnicodeDecodeError:
        raise ProvisionError("partition table is neither binary (magic AA50) nor CSV; flash encryption?")
    return mod.PartitionTable.from_csv(text)


def find_nvs(table) -> tuple[int, int]:
    """Returns (offset, size) of the data/nvs partition labelled "nvs"."""
    if any(p.type == 1 and p.subtype == 4 for p in table):
        raise ProvisionError("an nvs_keys partition exists: NVS encryption is not supported")
    for p in table:
        if p.name == NVS_LABEL:
            if p.type != 1 or p.subtype != 2:
                raise ProvisionError(f"partition '{NVS_LABEL}' is not data/nvs")
            if p.encrypted:
                raise ProvisionError(f"partition '{NVS_LABEL}' is flagged encrypted")
            if p.size % 4096 or p.size < 0x3000:
                raise ProvisionError(f"partition '{NVS_LABEL}' size 0x{p.size:x} is not a read/write NVS size")
            return p.offset, p.size
    raise ProvisionError(f"no '{NVS_LABEL}' partition in the partition table")


# --- NVS image -> entries --------------------------------------------------------------------

def parse_nvs(image: bytes) -> tuple[list[Entry], list[str]]:
    """Decodes the live key/value set of an NVS v2 image. Later pages win on duplicate keys."""
    parser = _import_idf("components/nvs_flash/nvs_partition_tool", "nvs_parser")
    warnings: list[str] = []
    part = parser.NVS_Partition(NVS_LABEL, bytearray(image))
    pages = []
    for page in part.pages:
        if page.is_empty:
            continue
        status = page.header["status"]
        if status not in ("Active", "Full", "Erasing"):
            warnings.append(f"page 0x{page.start_address:x}: status {status}, skipped")
            continue
        if page.header["crc"]["original"] != page.header["crc"]["computed"]:
            warnings.append(f"page 0x{page.start_address:x}: header CRC mismatch, skipped")
            continue
        if page.header["version"] != 2:
            raise ProvisionError(f"page 0x{page.start_address:x}: NVS format version {page.header['version']}, expected 2")
        pages.append(page)
    if not pages and image.count(0xFF) != len(image):
        raise ProvisionError("NVS partition has no valid pages (encrypted or not an NVS partition)")
    pages.sort(key=lambda pg: pg.header["page_index"])

    written = []
    for page in pages:
        for e in page.entries:
            if e.state != "Written":
                continue
            if e.metadata["crc"]["original"] != e.metadata["crc"]["computed"]:
                warnings.append(f"page 0x{page.start_address:x} entry {e.index}: CRC mismatch, skipped")
                continue
            written.append(e)

    ns_names = {e.data["value"]: e.key for e in written
                if e.metadata["namespace"] == 0 and e.metadata["type"] == "uint8_t"}
    values: dict[tuple[str, str], tuple[str, object]] = {}
    blob_index: dict[tuple[str, str], tuple[int, int, int]] = {}
    chunks: dict[tuple[str, str, int], bytes] = {}
    for e in written:
        ns_idx = e.metadata["namespace"]
        if ns_idx == 0:
            continue
        ns = ns_names.get(ns_idx)
        if ns is None:
            warnings.append(f"entry '{e.key}': unknown namespace index {ns_idx}, skipped")
            continue
        k = (ns, e.key)
        etype = e.metadata["type"]
        if etype in _PARSER_KIND:
            values[k] = (_PARSER_KIND[etype], e.data["value"])
        elif etype in ("string", "blob", "blob_data"):
            size = e.data["size"]
            raw = b"".join(bytes(c.raw) for c in e.children)
            if len(raw) < size:
                warnings.append(f"{ns}:{e.key}: data truncated, skipped")
                continue
            data = raw[:size]
            if size and zlib.crc32(data, 0xFFFFFFFF) != e.metadata["crc"]["data_original"]:
                warnings.append(f"{ns}:{e.key}: data CRC mismatch, skipped")
                continue
            if etype == "string":
                values[k] = ("string", data)
            elif etype == "blob":
                values[k] = ("blob", data)
            else:
                chunks[(ns, e.key, e.metadata["chunk_index"])] = data
        elif etype == "blob_index":
            blob_index[k] = (e.data["size"], e.data["chunk_count"], e.data["chunk_start"])
            values[k] = ("blob", None)
        else:
            warnings.append(f"{ns}:{e.key}: unsupported type {etype}, skipped")

    for k, (size, count, start) in blob_index.items():
        parts = [chunks.get((k[0], k[1], start + i)) for i in range(count)]
        if any(p is None for p in parts) or sum(len(p) for p in parts) != size:
            warnings.append(f"{k[0]}:{k[1]}: incomplete blob, skipped")
            del values[k]
            continue
        values[k] = ("blob", b"".join(parts))

    entries = []
    for (ns, key), (kind, value) in values.items():
        if kind == "blob" and value is None:
            continue
        if kind == "string":
            try:
                value = value.decode().rstrip("\0")
            except UnicodeDecodeError:
                warnings.append(f"{ns}:{key}: string is not UTF-8, skipped")
                continue
        entries.append(Entry(ns, key, kind, value))
    return entries, warnings


# --- merge -----------------------------------------------------------------------------------

def ssid_keys(index: int) -> tuple[str, str, str]:
    suffix = "" if index == 0 else str(index)
    return f"ssid{suffix}", f"password{suffix}", f"channel{suffix}"


def saved_ssids(entries: list[Entry]) -> list[tuple[str, str, int]]:
    """The SSID list as SsidManager::LoadFromNvs builds it."""
    by_key = {(e.namespace, e.key): e for e in entries}
    out = []
    for i in range(MAX_SSIDS):
        ks, kp, kc = ssid_keys(i)
        se, pe = by_key.get((WIFI_NS, ks)), by_key.get((WIFI_NS, kp))
        if se is None or pe is None or se.kind != "string" or pe.kind != "string":
            continue
        if len(se.value.encode()) > SSID_MAX_BYTES or len(pe.value.encode()) > PASSWORD_MAX_BYTES:
            continue
        ce = by_key.get((WIFI_NS, kc))
        out.append((se.value, pe.value, ce.value if ce is not None and ce.kind == "u8" else 0))
    return out


def merge(entries: list[Entry], inputs: Inputs) -> tuple[list[Entry], list[Entry]]:
    """Returns (entries to write, entries dropped). The new SSID goes first with channel 0 (unknown)."""
    list_keys = {k for i in range(MAX_SSIDS) for k in ssid_keys(i)}
    kept, dropped = [], []
    for e in entries:
        if e.namespace in DROP_NAMESPACES:
            dropped.append(e)
            continue
        if e.namespace == WIFI_NS and inputs.ssid is not None and e.key in list_keys:
            continue
        if e.namespace == WIFI_NS and inputs.ota_url is not None and e.key == OTA_URL_KEY:
            continue
        if e.key in GENERATOR_SPECIAL_KEYS:
            raise ProvisionError(f"{e.namespace}:{e.key}: nvs_partition_gen cannot write this key unchanged")
        kept.append(e)
    if inputs.ssid is not None:
        ssids = [(inputs.ssid, inputs.password, 0)]
        ssids += [s for s in saved_ssids(entries) if s[0] != inputs.ssid]
        for i, (ssid, password, channel) in enumerate(ssids[:MAX_SSIDS]):
            ks, kp, kc = ssid_keys(i)
            kept += [Entry(WIFI_NS, ks, "string", ssid), Entry(WIFI_NS, kp, "string", password),
                     Entry(WIFI_NS, kc, "u8", channel)]
    if inputs.ota_url is not None:
        kept.append(Entry(WIFI_NS, OTA_URL_KEY, "string", inputs.ota_url))
    return kept, dropped


# --- entries -> CSV -> image -----------------------------------------------------------------

def _check_key(name: str) -> None:
    if not 1 <= len(name) <= 15 or not name.isascii() or name.startswith("#") or "\0" in name:
        raise ProvisionError(f"NVS key/namespace '{name}' cannot be written by nvs_partition_gen")


def _private_write(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)


def entries_to_csv(entries: list[Entry], file_dir: Path) -> str:
    """nvs_partition_gen CSV, one namespace block per namespace in first-seen order.

    Strings with CR/LF go through "file" rows (the generator filters CSV lines starting with '#').
    """
    by_ns: dict[str, list[Entry]] = {}
    for e in entries:
        by_ns.setdefault(e.namespace, []).append(e)
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["key", "type", "encoding", "value"])
    n_files = 0
    for ns, items in by_ns.items():
        _check_key(ns)
        w.writerow([ns, "namespace", "", ""])
        for e in items:
            _check_key(e.key)
            if e.kind in PRIMITIVES:
                w.writerow([e.key, "data", e.kind, str(int(e.value))])
            elif e.kind == "string":
                if "\n" in e.value or "\r" in e.value:
                    path = file_dir / f"value-{n_files}.txt"
                    n_files += 1
                    _private_write(path, e.value.encode())
                    w.writerow([e.key, "file", "string", str(path)])
                else:
                    w.writerow([e.key, "data", "string", e.value])
            elif e.kind == "blob":
                if not e.value:
                    raise ProvisionError(f"{ns}:{e.key}: empty blob cannot be written by nvs_partition_gen")
                w.writerow([e.key, "data", "base64", base64.b64encode(e.value).decode()])
            else:
                raise ProvisionError(f"{ns}:{e.key}: unknown kind {e.kind}")
    return buf.getvalue()


def idf_python() -> Path:
    env = os.environ.get("IDF_PYTHON_ENV_PATH")
    pattern = str(Path(env) / "bin/python") if env else str(IDF_TOOLS_PATH / "python_env/idf*_env/bin/python")
    found = sorted(glob.glob(pattern))
    if len(found) != 1:
        raise ProvisionError(f"expected one ESP-IDF python env at {pattern}, found {len(found)} (set IDF_PYTHON_ENV_PATH)")
    return Path(found[0])


def generate_image(csv_path: Path, size: int, redact) -> bytes:
    out_name = "nvs-generated.bin"
    r = subprocess.run(
        [str(idf_python()), "-m", "esp_idf_nvs_partition_gen", "generate", csv_path.name, out_name, hex(size)],
        cwd=csv_path.parent, capture_output=True, text=True)
    out = csv_path.parent / out_name
    if r.returncode != 0 or not out.exists():
        lines = (r.stdout + r.stderr).strip().splitlines()
        tail = redact(lines[-1]) if lines else ""
        raise ProvisionError(f"nvs_partition_gen failed ({r.returncode}): {tail}")
    image = out.read_bytes()
    out.unlink()
    if len(image) != size:
        raise ProvisionError(f"generated image is {len(image)} bytes, partition is {size}")
    return image


def as_map(entries: list[Entry]) -> dict[tuple[str, str], tuple[str, object]]:
    return {(e.namespace, e.key): (e.kind, e.value) for e in entries}


# --- output hygiene --------------------------------------------------------------------------

class Redactor:
    def __init__(self, inputs: Inputs):
        pairs = [(inputs.password, "<WIFI_PASSWORD>"), (inputs.ssid, "<WIFI_SSID>"), (inputs.ota_url, "<OTA_URL>")]
        host = urlsplit(inputs.ota_url).hostname if inputs.ota_url else None
        if host:
            pairs.append((host, "<OTA_HOST>"))
        self.pairs = sorted([(s, r) for s, r in pairs if s], key=lambda p: -len(p[0]))

    def __call__(self, text: str) -> str:
        for secret, label in self.pairs:
            text = text.replace(secret, label)
        return _BSSID.sub(r"\1<bssid>", text)


def describe(entries: list[Entry]) -> list[str]:
    """namespace: key(kind[, bytes]) lines; never values."""
    by_ns: dict[str, list[str]] = {}
    for e in entries:
        size = f",{len(e.value.encode()) if e.kind == 'string' else len(e.value)}B" if e.kind in ("string", "blob") else ""
        by_ns.setdefault(e.namespace, []).append(f"{e.key}({e.kind}{size})")
    return [f"  {ns}: {' '.join(keys)}" for ns, keys in by_ns.items()]


# --- device ----------------------------------------------------------------------------------

def resolve_port(port: str | None) -> str:
    if port:
        return port
    ports = sorted(glob.glob("/dev/cu.usbmodem*"))
    if len(ports) != 1:
        raise ProvisionError(f"expected one /dev/cu.usbmodem* port, found {ports or 'none'}; pass --port")
    return ports[0]


def check_esptool() -> None:
    pin = None
    for line in (ROOT / "vendor.lock").read_text().splitlines():
        parts = line.split()
        if len(parts) == 3 and parts[0] == "esptool":
            pin = parts[2]
    try:
        import esptool
    except ImportError:
        raise ProvisionError("esptool missing: run with .venv/bin/python (scripts/venv.sh creates it)")
    if esptool.__version__ != pin:
        raise ProvisionError(f"esptool {esptool.__version__} != vendor.lock pin {pin}: run with .venv/bin/python")


def run_esptool(port: str, *args: str) -> str:
    cmd = [sys.executable, "-m", "esptool", "--chip", "esp32s3", "--port", port, *args]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise ProvisionError(f"esptool {args[0]} failed ({r.returncode}): {(r.stdout + r.stderr).strip()[-600:]}")
    return r.stdout


def read_flash(port: str, offset: int, size: int, tmp: Path) -> bytes:
    path = tmp / f"read-{offset:x}.bin"
    run_esptool(port, "read-flash", hex(offset), hex(size), str(path))
    data = path.read_bytes()
    path.unlink()
    if len(data) != size:
        raise ProvisionError(f"read {len(data)} bytes at 0x{offset:x}, expected {size}")
    return data


def write_backup(data: bytes, mac: str, ts: str, backup_dir: Path, suffix: str = "") -> tuple[Path, str]:
    backup_dir.mkdir(parents=True, exist_ok=True)
    path = backup_dir / f"nvs-{ts}{suffix}.bin"
    _private_write(path, data)
    digest = hashlib.sha256(data).hexdigest()
    sidecar = backup_dir / f"nvs-{ts}{suffix}.sha256"
    _private_write(sidecar, f"{digest}  {path.name}\n".encode())
    if mac and not suffix:
        _private_write(backup_dir / f"nvs-{ts}.mac", f"{mac}\n".encode())
    return path, digest


def mac_ipv4_addresses() -> dict[str, str]:
    try:
        out = subprocess.run(["ifconfig"], capture_output=True, text=True).stdout
    except OSError:
        return {}
    addrs, iface = {}, None
    for line in out.splitlines():
        m = re.match(r"^(\S+): flags=", line)
        if m:
            iface = m.group(1)
        m = re.match(r"^\s+inet (\d+\.\d+\.\d+\.\d+)", line)
        if m and iface and not m.group(1).startswith("127."):
            addrs[m.group(1)] = iface
    return addrs


def expected_ota_url(inputs: Inputs, entries: list[Entry]) -> str | None:
    if inputs.ota_url:
        return inputs.ota_url
    if inputs.ota_url is None:
        stored = as_map(entries).get((WIFI_NS, OTA_URL_KEY))
        if stored and stored[0] == "string" and stored[1]:
            return stored[1]
    cfg = ROOT / "firmware/board/config.weixue.json"
    m = re.search(r'CONFIG_OTA_URL=\\?"([^"\\]+)', cfg.read_text()) if cfg.exists() else None
    return m.group(1) if m else None


def tail_boot_log(port: str, seconds: float, redact, ssid: str | None, ota_url: str | None) -> tuple[bool, bool]:
    """Resets the board, prints the redacted boot log, returns (wifi joined, OTA check done)."""
    import serial

    host_port = None
    if ota_url:
        u = urlsplit(ota_url)
        host_port = f"{u.hostname}:{u.port or (443 if u.scheme == 'https' else 80)}"

    def open_port():
        s = serial.Serial()
        s.port, s.baudrate, s.timeout, s.dtr, s.rts = port, 115200, 0.2, False, False
        s.open()
        return s

    deadline = time.monotonic() + seconds
    s = None
    while s is None:
        try:
            s = open_port()
        except (serial.SerialException, OSError):
            if time.monotonic() > deadline:
                raise ProvisionError(f"cannot open {port} for the boot log")
            time.sleep(0.3)
    s.rts = True
    time.sleep(0.1)
    s.rts = False
    wifi_ok = ota_conn = ota_done = False
    buf = b""
    try:
        while time.monotonic() < deadline and not (wifi_ok and ota_done):
            try:
                data = s.read(4096)
            except (serial.SerialException, OSError):
                s.close()
                while time.monotonic() < deadline:
                    try:
                        s = open_port()
                        break
                    except (serial.SerialException, OSError):
                        time.sleep(0.2)
                continue
            buf += data
            while b"\n" in buf:
                raw, buf = buf.split(b"\n", 1)
                line = _ANSI.sub("", raw.decode("utf-8", "replace")).rstrip("\r")
                print("  | " + redact(line))
                if "WifiBoard: Connected to WiFi: " in line:
                    joined = line.split("Connected to WiFi: ", 1)[1].strip()
                    wifi_ok = wifi_ok or ssid is None or joined == ssid
                if host_port and f"Established new connection to {host_port} " in line:
                    ota_conn = True
                if ota_conn and ("Activation done" in line or "Current is the latest version" in line
                                 or "New version available" in line):
                    ota_done = True
    finally:
        s.close()
    return wifi_ok, ota_done


# --- main ------------------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", help="serial port (default: the single /dev/cu.usbmodem*)")
    ap.add_argument("--dry-run", action="store_true", help="stop after generating and checking the new image")
    ap.add_argument("--partition-table", type=Path,
                    help="partitions CSV or binary that must agree with the device's NVS entry")
    ap.add_argument("--tail-seconds", type=float, default=60.0, help="boot log capture limit after the write")
    args = ap.parse_args(argv)

    try:
        inputs, warnings = read_inputs(dict(os.environ))
        redact = Redactor(inputs)
        if inputs.ssid is None and inputs.ota_url is None and not args.dry_run:
            raise ProvisionError("set WIFI_SSID + WIFI_PASSWORD and/or OTA_URL (or use --dry-run)")
        for w in warnings:
            print(f"warning: {w}")
        if inputs.ota_url:
            host = urlsplit(inputs.ota_url).hostname
            addrs = mac_ipv4_addresses()
            if re.fullmatch(r"\d+\.\d+\.\d+\.\d+", host or "") and addrs and host not in addrs:
                print("warning: OTA_URL host is not an IPv4 address of this Mac; current: "
                      + ", ".join(f"{i} {a}" for a, i in addrs.items()))
        check_esptool()
        port = resolve_port(args.port)
        print(f"port: {port}")
        ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")

        with tempfile.TemporaryDirectory(prefix="usb-provision-") as tmp_name:
            tmp = Path(tmp_name)
            id_out = run_esptool(port, "flash-id")
            mac = next((l.split()[-1] for l in id_out.splitlines() if l.startswith("MAC:")), "")
            print(f"mac: {mac or 'unknown'}")
            offset, size = find_nvs(parse_partition_table(read_flash(port, PT_OFFSET, PT_SIZE, tmp)))
            print(f"partition '{NVS_LABEL}': offset 0x{offset:x} size 0x{size:x} (device partition table)")
            if args.partition_table:
                f_off, f_size = find_nvs(parse_partition_table(args.partition_table.read_bytes()))
                if (f_off, f_size) != (offset, size):
                    raise ProvisionError(f"{args.partition_table}: nvs at 0x{f_off:x}/0x{f_size:x} differs from the device")
                print(f"partition '{NVS_LABEL}': matches {args.partition_table}")

            t0 = time.monotonic()
            current = read_flash(port, offset, size, tmp)
            backup, digest = write_backup(current, mac, ts, BACKUP_DIR)
            print(f"backup: {backup} ({len(current)} B, {time.monotonic() - t0:.1f} s) sha256 {digest}")
            print(f"restore: .venv/bin/esptool --chip esp32s3 --port {port} write-flash 0x{offset:x} {backup}")

            entries, parse_warnings = parse_nvs(current)
            for w in parse_warnings:
                print(f"warning: {w}")
            print(f"current entries: {len(entries)}; saved SSIDs: {len(saved_ssids(entries))}")
            print("\n".join(describe(entries)))

            merged, dropped = merge(entries, inputs)
            if dropped:
                print(f"dropped (Wi-Fi driver cache): {len(dropped)} entries in {', '.join(sorted({e.namespace for e in dropped}))}")
            if inputs.ssid is not None:
                print(f"wifi list: new SSID at index 0, {len(saved_ssids(merged))} saved SSIDs")
            print("wifi/ota_url: " + ("kept" if inputs.ota_url is None else "set" if inputs.ota_url else "cleared (CONFIG_OTA_URL)"))

            csv_path = tmp / "nvs.csv"
            _private_write(csv_path, entries_to_csv(merged, tmp).encode())
            image = generate_image(csv_path, size, redact)
            regenerated, regen_warnings = parse_nvs(image)
            if regen_warnings or as_map(regenerated) != as_map(merged):
                raise ProvisionError("generated image does not decode to the intended entries")
            new_path, new_digest = write_backup(image, mac, ts, BACKUP_DIR, ".new")
            print(f"new image: {new_path} sha256 {new_digest}; decodes to {len(regenerated)} entries as intended")

            if args.dry_run:
                print("dry run: device not written")
                return 0

            t0 = time.monotonic()
            run_esptool(port, "write-flash", hex(offset), str(new_path))
            print(f"write-flash 0x{offset:x}: {size} B in {time.monotonic() - t0:.1f} s, hash verified")

        print("boot log (redacted):")
        wifi_ok, ota_ok = tail_boot_log(port, args.tail_seconds, redact, inputs.ssid,
                                        expected_ota_url(inputs, merged))
        print(f"wifi joined: {'yes' if wifi_ok else 'NO'}; OTA check on the expected URL: {'yes' if ota_ok else 'NO'}")
        return 0 if wifi_ok and ota_ok else 3
    except ProvisionError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
