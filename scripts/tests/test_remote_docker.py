"""Host tests for scripts/lib/remote_docker.py and remote_docker.sh: local vs SSH command construction
(stdlib unittest; fake ssh and docker on PATH, no network)."""
import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

LIB = Path(__file__).resolve().parents[1] / "lib"
sys.path.insert(0, str(LIB))
import remote_docker as rd  # noqa: E402

SCRIPT = 'import json, sys\nprint(json.dumps({"a": "it\'s $HOME"}))\n'
ARGS = ["exec", "-i", "agent-service", "python", "-c", SCRIPT]

# Records its argv as one JSON line in $FAKE_LOG.
RECORDER = """#!/usr/bin/env python3
import json, os, sys
with open(os.environ["FAKE_LOG"], "a") as f:
    f.write(json.dumps([os.path.basename(sys.argv[0])] + sys.argv[1:]) + "\\n")
"""
# Records its argv, then runs the remote command with sh like sshd would (docker is the recorder).
FAKE_SSH = RECORDER + """import subprocess
sys.exit(subprocess.call(["sh", "-c", sys.argv[-1]]))
"""


class PythonHelperTest(unittest.TestCase):
    def test_local_mode_runs_docker_directly(self):
        self.assertEqual(rd.docker_argv(ARGS, {"WEIXUE_SERVER_SSH": ""}), ["docker", *ARGS])
        self.assertEqual(rd.server_host({"WEIXUE_SERVER_SSH": ""}), "")

    def test_remote_mode_quotes_every_argument_for_the_remote_shell(self):
        env = {"WEIXUE_SERVER_SSH": "jay@192.0.2.5", "WEIXUE_SERVER_SSH_OPTS": "-o IdentitiesOnly=yes -i ~/.ssh/k"}
        argv = rd.docker_argv(ARGS, env)
        self.assertEqual(argv[:-1], ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                                     "-o", "IdentitiesOnly=yes", "-i", "~/.ssh/k", "jay@192.0.2.5"])
        self.assertEqual(shlex.split(argv[-1]), ["docker", *ARGS])
        self.assertEqual(rd.server_host(env), "192.0.2.5")

    def test_settings_come_from_the_live_env_unless_set_in_the_environment(self):
        with tempfile.TemporaryDirectory() as d:
            live = Path(d) / ".env"
            live.write_text("WEIXUE_SERVER_SSH=old@h\nWEIXUE_SERVER_SSH=\"jay@192.0.2.5\"\n"
                            "WEIXUE_SERVER_SSH_OPTS='-p 2222'\n")
            env = {"WEIXUE_LIVE_ENV": str(live)}
            self.assertEqual(rd.docker_argv(["ps"], env)[5:], ["-p", "2222", "jay@192.0.2.5", "docker ps"])
            self.assertEqual(rd.docker_argv(["ps"], {**env, "WEIXUE_SERVER_SSH": ""}), ["docker", "ps"])
            self.assertEqual(rd.docker_argv(["ps"], {"WEIXUE_LIVE_ENV": str(Path(d) / "missing")}), ["docker", "ps"])

    def test_run_passes_the_argv_and_options_to_subprocess(self):
        seen = {}

        def run(argv, **kw):
            seen.update(argv=argv, kw=kw)
            return subprocess.CompletedProcess(argv, 0, "", "")

        with mock.patch.dict(os.environ, {"WEIXUE_SERVER_SSH": "jay@192.0.2.5"}), \
                mock.patch.object(rd.subprocess, "run", run):
            rd.run(["logs", "agent-service"], capture_output=True, timeout=30)
        self.assertEqual(seen["argv"][-2:], ["jay@192.0.2.5", "docker logs agent-service"])
        self.assertEqual(seen["kw"], {"capture_output": True, "timeout": 30})


class ShellHelperTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.tmp = Path(self.dir.name)
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        for name, body in {"docker": RECORDER, "ssh": FAKE_SSH}.items():
            (bin_dir / name).write_text(body)
            (bin_dir / name).chmod(0o755)
        (self.tmp / "weixue" / "server").mkdir(parents=True)
        self.live = self.tmp / "live" / ".env"
        self.live.parent.mkdir()
        self.live.write_text("LAN_IP=10.9.9.9\n")
        self.log = self.tmp / "calls.jsonl"
        env = {k: v for k, v in os.environ.items() if not k.startswith("WEIXUE_SERVER_")}
        self.env = {**env, "PATH": f"{bin_dir}:{os.environ['PATH']}", "FAKE_LOG": str(self.log),
                    "WEIXUE_LIVE_ENV": str(self.live)}

    def tearDown(self):
        self.dir.cleanup()

    def sh(self, *args, **env):
        out = subprocess.run(["bash", str(LIB / "remote_docker.sh"), *args], cwd=self.tmp, capture_output=True,
                             text=True, env={**self.env, **env})
        calls = [json.loads(l) for l in self.log.read_text().splitlines()] if self.log.exists() else []
        self.log.unlink(missing_ok=True)
        return out, calls

    def test_local_mode_runs_docker_with_the_arguments_unchanged(self):
        out, calls = self.sh(*ARGS)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(calls, [["docker", *ARGS]])

    def test_remote_mode_round_trips_the_arguments_through_ssh(self):
        out, calls = self.sh(*ARGS, WEIXUE_SERVER_SSH="jay@192.0.2.5",
                             WEIXUE_SERVER_SSH_OPTS="-o IdentitiesOnly=yes -i ~/.ssh/k")
        self.assertEqual(out.returncode, 0, out.stderr)
        ssh, docker = calls
        self.assertEqual(ssh[:-1], ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                                    "-o", "IdentitiesOnly=yes", "-i", "~/.ssh/k", "jay@192.0.2.5"])
        self.assertEqual(docker, ["docker", *ARGS])
        self.assertEqual(shlex.split(ssh[-1]), shlex.split(rd.docker_argv(ARGS, {"WEIXUE_SERVER_SSH": "h"})[-1]))

    def test_remote_mode_from_the_live_env(self):
        self.live.write_text("WEIXUE_SERVER_SSH=jay@192.0.2.5\n")
        out, calls = self.sh("logs", "--since", "60s", "agent-service")
        self.assertEqual([c[0] for c in calls], ["ssh", "docker"])
        self.assertEqual(calls[1], ["docker", "logs", "--since", "60s", "agent-service"])
        out, calls = self.sh("ps", WEIXUE_SERVER_SSH="")
        self.assertEqual(calls, [["docker", "ps"]])

    def test_compose_runs_in_the_server_directory(self):
        out, calls = self.sh("compose", "ps", "--format", "{{.Name}} {{.Status}}")
        self.assertEqual(calls, [["docker", "compose", "-f", str(self.live.parent / "docker-compose.yml"),
                                  "ps", "--format", "{{.Name}} {{.Status}}"]])
        out, calls = self.sh("compose", "stop", WEIXUE_SERVER_SSH="jay@192.0.2.5")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(calls[0][-1], "cd 'weixue/server' && docker compose 'stop'")
        self.assertEqual(calls[1], ["docker", "compose", "stop"])
        out, calls = self.sh("compose", "ps", WEIXUE_SERVER_SSH="jay@192.0.2.5", WEIXUE_SERVER_DIR="/srv/w x")
        self.assertEqual(calls[0][-1], "cd '/srv/w x' && docker compose 'ps'")

    def test_host_prints_the_server_or_nothing(self):
        self.assertEqual(self.sh("host", WEIXUE_SERVER_SSH="jay@192.0.2.5")[0].stdout, "192.0.2.5\n")
        self.assertEqual(self.sh("host")[0].stdout, "")


if __name__ == "__main__":
    unittest.main()
