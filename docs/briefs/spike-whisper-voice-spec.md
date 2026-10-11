# Feature Spec: Whisper spike
Task: spike-whisper-voice | Branch: spike/whisper-voice | Date: 2026-10-11

## Summary
Measured whisper handling on the 1.75C with proxy clips (Google TTS Charon, normal and LPC-whispered, played at Mac volume 15/25/35) and three device settings. The raw microphone was captured over UDP and re-analysed offline. Report: `docs/logs/spike-whisper-2026-10-11.md`.

## Changes
- `firmware/patches/37-audio-tune-diag.patch` + `firmware/overrides/main/weixue_audio_tune.h`: `diag audio gain <dB>` (ES7210 PGA, applied live) and `diag audio wakenet <t|default>`. They are diag-console only; the release binary has 0 `diag` strings.
- `firmware/build.sh`: `WEIXUE_AUDIO_DEBUG_UDP=<ip:port>` turns on the upstream audio debugger. It works only with `WEIXUE_FACE_DIAG=1` and is refused otherwise; a release build fails if the debugger is on.
- `scripts/spike_whisper.py`: makes the clips, runs trials and silence runs, and does the offline analysis (Silero, Google STT batch). `docs/firmware.md` rows were added.

## Design Decisions
- Server VAD and STT variants were evaluated offline on the captured device audio, so the live server stayed read-only.

## Problems & Resolutions
- Docker Desktop did not forward host UDP, so the receiver runs on the firewall-allowed Homebrew Python 3.14.
- Opening the port resets the board, so every run applies its settings after boot.

## Open Questions
- Which recognizer to use for quiet speech once streaming returns (external service; see question.json). Recommendation: B, streaming plus chirp_3 re-recognition for low-level or empty finals.
- Whether a WakeNet threshold of 0.5 may become the default. That depends on the false-wake budget and needs a soak first.

## Future Work
- Release knob for microphone gain 37.5 dB (recommended default with barge-in off).
- Real-whisper set: Jay's recordings are absent (`session-clips/whisper/`).

## Test Coverage
120 device trials and a 10-minute silence run, all at device volume 0. The board was flashed back to main's release build, which booted to idle. After the rebase both the diag build (with the knob) and the release build compile; the release has 0 `diag` strings and no debugger. Host tests pass. `flash_scripts_test.sh` fails the same 1 check on main ("new override listed"). Deferred: the rebased diag build was not re-flashed (only patch context changed); echo test at 37.5 dB with volume up; WakeNet 0.5 soak with media; live streaming latest_short run; real whispers.
