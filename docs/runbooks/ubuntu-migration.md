# Runbook: backend on the Ubuntu server (decision 77)

Moves the Docker stack from the Mac to `jay@192.168.8.125` (Ubuntu 24.04, x86_64, Docker Engine + compose plugin, `jay` in the `docker` group, interface `wlp0s20f3`, TZ Europe/Dublin). The Mac stays the device and test host (`docs/server.md`, Deployment host). Commands run in **bash** on the Mac unless marked *server*; nothing here prints a secret.

```bash
SRV=jay@192.168.8.125
SSH="ssh -o IdentitiesOnly=yes -i $HOME/.ssh/id_ed25519 -o BatchMode=yes"
MAC=~/Projects/weixue          # live checkout on the Mac
D=$MAC/server/data
```

Server layout: checkout `/home/jay/weixue`, upstream clone `/home/jay/weixue-vendor/xiaozhi-esp32-server`, secrets `/home/jay/weixue-secrets/` (0700, files 0600).

If Docker is missing on a fresh host (sudo, Jay): `sudo apt-get install -y docker.io docker-compose-v2 && sudo usermod -aG docker jay`, then log in again.

## 1. Checkout on the server

Option A, `git clone` with a read-only deploy key (adding the key to `Jay-Wang2000/weixue` is a GitHub change: Jay approves it first):

```bash
$SSH $SRV 'ssh-keygen -q -t ed25519 -N "" -C weixue-deploy@$(hostname) -f ~/.ssh/weixue_deploy && cat ~/.ssh/weixue_deploy.pub' > /tmp/weixue_deploy.pub
gh repo deploy-key add /tmp/weixue_deploy.pub --repo Jay-Wang2000/weixue --title jay-dev-ubuntu   # read-only unless --allow-write
$SSH $SRV 'ssh-keyscan github.com >> ~/.ssh/known_hosts &&
  GIT_SSH_COMMAND="ssh -i ~/.ssh/weixue_deploy -o IdentitiesOnly=yes" git clone git@github.com:Jay-Wang2000/weixue.git /home/jay/weixue &&
  git -C /home/jay/weixue config core.sshCommand "ssh -i ~/.ssh/weixue_deploy -o IdentitiesOnly=yes"'
```

Updates: *server* `git -C /home/jay/weixue pull --ff-only`.

Option B, rsync a clean clone of the Mac's checkout (no GitHub access on the server):

```bash
rm -rf /tmp/weixue-clone && git clone -q --branch main "$MAC" /tmp/weixue-clone
git -C /tmp/weixue-clone remote set-url origin git@github.com:Jay-Wang2000/weixue.git
rsync -a -e "$SSH" /tmp/weixue-clone/ $SRV:/home/jay/weixue/
```

Updates: repeat the three lines (rsync without `--delete` never touches the ignored `server/.env` and `server/data/`), then *server* `git -C /home/jay/weixue status --short` is empty.

Upstream clone (pinned in `vendor.lock`; `setup.sh` refuses any other commit or local changes):

```bash
rsync -a -e "$SSH" ~/Projects/weixue-vendor/xiaozhi-esp32-server/ $SRV:/home/jay/weixue-vendor/xiaozhi-esp32-server/
$SSH $SRV 'git -C /home/jay/weixue-vendor/xiaozhi-esp32-server rev-parse HEAD'   # = the xiaozhi-esp32-server line of vendor.lock
```

## 2. Secrets and server/.env

```bash
$SSH $SRV 'mkdir -p -m 700 /home/jay/weixue-secrets'
SA=$(sed -n 's/^GOOGLE_SA_FILE=//p' $MAC/server/.env | tail -n 1 | tr -d '"'"'")
rsync --chmod=F600 -e "$SSH" "$SA" $SRV:/home/jay/weixue-secrets/google-sa.json
rsync --chmod=F600 -e "$SSH" $MAC/server/.env $SRV:/home/jay/weixue/server/.env
$SSH $SRV 'cd /home/jay/weixue/server &&
  sed -i -E "/^(WG_TUNNEL|COMPOSE_PROFILES|AGENT_SERVICE_HOST|WEIXUE_SERVER_SSH|WEIXUE_SERVER_SSH_OPTS|WEIXUE_SERVER_DIR|WEIXUE_VENDOR|LAN_IP|GOOGLE_SA_FILE)=/d" .env &&
  printf "%s\n" LAN_IP=192.168.8.125 GOOGLE_SA_FILE=/home/jay/weixue-secrets/google-sa.json WEIXUE_VENDOR=/home/jay/weixue-vendor >> .env &&
  chmod 600 .env && grep -E "^(LAN_IP|BIND_IP|GOOGLE_SA_FILE|WEIXUE_VENDOR)=" .env'
```

The server's `.env` keeps every token of the Mac's (`AGENT_SERVICE_TOKEN`, `MONITOR_TOKEN`, `WEIXUE_PROACTIVE_TOKEN`) and `BIND_IP=0.0.0.0`; `WEIXUE_SERVER_SSH` stays unset there. Add `COMPOSE_PROFILES=console` back by hand only if the console is wanted.

## 3. Data

```bash
rsync -a -e "$SSH" $D/models $D/mcp-endpoint $D/mcp-bridge $D/weixue-gateway $SRV:/home/jay/weixue/server/data/
```

`models/` is 896 MB (omit it and `setup.sh` downloads it); `mcp-endpoint/` carries the endpoint key, so issued endpoint URLs stay valid.

Staging copy of agent-service (the Mac stack keeps running; the SQLite online backup is consistent):

```bash
mkdir -p -m 700 /tmp/agent-snap/db
sqlite3 "file:$D/agent-service/db/agent.db?mode=ro" ".backup /tmp/agent-snap/db/agent.db"
sqlite3 /tmp/agent-snap/db/agent.db "SELECT COUNT(*) FROM approvals WHERE state IN ('in_flight','pending','awaiting_decision_after_wait','replay_dispatched'); SELECT COUNT(*) FROM turns WHERE state IN ('running','queued');"
rsync -a --delete -e "$SSH" /tmp/agent-snap/db/ $SRV:/home/jay/weixue/server/data/agent-service/db/
rsync -a -e "$SSH" $D/agent-service/memory $SRV:/home/jay/weixue/server/data/agent-service/
rm -rf /tmp/agent-snap
```

Both counts must be 0 before the server stack starts on the snapshot: at start agent-service fails running turns and denies at ha-mcp the holds its database says it owns (`weixue_agent/recovery.py`), which would hit a live hold of the Mac's service. Files under `data/agent-service/` belong to uid 1000 (`jay` on the server, the container user).

## 4. Build, deploy, check (staging)

```bash
$SSH $SRV 'cd /home/jay/weixue/server && ./scripts/setup.sh'      # builds the x86_64 images; ends with ready: … and the OTA/WS/Monitor lines
$SSH $SRV 'cd /home/jay/weixue/server && docker compose ps --format "{{.Name}} {{.Status}}"'
```

One API text turn through the server's agent-service from the Mac (remote mode, `docs/server.md`):

```bash
cd $MAC && WEIXUE_SERVER_SSH=$SRV WEIXUE_SERVER_SSH_OPTS="-o IdentitiesOnly=yes -i ~/.ssh/id_ed25519" .venv/bin/python - <<'PY'
import sys, time
sys.path[:0] = ["scripts", "scripts/lib", "firmware/tools"]
from audio_session import AgentClient
import remote_docker
device = remote_docker.setting("ALLOWED_DEVICES").split(",")[0].strip()
agent = AgentClient()
status, chat = agent.request("POST", "/v1/chats", {"device_id": device, "switch": False}, idempotent=True)
status, msg = agent.request("POST", f"/v1/chats/{chat['chat_id']}/messages",
                            {"device_id": device, "text": "Do not call any tool. Reply with exactly: server ok", "lang": "en",
                             "origin": {"kind": "api"}}, idempotent=True)
for _ in range(60):
    _, task = agent.request("GET", f"/v1/tasks/{msg['task_id']}")
    if task and task.get("state") in ("done", "failed", "cancelled"):
        break
    time.sleep(2)
print(status, task.get("state"), task.get("summary"))
PY
```

`ALLOWED_DEVICES` is read from the Mac's `server/.env`. Then stop the server stack so only one stack talks to ha-mcp; images, data and `.env` stay for the cutover:

```bash
$SSH $SRV 'cd /home/jay/weixue/server && docker compose stop'
```

## 5. Cutover (orchestrator, after the task merges)

1. Server checkout at the merged `main` (section 1 update).
2. Stop the Mac stack: `cd $MAC/server && docker compose stop`.
3. Final copy of the agent-service and gateway state, consistent because both stacks are stopped:
   ```bash
   rsync -a --delete -e "$SSH" $D/agent-service/db/ $SRV:/home/jay/weixue/server/data/agent-service/db/
   rsync -a --delete -e "$SSH" $D/agent-service/memory/ $SRV:/home/jay/weixue/server/data/agent-service/memory/
   rsync -a --delete -e "$SSH" $D/weixue-gateway/ $SRV:/home/jay/weixue/server/data/weixue-gateway/
   ```
4. Start on the server: `$SSH $SRV 'cd /home/jay/weixue/server && ./scripts/setup.sh'` (readiness as in section 4).
5. Point the device at the server, under the device lock (`docs/firmware.md`, Device lock):
   ```bash
   eval "$($MAC/scripts/device_lock.sh acquire | grep ^export)"
   OTA_URL=http://192.168.8.125:8003/xiaozhi/ota/ $MAC/.venv/bin/python $MAC/firmware/tools/usb_provision.py
   $MAC/scripts/device_lock.sh release
   ```
   The boot log check of `usb_provision.py` and `$SSH $SRV 'docker logs --since 2m xiaozhi-esp32-server 2>&1 | grep -c "<device MAC>"'` show the device on the server.
6. Remote mode on the Mac: append to `$MAC/server/.env`
   ```bash
   WEIXUE_SERVER_SSH=jay@192.168.8.125
   WEIXUE_SERVER_SSH_OPTS="-o IdentitiesOnly=yes -i ~/.ssh/id_ed25519"
   ```
   `LAN_IP` there stays the Mac's own address (audio_session.sh home-mode check).
7. Voice check (home mode): `scripts/audio_session.sh --no-flash -- --items one_breath --count 5`.
8. Monitor: `ssh -L 8020:127.0.0.1:8020 $SRV`, then `http://127.0.0.1:8020/`.

## Rollback

1. `$SSH $SRV 'cd /home/jay/weixue/server && docker compose stop'`.
2. When the server stack ran after the cutover, copy its state back with both stacks stopped: the three rsync lines of step 3 with source and destination swapped.
3. Delete the `WEIXUE_SERVER_SSH*` lines from `$MAC/server/.env`, then `cd $MAC/server && docker compose up -d` and `python3 scripts/check_ready.py 127.0.0.1`.
4. Re-point the device as in step 5 with `OTA_URL=http://<Mac LAN_IP>:8003/xiaozhi/ota/` (the Mac's `LAN_IP` in `server/.env`).
