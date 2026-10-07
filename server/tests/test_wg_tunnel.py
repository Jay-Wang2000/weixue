"""WG_TUNNEL handling in scripts/setup.sh, the wg sidecar scripts and the compose `tunnel` profile."""
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

SERVER_DIR = Path(__file__).resolve().parent.parent
WG_DIR = SERVER_DIR / "wg"
SENTINEL = "PrivateKey = sentinel-must-not-be-printed"
BASE_ENV = [
    "LAN_IP=192.0.2.1",
    "BIND_IP=0.0.0.0",
    "ALLOWED_DEVICES=7c:0c:5f:42:4b:fc",
    "TZ=UTC",
    "TZ_OFFSET=0",
    "ANTHROPIC_API_KEY=x",
    "HA_MCP_URL=http://192.0.2.2:9584/secret",
]
STUBS = {
    "curl": "exit 0",
    "git": 'case "$*" in *rev-parse*) echo deadbeef ;; esac',
    "python3": "exit 0",
    "docker": (
        'echo "COMPOSE_PROFILES=${COMPOSE_PROFILES-<unset>} $*" >> "$DOCKER_LOG"\n'
        '[ "$1" = logs ] && echo "ws://192.0.2.1:8004/mcp_endpoint/mcp/?token=t"\n'
        "exit 0"
    ),
}


def write_stubs(bin_dir, stubs):
    bin_dir.mkdir(exist_ok=True)
    for name, body in stubs.items():
        path = bin_dir / name
        path.write_text(f"#!/bin/sh\n{body}\n")
        path.chmod(0o755)


class Setup:
    """A copy of setup.sh in tmp_path with docker, git, curl and python3 stubbed on PATH."""

    def __init__(self, tmp_path):
        self.tmp = tmp_path
        self.server = tmp_path / "server"
        (self.server / "scripts").mkdir(parents=True)
        (self.server / "data" / "mcp-endpoint").mkdir(parents=True)
        (self.server / "data" / "mcp-endpoint" / ".mcp-endpoint-server.cfg").write_text("")
        shutil.copy(SERVER_DIR / "scripts" / "setup.sh", self.server / "scripts" / "setup.sh")
        (tmp_path / "vendor.lock").write_text("xiaozhi-esp32-server https://example.invalid deadbeef\n")
        self.sa = tmp_path / "sa.json"
        self.sa.write_text("{}")
        self.env_file = self.server / ".env"
        self.docker_log = tmp_path / "docker.log"
        write_stubs(tmp_path / "bin", STUBS)

    def write_env(self, *extra):
        self.env_file.write_text("\n".join(BASE_ENV + [f"GOOGLE_SA_FILE={self.sa}", *extra]) + "\n")
        self.env_file.chmod(0o600)

    def write_conf(self, mode):
        (self.server / ".secrets").mkdir(exist_ok=True)
        conf = self.server / ".secrets" / "wg0.conf"
        conf.write_text(f"[Interface]\n{SENTINEL}\n")
        conf.chmod(mode)

    def run(self):
        env = {"PATH": f"{self.tmp / 'bin'}:/usr/bin:/bin", "HOME": str(self.tmp), "DOCKER_LOG": str(self.docker_log)}
        proc = subprocess.run(
            ["bash", str(self.server / "scripts" / "setup.sh")], env=env, capture_output=True, text=True, timeout=30
        )
        return proc.returncode, proc.stdout + proc.stderr

    def docker_calls(self):
        return self.docker_log.read_text().splitlines()

    def compose_calls(self):
        return [line for line in self.docker_calls() if " compose " in line]

    def env_profiles(self):
        return [line for line in self.env_file.read_text().splitlines() if line.startswith("COMPOSE_PROFILES=")]


@pytest.fixture
def setup(tmp_path):
    return Setup(tmp_path)


def test_setup_tunnel_requires_conf(setup):
    setup.write_env("WG_TUNNEL=1")
    code, out = setup.run()
    assert code == 1 and "server/.secrets/wg0.conf does not exist" in out


def test_setup_tunnel_requires_mode_0600(setup):
    setup.write_env("WG_TUNNEL=1")
    setup.write_conf(0o644)
    code, out = setup.run()
    assert code == 1 and "must be mode 0600" in out


@pytest.mark.parametrize("value", ["0", "yes", "true"])
def test_setup_rejects_other_values(setup, value):
    setup.write_env(f"WG_TUNNEL={value}")
    setup.write_conf(0o600)
    code, out = setup.run()
    assert code == 1 and "WG_TUNNEL must be 1 or empty" in out


@pytest.mark.parametrize(
    "existing, profiles",
    [
        ([], "tunnel"),
        (["COMPOSE_PROFILES=tunnel"], "tunnel"),
        (["COMPOSE_PROFILES=console"], "console,tunnel"),
        (['COMPOSE_PROFILES="console"'], "console,tunnel"),
    ],
)
def test_setup_tunnel_on_writes_profile_and_recreates_wg(setup, existing, profiles):
    setup.write_env("WG_TUNNEL=1", *existing)
    setup.write_conf(0o600)
    code, out = setup.run()
    assert code == 0, out
    assert "sentinel" not in out
    assert setup.env_profiles() == [f"COMPOSE_PROFILES={profiles}"]
    assert setup.env_file.stat().st_mode & 0o777 == 0o600
    calls = setup.compose_calls()
    assert calls and all(c.startswith(f"COMPOSE_PROFILES={profiles} ") for c in calls)
    assert calls[-1].split("--force-recreate ")[1].split() == ["wg", "mcp-bridge", "xiaozhi-esp32-server"]
    assert any(c.endswith(" compose build wg mcp-bridge xiaozhi-esp32-server") for c in calls)
    assert not any(c.endswith(" rm -f wg") for c in setup.docker_calls())


@pytest.mark.parametrize(
    "existing, seen",
    [
        ([], "<unset>"),
        (["COMPOSE_PROFILES=tunnel"], "<unset>"),
        (["COMPOSE_PROFILES=console,tunnel"], "console"),
        (["COMPOSE_PROFILES='console,tunnel'"], "console"),
    ],
)
def test_setup_tunnel_off_strips_profile_and_skips_wg(setup, existing, seen):
    setup.write_env("WG_TUNNEL=", *existing)
    code, out = setup.run()
    assert code == 0, out
    assert setup.env_profiles() == ([] if seen == "<unset>" else [f"COMPOSE_PROFILES={seen}"])
    assert "HA_MCP_URL=http://192.0.2.2:9584/secret" in setup.env_file.read_text()
    calls = setup.compose_calls()
    assert calls and all(c.startswith(f"COMPOSE_PROFILES={seen} ") for c in calls)
    assert calls[-1].split("--force-recreate ")[1].split() == ["mcp-bridge", "xiaozhi-esp32-server"]
    assert any(c.endswith(" compose build mcp-bridge xiaozhi-esp32-server") for c in calls)
    docker_calls = setup.docker_calls()
    assert docker_calls[-1].endswith(" rm -f wg") and docker_calls[-2] == calls[-1]


def test_failed_up_and_rollback_print_the_manual_restore_with_the_profile(setup):
    setup.write_env("WG_TUNNEL=1", "COMPOSE_PROFILES=console")
    setup.write_conf(0o600)
    write_stubs(setup.tmp / "bin", {"docker": STUBS["docker"].replace(
        "exit 0", 'case "$*" in *--force-recreate*) exit 1 ;; *"image inspect"*) exit 1 ;; esac\nexit 0')})
    code, out = setup.run()
    assert code == 1
    assert "docker compose up failed; rolling back" in out
    assert "  docker compose --profile console --profile tunnel up -d --force-recreate wg mcp-bridge xiaozhi-esp32-server" in out
    assert "sentinel" not in out


def test_rollback_recreates_in_the_previous_tunnel_mode(setup):
    setup.write_env("WG_TUNNEL=1")
    setup.write_conf(0o600)
    code, out = setup.run()
    assert code == 0, out
    deployed = setup.env_file.read_text()
    setup.write_env("WG_TUNNEL=")
    counter = setup.tmp / "recreates"
    write_stubs(setup.tmp / "bin", {"docker": STUBS["docker"].replace("exit 0", (
        f'case "$*" in *--force-recreate*) n=$(cat {counter} 2>/dev/null || echo 0); echo $((n + 1)) > {counter};'
        ' [ "$n" -ge 1 ] || exit 1 ;; esac\nexit 0'))})
    code, out = setup.run()
    assert code == 1 and "docker compose up failed; rolling back" in out
    assert setup.env_file.read_text() == deployed
    calls = setup.docker_calls()
    assert calls[-1].startswith("COMPOSE_PROFILES=tunnel ")
    assert calls[-1].split("--force-recreate ")[1].split() == ["wg", "mcp-bridge", "xiaozhi-esp32-server"]
    assert not any(c.endswith(" rm -f wg") for c in calls)


# --- wg sidecar scripts ---------------------------------------------------------

KEY = "yAnz5TF+lXXJte14tji3zlMNq+hd2rYUIgJBgB3fBmk="
SECRETS = [KEY, "198.51.100.7", "2001:db8::1", "fd00::7", "home.example-ddns.net"]
WG_QUICK_OUTPUT = f"""[#] ip link add wg0 type wireguard
[#] wg setconf wg0 /dev/fd/63
Name does not resolve: `home.example-ddns.net:51820'
peer {KEY} endpoint home.example-ddns.net:51820 via 198.51.100.7/24, 2001:db8::1/64 and fd00::7
Configuration parsing error
[#] ip link delete dev wg0"""


def source_entrypoint(script, stdin="", path=None):
    env = {"PATH": f"{path}:/usr/bin:/bin" if path else "/usr/bin:/bin"}
    return subprocess.run(
        ["bash", "-c", f'source "$1"; {script}', "bash", str(WG_DIR / "entrypoint.sh")],
        input=stdin, env=env, capture_output=True, text=True, timeout=10,
    )


def test_redact_removes_keys_addresses_and_endpoint():
    proc = source_entrypoint("redact", WG_QUICK_OUTPUT)
    assert proc.returncode == 0, proc.stderr
    for secret in SECRETS:
        assert secret not in proc.stdout
    assert "`<redacted>'" in proc.stdout
    assert "peer <key> endpoint <endpoint> via <ip>, <ip6> and <ip6>" in proc.stdout
    assert "Configuration parsing error" in proc.stdout


def test_wg_quick_failure_is_retried_with_one_redacted_line(tmp_path):
    output = tmp_path / "output"
    output.write_text(WG_QUICK_OUTPUT + "\n")
    count = tmp_path / "count"
    write_stubs(
        tmp_path / "bin",
        {"wg-quick": f'n=$(cat {count} 2>/dev/null || echo 0); echo $((n + 1)) > {count}\n'
                     f'[ "$n" -ge 2 ] && exit 0; cat {output} >&2; exit 1'},
    )
    proc = source_entrypoint("RETRY_S=0; up_with_retry", path=tmp_path / "bin")
    assert proc.returncode == 0, proc.stderr
    assert count.read_text().strip() == "3"
    lines = proc.stdout.splitlines()
    assert len(lines) == 2
    assert all(line.startswith("wg: wg-quick up failed, retry in 0 s: Name does not resolve") for line in lines)
    for secret in SECRETS:
        assert secret not in proc.stdout


NOW = 1_800_000_000


@pytest.mark.parametrize("age, healthy", [(5, True), (179, True), (181, False), (None, False)])
def test_healthcheck_handshake_age(tmp_path, age, healthy):
    handshake = 0 if age is None else NOW - age
    write_stubs(tmp_path / "bin", {"wg": f"printf 'peerA\\t{handshake}\\n'", "date": f"echo {NOW}"})
    proc = subprocess.run(
        ["sh", str(WG_DIR / "healthcheck.sh")], env={"PATH": f"{tmp_path / 'bin'}:/usr/bin:/bin"}, timeout=10
    )
    assert (proc.returncode == 0) is healthy


# --- compose ------------------------------------------------------------------


def _docker():
    rd = Path.home() / ".rd" / "bin" / "docker"
    return str(rd) if rd.exists() else shutil.which("docker")


@pytest.mark.parametrize("tunnel", [False, True])
def test_compose_profile_from_env_file_switches_bridge_namespace(tmp_path, tunnel):
    docker = _docker()
    if not docker or subprocess.run([docker, "compose", "version"], capture_output=True).returncode != 0:
        pytest.skip("docker compose not available")
    env_file = tmp_path / ".env"
    lines = ["LAN_IP=192.0.2.1", "GOOGLE_SA_FILE=/dev/null", "HA_MCP_URL=http://192.0.2.2:9584/secret"]
    if tunnel:
        lines += ["WG_TUNNEL=1", "COMPOSE_PROFILES=tunnel"]
    env_file.write_text("\n".join(lines) + "\n")
    proc = subprocess.run(
        [docker, "compose", "--env-file", str(env_file), "-f", str(SERVER_DIR / "docker-compose.yml"),
         "config", "--format", "json"],
        env={"PATH": os.environ.get("PATH", ""), "HOME": str(Path.home())}, capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    services = json.loads(proc.stdout)["services"]
    assert ("wg" in services) is tunnel
    assert (services["mcp-bridge"].get("network_mode") or None) == ("service:wg" if tunnel else None)
