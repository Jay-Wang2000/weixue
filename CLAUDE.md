# weixue — ESP32-S3 round-AMOLED voice home agent

Read `DESIGN.md` first; decisions live in its ADRs. Research background: `~/Projects/research/reports/weixue-home-agent-v1.md`.

## Stack
- Firmware: fork of `78/xiaozhi-esp32`, board `esp32-s3-touch-amoled-1.75c`, ESP-IDF (version per upstream), LVGL. Flash over `/dev/cu.usbmodem2101` (may change).
- Backend: fork of `xinnan-tech/xiaozhi-esp32-server`, Docker Compose on this Mac (later Ubuntu). New providers: Google Cloud STT/TTS, Anthropic LLM.
- HA: via ha-mcp (`~/Projects/ha-agent`); never print the secret-path URL; policy changes need user confirmation.

## Rules
- Python in a project venv. ESP-IDF and upstream clones live in the sibling `~/Projects/weixue-vendor` shared by all worktrees (override with WEIXUE_VENDOR); no global installs.
- Secrets: Google service account JSON and API keys come from `~/Projects/ha-agent/.env` / `.secrets/` (0600). Never commit, never print values.
- Backup Flash before any erase; keep `backup/` out of git.
- Git: personal identity (`Jay_Wang <56106412+Jay-Wang2000@users.noreply.github.com>`), remote via `github.com-personal` SSH alias. Push only to the personal forks/repo.
- Anthropic work: read the `claude-api` skill first.
- Hardware stays connected to the Mac; verify the serial port before flashing.

## Git workflow (overrides the workspace pipeline's local-merge step)
- Never commit or push directly to `main`. Every task is a `feat/*` branch. Exception: orchestrator bookkeeping (`tasks.yml`, `docs/briefs/`) is committed on `main` by the orchestrator only.
- After the review and test gates pass and the user approves the spec, push the branch to the personal remote and open a pull request (follow the `creating-pull-requests` skill). The PR is merged on GitHub; `main` is only advanced by merged PRs.
- Each task = one commit (squash before opening the PR) + one PR titled `<task-id>: <short description>`, body linking `DESIGN.md` and the spec doc. No footers.
- Remote: `origin` = `git@github.com-personal:Jay-Wang2000/weixue.git` (private, created 2026-10-10). From now on every task branch is pushed and opened as a PR by the orchestrator after its gate passes (`gh pr create`, title `<task-id>: <short description>`, body linking `DESIGN.md` and the spec doc); the orchestrator merges it on GitHub (squash) unless Jay asks to review first. Orchestrator bookkeeping commits (`tasks.yml`, `docs/`) still land directly on `main` and are pushed. Upstream code (`xiaozhi-esp32`, `xiaozhi-esp32-server`) stays vendored locally; forks later.

## Process
- Working methodology: `docs/METHODOLOGY.md`. After every wave merge, run the Codex external review of `main` (spec + code) and file its findings before starting the next wave.

## External-service decisions need Jay's approval
- Any design decision about how we integrate with or configure an external service (Home Assistant / ha-mcp policy, Google Cloud STT/TTS models, locations, retention, Anthropic models, GitHub, networking) is proposed to Jay with options and a recommendation and waits for his approval before it is implemented. Agents write `question.json`; the orchestrator does not decide these alone.

## End-to-end testing rule (revised 2026-10-07)
- Per task: host tests plus simulated-device or scratch-stack evidence; device flashing when the task changes firmware. No per-task voice/audio run.
- Once per milestone: one full end-to-end session on the live stack and device (wake word → STT → LLM/agent → HA via ha-mcp → TTS → audio, plus barge-in and the deferred audio items), logs under `docs/logs/`. M1's is the `feat-voice-integration` gate; M2 gets its own at milestone end.

## Hardware verification rule
- Hold the device lock from flash through the end of the on-device test. Firmware branches merge serially with rebuild + on-device smoke after each rebase; after a wave, flash `main` to the board (device reconciliation).
- Any change to firmware UI or device behaviour (face, ring, panel, gestures, wake word, audio) must be flashed to the connected 1.75C and verified end-to-end on the device during review/test, not only by build success or emulator. The tester flashes the branch build, exercises the feature, and records what was observed (photo/log excerpt) in the test result. The device is always connected to this Mac.

## Office mode
- When Jay is in the office (Mac away from the home LAN), no agent may use macOS `say` or play audio; the Mac output is muted. Device voice tests are deferred; Jay is told only when a test needs him.
- Home Assistant stays reachable from the office through the WireGuard sidecar (`server/.env` WG_TUNNEL=1, compose profile `tunnel`, see `docs/server.md`): only the mcp-bridge container uses it. HA-dependent server tasks are not deferred; only audio is. Deploying a compose file that lacks the `wg` service would drop the tunnel, so server branches must be rebased onto `main` before any live deploy.
