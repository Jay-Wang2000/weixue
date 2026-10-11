"""server/scripts: setup.sh publish/rollback (fake docker), validate_config.py, check_ready.py,
the dependency lock and the network boundary in compose and the config template."""
import asyncio
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
import yaml

from tests.conftest import SERVER_DIR, VENDOR_DIR

SCRIPTS = SERVER_DIR / "scripts"
UPSTREAM_CONFIG = VENDOR_DIR / "config.yaml"
VENDOR_ROOT = VENDOR_DIR.parent.parent.parent
ENV = {
    "LAN_IP": "192.168.1.10",
    "BIND_IP": "0.0.0.0",
    "TZ": "America/Los_Angeles",
    "TZ_OFFSET": "-7",
    "ANTHROPIC_API_KEY": "sk-test",
    "GOOGLE_SA_FILE": "/dev/null",
    "HA_MCP_URL": "http://ha.local:9584/redacted",
    "ALLOWED_DEVICES": "7c:0c:5f:42:4b:fc",
    "MCP_ENDPOINT": "ws://mcp-endpoint-server:8004/mcp_endpoint/mcp/?token=t",
}


def _render(**overrides):
    env = {**os.environ, **ENV, **overrides}
    result = subprocess.run(
        [sys.executable, str(SCRIPTS / "render_config.py"), str(SERVER_DIR / "config" / "config.template.yaml")],
        env=env, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def _validate(tmp_path, text):
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    return subprocess.run(
        [sys.executable, str(SCRIPTS / "validate_config.py"), str(path), str(UPSTREAM_CONFIG)],
        capture_output=True, text=True,
    )


# ------------------------------------------------------------------ validate_config.py


def test_rendered_template_is_valid(tmp_path):
    result = _validate(tmp_path, _render())
    assert result.returncode == 0, result.stdout


@pytest.mark.parametrize(
    "mutate, problem",
    [
        (lambda c: c["LLM"]["AnthropicLLM"].update(api_key=" "), "api_key is empty"),
        (lambda c: c["server"]["auth"].update(allowed_devices=[]), "allowed_devices is empty"),
        (lambda c: c["server"]["auth"].pop("allowed_devices"), "allowed_devices is empty"),
        (lambda c: c["server"]["auth"].update(allowed_devices=["7C:0C:5F:42:4B:FC"]), "lower-case MAC"),
        (lambda c: c["server"]["auth"].update(enabled="yes"), "not a boolean"),
        (lambda c: c["selected_module"].update(TTS="NoSuchTTS"), "selected_module.TTS"),
        (lambda c: c.update(mcp_endpoint=""), "mcp_endpoint"),
        (lambda c: c["server"].update(websocket="http://x"), "server.websocket"),
    ],
)
def test_invalid_configs_are_reported_without_values(tmp_path, mutate, problem):
    config = yaml.safe_load(_render())
    mutate(config)
    result = _validate(tmp_path, yaml.safe_dump(config, allow_unicode=True))
    assert result.returncode == 1
    assert problem in result.stdout
    assert "sk-test" not in result.stdout


@pytest.mark.parametrize(
    "voice, llm, intent",
    [(None, "AnthropicLLM", "function_call"), ("", "AnthropicLLM", "function_call"),
     ("anthropic", "AnthropicLLM", "function_call"), ("claude_agent", "ClaudeAgentLLM", "nointent")],
)
def test_voice_llm_selects_the_llm_and_intent(tmp_path, monkeypatch, voice, llm, intent):
    monkeypatch.delenv("VOICE_LLM", raising=False)
    text = _render() if voice is None else _render(VOICE_LLM=voice)
    config = yaml.safe_load(text)
    assert config["selected_module"]["LLM"] == llm and config["selected_module"]["Intent"] == intent
    assert config["LLM"]["ClaudeAgentLLM"] == {"type": "claude_agent"}
    assert config["LLM"]["AnthropicLLM"]["type"] == "anthropic"
    assert _validate(tmp_path, text).returncode == 0


def test_unknown_voice_llm_is_refused():
    env = {**os.environ, **ENV, "VOICE_LLM": "groq"}
    result = subprocess.run(
        [sys.executable, str(SCRIPTS / "render_config.py"), str(SERVER_DIR / "config" / "config.template.yaml")],
        env=env, capture_output=True, text=True,
    )
    assert result.returncode == 1 and "VOICE_LLM must be one of claude_agent, anthropic" in result.stderr


def test_claude_agent_with_function_call_is_reported(tmp_path):
    config = yaml.safe_load(_render(VOICE_LLM="claude_agent"))
    config["selected_module"]["Intent"] = "function_call"
    result = _validate(tmp_path, yaml.safe_dump(config, allow_unicode=True))
    assert result.returncode == 1 and "ClaudeAgentLLM needs an Intent of type nointent" in result.stdout


def test_agent_service_env_reaches_the_server_and_the_switch_is_documented():
    environment = yaml.safe_load((SERVER_DIR / "docker-compose.yml").read_text())["services"]["xiaozhi-esp32-server"]["environment"]
    assert {"AGENT_SERVICE_URL=http://${AGENT_SERVICE_HOST:-agent-service}:8010",
            "AGENT_SERVICE_TOKEN=${AGENT_SERVICE_TOKEN:-}"} <= set(environment)
    assert "VOICE_LLM=anthropic" in (SERVER_DIR / ".env.example").read_text().splitlines()


def test_unparsable_yaml_is_rejected(tmp_path):
    result = _validate(tmp_path, "server: [unclosed\n  api_key: sk-secret")
    assert result.returncode == 1 and "cannot load YAML" in result.stdout
    assert "sk-secret" not in result.stdout


# ------------------------------------------------------------------ network boundary, lock


@pytest.mark.parametrize(
    "value, devices",
    [
        ("7c:0c:5f:42:4b:fc", ["7c:0c:5f:42:4b:fc"]),
        (" 7c:0c:5f:42:4b:fc, aa:bb:cc:dd:ee:ff ,", ["7c:0c:5f:42:4b:fc", "aa:bb:cc:dd:ee:ff"]),
        ("", []),
    ],
)
def test_template_takes_the_allow_list_from_allowed_devices(value, devices):
    auth = yaml.safe_load(_render(ALLOWED_DEVICES=value))["server"]["auth"]
    assert auth == {"enabled": True, "allowed_devices": devices}


def test_render_fails_without_allowed_devices():
    env = {**os.environ, **ENV}
    env.pop("ALLOWED_DEVICES")
    result = subprocess.run(
        [sys.executable, str(SCRIPTS / "render_config.py"), str(SERVER_DIR / "config" / "config.template.yaml")],
        env=env, capture_output=True, text=True,
    )
    assert result.returncode == 1 and "ALLOWED_DEVICES is not set" in result.stderr


def test_env_example_lists_the_device():
    lines = (SERVER_DIR / ".env.example").read_text().splitlines()
    assert "ALLOWED_DEVICES=7c:0c:5f:42:4b:fc" in lines


def test_env_example_sets_every_required_key():
    setup = (SCRIPTS / "setup.sh").read_text()
    required = re.search(r"^REQUIRED_KEYS=\((.*)\)$", setup, re.M).group(1).split()
    keys = {line.split("=")[0] for line in (SERVER_DIR / ".env.example").read_text().splitlines() if "=" in line and not line.startswith("#")}
    assert {"BIND_IP", "ALLOWED_DEVICES"} <= set(required) <= keys


def test_ota_and_websocket_bind_to_bind_ip_or_lan_ip():
    ports = yaml.safe_load((SERVER_DIR / "docker-compose.yml").read_text())["services"]["xiaozhi-esp32-server"]["ports"]
    assert ports == [
        "${BIND_IP:-${LAN_IP:?set LAN_IP in server/.env}}:8000:8000",
        "${BIND_IP:-${LAN_IP:?set LAN_IP in server/.env}}:8003:8003",
    ]


def test_server_gets_tz_only_as_set_in_env():
    environment = yaml.safe_load((SERVER_DIR / "docker-compose.yml").read_text())["services"]["xiaozhi-esp32-server"]["environment"]
    assert {"TZ=${TZ:-UTC}", "WEIXUE_TZ=${TZ:-}"} <= set(environment)


def _pins(path):
    pins = {}
    for line in path.read_text().splitlines():
        line = line.split("#")[0].strip()
        if line:
            name, version = line.split("==")
            pins[re.sub(r"[-_.]+", "-", name).lower()] = version
    return pins


def test_lock_pins_every_direct_requirement_at_the_same_version():
    direct = _pins(SERVER_DIR / "requirements-overlay.txt")
    locked = _pins(SERVER_DIR / "requirements-overlay.lock")
    assert direct and all(locked.get(name) == version for name, version in direct.items())
    assert {"google-api-core", "grpcio", "protobuf", "httpx2", "pydantic"} <= set(locked)


def test_image_installs_the_lock_without_resolution_and_runs_pip_check():
    dockerfile = (SERVER_DIR / "Dockerfile").read_text()
    assert "--no-deps" in dockerfile and "-r /tmp/requirements-overlay.lock" in dockerfile
    assert "&& pip check" in dockerfile
    assert "requirements-overlay.txt" not in dockerfile


# ------------------------------------------------------------------ check_ready.py


class _Ota(BaseHTTPRequestHandler):
    def do_GET(self):
        body = "OTA接口运行正常，向设备发送的websocket地址是：ws://x".encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


def _ws_server(port_box, stop):
    import websockets

    async def handler(ws):
        await ws.send("端口正常，如需测试连接，请启动digital-human测试")
        await ws.close()

    async def main():
        async with websockets.serve(handler, "127.0.0.1", 0) as server:
            port_box.append(server.sockets[0].getsockname()[1])
            while not stop.is_set():
                await asyncio.sleep(0.05)

    asyncio.run(main())


def _check_ready(*args):
    return subprocess.run(
        [sys.executable, str(SCRIPTS / "check_ready.py"), "127.0.0.1", *args],
        capture_output=True, text=True, timeout=30,
    )


def test_check_ready_passes_on_ota_banner_and_websocket_frame():
    http = HTTPServer(("127.0.0.1", 0), _Ota)
    threading.Thread(target=http.serve_forever, daemon=True).start()
    port_box, stop = [], threading.Event()
    threading.Thread(target=_ws_server, args=(port_box, stop), daemon=True).start()
    while not port_box:
        pass
    try:
        result = _check_ready("--timeout", "10", "--ota-port", str(http.server_port), "--ws-port", str(port_box[0]))
        assert result.returncode == 0, result.stdout
        down = _check_ready("--timeout", "1", "--ota-port", str(http.server_port), "--ws-port", "1")
        assert down.returncode == 1 and "OTA ok, WebSocket down" in down.stdout
    finally:
        stop.set()
        http.shutdown()


# ------------------------------------------------------------------ setup.sh with a fake docker

FAKE_DOCKER = r'''#!PYTHON
import json, os, subprocess, sys
state_path = os.environ["FAKE_STATE"]
state = json.load(open(state_path))
args = sys.argv[1:]
state["log"].append(" ".join(args))
fails = os.environ.get("FAKE_FAIL", "").split(",")
code = 0
if args[:1] == ["info"]:
    pass
elif args[:2] == ["image", "inspect"]:
    code = 0 if args[2] in state["tags"] else 1
elif args[:2] == ["rm", "-f"]:
    state.setdefault("removed", []).extend(args[2:])
elif args[:1] == ["tag"]:
    state["tags"][args[2]] = state["tags"][args[1]]
elif args[:2] == ["logs", "mcp-endpoint-server"]:
    print("ws://172.18.0.2:8004/mcp_endpoint/mcp/?token=" + ("new" if "new-key" in fails else "abc"))
elif args[:1] == ["exec"]:
    target = [a for a in args[1:] if not a.startswith("-")][0]
    state.setdefault("execs", []).append(target)
    code = 1 if f"exec-{target}" in fails and os.environ.get("ATTEMPT") == "new" else 0
elif args[:2] == ["compose", "build"]:
    if "build" in fails:
        code = 1
    else:
        state["tags"]["weixue/mcp-bridge:local"] = "bridge-new"
        state["tags"]["weixue/agent-service:local"] = "agent-new"
        state["tags"]["weixue/agent-monitor:local"] = "monitor-new"
        state["tags"]["weixue/xiaozhi-esp32-server:local"] = "server-new"
elif args[:2] == ["compose", "up"]:
    if args[-1] == "mcp-endpoint-server" and "new-key" in fails and "--force-recreate" not in args:
        open("data/mcp-endpoint/.mcp-endpoint-server.cfg", "w").write("regenerated key")
    if "--force-recreate" in args:
        state.setdefault("recreated", []).append(args[args.index("--force-recreate") + 1:])
        state["recreates"] = n = state.get("recreates", 0) + 1
        state.setdefault("up_env", []).append([os.environ.get("BIND_IP"), os.environ.get("COMPOSE_PROFILES")])
        db = "data/agent-service/db/agent.db"
        if n == 1 and os.environ.get("FAKE_MIGRATE") and os.path.exists(db):
            open(db, "w").write("migrated by the new image")
        if ("up" in fails and n == 1) or ("rollback-up" in fails and n == 2):
            code = 1
        else:
            state["running"].append(state["tags"]["weixue/xiaozhi-esp32-server:local"])
elif args[:1] == ["stop"]:
    state.setdefault("stopped", []).extend(args[1:])
    state.setdefault("events", []).append("stop " + " ".join(args[1:]))
elif args[:1] == ["start"]:
    state.setdefault("started", []).extend(args[1:])
    state["stopped"] = [c for c in state.get("stopped", []) if c not in args[1:]]
elif args[:1] == ["inspect"]:
    running = ("agent-running" in fails or args[-1] not in state.get("stopped", [])) and not (
        args[-1] == "mcp-endpoint-server" and "endpoint-down" in fails)
    print("true" if running else "false")
elif args[:1] == ["run"]:
    mounts = dict(a.split(":")[1::-1] for a in args if a.count(":") == 2 and a.endswith(":ro"))
    script, config = args[-2], args[-1]
    code = subprocess.run([os.environ["FAKE_PYTHON"], mounts[script], mounts[config], os.environ["FAKE_DEFAULTS"]]).returncode
else:
    code = 2
json.dump(state, open(state_path, "w"))
sys.exit(code)
'''

READY_STUB = '''#!/usr/bin/env python3
import os, sys
count_file = os.environ["FAKE_READY_COUNT"]
n = int(open(count_file).read()) if os.path.exists(count_file) else 0
open(count_file, "w").write(str(n + 1))
sys.exit(0 if os.environ.get("FAKE_READY", "ok") == "ok" or (os.environ["FAKE_READY"] == "fail-new" and n > 0) else 1)
'''

FAKE_MV = """#!/bin/sh
# Fails once per run (FAKE_MV_MARK) for an argument that contains FAKE_MV_FAIL.
if [ -n "${FAKE_MV_FAIL:-}" ] && [ ! -e "$FAKE_MV_MARK" ]; then
  for a in "$@"; do
    case "$a" in *"$FAKE_MV_FAIL"*) touch "$FAKE_MV_MARK"; echo "mv: injected failure" >&2; exit 1 ;; esac
  done
fi
exec /bin/mv "$@"
"""

FAKE_UNAME = """#!/bin/sh
echo "${FAKE_UNAME:-Darwin}"
"""

FAKE_IP = """#!/bin/sh
echo '1: lo    inet 127.0.0.1/8 scope host lo\\       valid_lft forever preferred_lft forever'
echo '2: eth0    inet 10.9.8.7/24 brd 10.9.8.255 scope global eth0\\       valid_lft forever preferred_lft forever'
echo '2: eth0    inet 10.9.8.50/24 brd 10.9.8.255 scope global secondary eth0:1\\       valid_lft forever preferred_lft forever'
echo '5: wg0    inet 10.8.0.2 peer 10.8.0.1/32 scope global wg0\\       valid_lft forever preferred_lft forever'
"""

OLD_CONFIG = "old: config\n"


@pytest.fixture
def setup_tree(tmp_path):
    root = tmp_path / "repo"
    server = root / "server"
    shutil.copytree(SCRIPTS, server / "scripts")
    shutil.copytree(SERVER_DIR / "config", server / "config")
    shutil.copy(SERVER_DIR.parent / "vendor.lock", root / "vendor.lock")
    (server / "scripts" / "check_ready.py").write_text(READY_STUB)
    sa = tmp_path / "sa.json"
    sa.write_text("{}")
    env_file = {**ENV, "GOOGLE_SA_FILE": str(sa)}
    env_file.pop("MCP_ENDPOINT")
    (server / ".env").write_text("".join(f"{k}={v}\n" for k, v in env_file.items()))
    model = server / "data" / "models" / "SenseVoiceSmall" / "model.pt"
    model.parent.mkdir(parents=True)
    with open(model, "wb") as f:
        f.truncate(900_000_001)
    (server / "data" / "mcp-endpoint").mkdir()
    (server / "data" / "mcp-endpoint" / ".mcp-endpoint-server.cfg").write_text("key")
    (server / "data" / ".config.yaml").write_text(OLD_CONFIG)
    os.chmod(server / "data" / ".config.yaml", 0o600)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(FAKE_DOCKER.replace("PYTHON", sys.executable, 1))
    docker.chmod(0o755)
    for name, body in {"uname": FAKE_UNAME, "ip": FAKE_IP, "mv": FAKE_MV}.items():
        (bin_dir / name).write_text(body)
        (bin_dir / name).chmod(0o755)
    state = tmp_path / "state.json"
    state.write_text(json.dumps({
        "log": [], "running": [],
        "tags": {"weixue/mcp-bridge:local": "bridge-old", "weixue/xiaozhi-esp32-server:local": "server-old"},
    }))
    env = {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "HOME": str(tmp_path / "home"),
        "WEIXUE_VENDOR": str(VENDOR_ROOT),
        "FAKE_STATE": str(state),
        "FAKE_PYTHON": sys.executable,
        "FAKE_DEFAULTS": str(UPSTREAM_CONFIG),
        "FAKE_READY_COUNT": str(tmp_path / "ready-count"),
        "FAKE_MV_MARK": str(tmp_path / "mv-failed"),
        "DEPLOY_WAIT_S": "1",
    }

    def set_env(**values):
        env_path = server / ".env"
        lines = [l for l in env_path.read_text().splitlines() if l.split("=")[0] not in values]
        env_path.write_text("".join(f"{l}\n" for l in lines + [f"{k}={v}" for k, v in values.items() if v is not None]))

    def run(**extra):
        state.write_text(json.dumps({**json.loads(state.read_text()), "recreates": 0}))
        (tmp_path / "mv-failed").unlink(missing_ok=True)
        result = subprocess.run(
            ["bash", str(server / "scripts" / "setup.sh")], env={**env, **extra},
            capture_output=True, text=True, timeout=120,
        )
        return result, json.loads(state.read_text())

    run.set_env = set_env
    run.ready_count = lambda: int((tmp_path / "ready-count").read_text()) if (tmp_path / "ready-count").exists() else 0
    return server, run


def _leftovers(server):
    return sorted(p.name for p in (server / "data").rglob("*.XXXXXX*")) + sorted(
        p.name for p in (server / "data").glob(".config.yaml.?*") if not p.name.endswith(".rollback")
    )


def _mode(path):
    return stat.S_IMODE(path.stat().st_mode)


def test_setup_publishes_validated_config_and_new_images(setup_tree):
    server, run = setup_tree
    result, state = run()
    assert result.returncode == 0, result.stdout + result.stderr
    config = server / "data" / ".config.yaml"
    assert yaml.safe_load(config.read_text())["server"]["auth"]["enabled"] is True
    assert _mode(config) == 0o600
    assert (server / "data" / ".config.yaml.rollback").read_text() == OLD_CONFIG
    endpoint = server / "data" / "mcp-bridge" / "endpoint-url"
    assert endpoint.read_text() == "ws://mcp-endpoint-server:8004/mcp_endpoint/mcp/?token=abc\n"
    assert _mode(endpoint) == 0o600
    assert state["running"] == ["server-new"]
    assert state["tags"]["weixue/xiaozhi-esp32-server:rollback"] == "server-old"
    assert _leftovers(server) == []
    assert "sk-test" not in result.stdout + result.stderr
    assert state["execs"] == ["mcp-bridge", "xiaozhi-esp32-server", "agent-service", "agent-monitor"]
    assert ("ready: bridge healthy, Home Assistant answered via the voice path, agent-service healthy, "
            "agent-monitor healthy") in result.stdout
    assert not list((server / "data").rglob("*.cfg.rollback")) and not (server / "data" / "mcp-bridge" / "endpoint-url.rollback").exists()


@pytest.mark.parametrize(
    "extra, built",
    [
        ({"TZ_OFFSET": "seven"}, False),
        ({"FAKE_FAIL": "build"}, True),
        ({"ANTHROPIC_API_KEY": '" "'}, True),
    ],
)
def test_failed_render_build_or_validation_changes_nothing(setup_tree, extra, built):
    server, run = setup_tree
    if "TZ_OFFSET" in extra or "ANTHROPIC_API_KEY" in extra:
        env_path = server / ".env"
        lines = [l for l in env_path.read_text().splitlines() if not l.startswith(tuple(extra))]
        env_path.write_text("\n".join(lines + [f"{k}={v}" for k, v in extra.items() if not k.startswith("FAKE")]) + "\n")
    result, state = run(**{k: v for k, v in extra.items() if k.startswith("FAKE")})
    assert result.returncode != 0
    assert (server / "data" / ".config.yaml").read_text() == OLD_CONFIG
    assert not (server / "data" / "mcp-bridge" / "endpoint-url").exists()
    assert state["running"] == []
    assert state["tags"]["weixue/xiaozhi-esp32-server:local"] == "server-old"
    assert state["tags"]["weixue/mcp-bridge:local"] == "bridge-old"
    assert any(l.startswith("compose build") for l in state["log"]) is built
    assert _leftovers(server) == []


def test_failed_readiness_restores_previous_config_and_images(setup_tree):
    server, run = setup_tree
    result, state = run(FAKE_READY="fail-new")
    assert result.returncode == 1
    assert "rolling back" in result.stdout
    assert (server / "data" / ".config.yaml").read_text() == OLD_CONFIG
    assert state["running"] == ["server-new", "server-old"]
    assert state["tags"]["weixue/xiaozhi-esp32-server:local"] == "server-old"
    assert state["tags"]["weixue/mcp-bridge:local"] == "bridge-old"
    assert "previous version is not ready" not in result.stdout


@pytest.mark.parametrize(
    "values, message",
    [
        ({"BIND_IP": None}, "server/.env must set BIND_IP"),
        ({"BIND_IP": ""}, "server/.env must set BIND_IP"),
        ({"ALLOWED_DEVICES": None}, "server/.env must set ALLOWED_DEVICES"),
        ({"ALLOWED_DEVICES": "", "LAN_IP": None}, "server/.env must set LAN_IP ALLOWED_DEVICES"),
        ({"TZ": None}, "server/.env must set TZ"),
    ],
)
def test_preflight_fails_before_any_docker_call(setup_tree, values, message):
    server, run = setup_tree
    run.set_env(**values)
    env_before = (server / ".env").read_text()
    result, state = run()
    assert result.returncode == 1
    assert message in result.stdout
    if "must set" in message:
        assert "required: LAN_IP BIND_IP ALLOWED_DEVICES TZ TZ_OFFSET ANTHROPIC_API_KEY GOOGLE_SA_FILE HA_MCP_URL" in result.stdout
    assert state["log"] == []
    assert (server / ".env").read_text() == env_before
    assert (server / "data" / ".config.yaml").read_text() == OLD_CONFIG


@pytest.mark.parametrize(
    "system, bind_ip, accepted",
    [
        ("Darwin", "0.0.0.0", True),
        ("Darwin", "127.0.0.1", True),
        ("Darwin", "10.9.8.7", False),
        ("Linux", "0.0.0.0", True),
        ("Linux", "127.0.0.1", True),
        ("Linux", "10.9.8.7", True),
        ("Linux", "10.9.8.50", True),
        ("Linux", "10.8.0.2", True),
        ("Linux", "10.8.0.1", False),
        ("Linux", "10.9.8.70", False),
        ("Linux", "10.9.8.7/24", False),
    ],
)
def test_preflight_bind_ip_per_system(setup_tree, system, bind_ip, accepted):
    server, run = setup_tree
    run.set_env(BIND_IP=bind_ip)
    result, state = run(FAKE_UNAME=system)
    if accepted:
        assert result.returncode == 0, result.stdout + result.stderr
        assert state["running"] == ["server-new"]
    else:
        assert result.returncode == 1
        expected = "on macOS use 0.0.0.0 or 127.0.0.1" if system == "Darwin" else "is not 0.0.0.0, 127.0.0.1 or an IPv4 address on a local interface"
        assert f"BIND_IP={bind_ip}" in result.stdout and expected in result.stdout
        assert state["log"] == []


def test_success_snapshots_env_and_rollback_restores_it(setup_tree):
    server, run = setup_tree
    result, _ = run()
    assert result.returncode == 0, result.stdout + result.stderr
    deployed = server / ".env.deployed"
    assert deployed.read_text() == (server / ".env").read_text()
    assert _mode(deployed) == 0o600
    previous = deployed.read_text()
    run.set_env(BIND_IP="127.0.0.1")
    attempted = (server / ".env").read_text()
    result, state = run(FAKE_FAIL="up")
    assert result.returncode == 1
    assert "server/.env restored from the last successful run" in result.stdout
    assert (server / ".env").read_text() == previous
    assert (server / ".env.failed").read_text() == attempted
    assert _mode(server / ".env.failed") == 0o600
    assert state["up_env"][-2:] == [["127.0.0.1", None], ["0.0.0.0", None]]
    result, _ = run()
    assert result.returncode == 0 and not (server / ".env.failed").exists()


def test_failed_up_takes_the_rollback_path(setup_tree):
    server, run = setup_tree
    result, state = run(FAKE_FAIL="up")
    assert result.returncode == 1
    assert "docker compose up failed; rolling back" in result.stdout
    assert "manual restore" not in result.stdout
    assert (server / "data" / ".config.yaml").read_text() == OLD_CONFIG
    assert state["running"] == ["server-old"]
    assert state["tags"]["weixue/xiaozhi-esp32-server:local"] == "server-old"
    assert state["tags"]["weixue/mcp-bridge:local"] == "bridge-old"
    assert run.ready_count() == 1


@pytest.mark.parametrize(
    "fail, ready, running",
    [
        ("up,rollback-up", "ok", []),
        ("rollback-up", "fail-new", ["server-new"]),
    ],
)
def test_failed_rollback_up_prints_the_manual_restore(setup_tree, fail, ready, running):
    server, run = setup_tree
    result, state = run(FAKE_FAIL=fail, FAKE_READY=ready)
    assert result.returncode == 1
    out = result.stdout
    assert "rollback failed: the stack is down; manual restore:" in out
    block = out.split("manual restore:\n")[1].splitlines()
    assert block[:4] == [
        f"  cd {server}",
        "  docker tag weixue/mcp-bridge:rollback weixue/mcp-bridge:local",
        "  docker tag weixue/xiaozhi-esp32-server:rollback weixue/xiaozhi-esp32-server:local",
        "  docker compose up -d --force-recreate mcp-bridge xiaozhi-esp32-server",
    ]
    assert block[4].startswith("docker-compose.yml is not rolled back")
    assert (server / "data" / ".config.yaml").read_text() == OLD_CONFIG
    assert state["tags"]["weixue/xiaozhi-esp32-server:local"] == "server-old"
    assert state["running"] == running
    assert run.ready_count() == (0 if ready == "ok" else 1)


# ------------------------------------------------------------------ deployment readiness and the endpoint unit


@pytest.mark.parametrize(
    "fail, message",
    [
        ("exec-mcp-bridge", "mcp-bridge not ready after 1s"),
        ("exec-xiaozhi-esp32-server", "Home Assistant via the voice path not ready after 1s"),
    ],
)
def test_unready_bridge_or_ha_path_rolls_back_and_keeps_the_deployed_env(setup_tree, fail, message):
    server, run = setup_tree
    result, _ = run()
    assert result.returncode == 0, result.stdout + result.stderr
    deployed = (server / ".env.deployed").read_text()
    run.set_env(ATTEMPT="new")
    attempted = (server / ".env").read_text()
    result, state = run(FAKE_FAIL=fail)
    assert result.returncode == 1
    assert message in result.stdout and "new version is not ready; rolling back" in result.stdout
    assert (server / ".env.deployed").read_text() == deployed
    assert (server / ".env").read_text() == deployed and (server / ".env.failed").read_text() == attempted
    assert state["recreated"][-2:] == [["mcp-bridge", "agent-service", "agent-monitor", "xiaozhi-esp32-server"]] * 2
    assert "previous version is not ready" not in result.stdout


def test_rollback_restores_the_endpoint_url_with_the_config(setup_tree):
    server, run = setup_tree
    url = server / "data" / "mcp-bridge" / "endpoint-url"
    url.parent.mkdir(parents=True, exist_ok=True)
    url.write_text("ws://mcp-endpoint-server:8004/mcp_endpoint/mcp/?token=old\n")
    os.chmod(url, 0o600)
    result, state = run(FAKE_READY="fail-new")
    assert result.returncode == 1
    assert url.read_text().endswith("token=old\n") and _mode(url) == 0o600
    assert (server / "data" / ".config.yaml").read_text() == OLD_CONFIG
    assert state["recreated"][-1] == ["mcp-bridge", "xiaozhi-esp32-server"]


def test_rollback_restores_a_regenerated_endpoint_key_and_recreates_the_endpoint(setup_tree):
    server, run = setup_tree
    key = server / "data" / "mcp-endpoint" / ".mcp-endpoint-server.cfg"
    run.set_env(ATTEMPT="new")
    result, state = run(FAKE_FAIL="new-key,exec-mcp-bridge")
    assert result.returncode == 1
    assert key.read_text() == "key"
    assert "endpoint key restored from the last run" in result.stdout
    assert state["recreated"][-1] == ["mcp-endpoint-server", "mcp-bridge", "xiaozhi-esp32-server"]
    assert "token=new" not in (server / "data" / ".config.yaml").read_text()


def test_new_endpoint_key_publishes_one_url_to_both_consumers(setup_tree):
    server, run = setup_tree
    result, _ = run(FAKE_FAIL="new-key")
    assert result.returncode == 0, result.stdout + result.stderr
    config = yaml.safe_load((server / "data" / ".config.yaml").read_text())
    url = (server / "data" / "mcp-bridge" / "endpoint-url").read_text().strip()
    assert config["mcp_endpoint"] == url and url.endswith("token=new")


def test_check_ha_path_judges_the_answer():
    sys.path.insert(0, str(SCRIPTS))
    try:
        import check_ha_path
    finally:
        sys.path.remove(str(SCRIPTS))

    def result(text, error=False):
        return {"content": [{"type": "text", "text": text}], "isError": error}

    assert check_ha_path.answered(result('{"data": {"entity_id": "zone.home", "state": "0"}}'))
    assert check_ha_path.answered(result("Tool error: ENTITY_NOT_FOUND zone.home"))
    assert not check_ha_path.answered(result("Home Assistant is unreachable right now."))
    assert not check_ha_path.answered(result("Tool error: ConnectError"))
    assert not check_ha_path.answered(result('{"data": 1}', error=True))
    assert not check_ha_path.answered(None)
    assert check_ha_path.caller_url("ws://e:8004/mcp_endpoint/mcp/?token=t") == "ws://e:8004/mcp_endpoint/call/?token=t"


def test_check_ha_path_end_to_end_against_a_fake_endpoint(tmp_path):
    import websockets

    calls = []

    async def handler(ws):
        assert ws.request.path.startswith("/mcp_endpoint/call/")
        async for raw in ws:
            message = json.loads(raw)
            calls.append(message.get("method"))
            if "id" not in message:
                continue
            if message["method"] == "initialize":
                result = {"protocolVersion": "2024-11-05"}
            elif message["method"] == "tools/list":
                result = {"tools": [{"name": "ha_get_state"}]}
            else:
                result = {"content": [{"type": "text", "text": '{"data": {"state": "0"}}'}], "isError": False}
            await ws.send(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}))

    port_box, stop = [], threading.Event()

    def serve():
        async def main():
            async with websockets.serve(handler, "127.0.0.1", 0) as srv:
                port_box.append(srv.sockets[0].getsockname()[1])
                while not stop.is_set():
                    await asyncio.sleep(0.05)

        asyncio.run(main())

    threading.Thread(target=serve, daemon=True).start()
    while not port_box:
        pass
    config = tmp_path / "config.yaml"
    config.write_text(f'mcp_endpoint: "ws://127.0.0.1:{port_box[0]}/mcp_endpoint/mcp/?token=secret-token"\n')
    try:
        ok = subprocess.run([sys.executable, str(SCRIPTS / "check_ha_path.py"), "--config", str(config), "--timeout", "10"],
                            capture_output=True, text=True, timeout=30)
        down = subprocess.run([sys.executable, str(SCRIPTS / "check_ha_path.py"), "--config", str(tmp_path / "missing.yaml")],
                              capture_output=True, text=True, timeout=30)
    finally:
        stop.set()
    assert ok.returncode == 0, ok.stdout + ok.stderr
    assert calls == ["initialize", "notifications/initialized", "tools/list", "tools/call"]
    assert down.returncode == 1 and "config unreadable" in down.stdout
    assert "secret-token" not in ok.stdout + ok.stderr


def test_agent_service_token_is_generated_once_and_never_printed(setup_tree):
    server, run = setup_tree
    result, _ = run()
    assert result.returncode == 0, result.stdout + result.stderr
    tokens = [l.split("=", 1)[1] for l in (server / ".env").read_text().splitlines()
              if l.startswith("AGENT_SERVICE_TOKEN=")]
    assert len(tokens) == 1 and len(tokens[0]) == 64 and int(tokens[0], 16) >= 0
    assert tokens[0] not in result.stdout + result.stderr
    result, _ = run()
    again = [l for l in (server / ".env").read_text().splitlines() if l.startswith("AGENT_SERVICE_TOKEN=")]
    assert again == [f"AGENT_SERVICE_TOKEN={tokens[0]}"]


def test_first_agent_service_deploy_rolls_back_without_it(setup_tree):
    server, run = setup_tree
    result, state = run(FAKE_FAIL="exec-agent-service", ATTEMPT="new")
    assert result.returncode == 1
    assert "agent-service not ready after 1s" in result.stdout and "rolling back" in result.stdout
    assert state["recreated"][-1] == ["mcp-bridge", "xiaozhi-esp32-server"]
    assert "agent-service" in state["removed"]
    assert "previous version is not ready" not in result.stdout


@pytest.mark.parametrize("tunnel, host", [("", None), ("1", "wg")])
def test_agent_service_host_follows_the_tunnel_mode(setup_tree, tunnel, host):
    server, run = setup_tree
    if tunnel:
        (server / ".secrets").mkdir()
        (server / ".secrets" / "wg0.conf").write_text("[Interface]\n")
        os.chmod(server / ".secrets" / "wg0.conf", 0o600)
    run.set_env(WG_TUNNEL=tunnel or None)
    result, _ = run()
    assert result.returncode == 0, result.stdout + result.stderr
    hosts = [l for l in (server / ".env").read_text().splitlines() if l.startswith("AGENT_SERVICE_HOST=")]
    assert hosts == ([f"AGENT_SERVICE_HOST={host}"] if host else [])


def test_monitor_token_is_generated_once_and_never_printed(setup_tree):
    server, run = setup_tree
    result, _ = run()
    assert result.returncode == 0, result.stdout + result.stderr
    env = (server / ".env").read_text().splitlines()
    tokens = [l.split("=", 1)[1] for l in env if l.startswith("MONITOR_TOKEN=")]
    agent = [l.split("=", 1)[1] for l in env if l.startswith("AGENT_SERVICE_TOKEN=")]
    assert len(tokens) == 1 and len(tokens[0]) == 64 and tokens[0] != agent[0]
    assert tokens[0] not in result.stdout + result.stderr
    assert _mode(server / ".env.deployed") == 0o600
    result, _ = run()
    assert [l for l in (server / ".env").read_text().splitlines() if l.startswith("MONITOR_TOKEN=")] == \
        [f"MONITOR_TOKEN={tokens[0]}"]
    assert tokens[0] not in result.stdout + result.stderr


def test_first_monitor_deploy_rolls_back_without_it(setup_tree):
    server, run = setup_tree
    result, state = run()
    assert result.returncode == 0, result.stdout + result.stderr
    state["tags"].pop("weixue/agent-monitor:local")
    (server.parent.parent / "state.json").write_text(json.dumps(state))
    result, state = run(FAKE_FAIL="exec-agent-monitor", ATTEMPT="new")
    assert result.returncode == 1
    assert "agent-monitor not ready after 1s" in result.stdout and "rolling back" in result.stdout
    assert state["recreated"][-1] == ["mcp-bridge", "agent-service", "xiaozhi-esp32-server"]
    assert "agent-monitor" in state["removed"] and "previous version is not ready" not in result.stdout


def test_monitor_is_bound_to_localhost_only():
    services = yaml.safe_load((SERVER_DIR / "docker-compose.yml").read_text())["services"]
    monitor = services["agent-monitor"]
    assert monitor["ports"] == ["127.0.0.1:8020:8020"]
    assert "env_file" not in monitor and "network_mode" not in monitor
    assert {e.split("=", 1)[0] for e in monitor["environment"]} == {
        "AGENT_SERVICE_URL", "MONITOR_TOKEN", "GATEWAY_URL", "MCP_ENDPOINT", "WG_TUNNEL", "AGENT_HA_PROBE_S",
        "MONITOR_HOME", "TZ"}
    assert monitor["volumes"] == ["./data/agent-service/db:/db", "./data/mcp-bridge/status:/bridge:ro"]
    assert all("memory" not in v for v in monitor["volumes"])
    assert "./data/mcp-bridge/status:/app/status" in services["mcp-bridge"]["volumes"]


def test_agent_db_moves_into_db_only_with_the_service_stopped(setup_tree):
    server, run = setup_tree
    data = server / "data" / "agent-service"
    data.mkdir(parents=True, exist_ok=True)
    for name in ("agent.db", "agent.db-wal", "agent.db-shm"):
        (data / name).write_text(name)
    result, state = run(FAKE_FAIL="agent-running")
    assert result.returncode == 1 and "still running" in result.stdout
    assert (data / "agent.db").exists() and not (data / "db" / "agent.db").exists()
    assert not state.get("recreated")
    assert (server / "data" / ".config.yaml").read_text() == OLD_CONFIG
    result, state = run()
    assert result.returncode == 0, result.stdout + result.stderr
    assert sorted(p.name for p in (data / "db").iterdir()) == ["agent.db", "agent.db-shm", "agent.db-wal"]
    assert not (data / "agent.db").exists() and (data / "db" / "agent.db").read_text() == "agent.db"
    log = state["log"]
    stop = log.index("stop agent-service")
    assert stop < next(i for i, l in enumerate(log) if "--force-recreate" in l)
    assert "inspect -f {{.State.Running}} agent-service" in log[stop + 1]
    result, state = run()
    assert result.returncode == 0 and "stop agent-service" not in state["log"][len(log):]


def test_agent_db_is_backed_up_before_the_new_image_and_restored_on_rollback(setup_tree):
    server, run = setup_tree
    data = server / "data" / "agent-service"
    (data / "db").mkdir(parents=True)
    for name in ("agent.db", "agent.db-wal"):
        (data / "db" / name).write_text(f"v1 {name}")
    result, state = run(FAKE_FAIL="agent-running")
    assert result.returncode == 1 and "not backed up and nothing was published" in result.stdout
    assert not state.get("recreated") and (data / "db" / "agent.db").read_text() == "v1 agent.db"
    assert (server / "data" / ".config.yaml").read_text() == OLD_CONFIG
    result, state = run(FAKE_MIGRATE="1")
    assert result.returncode == 0, result.stdout + result.stderr
    assert sorted(p.name for p in (data / "db-rollback").iterdir()) == ["agent.db", "agent.db-wal"]
    assert (data / "db-rollback" / "agent.db").read_text() == "v1 agent.db"
    log = state["log"]
    stop = log.index("stop agent-service agent-monitor")
    assert stop < next(i for i, l in enumerate(log) if "--force-recreate" in l)
    assert (data / "db" / "agent.db").read_text() == "migrated by the new image"
    (data / "db" / "agent.db").write_text("v1 agent.db")
    result, state = run(FAKE_FAIL="exec-agent-service", ATTEMPT="new", FAKE_MIGRATE="1")
    assert result.returncode == 1 and "rolling back" in result.stdout
    assert "agent-service database restored from before this run" in result.stdout
    assert (data / "db" / "agent.db").read_text() == "v1 agent.db"
    assert (data / "db" / "agent.db-wal").read_text() == "v1 agent.db-wal"
    assert (data / "db-failed" / "agent.db").read_text() == "migrated by the new image"
    rerun = state["log"][len(log):]
    restore = max(i for i, l in enumerate(rerun) if l == "stop agent-service agent-monitor")
    assert restore > next(i for i, l in enumerate(rerun) if "--force-recreate" in l)
    assert state["recreated"][-1] == ["mcp-bridge", "agent-service", "agent-monitor", "xiaozhi-esp32-server"]


# ------------------------------------------------------------------ setup.sh transaction (pre-publish)


@pytest.mark.parametrize("fail, extra_env", [
    ("new-key,build", {}),
    ("new-key", {"ANTHROPIC_API_KEY": '" "'}),
    ("new-key", {"TZ_OFFSET": "seven"}),
])
def test_failure_before_publishing_restores_env_and_the_endpoint_unit(setup_tree, fail, extra_env):
    server, run = setup_tree
    if extra_env:
        run.set_env(**extra_env)
    before = (server / ".env").read_text()
    key = server / "data" / "mcp-endpoint" / ".mcp-endpoint-server.cfg"
    result, state = run(FAKE_FAIL=fail, WG_TUNNEL="")
    assert result.returncode == 1
    assert "before publishing" in result.stdout and "endpoint key restored from the last run" in result.stdout
    assert (server / ".env").read_text() == before, "tokens and profiles generated by the failed run are gone"
    assert key.read_text() == "key" and not list((server / "data").rglob("*.rollback"))
    assert state["recreated"] == [["mcp-endpoint-server"]]
    assert (server / "data" / ".config.yaml").read_text() == OLD_CONFIG
    assert state["tags"]["weixue/xiaozhi-esp32-server:local"] == "server-old"
    assert not (server / ".env.txn").exists() and _leftovers(server) == []


def test_an_endpoint_this_run_started_is_stopped_again(setup_tree):
    server, run = setup_tree
    result, state = run(FAKE_FAIL="build")
    assert result.returncode == 1 and "mcp-endpoint-server" not in state.get("stopped", []), \
        "an endpoint that was running stays up"
    result, state = run(FAKE_FAIL="endpoint-down,build")
    assert result.returncode == 1 and "mcp-endpoint-server" in state.get("stopped", [])


def test_a_failed_database_move_puts_it_back_and_restarts_the_service(setup_tree):
    server, run = setup_tree
    data = server / "data" / "agent-service"
    data.mkdir(parents=True, exist_ok=True)
    for name in ("agent.db", "agent.db-wal", "agent.db-shm"):
        (data / name).write_text(name)
    result, state = run(FAKE_MV_FAIL="agent.db-wal")
    assert result.returncode == 1 and "before publishing" in result.stdout
    assert sorted(p.name for p in data.iterdir() if p.is_file()) == ["agent.db", "agent.db-shm", "agent.db-wal"]
    assert (data / "agent.db").read_text() == "agent.db" and not list((data / "db").iterdir())
    assert state["started"] == ["agent-service"] and not state.get("recreated")
    assert (server / "data" / ".config.yaml").read_text() == OLD_CONFIG
    assert state["tags"]["weixue/xiaozhi-esp32-server:local"] == "server-old"
    result, state = run()
    assert result.returncode == 0, result.stdout + result.stderr
    assert sorted(p.name for p in (data / "db").iterdir()) == ["agent.db", "agent.db-shm", "agent.db-wal"]


def test_an_unexpected_failure_while_publishing_rolls_back(setup_tree):
    server, run = setup_tree
    url = server / "data" / "mcp-bridge" / "endpoint-url"
    url.parent.mkdir(parents=True, exist_ok=True)
    url.write_text("ws://mcp-endpoint-server:8004/mcp_endpoint/mcp/?token=old\n")
    (server / "data" / ".config.yaml.rollback").write_text("stale: config\n")
    result, state = run(FAKE_MV_FAIL="endpoint-url.")
    assert result.returncode == 1 and "while publishing; rolling back" in result.stdout
    assert (server / "data" / ".config.yaml").read_text() == OLD_CONFIG, "never a rollback copy of an older run"
    assert url.read_text().endswith("token=old\n")
    assert state["tags"]["weixue/xiaozhi-esp32-server:local"] == "server-old"
    assert not (server / ".env.txn").exists() and _leftovers(server) == []


def test_services_the_database_backup_stopped_start_again_when_it_fails(setup_tree):
    server, run = setup_tree
    (server / "data" / "agent-service" / "db").mkdir(parents=True)
    (server / "data" / "agent-service" / "db" / "agent.db").write_text("v1")
    result, state = run(FAKE_FAIL="agent-running")
    assert result.returncode == 1 and "before publishing" in result.stdout
    assert state["started"] == ["agent-service", "agent-monitor"] and not state.get("recreated")
