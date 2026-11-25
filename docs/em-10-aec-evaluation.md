# EM-10 (R1): echo cancellation that keeps the audience's laughter

Date: 2026-11-24. Machine: MacBook Air (Apple Silicon), macOS, Python 3.12 for YAMNet (TensorFlow 2.18.1, as on the server), Python 3.14 for the client.

R1 asks the PC to remove the content's sound from the microphone while keeping the audience's reaction. The question here is narrower and measurable: **after echo cancellation, does YAMNet still find the laughter?**

## 1. The GStreamer route is closed on both platforms

| Check | Result |
|---|---|
| Ubuntu 24.04, `gstreamer1.0-plugins-bad` 1.24.2 | no `webrtcdsp` / `webrtcechoprobe` (README, known limits) |
| macOS, Homebrew GStreamer 1.28.9 | `No such element or plugin 'webrtcdsp'` / `'webrtcechoprobe'` |
| Homebrew `webrtc-audio-processing` (needed to build the plugin) | `No available formula` |
| Other elements named echo/webrtc | `audioecho` (adds echo), `webrtcbin`, WHIP/WHEP (network transport): none cancels echo |

So `test_echo_capture.py::test_synthetic_echo_mixture_and_seek` cannot run on either platform without building the plugin and its library from source.

## 2. Round 1: WebRTC AEC3 without GStreamer (`pywebrtc-audio` 0.2.0)

`pywebrtc-audio` wraps the WebRTC audio processing module (AEC3) with prebuilt wheels for Linux, macOS and Windows. Synthetic 16 kHz input, 0.5 s chunks as the client sends them, thresholds from the GStreamer test (`client/tools/aec/synthetic_pywebrtc.py`):

| Room delay | Condition | ERLE | near_gain | Time per 0.5 s |
|---|---|---|---|---|
| 40 / 120 ms | echo only | 32.4 dB | – | ~3 ms |
| 40 / 120 ms | reaction only | – | 0.55–0.64 | ~3 ms |
| 40 / 120 ms | double talk | 21.4–25.7 dB | 0.70–0.74 | ~3 ms |

Both thresholds pass (ERLE > 3 dB, near_gain > 0.15), and the delay hint changes nothing. But the correlation between the output and the clean reaction was about −0.05. Searching over lags: the output is **8 ms late**, and after that shift the reaction alone correlates only 0.60 (spectrum 0.38), less than in double talk (0.81 / 0.75). The thresholds measure energy, not whether the reaction survives.

## 3. Round 2: YAMNet on real laughter

Playback: macOS `say` (spoken English). Laughter: the 40 ESC-50 "laughing" clips (200 s; Piczak, ACM MM 2015, CC BY-NC 3.0). A first try with a recorded voice message scored as Speech 0.95–0.99, laughter ≤ 0.04, so it was not used.

With 3 s of playback first, AEC3 removed **44–46 dB** of echo, yet laughter windows kept: **0 of 3**. The delay hint (0 / 60 ms), the processor class (`AudioProcessor` / `EchoCanceller`) and the warm-up made no difference. The echo was gone; AEC3's residual-echo suppressor turned the microphone down whenever playback and laughter overlapped. In a video call people take turns; here the content never stops, so it is always double talk.

## 4. Our linear canceller

`client/encore_client/sensing/linear_aec.py`: partitioned-block frequency-domain NLMS (256-sample blocks, 4096 taps), no suppressor, with a foreground/background filter pair. Defects found and fixed while building it, each checked by a test in `client/tests/test_linear_aec.py`:

| Defect | Symptom | Fix |
|---|---|---|
| Step not normalised over partitions | diverged on every input | normalise by the power of all partitions |
| Empty bins with tonal playback | AM 220 Hz tone diverged | floor = 0.1 × mean bin power |
| Normaliser lagging at onsets | AM tone diverged | follow rises at once (now also covered by the background reset) |
| Zero-padding short outputs | 4 ms gap in every 0.5 s chunk (8000 = 31 × 256 + 64) | start one block behind: constant 16 ms delay |
| Filter adapting on the laugh with no echo | playback injected into the output | background/foreground pair: output untouched when there is no echo |

Synthetic convergence (60 ms room), echo removed after 3 / 9 / 24 s: speech-like 14 / 22 / 28 dB, AM tone 26 / 29 / 38 dB, white noise 25 / 64 / 103 dB.

## 5. Round 4: AEC3 vs ours, 40 laughter clips, latency removed

`client/tools/aec/yamnet_compare.py`, 15 s warm-up, 79 of 415 windows trigger on the clean laughter (mean 0.65):

| Echo vs laughter | No AEC | WebRTC AEC3 | Linear (ours) |
|---|---|---|---|
| −6 dB | 10 (13%) | 2 (3%) | **47 (59%)** |
| 0 dB | 8 (10%) | 0 (0%) | **42 (53%)** |
| +6 dB | 2 (3%) | 0 (0%) | **40 (51%)** |
| no echo | 79 (100%) | 42 (53%) | 56 (71%) |
| ERLE | – | 51.9 dB | 41.7 dB |

No processor made the echo alone trigger laughter (0 false windows everywhere).

**Measurement noise.** Shifting the clean laughter by 16 ms, with no processing at all, keeps 61 of 79. Many windows sit near the 0.4 trigger, so counts move by about ±20% from alignment alone. Only differences larger than that are conclusions.

## 6. Conclusions

1. Without echo cancellation, spoken playback hides the laughter from YAMNet even when it is 6 dB quieter than the laughter (13% kept). R1 is necessary.
2. WebRTC AEC3 as shipped for calls is worse than nothing for this product (0–3% kept) despite 52 dB of echo removal.
3. The linear canceller keeps 51–59% at every echo level, 4 to 20 times more than no AEC, with 42 dB of echo removal.

## 7. Limits and next steps

- The room is a linear synthetic response; real speakers add non-linear distortion a linear filter cannot remove. Next: a real room recording (EM-10 acceptance case 2).
- Playback was speech only; music and effects are untested with YAMNet.
- With no echo the canceller still loses some windows (56 vs the 61 of a pure shift): the background is sometimes copied during a laugh. A stricter copy rule is the next tuning step.
- Wired in as the player's fallback when `webrtcdsp`/`webrtcechoprobe` are missing (section 8). Letting the user switch it (R12, EM-21) comes next.

## 8. In the app: the player's fallback

`player/pipeline.py` keeps the WebRTC filter when both plugins exist. Otherwise (`echo_reference == "linear"`, as on Ubuntu 24.04 and macOS) the audio branch becomes `volume ! tee`, one copy to the sink and one, as mono S16 at 48 kHz, to an `appsink` at the same running time. `player/linear_echo.py` keeps those samples by running time (`ReferenceBuffer`, 2 s) and feeds them with each 10 ms microphone block to the linear canceller (768-sample blocks, 256 ms tail). Status follows the WebRTC filter: `warming_up` for 500 ms, then `active`; `reference_unavailable` when the reference does not cover the block, after a seek, while paused, or at speeds other than 1x.

**The reference is read from a cursor, not looked up per block.** The microphone's timestamps are whole milliseconds and the capture callback jitters, and 1 ms is 48 samples. Looking up each block's reference by its own timestamp made the echo path seem to jump by a few samples every 10 ms; with ±3 ms jitter the canceller removed nothing (0.0 dB, `test_reading_the_reference_by_each_timestamp_would_have_failed`). `LinearEchoPath` therefore places only the first block by its timestamp and then advances by exactly the samples consumed: 43.8 dB with the same jitter, the same as with exact timestamps. A timestamp more than 20 ms from the cursor (a dropped block, clock drift) moves the cursor and restarts the filter. The cursor sits 5 ms before the timestamp, which the filter sees as 5 ms more echo delay; a slightly late timestamp then neither asks for audio not played yet nor puts the echo ahead of its reference.

The first version of this wiring (PR #47, c699ea6) looked up each block by timestamp, and its playback test cut every simulated microphone block at whatever the position happened to be. Both made the echo path move every block. The test now cuts back-to-back blocks from one simulated recording, as a real microphone delivers them.

Known costs: the cleaned microphone audio is 16 ms behind the raw audio (one block). Two separate clocks (sound card and microphone) can drift apart by up to about 0.1 ms per second, so on a long session the 20 ms rule restarts the filter every few minutes (about 3 s to regain 15 dB on the CI signal).

Tests: `client/tests/test_linear_echo.py` (reference timing, gaps, seeks, threads, warm-up, 48 kHz cancellation, jittery timestamps, the per-block lookup it replaces, re-anchoring, pass-through; NumPy only) and `test_echo_capture.py::LinearEchoPlaybackTest`, the WebRTC test's scene run through the real player where the plugins are missing (CI on Ubuntu).

## Reproduce (macOS)

```sh
brew install python@3.12
python3.12 -m venv ~/venvs/yamnet
~/venvs/yamnet/bin/pip install -r server/requirements-yamnet.txt pywebrtc-audio
PY=~/venvs/yamnet/bin/python sh client/tools/aec/make_inputs.sh
~/venvs/yamnet/bin/python client/tools/aec/yamnet_compare.py /tmp/aec/laugh_esc50_all.wav /tmp/aec/content.wav
~/venvs/yamnet/bin/python client/tools/aec/synthetic_pywebrtc.py
python -m pytest -q client/tests/test_linear_aec.py
```
