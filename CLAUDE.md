# weixue — ESP32-S3 round-AMOLED voice home agent

Read `DESIGN.md` first; decisions live in its ADRs. Research background: `~/Projects/research/reports/weixue-home-agent-v1.md`.

## Stack
- Firmware: fork of `78/xiaozhi-esp32`, board `esp32-s3-touch-amoled-1.75c`, ESP-IDF (version per upstream), LVGL. Flash over `/dev/cu.usbmodem2101` (may change).
- Backend: fork of `xinnan-tech/xiaozhi-esp32-server`, Docker Compose on this Mac (later Ubuntu). New providers: Google Cloud STT/TTS, Anthropic LLM.
- HA: via ha-mcp (`~/Projects/ha-agent`); never print the secret-path URL; policy changes need user confirmation.

## Rules
- Python in a project venv; ESP-IDF installed under the project, no global installs.
- Secrets: Google service account JSON and API keys come from `~/Projects/ha-agent/.env` / `.secrets/` (0600). Never commit, never print values.
- Backup Flash before any erase; keep `backup/` out of git.
- Git: personal identity (`Jay_Wang <56106412+Jay-Wang2000@users.noreply.github.com>`), remote via `github.com-personal` SSH alias. Push only to the personal forks/repo.
- Anthropic work: read the `claude-api` skill first.
- Hardware stays connected to the Mac; verify the serial port before flashing.

## Git workflow (overrides the workspace pipeline's local-merge step)
- Never commit or push directly to `main`. Every task is a `feat/*` branch.
- After the review and test gates pass and the user approves the spec, push the branch to the personal remote and open a pull request (follow the `creating-pull-requests` skill). The PR is merged on GitHub; `main` is only advanced by merged PRs.
- Each task = one commit (squash before opening the PR) + one PR titled `<task-id>: <short description>`, body linking `DESIGN.md` and the spec doc. No footers.
- Remote `origin` is the personal repo `Jay-Wang2000/weixue` over the `github.com-personal` SSH alias (to be created). Upstream forks (`xiaozhi-esp32`, `xiaozhi-esp32-server`) get their own PR-per-change workflow on the forks.
