# EM-11: reaction recognition on labeled clips it was never tuned on (R2)

Date: 2026-11-25. Machine: laptop, AMD Ryzen 7 8845HS, Ubuntu 24.04.3, CPU only. Server code: `main` at 31f40ef; this work changes nothing in `server/encore_server/`.

R2's check case (`documents/submission-b/sections/specifications.tex`) asks for labeled laughter, scream, gasp, speech, soundtrack-only and mixed recordings, with per-class errors and the measured detection delay. It also says thresholds are calibrated on development recordings and evaluated on separate ones. This is the separate evaluation. Nothing was tuned on it, and the server's thresholds were not changed.

**Short answer:** with the current thresholds, YAMNet finds about a third of the laughter clips, about half of the screams and half of the gasps. It rarely fires on speech or music. It does fire a gasp on breathing, sighs and sneezes: 15 of 40 such clips. When a reaction is found, it opens 0.7–2.5 s after the sound starts (medians).

## 1. Data

- **Source:** the FSD50K eval split (Fonseca et al., IEEE/ACM TASLP 2022, [Zenodo 4060432](https://zenodo.org/records/4060432)). The clips are Freesound sounds, and their uploaders do not appear in FSD50K's dev split. They are not from AudioSet (YouTube), which YAMNet was trained on.
- **Download:** the labels (`FSD50K.ground_truth.zip`, 334.7 kB) came from Zenodo. The 280 clips (146.6 MB) were fetched one WAV at a time from the Hugging Face mirror `Fhrozen/FSD50k`. They are converted with `ffmpeg -ac 1 -ar 16000 -sample_fmt s16`.
- **Not committed:** the clips are not in the repository. Each clip carries its own Freesound license (CC0, CC-BY, CC-BY-NC or Sampling+). `docs/em-11-clips.json` lists the Freesound ids.
- **Groups:** a clip joins the first group whose labels it fits. 40 clips per group were drawn in a fixed shuffled order (seed 11). Clips over 2 MB (about 22 s at 44.1 kHz) were skipped, 22 in all, which favours shorter clips.
- **Excluded:** the 8 ESC-50 clips EM-26 already ran.

| Group | Labels (any of) | Not if labeled | Eligible | Median length |
|---|---|---|---|---|
| laughter | Laughter, Giggle, Chuckle_and_chortle | Screaming, Gasp, Music | 236 | 4.8 s |
| scream | Screaming | laughter labels, Gasp, Music | 113 | 3.6 s |
| gasp | Gasp | laughter labels, Screaming, Music | 43 | 2.8 s |
| speech | Speech | any reaction, Shout, Yell, Music | 650 | 6.3 s |
| music | Music | Human_voice | 1 869 | 5.0 s |
| shout_yell | Shout, Yell | any reaction, Music | 151 | 4.9 s |
| breath_cough | Breathing, Cough, Sigh, Sneeze | any reaction, Music | 297 | 3.4 s |

The labels are per clip, not per moment. A "laughter" clip can also hold speech or silence, and the delay below is then an upper bound. These are uploaded recordings, not an audience in a living room: there is no room, microphone or echo canceller in the path.

## 2. Method

`server/tools/em11_eval.py` sends each clip into the real `ServerSession` as a PC would: 0.5 s audio chunks, microphone generation 1, camera off. Everything after the socket is production code: admission, 0.975 s windows every 0.48 s, YAMNet, the room baseline and the detector.

- **Model:** YAMNet `google/yamnet/1` from the TF-Hub cache, with TensorFlow 2.18.1, tensorflow-hub 0.16.1 and numpy 1.26.4. Each reaction is the highest score among its YAMNet classes (`server/encore_server/models.py`): laughter is Laughter, Baby laughter, Giggle, Snicker, Belly laugh and Chuckle/chortle; scream is Screaming; gasp is Gasp.
- **Two server set-ups:**
  - **audio-only:** no face model; `DetectorConfig` `detector-v1` opens at laughter .4, scream .4, gasp .3.
  - **face-server:** started with a face model, as in the demo, with the camera off. Laughter then opens on sound alone at `TabRules.strong_audio` .25. Scream and gasp are the same as audio-only.
- **Conditions:**
  - **clean:** 2 s of silence, the clip, then 3 s of silence.
  - **over playback** (the three reaction groups only): the clip over a speech or a music clip from the same set, looped from 2 s before the reaction to 3 s after it. The reaction-to-playback level is +10 dB (playback the echo canceller mostly removed) or 0 dB (none removed). Levels are active levels: 10 ms frames within 30 dB of the loudest.
- **Scoring:** an event counts for the clip if its trigger window touches the clip. Events while only the playback sounds are counted apart.
- **Delay:** from the onset to OPEN on the simulated clock.
  - The onset is the first 10 ms frame within 20 dB of the clip's loudest.
  - A chunk reaches the server when its last sample is captured, plus the time `on_audio` took. There is no network in this measurement.
  - A quieter part of the clip before the onset can make the delay negative. This happened on two gasp clips: down to −0.48 s alone and −0.68 s over playback.
- **Cost:** 280 clips, 1 520 runs and 14 014 distinct windows took 96 s. A window took a median 4 ms of YAMNet time.

The tool's own test (`server/tests/test_em11_eval.py`, in CI) uses a scripted model. It checks grouping, the onset and level arithmetic, and the scoring through `ServerSession`.

## 3. Results

### Reaction clips alone (clean)

| Reaction | Found, audio-only | Found, face-server | Another reaction opened on these clips | Delay median / p90, audio-only | Delay, face-server |
|---|---|---|---|---|---|
| laughter | 11/40 (28%, 95% CI 16–43%) | 13/40 (32%, 20–48%) | gasp 1 | 2.50 / 3.77 s | 1.42 / 2.79 s |
| scream | 18/40 (45%, 31–60%) | 18/40 | laughter 2, gasp 4 | 0.90 / 1.35 s | same |
| gasp | 20/40 (50%, 35–65%) | 20/40 | scream 1 | 0.69 / 3.03 s | same |

Three clips opened two reactions: one laughter clip opened gasp as well, and two scream clips opened gasp as well.

### Clips with no reaction (false alarms, clean)

| Group | Clips | Opened, audio-only | Opened, face-server |
|---|---|---|---|
| speech | 40 | gasp 2 (5%) | gasp 2 |
| music | 40 | none (0%, 95% CI 0–9%) | laughter 1 |
| shout_yell | 40 | scream 3 (8%) | scream 3 |
| breath_cough | 40 | **gasp 15 (38%)**, laughter 1 | gasp 15, laughter 2 |

The false gasps all come from breathing (11 of 19 clips), sneezes (3 of 7) and one sigh (1 of 4). Coughs gave none (0 of 10).

### Reactions over playback (audio-only)

| Reaction | Playback | +10 dB found | 0 dB found | Delay median at +10 / 0 dB | Opened while only the playback sounded |
|---|---|---|---|---|---|
| laughter | speech | 10/40 (25%) | 6/40 (15%) | 3.27 / 3.45 s | 1 in 400 s |
| laughter | music | 11/40 (28%) | 10/40 (25%) | 1.79 / 2.25 s | 0 in 400 s |
| scream | speech | 8/40 (20%) | 1/40 (2%) | 1.22 / 0.82 s | 1 in 400 s |
| scream | music | 5/40 (12%) | 5/40 (12%) | 0.96 / 1.33 s | 0 in 400 s |
| gasp | speech | 11/40 (28%) | 6/40 (15%) | 1.88 / 2.93 s | 2 in 400 s |
| gasp | music | 5/40 (12%) | 3/40 (8%) | 5.81 / 3.09 s | 0 in 400 s |

- Over playback, a wrong reaction opened on 0–2 clips per row.
- With the face-server set-up, laughter over playback was found a little more often: 14, 9, 14 and 11 clips in the order of the table. Scream and gasp did not change.
- Over playback some p90 delays reach 10–17 s, because a reaction that was found late in a long clip is still counted.

### Where the scores fall (clean, peak score while the clip plays)

Share of each group's clips whose peak reaches the level. With the plain detector, a reaction opens exactly when its peak reaches its trigger level. This is a description of this set, not a tuning result.

| Reaction score | Level | laughter | scream | gasp | speech | music | shout_yell | breath_cough |
|---|---|---|---|---|---|---|---|---|
| laughter | 0.1 | 55% | 10% | 8% | 2% | 2% | 5% | 10% |
| laughter | 0.25 | 32% | 5% | 0% | 0% | 2% | 0% | 5% |
| laughter | 0.4 | 28% | 5% | 0% | 0% | 0% | 0% | 2% |
| scream | 0.1 | 10% | 80% | 30% | 5% | 0% | 15% | 5% |
| scream | 0.25 | 0% | 58% | 8% | 0% | 0% | 10% | 0% |
| scream | 0.4 | 0% | 45% | 2% | 0% | 0% | 8% | 0% |
| gasp | 0.1 | 2% | 18% | 80% | 5% | 0% | 2% | 52% |
| gasp | 0.3 | 2% | 10% | 50% | 5% | 0% | 0% | 38% |
| gasp | 0.5 | 2% | 2% | 35% | 2% | 0% | 0% | 35% |

## 4. Why clips are missed

For each missed clip, YAMNet was run on the whole clip and its top class noted, taking the maximum over frames.

- **Laughter, 29 missed:**
  - YAMNet's top class is Speech on 18 of them, then Animal, Silence and "Inside, small room" twice each.
  - Scores split into two clusters. The 11 clips that were found peak at 0.41–0.99. Of the other 29, only 2 reach 0.25, and 18 stay below 0.1.
  - By kind:

    | Kind of laughter clip | Clips | Found, audio-only | Found, face-server |
    |---|---|---|---|
    | single laugher, no speech label | 19 | 10 | 11 |
    | crowd (Crowd or Human_group_actions) | 7 | 0 | 0 |
    | laughter within speech | 6 | 1 | 1 |
    | shorter than 1.5 s | 8 | 0 | 1 |

- **Scream, 22 missed:** the top class is Speech on 6, Silence on 3 and Groan on 2. One had Screaming on top, but below .4.
- **Gasp, 20 missed:** the top class is Sneeze on 4, Silence, Speech and Cough on 3, 3 and 2, then Grunt and Sigh on 2 each.

Gasp is the hardest class to separate. YAMNet's Gasp score is high on breathing and sneezes and low on many labeled gasps, so no single gasp level separates them: at .3 the rule finds 50% of gasps and flags 38% of breathing and cough clips, and at .5 it is 35% and 35%.

## 5. What this means

- **R2's evaluation now exists, and it is weak.** About a third of laughter clips are found. Laughter is the class the product depends on most.
- **Single, clear laughter works.** 10–11 of 19 such clips are found, and found clips peak far above the threshold. Crowd laughter, laughter inside speech and short chuckles are mostly missed, because YAMNet calls them speech. A living room with a few viewers is closer to the clear-laughter case than to a crowd, but no audience recording has been checked.
- **False alarms are rare for speech and music.** Speech and music sounding alone opened 0–2 events in 400 s of playback per condition. The one exception is gasp on breathing and sneezes. A viewer breathing heavily near the microphone could open gasp events.
- **Playback hurts recall.** At 0 dB, screams over speech are almost never found (1 of 40). The echo canceller's job (R1) matters for R2 too.
- **The delay is mostly the window.**
  - A reaction is scored in a 0.975 s window whose trigger is its centre, and the PC sends 0.5 s chunks.
  - Scream and gasp open 0.7–0.9 s after onset (medians).
  - Laughter needs a window above .4, or .25 with the face server, and takes 1.4–2.5 s.

Possible next steps. None was done here, and each needs a team decision. Tune on development data, not on this set: FSD50K's dev split has disjoint uploaders and could serve as it.

1. **Gasp rule:** require gasp to beat YAMNet's Breathing, Sigh and Sneeze scores in the same window. The spec's `best_of` rule does the same across the three reactions.
2. **Laughter level:** a lower level (.1–.25) would find more laughter, if the false alarms on development data stay as low as here.
3. **Another model:** the spec still names PANNs (32 kHz), or YAMNet could be fine-tuned on laughter that is mixed with speech.
4. **Audience recordings:** recordings made in front of the app would replace the uploaded clips.

## 6. EM-11 acceptance criteria

| Criterion | Status |
|---|---|
| Three reaction types from a versioned model, window and thresholds | Built. Each event carries `source.model` (`google/yamnet/1`) and `source.config` (`detector-v1` or the fusion string). The window and hop are fixed in `audio.py`. The spec also asks for a model checksum, and events do not carry one. |
| Camera off or no face: audio only; one strong face counts, several weak faces do not add up | Camera off: this evaluation, and EM-26's scripted cases (PR #45). No face found: EM-26's `no-face-*` cases. Several faces: the face worker scores up to 4 faces and keeps the largest smile (`vision.py`, `max(smiles)`), so one strong face counts and weak faces never add up; no test feeds it several real faces. EM-26's cases were not rerun here. |
| Per-class errors and detection delay on separate evaluation input | This document. The accuracy target is still to be set. |

## 7. Reproduce

```sh
# labels: FSD50K.ground_truth.zip from https://zenodo.org/records/4060432, unzipped into CACHE/labels/
python server/tools/em11_eval.py fetch --cache ~/.cache/encore/fsd50k-eval   # 280 clips, ~147 MB
TFHUB_CACHE_DIR=~/.cache/encore/tfhub python server/tools/em11_eval.py run \
    --cache ~/.cache/encore/fsd50k-eval --json results.json
```

`fetch` needs ffmpeg. `run` needs `server/requirements-yamnet.txt`. The same labels file and seed pick the same clips (`docs/em-11-clips.json`). Results are deterministic apart from the measured processing time, which is a few milliseconds.
