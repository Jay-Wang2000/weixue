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
        for script in ("setup.sh", "check_ha_path.py"):
            shutil.copy(SERVER_DIR / "scripts" / script, self.server / "scripts" / script)
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
    assert calls[-1].split("--force-recreate ")[1].split() == ["wg", "mcp-bridge", "agent-service", "agent-monitor", "xiaozhi-esp32-server"]
    assert any(c.endswith(" compose build wg mcp-bridge agent-service agent-monitor xiaozhi-esp32-server") for c in calls)
    assert not any(c.endswith(" rm -f wg") for c in setup.docker_calls())
    execs = [line.split(" exec ", 1)[1] for line in setup.docker_calls() if " exec " in line]
    assert execs == [
        "wg /usr/local/bin/healthcheck.sh",
        "mcp-bridge python -m weixue_bridge.health",
        "-i xiaozhi-esp32-server python - --timeout 20",
        "agent-service python -m weixue_agent.health",
        "agent-monitor python -m agent_monitor.health",
    ]
    assert "AGENT_SERVICE_HOST=wg" in setup.env_file.read_text()


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
    assert calls[-1].split("--force-recreate ")[1].split() == ["mcp-bridge", "agent-service", "agent-monitor", "xiaozhi-esp32-server"]
    assert any(c.endswith(" compose build mcp-bridge agent-service agent-monitor xiaozhi-esp32-server") for c in calls)
    docker_calls = setup.docker_calls()
    removed = next(i for i, c in enumerate(docker_calls) if c.endswith(" rm -f wg"))
    assert docker_calls[removed - 1] == calls[-1]
    assert [c.split(" exec ", 1)[1].split()[0] for c in docker_calls[removed + 1:]] == ["mcp-bridge", "-i", "agent-service", "agent-monitor"]
    assert "AGENT_SERVICE_HOST" not in setup.env_file.read_text()


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
    calls = setup.compose_calls()
    assert calls[-1].startswith("COMPOSE_PROFILES=tunnel ")
    assert calls[-1].split("--force-recreate ")[1].split() == ["wg", "mcp-bridge", "agent-service", "agent-monitor", "xiaozhi-esp32-server"]
    assert not any(c.endswith(" rm -f wg") for c in setup.docker_calls())


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


@pytest.mark.parametrize(
    "age, boundary, healthy",
    [(5, True, True), (179, True, True), (181, True, False), (None, True, False), (5, False, False)],
)
def test_healthcheck_handshake_age_and_boundary(tmp_path, age, boundary, healthy):
    handshake = 0 if age is None else NOW - age
    hooked = "exit 0" if boundary else "exit 1"
    write_stubs(tmp_path / "bin", {
        "wg": f"printf 'peerA\\t{handshake}\\n'", "date": f"echo {NOW}", "iptables": hooked, "ip6tables": "exit 1",
    })
    proc = subprocess.run(
        ["sh", str(WG_DIR / "healthcheck.sh")], env={"PATH": f"{tmp_path / 'bin'}:/usr/bin:/bin"}, timeout=10
    )
    assert (proc.returncode == 0) is healthy


CONF = """[Interface]
PrivateKey = {key}
Address = 10.66.0.2/32
MTU = 1420

[Peer]
PublicKey = {key}
Endpoint = home.example-ddns.net:51820
allowedips = {allowed}
"""


def _boundary(tmp_path, allowed):
    conf = tmp_path / "wg0.conf"
    conf.write_text(CONF.format(key=KEY, allowed=allowed))
    log = tmp_path / "rules"
    write_stubs(tmp_path / "bin", {
        tool: f'echo "{tool} $*" >> {log}; case "$1" in -C) exit 1 ;; esac; exit 0' for tool in ("iptables", "ip6tables")
    })
    proc = source_entrypoint(f'install_boundary "{conf}"', path=tmp_path / "bin")
    rules = log.read_text().splitlines() if log.exists() else []
    return proc, rules


def test_boundary_drops_allowed_ips_outside_wg0_before_the_tunnel(tmp_path):
    proc, rules = _boundary(tmp_path, "192.168.8.0/24, fd00:8::/64")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == "wg: boundary: 2 network(s) only through wg0"
    assert rules == [
        "iptables -N WG_BOUNDARY",
        "iptables -C OUTPUT -j WG_BOUNDARY",
        "iptables -I OUTPUT 1 -j WG_BOUNDARY",
        "ip6tables -N WG_BOUNDARY",
        "ip6tables -C OUTPUT -j WG_BOUNDARY",
        "ip6tables -I OUTPUT 1 -j WG_BOUNDARY",
        "iptables -A WG_BOUNDARY -d 192.168.8.0/24 ! -o wg0 -j DROP",
        "ip6tables -A WG_BOUNDARY -d fd00:8::/64 ! -o wg0 -j DROP",
    ]
    for secret in SECRETS + ["192.168.8.0"]:
        assert secret not in proc.stdout


@pytest.mark.parametrize("allowed, message", [
    ("0.0.0.0/0", "must not contain a default route"),
    ("192.168.8.0/24, ::/0", "must not contain a default route"),
])
def test_boundary_refuses_a_full_tunnel(tmp_path, allowed, message):
    proc, rules = _boundary(tmp_path, allowed)
    assert proc.returncode == 1 and message in proc.stdout and rules == []


def test_boundary_refuses_a_config_without_allowed_ips(tmp_path):
    conf = tmp_path / "wg0.conf"
    conf.write_text("[Interface]\nPrivateKey = x\n")
    proc = source_entrypoint(f'install_boundary "{conf}"')
    assert proc.returncode == 1 and "no AllowedIPs" in proc.stdout


def test_boundary_is_installed_before_wg_quick_runs():
    main = (WG_DIR / "entrypoint.sh").read_text().split("main() {", 1)[1]
    assert main.index('install_boundary "$CONF"') < main.index("up_with_retry")


def test_failed_rule_stops_the_sidecar(tmp_path):
    conf = tmp_path / "wg0.conf"
    conf.write_text(CONF.format(key=KEY, allowed="192.168.8.0/24"))
    write_stubs(tmp_path / "bin", {"iptables": 'case "$1" in -A) exit 4 ;; -C) exit 1 ;; esac; exit 0'})
    proc = source_entrypoint(f'set -e; install_boundary "{conf}"; echo after', path=tmp_path / "bin")
    assert proc.returncode != 0 and "after" not in proc.stdout


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
    bridge = services["mcp-bridge"]
    assert ("wg" in services) is tunnel
    assert (bridge.get("network_mode") or None) == ("service:wg" if tunnel else None)
    assert bridge["depends_on"]["wg"] == {"condition": "service_healthy", "required": False}
    assert bridge["environment"]["TUNNEL_IFACE"] == ("wg0" if tunnel else "")
    agent = services["agent-service"]
    assert (agent.get("network_mode") or None) == ("service:wg" if tunnel else None)
    assert agent["depends_on"]["wg"] == {"condition": "service_healthy", "required": False}
    assert set(agent["environment"]) == {"ANTHROPIC_API_KEY", "HA_MCP_URL", "AGENT_SERVICE_TOKEN", "TZ", "MONITOR_TOKEN",
                                         "AGENT_DB", "AGENT_HA_PROBE_S", "AGENT_HA_PROBE_TIMEOUT_S",
                                         "AGENT_HOME_SNAPSHOT_S", "AGENT_HOME_ENTITIES", "HA_MCP_READ_TIMEOUT_S"}
    assert agent["environment"]["AGENT_DB"] == "/data/db/agent.db"
    assert "ports" not in agent and "env_file" not in agent
    monitor = services["agent-monitor"]
    assert monitor["environment"]["WG_TUNNEL"] == ("1" if tunnel else "")
    assert "network_mode" not in monitor and "env_file" not in monitor
