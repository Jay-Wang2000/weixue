# Spike: whispered speech on the device (decision 58), 2026-10-11

Task `spike-whisper-voice`. Board: 1.75C on USB, diag build of this branch with the audio debugger (`WEIXUE_AUDIO_DEBUG_UDP`), device output volume 0 for every run. Live server read-only; its recognizer during the runs was `GoogleSTT` (chirp_3 batch, `VOICE_ASR`), not the streaming `latest_short` path. Runs 08:13-08:31Z (trials) and 08:42-08:54Z (silence); main's release build flashed back at 08:55Z and booted to idle (`spike-whisper-2026-10-11-release-boot-serial.log`).

## Method

- Proxy set: 10 one-breath requests ("Jarvis, what time is it?", "... what is seven times eight?", ...; no Home Assistant actions), Google TTS `en-US-Chirp3-HD-Charon` at 24 kHz. `voiced` = the TTS clip; `whisper` = the same clip with its excitation replaced by noise (frame-wise LPC, same spectral envelope and RMS); Google STT reads all 10 whisper clips correctly when fed directly. Played with `afplay` from the Mac speakers at Mac output volume 15/25/35 (Mac-to-board distance not measured). Clips: `$WEIXUE_VENDOR/session-clips/whisper-proxy/`.
- Real whispers: none yet. `~/Projects/weixue-vendor/session-clips/whisper/` does not exist.
- Per trial (`scripts/spike_whisper.py trials`): the device's `Wake word detected` line, the transcript it gets back (`>> ...`), then BOOT presses until idle. The raw microphone channel (16 kHz, before the AFE) streams over UDP to the Mac and is cut per trial with host markers.
- Offline, on each captured trial: active level (RMS of the loudest 30 % of 20 ms frames), Silero VAD with the server's rule (hysteresis, 3 of 5 frames) at thresholds 0.5/0.35/0.2, and Google STT batch `latest_short` and `chirp_3` (us), as captured and with +18 dB digital gain.
- "STT ok" means every keyword of the request is in the transcript ("spell" heard as "tell" counts as wrong).
- Knobs varied: ES7210 microphone PGA gain 30 dB (board default) and 37.5 dB (maximum); WakeNet threshold model default (0.627) and 0.5 (`diag audio`, patch 37).

## Results

| setting | set | Mac vol | mic dBFS (median) | wake | device STT ok | VAD 0.5 | VAD 0.35 | VAD 0.2 | offline chirp_3 | offline chirp_3 +18 dB | offline latest_short | offline latest_short +18 dB |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| gain 30, WakeNet default | voiced | 35 | -46.5 | 8/10 | 8/10 | 10/10 | 10/10 | 10/10 | 10/10 | 10/10 | 6/10 | 9/10 |
| gain 30, WakeNet default | voiced | 25 | -52.5 | 5/10 | 5/10 | 10/10 | 10/10 | 10/10 | 10/10 | 10/10 | 6/10 | 8/10 |
| gain 30, WakeNet default | voiced | 15 | -59.3 | 3/10 | 2/10 | 9/10 | 10/10 | 10/10 | 10/10 | 10/10 | 4/10 | 4/10 |
| gain 30, WakeNet default | whisper | 35 | -47.2 | 6/10 | 5/10 | 10/10 | 10/10 | 10/10 | 10/10 | 10/10 | 7/10 | 8/10 |
| gain 30, WakeNet default | whisper | 25 | -53.0 | 3/10 | 1/10 | 10/10 | 10/10 | 10/10 | 9/10 | 8/10 | 1/10 | 5/10 |
| gain 37.5, WakeNet default | voiced | 35 | -39.3 | 8/10 | 8/10 | 10/10 | 10/10 | 10/10 | 10/10 | 10/10 | 9/10 | 9/10 |
| gain 37.5, WakeNet default | voiced | 25 | -44.4 | 6/10 | 5/10 | 10/10 | 10/10 | 10/10 | 10/10 | 10/10 | 6/10 | 8/10 |
| gain 37.5, WakeNet default | voiced | 15 | -51.5 | 6/10 | 5/10 | 10/10 | 10/10 | 10/10 | 9/10 | 10/10 | 3/10 | 5/10 |
| gain 37.5, WakeNet default | whisper | 35 | -39.8 | 7/10 | 6/10 | 10/10 | 10/10 | 10/10 | 10/10 | 10/10 | 7/10 | 8/10 |
| gain 37.5, WakeNet default | whisper | 25 | -45.7 | 6/10 | 5/10 | 10/10 | 10/10 | 10/10 | 9/10 | 9/10 | 2/10 | 7/10 |
| gain 37.5, WakeNet 0.5 | voiced | 15 | -51.9 | 10/10 | 10/10 | 10/10 | 10/10 | 10/10 | 10/10 | 10/10 | 1/10 | 7/10 |
| gain 37.5, WakeNet 0.5 | whisper | 25 | -45.9 | 10/10 | 6/10 | 10/10 | 10/10 | 10/10 | 9/10 | 8/10 | 4/10 | 5/10 |

False wakes, nothing played:

| setting | minutes | room level (median / p99 frame dBFS) | wakes | Silero windows opened (0.5 / 0.35 / 0.2) |
|---|---|---|---|---|
| gain 37.5, WakeNet 0.5 | 10 | -63.2 / -59.6 | 0 | 0 / 0 / 0 of 21 |

Other measurements:
- The 37.5 dB gain raises speech by 7.2-7.8 dB and the idle floor by 7.8 dB (median -70.7 to -62.9 dBFS): same signal-to-noise ratio, higher absolute level. Loudest peak at 37.5 dB: -22.8 dBFS (no clipping at these levels).
- Every device wake reached a server recognition (78 of 78; server logs), median chirp_3 latency 1.52-1.57 s. No "didn't catch that" replies.
- Wake fires a median 1.4-1.7 s after playback starts.
- Of the 78 woken trials, 66 had a correct transcript. 7 of the 12 misses contain "spell the word necessary" (heard "tell"/"sell"). Whispers at -46 dBFS lose more on the device path (6/10 correct) than offline chirp_3 on the same raw capture (9/10).
- latest_short batch returned an empty transcript for 64 of 120 captures (34 with +18 dB); chirp_3 got 116 of 120 right as captured and 115 with +18 dB.

## Findings

1. The wake word limits whisper first. With the release settings it fires 3/10 at -53..-59 dBFS. It is the only step where the 37.5 dB gain mattered (proxy whisper -46 dBFS: 6/10), because WakeNet scores the absolute level. WakeNet 0.5 fired 20/20 on the hardest sets.
2. Server VAD is not a limit at these levels. Silero at 0.5 opened on 119 of 120 captures, and every wake produced a recognition. Lowering `threshold` would change nothing here.
3. chirp_3 batch (live now) reads quiet and whispered captures almost perfectly. latest_short (the streaming path) loses most of them, and +18 dB digital gain recovers only part. Whisper accuracy depends on which model the streaming fix ends on.
4. A digital-gain proxy for AFE AGC did not help chirp_3 and only partly helped latest_short. The AFE AGC itself was not built: it needs `agc_init` at AFE creation (internal RAM, a different pipeline).

## Recommendation

| setting | release default now? | note |
|---|---|---|
| ES7210 microphone gain 37.5 dB (from 30) | yes, with barge-in off | Wake +3/10 at the lowest levels, no clipping, 0 false wakes in a quiet room. Speaker echo at this gain is unmeasured (device volume was 0). Echo into the microphone grows with gain, so with the barge-in flag on it needs an echo/self-interrupt test first. |
| WakeNet threshold 0.5 (from 0.627) | not yet | 20/20 at the hardest levels and 0 wakes in 10 quiet minutes. False wakes from TV, music or conversation are untested. It needs a multi-hour soak with media playing, and Jay decides the false-wake budget. |
| Server Silero `threshold` | no change | Not limiting (finding 2). |
| AFE AGC / server-side gain | no | chirp_3 needs none; latest_short gains little (finding 4). |
| Recognizer for low-level speech | decision for Jay (external service) | See the open question: stay on chirp_3 batch, use a chirp_3 batch re-recognition for low-level utterances on the streaming path, or accept latest_short losses. |

Needs the barge-in flag off: the 37.5 dB gain (until the echo test) and any lowered WakeNet threshold while the device speaks. Wake-word detection during playback stays off (`CONFIG_WAKE_WORD_DETECTION_IN_LISTENING` is off). A whisper at -46..-53 dBFS during playback competes with residual echo, so barge-in by whisper stays out of reach while barge-in uses level-based VAD.

Not possible from this spike: a claim about real whispers. The proxy covers -39 to -60 dBFS at the microphone; where a real whisper at bedside distance falls on that scale needs Jay's recordings. A proxy whispered "Jarvis" woke the device at every tested level with WakeNet 0.5, so a whispered wake word is not ruled out by the proxy.

## Open question (for Jay, external service)

Recognizer for quiet/whispered speech once streaming returns: (A) keep chirp_3 batch for every turn (best accuracy here; 1.52-1.57 s median recognition after the utterance ends); (B) streaming `latest_short`, plus a chirp_3 batch re-recognition when the utterance level is below about -45 dBFS or the stream final is empty (extra cost only on quiet turns); (C) streaming `latest_short` only (fastest, loses most whispers). Recommendation: B.

## Deferred

- Real-whisper set: Jay records 10 whispered requests ("Jarvis, ..."), phone at bedside distance, into `~/Projects/weixue-vendor/session-clips/whisper/`, then `scripts/spike_whisper.py trials` with a `real` set.
- Echo/self-interrupt test at gain 37.5 with device volume up and the barge-in flag on.
- WakeNet 0.5 soak (hours, media playing) and a streaming `latest_short` live run (server on `GoogleSTTStream`).
- The release knob for the 37.5 dB gain itself (board constructor argument) is a follow-up task; this branch adds only diag-console knobs.

## Evidence

`spike-whisper-2026-10-11-{g30,g375,g375wn50,silence-g375wn50}-{serial,server}.log` (redacted), `-analysis.jsonl` (every trial with transcripts, levels and VAD), `-silence.json`, `-release-boot-serial.log`. The raw UDP captures (room audio) stay in `$WEIXUE_VENDOR/spikes/whisper/runs/`, out of git.
