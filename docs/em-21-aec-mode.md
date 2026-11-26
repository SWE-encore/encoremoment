# EM-21 (R12): choosing echo cancellation and seeing what is in effect

Date: 2026-11-26. Builds on EM-10 (R1): the player's linear canceller (#44, #47, #48).

R12 asks the user to turn echo cancellation (AEC) on or off, to see the requested mode, the effective mode and a missing playback reference apart from microphone sensing, and to record both modes and the mode revision on the same input while the session keeps working.

## 1. What the window shows

A line under the microphone control, with its own button (`ui/echo.py`, decisions in `sensing/aec_mode.py`):

| Situation | Line |
|---|---|
| Before Start | Echo cancellation — requested on; applies from Start |
| On, cancelling | … requested on; effective: on, removing the film's sound (revision 1) |
| On, first 0.5 s after a reset | … effective: on, learning the room (about 0.5 s) |
| On, no reference (paused, seeking, not 1x) | … effective: not removing anything — no playback reference …; audio is analysed as captured ⚠️ |
| Off | … requested off; effective: off — audio is analysed as captured (revision 2) |
| Microphone off | … effective: none while the microphone is off |
| No controlled player (`--no-player`) | Echo cancellation: unavailable — no controlled player in this run (no button) |

⚠️ marks a request that is not in effect. The effective mode is the status of the newest microphone block, the same value each `audio_chunk` carries and the server copies into `event.source.aec`, so the screen never claims more than the data says.

## 2. Mode revision and the contract

The contract already had an `aec` sensor in `control_update` (R12) that the client never sent. Now:

- Start sends `control_update(sensor="aec", enabled=<requested>)`; each switch sends the next one. The `aec` generation is the **mode revision** (1, 2, 3, …), acknowledged by the server like any control.
- The switch does not touch the microphone generation: sensing, uploads and open reactions continue.
- Turning cancellation on again resets the canceller (`player.reset_echo()`), so it warms up instead of resuming with a filter learnt before the gap.
- A 0.5 s chunk that spans the switch is sent as `uncancelled` (existing rule: a chunk never claims full cancellation across a change).
- The choice is kept across sessions (R11) and announced again at the next Start. An off choice at Start sends nothing, because the server's `aec` sensor already starts off at revision 0.

## 3. The same input in both modes

Laughter (40 ESC-50 clips) under spoken playback, YAMNet windows that still trigger laughter, from the EM-10 evaluation (`docs/em-10-aec-evaluation.md`, section 5; 79 of 415 windows trigger on the clean laughter):

| Echo vs laughter | AEC off | AEC on (linear) |
|---|---|---|
| −6 dB | 10 (13%) | 47 (59%) |
| 0 dB | 8 (10%) | 42 (53%) |
| +6 dB | 2 (3%) | 40 (51%) |
| no echo | 79 (100%) | 56 (71%) |

So "off" is the right choice only when nothing plays through the speakers (headphones), and the screen says which mode the analysed audio really got.

Through the real `ServerSession` (`test_aec_mode.py::test_the_same_input_in_both_modes_and_a_switch_during_the_reaction`), one loud reaction in three runs: AEC on throughout, off before any audio, and switched off in the middle of the reaction. Each run gives one FINAL event with the same timecodes and microphone generation; the event records `active` or `off` from the window it opened in, and both revisions are acknowledged as `applied`.

## 4. Tests

`client/tests/test_aec_mode.py` (11 tests; the last needs PySide6 and runs in CI): announcement at Start, numbered changes, canceller reset, off passes audio unchanged and never calls the player, every status line, microphone off, no player, a new session, chunk statuses across a switch, the server run above, and the workspace button.

## 5. Limits

- Switching from cancelled to raw audio moves the analysis stream by the canceller's 16 ms delay once; the chunk is marked `uncancelled`.
- The server keeps the AEC status of the window an event opened in; an event that spans a switch is not marked as mixed. Changing that is a server (R2) decision.
- Real speakers in a real room are still untested (EM-10 acceptance case 2, EM-27).
