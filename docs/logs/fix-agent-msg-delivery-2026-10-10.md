# fix-agent-msg-delivery: diagnosis, sim and device run (2026-10-10)

Defects D1 and D2 of `device-run-2026-10-10.md`. Board `esp32-s3-touch-amoled-1.75c` on `/dev/cu.usbmodem2101`, home LAN, live server (`main` image) with this branch's four agent overlay files copied into the container for the run and `main`'s copied back after it. Device lock 22:18:03Z-22:37:33Z. Times: serial = device uptime, server = local (UTC+1).

| File | Content |
|---|---|
| `fix-agent-msg-delivery-2026-10-10-serial.log` | serial captures 1-6 (each starts with a board reset; FACEDUMP lines removed) |
| `fix-agent-msg-delivery-2026-10-10-server.log` | server excerpts (the container was recreated by another process at 22:37:50Z, after the restore, which discarded its log) |
| `fix-agent-msg-delivery-2026-10-10-fixture.jsonl` | fixture output: gateway acks and responses per item |
| `fix-agent-msg-delivery-sim-2026-10-10.log` | simulated device against a `main` and a branch scratch container |
| `fix-agent-msg-delivery-png/` | device card dumps (diag build), host renders of page 1 before/after and the sending card |

## Diagnosis

**D1.** The poll response is Content-Length delimited (aiohttp `json_response`), not chunked; the vendored `HttpClient` parses it correctly (1024 B reads, 8 KB queue, 40 s per read). `expected 2519 bytes but only received 1260` is one TCP segment: about 180 B of headers plus 1260 B of body is the device's MSS (`CONFIG_LWIP_TCP_MSS=1440`). The rest of the response never arrived and the socket reported `TCP receive failed: -1`; responses of 1-2 items fit one segment and always arrived. So the loss is on the link (device in `MAX_MODEM` Wi-Fi power save, 468 ms average ping), not the parser or the server write. A firmware fault turned it into D2: `ReadAll` returns `""` on a short body, the poll task then parsed nothing, kept `speak = true` and opened a session (`Proactive message pending (urgent=0)`), whose bare invocation had no lease. A disconnect before the headers is not an error to the vendored `HttpClient::GetStatusCode`, so such a poll waits out its 40 s timeout (S2:334-337; unchanged, it only delays the next poll). The sim reproduces the cut exactly (`body unreadable (1260 of 3295 bytes ... connection reset after 1440 B)`, then `Proactive message pending`, then `no current lease`).

**D2.** `on_invoke` awaited `conn.close()` inside the session's message loop: no frame was read until the close handshake ended, after up to the 10 s close timeout (SV:119 at 21:48:45, the next frame logged 21:48:55). Frames that reached the server before the connection was torn down were handled late; the device's `shown` and `approve`, written to its socket 5-7 s into that window, never reached the server. The v1 outbox counted "written to the transport" as delivered. In the sim with 1 s delays, `main` loses the approve sent into a closing session (`cw-main`: no `agent response`).

**Found during the run.** A device that reboots while its previous poll is held: the new boot's poll supersedes it, and the superseded request still took a snapshot page and sent it to the dead socket (reset 1: page 2 sent twice, recovered by the page rewind). Superseded polls now answer 204 with no agent items (reset 2: five bodies, no resend).

## Sim (scratch containers, live stack untouched)

| Run | Result |
|---|---|
| `d1-main`: `--weak-link 1440 --old-firmware --no-receipts` | 3475 B response cut after 1440 B, session with the bare invocation, `no current lease; closing` |
| `d1-branch`: `--weak-link 1440`, receipts | boot snapshot of 8 tasks + approval in 5 bodies, largest response 1180 B, 0 unreadable, `sync listed=8`; approve receipt `accepted` |
| `cw-main`: no-lease session, approval pushed into it, v1 device | approve lost |
| `cw-branch`: same, receipts | approve handled at once, receipt `accepted` in the session, session closed 4.1 s after the invocation (2 s quiet) |
| `cw-branch-v1dev`: branch, v1 device | approve recorded through the drain alone |
| `cw-main-receipts`: `main`, v1.1 device | no receipts from a v1 server: the device resends (first-wins dedupe keeps one decision) and gives up after 4 attempts |

## Device run (loopback build 3,128,256 B, then release 3,127,360 B, then `main`'s release 3,122,416 B)

| Check | Result |
|---|---|
| Boot, loopback | `bedside hint=day for this boot`, output volume 0, `sync listed=0 via=poll` |
| `ui` tour | 10/10 `shown` |
| `states` | 9/9 applied via poll and `shown`, all tiers |
| `burst` | applied, duplicate, applied (newer), stale, `dropped poll item of 2236 bytes` (sent alone), applied; bodies of 1-2 entries |
| Snapshot after reset, 8 full-size tasks | reset 1: 8 applied + `sync listed=8` 1.6 s after activation, one page resent (superseded poll, above); reset 2: 5 bodies, no resend, 8 + sync in 1.9 s |
| Closing window (`closing` x3: announcement session closed by the server, loopback approve in it) | 3/3: `agent ack sent ... awaiting receipt`, `agent receipt: approve ... result=accepted` 20-40 ms later, `Websocket disconnected` 0.9-1.0 s after; 3 responses recorded once each |
| Release smoke | 9/9 `states` shown, bedside page at night and left for an interrupt card, no loopback lines |
| `main` release flashed back | boot, `Activation done`, `sync listed=0 via=poll`; overlay files in the container equal `main`'s |

Not exercised on the device: a session without a current lease (no way to force a lease to expire on hardware; sim `cw-branch`), a truncated poll body (the link was good during the run; sim `d1-branch`), and screenshots of the three cosmetic fixes and of `Approve · sending` (the diag build dumped only titled cards; host renders in `fix-agent-msg-delivery-png/`).
