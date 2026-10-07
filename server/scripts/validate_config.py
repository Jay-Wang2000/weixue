#!/usr/bin/env python3
"""Validate a rendered data/.config.yaml before it is published.

Usage: validate_config.py CONFIG [DEFAULTS]

DEFAULTS is the upstream config.yaml the server merges CONFIG over (default:
/opt/xiaozhi-esp32-server/config.yaml, the path inside the server image). Prints one
line per problem, never a value, and exits 1 when there is any.
"""
import re
import sys

import yaml

MODULES = ("VAD", "ASR", "LLM", "TTS")
MAC = re.compile(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$")


def merge(default, custom):
    if not isinstance(default, dict) or not isinstance(custom, dict):
        return custom
    merged = dict(default)
    for key, value in custom.items():
        merged[key] = merge(merged[key], value) if key in merged else value
    return merged


def problems(custom, defaults):
    if not isinstance(custom, dict):
        return ["config is not a mapping"]
    found = []
    config = merge(defaults, custom)
    server = config.get("server") or {}
    if not str(server.get("websocket", "")).startswith("ws://"):
        found.append("server.websocket is not a ws:// URL")
    auth = server.get("auth") or {}
    if not isinstance(auth.get("enabled"), bool):
        found.append("server.auth.enabled is not a boolean")
    elif auth["enabled"]:
        devices = custom.get("server", {}).get("auth", {}).get("allowed_devices")
        if not isinstance(devices, list) or not devices:
            found.append("server.auth.allowed_devices is empty while auth is enabled")
        elif not all(isinstance(d, str) and MAC.match(d) for d in devices):
            found.append("server.auth.allowed_devices has an entry that is not a lower-case MAC")
    selected = config.get("selected_module") or {}
    for module in MODULES:
        name = selected.get(module)
        if not name or name not in (config.get(module) or {}):
            found.append(f"selected_module.{module} names no {module} entry")
    llm = (config.get("LLM") or {}).get(selected.get("LLM"), {})
    key = llm.get("api_key") if isinstance(llm, dict) else None
    if "api_key" in (llm or {}) and (not isinstance(key, str) or not key.strip() or "你" in key):
        found.append(f"LLM.{selected.get('LLM')}.api_key is empty")
    if not str(config.get("mcp_endpoint", "")).startswith("ws://"):
        found.append("mcp_endpoint is not a ws:// URL")
    return found


def main(argv):
    if len(argv) not in (2, 3):
        sys.exit(__doc__.strip().splitlines()[2])
    defaults_path = argv[2] if len(argv) == 3 else "/opt/xiaozhi-esp32-server/config.yaml"
    try:
        with open(argv[1], encoding="utf-8") as f:
            custom = yaml.safe_load(f)
        with open(defaults_path, encoding="utf-8") as f:
            defaults = yaml.safe_load(f)
    except (OSError, yaml.YAMLError) as e:
        print(f"validate_config: cannot load YAML ({type(e).__name__})")
        return 1
    found = problems(custom, defaults)
    for problem in found:
        print(f"validate_config: {problem}")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
