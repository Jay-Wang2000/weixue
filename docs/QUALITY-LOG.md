# Gate quality log

Purpose: detect whether the time-bounded gate (`.claude/workflows/wave-gate.js`: capped/deduped findings, info skipped, 1 skeptic per warning, 1 fix pass) lets more defects through than the original gate (every finding, 2 skeptics, 2 fix passes). The control is the independent post-wave Codex review: count its P1/P2 correctness findings per merged task, plus defects found on the device after merge.

Rule: if Codex P1 findings per task rise above the baseline for two consecutive waves, or an on-device regression traces to a finding the gate dropped, restore 2 skeptics for warnings and raise the cap.

| Wave | Gate variant | Gate agents (approx.) | Gate wall time | Confirmed issues (err/warn) | Codex P1 / P2 after merge | Escaped defects seen on device | Verdict |
|---|---|---|---|---|---|---|---|
| 1 (toolchain, server) | original | 54 + 81 | ~11 + 18 min | 0/21, 0/28 | included in Wave 2 review | — | baseline |
| 2 (firmware, stt, tts, llm) + integration | original | 203 + 37 | ~66 + 35 min | 0/15, 0/21, 0/18 (+ integration 0/11) | 5 P1, 5 P2 across 4 tasks (tool index, turn leak, deadline, TTS cancel, exposure) | TTS lost spaces ("isfour"); farewell/long listening | baseline |
| 3 (timing, face, imu, proactive) | original (launched before change) | 60 (only proactive reached the gate; 3 tasks blocked on the board being powered off) | 88 min total; proactive gate ~25 min | proactive 0/4 | tbd (after Wave 3 Codex review) | tbd | partial |
| 3 serial chain (screen-off, proactive, face, imu, timing + reconcile) | time-bounded (3 lenses ∥ tester, dedupe, cap 10, warning=1 skeptic) | 71 for 5 tasks + reconcile | 3.7 h total incl. builds/flashes on one board | 1, 5, 4, 2, 2 (all warnings, 0 errors) | 3 P1 / 6 P2 over 5 tasks (0.6 P1/task vs 1.25 P1/task baseline) | one-breath "Jarvis, what time is it?" fails (capture gap at wake; 0/8 in the task's own log, yet merged) | not worse than baseline on Codex counts; one acceptance slip → testers must fail on the task's own negative evidence |
| 4 (ring/bedside, panel) | time-bounded (wave-gate.js) | | | | | | |
