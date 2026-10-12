"""docker on the host that runs the stack: this one, or over SSH when WEIXUE_SERVER_SSH is user@host.

Python equivalent of scripts/lib/remote_docker.sh, with the same settings: WEIXUE_SERVER_SSH and
WEIXUE_SERVER_SSH_OPTS come from the environment when set there (an empty value included), else from the
live server/.env (WEIXUE_LIVE_ENV, default ~/Projects/weixue/server/.env)."""
import os
import shlex
import subprocess

LIVE_ENV = os.path.join("~", "Projects", "weixue", "server", ".env")
SSH_BASE = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]


def setting(key, environ=None):
    """KEY from environ when set there, else its last assignment in the live .env without surrounding quotes."""
    environ = os.environ if environ is None else environ
    if key in environ:
        return environ[key]
    value = ""
    try:
        with open(os.path.expanduser(environ.get("WEIXUE_LIVE_ENV") or LIVE_ENV), encoding="utf-8") as f:
            for line in f.read().splitlines():
                if line.startswith(key + "="):
                    value = line[len(key) + 1:]
    except OSError:
        return ""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    return value


def server_host(environ=None):
    """Host part of WEIXUE_SERVER_SSH; empty in local mode."""
    target = setting("WEIXUE_SERVER_SSH", environ)
    return target.split("@", 1)[-1] if target else ""


def docker_argv(args, environ=None):
    """argv that runs `docker ARGS` on the stack's host."""
    target = setting("WEIXUE_SERVER_SSH", environ)
    if not target:
        return ["docker", *args]
    opts = setting("WEIXUE_SERVER_SSH_OPTS", environ).split()
    return [*SSH_BASE, *opts, target, "docker " + " ".join(shlex.quote(a) for a in args)]


def run(args, **kwargs):
    """subprocess.run of `docker ARGS` on the stack's host."""
    return subprocess.run(docker_argv(args), **kwargs)
