# EM-11: reaction recognition on labeled clips it was never tuned on (R2)

Dates: 2026-11-25 (sections 1–4, `detector-v1`) and 2026-11-26 (section 5, tuning and `detector-v2`; section 6, `detector-v3`). Machine: laptop, AMD Ryzen 7 8845HS, Ubuntu 24.04.3, CPU only. Server code: `main` at 31f40ef. Sections 5 and 6 each change one level in `server/encore_server/detector.py`.

R2's check case (`documents/submission-b/sections/specifications.tex`) asks for labeled laughter, scream, gasp, speech, soundtrack-only and mixed recordings, with per-class errors and the measured detection delay. It also says thresholds are calibrated on development recordings and evaluated on separate ones. Sections 1–4 are that separate evaluation, run with the thresholds as they were (`detector-v1`). Section 5 chooses new levels on FSD50K's development split and measures them once on the same evaluation clips. Nothing was tuned on the evaluation clips.

**Short answer:**
- With `detector-v1`, YAMNet finds about a third of the laughter clips, about half of the screams and half of the gasps. It rarely fires on speech or music, but it opens a gasp on 15 of 40 breathing, sigh and sneeze clips. A found reaction opens 0.7–2.5 s after the sound starts (medians).
- Tuning on development clips lowers the laughter level from .4 to .25 (`detector-v2`). On the evaluation clips it finds 13 of 40 laughs instead of 11 and opens sooner (median 1.43 s instead of 2.50 s). It adds two false laughter events on the 160 clean clips with no reaction.
- A rule to stop breathing from opening gasp did not work on development data, so gasp is unchanged.
- The team then lowered scream from .4 to .2 (`detector-v3`), the level the development clips pointed to. On the evaluation clips it finds 26 of 40 screams instead of 18. It also opens scream on 2 of 40 speech clips and 5 of 40 shouts.
- Section 8 sets the accuracy target and records the team's decisions on gasp, the model and the evidence.

## 1. Data

- **Source:** the FSD50K eval split (Fonseca et al., IEEE/ACM TASLP 2022, [Zenodo 4060432](https://zenodo.org/records/4060432)). The clips are Freesound sounds, and their uploaders do not appear in FSD50K's dev split. They are not from AudioSet (YouTube), which YAMNet was trained on.
- **Download:** the labels (`FSD50K.ground_truth.zip`, 334.7 kB) came from Zenodo. The 280 clips (146.6 MB) were fetched one WAV at a time from the Hugging Face mirror `Fhrozen/FSD50k`. They are converted with `ffmpeg -ac 1 -ar 16000 -sample_fmt s16`.
- **Not committed:** the clips are not in the repository. Each clip carries its own Freesound license (CC0, CC-BY, CC-BY-NC or Sampling+). `docs/em-11-clips.json` lists the Freesound ids.
- **Groups:** a clip joins the first group whose labels it fits. 40 clips per group were drawn in a fixed shuffled order (seed 11). Clips over 2 MB (about 22 s at 44.1 kHz) were skipped, 22 in all, which favours shorter clips.
- **Excluded:** the 8 ESC-50 clips EM-26 already ran.
- **Development clips (section 5 only):** the FSD50K dev split, with the same groups and seed and 60 clips per group. Gasp has 55, every eligible gasp clip under 2 MB. That makes 415 clips (144.6 MB) from the same mirror (`clips/dev/`). No clip is in both sets, and FSD50K keeps uploaders apart between dev and eval. The ids are in `docs/em-11-dev-clips.json`. These clips are shorter than the evaluation ones: median 2.8 s for laughter and 1.1 s for gasp.

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

- **Model:** YAMNet `google/yamnet/1` from the TF-Hub cache (weights `sha256:c59c5744e1b92942`, the name events now carry), with TensorFlow 2.18.1, tensorflow-hub 0.16.1 and numpy 1.26.4. Each reaction is the highest score among its YAMNet classes (`server/encore_server/models.py`): laughter is Laughter, Baby laughter, Giggle, Snicker, Belly laugh and Chuckle/chortle; scream is Screaming; gasp is Gasp.
- **Two server set-ups:**
  - **audio-only:** no face model; `DetectorConfig` `detector-v1` opens at laughter .4, scream .4, gasp .3. `detector-v2` (section 5) changes laughter to .25, and `detector-v3` (section 6) changes scream to .2.
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

## 3. Results with detector-v1

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

## 5. Tuning on development clips: detector-v2

`em11_eval.py tune` plays each development clip alone through `ServerSession` and keeps all 521 YAMNet class scores of every window that touches the clip. A clip opens a reaction when any of those windows passes. On a clean clip, that is exactly when the detector opens.

**Criterion, set before the scan:** find as many clips of the reaction as possible, while no other group opens it on more than 10% of its clips. Ties go to the simpler rule, then to the higher level. The 10% limit is a choice made for this scan, not a number from the spec. The tool prints every level, so the team can pick another point.

### Laughter: .4 → .25

| Level | Laughter found (of 60) | Most often opened in another group |
|---|---|---|
| .1 | 35 (58%) | gasp 10 of 55 (18%), scream 10 (17%) |
| .15 | 33 (55%) | gasp 6 of 55 (11%) |
| .2 | 31 (52%) | speech 4 (7%), gasp 4 of 55 (7%) |
| **.25** | **31 (52%, 95% CI 39–64%)** | speech 4 (7%) |
| .3 | 29 (48%) | speech 4 (7%) |
| .4 (v1) | 24 (40%, 29–53%) | speech 2, scream 2 (3%) |
| .5 | 22 (37%) | speech 2 (3%) |

- .2 and .25 find the same clips, and the criterion takes the higher level, .25.
- Music never opens laughter at any level.
- .25 is also the level at which the face-server set-up opens laughter on sound alone (`TabRules.strong_audio`). Both set-ups now agree when the camera is off.

### Gasp: unchanged

The plan was a gasp rule: a window counts as a gasp only if its gasp score is at least a set multiple (the ratio) of its highest rival score, so breathing could no longer open gasp. The scan tried three rival sets: Breathing; Breathing, Sigh and Sneeze; and all respiratory classes. Each was tried at ratios .5, 1, 1.5 and 2, at every level.

| At level .3 | Gasps found (of 55) | Breathing, sigh, sneeze, cough clips opened (of 60) |
|---|---|---|
| no rule (v1) | 29 | 23 |
| gasp ≥ 1.5 × Breathing | 27 | 17 |
| gasp ≥ 2 × Breathing | 22 | 11 |
| gasp ≥ Breathing, Sigh and Sneeze | 13 | 11 |
| gasp ≥ 1.5 × Breathing, Sigh and Sneeze | 3 | 2 |

- No setting keeps every other group at or under 10% and still finds more than 4 of the 55 gasps.
- The planned rule (gasp must beat Breathing, Sigh and Sneeze) halves both sides: 13 gasps instead of 29, and 11 breathing clips instead of 23.
- Breathing alone barely helps. At best, gasp ≥ 1.5 × Breathing at .25 finds as many gasps as v1 (29) and opens on 19 breathing clips instead of 23.
- In AudioSet, which YAMNet learned from, Gasp is a kind of Breathing. On these clips, Sigh and Sneeze also score high on real gasps.
- YAMNet's scores do not tell the two apart, so this PR leaves gasp at .3 with no rule.

### Scream: not changed here

Development clips point to .2. At that level 49 of 60 screams are found (82%, 70–89%), against 39 (65%, 52–76%) at .4. At .2, scream opens on 5 shout/yell clips (8%), 4 laughter clips and 4 gasp clips, and never on speech or music. PR #51 did not change scream; .2 was left as a candidate for the team, which adopted it on 2026-11-26 (section 6).

### detector-v2 on the evaluation clips (measured once)

The run used the same 280 clips and 1 520 runs as section 3. Only the audio-only set-up changes. The face-server results are identical, because its laughter level was already .25.

| Audio-only | detector-v1 | detector-v2 |
|---|---|---|
| Laughter found, clean | 11/40 (28%, 16–43%) | 13/40 (32%, 20–48%) |
| Delay median / p90 | 2.50 / 3.77 s | 1.43 / 2.79 s |
| Laughter found over speech, +10 / 0 dB | 10 / 6 of 40 | 14 / 9 of 40 |
| Laughter found over music, +10 / 0 dB | 11 / 10 of 40 | 14 / 11 of 40 |
| Laughter opened on clean clips with no reaction (160) | 1 (breath_cough) | 3 (breath_cough 2, music 1) |
| Laughter opened on scream or gasp clips over playback (320 runs) | 6 | 11 |
| Events while only the playback sounded (2 400 s) | 4 | 5 |

- No clip found by detector-v1 is missed by detector-v2. Scream and gasp results are unchanged.
- Most of the delay gain comes from the two new clips, which open quickly. On the 11 clips both versions find, the median goes from 2.50 s to 2.22 s, and 5 of them open 0.5–1.5 s sooner.
- The gain is smaller than development data suggested: from 40% to 52% on dev, but only from 28% to 32% on eval. Most missed evaluation laughs stay below .1 (section 4), and no level helps there without opening on screams and gasps.

## 6. detector-v3: scream .2 (team decision, measured once)

On 2026-11-26 the team adopted the scream level the development clips pointed to (section 5): .2 instead of .4. `detector-v3` is `detector-v2` with that one change. It was measured once on the same 280 evaluation clips and 1 520 runs. Laughter and gasp results are identical to `detector-v2`, and so are the face-server results for scream, because scream never used the face.

| Audio-only | detector-v2 (scream .4) | detector-v3 (scream .2) |
|---|---|---|
| Screams found, clean | 18/40 (45%, 31–60%) | 26/40 (65%, 50–78%) |
| Delay median / p90 | 0.90 / 1.35 s | 0.78 / 1.49 s |
| Screams found over speech, +10 / 0 dB | 8 / 1 of 40 | 15 / 8 of 40 |
| Screams found over music, +10 / 0 dB | 5 / 5 of 40 | 13 / 8 of 40 |
| Scream opened on clean clips of: | | |
| — speech | 0/40 | 2/40 (peaks .213 and .235) |
| — shout or yell | 3/40 | 5/40 |
| — gasp | 1/40 | 6/40 |
| — laughter, music, breathing | 0 | 0 |
| Events while only the playback sounded (2 400 s, all reactions) | 5 | 7 |

- Scream is now the best-found reaction on clean clips. Over speech at 0 dB, `detector-v2` found 1 scream of 40 and `detector-v3` finds 8.
- The cost is scream opening on other loud voices: a few more shouts and gasps, and, unlike on the development clips, 2 speech clips. Both peak just above .2.
- Clean speech now gives 4 events in 290 s of speech (0.8 per minute; `detector-v2`: 2), and clean music 1 in 234 s. Playback alone gives 7 in 2 400 s (0.2 per minute).

## 7. What this means

- **R2's evaluation now exists, and it is weak.** About a third of laughter clips are found. Laughter is the class the product depends on most.
- **detector-v2 helps a little.** Lowering laughter to .25 finds a few more laughs and opens them sooner, for a few more false laughter events. It does not change the picture: about a third of evaluation laughs are found.
- **Single, clear laughter works.** 10–11 of 19 such clips are found, and found clips peak far above the threshold. Crowd laughter, laughter inside speech and short chuckles are mostly missed, because YAMNet calls them speech. A living room with a few viewers is closer to the clear-laughter case than to a crowd, but no audience recording has been checked.
- **False alarms are rare for speech and music.** Speech and music sounding alone opened 0–2 events in 400 s of playback per condition. The one exception is gasp on breathing and sneezes. A viewer breathing heavily near the microphone could open gasp events, and with YAMNet no rule fixes that (section 5).
- **Playback hurts recall.** At 0 dB, `detector-v1` and `detector-v2` almost never found screams over speech (1 of 40); `detector-v3` finds 8. Laughter and gasp over playback at 0 dB stay at 8–28%. The echo canceller's job (R1) matters for R2 too.
- **The delay is mostly the window.**
  - A reaction is scored in a 0.975 s window whose trigger is its centre, and the PC sends 0.5 s chunks.
  - Scream and gasp open 0.7–0.8 s after onset (medians, `detector-v3`).
  - Laughter needs a window above .25 and takes about 1.4 s (`detector-v1`: .4 and 2.5 s).

## 8. Accuracy target and team decisions (2026-11-26)

**Accuracy target.** EM-11's first criterion leaves the target to be set after the evaluation. The team set it at the level the demo meets today, measured audio-only on the clean evaluation clips. It is a floor that a later change must not fall below. It is not a claim that recognition is good: **laughter recall is weak**, about a third of the laughs. Crowd laughter, laughter inside speech and short chuckles are mostly missed (section 4), and EM-26's live run missed one soft chuckle as well.

| Target | detector-v3 | Met |
|---|---|---|
| Laughter found ≥ 30% | 13/40 (32%, 20–48%) | yes, barely |
| Scream found ≥ 45% | 26/40 (65%, 50–78%) | yes |
| Gasp found ≥ 50% | 20/40 (50%, 35–65%) | yes, at the line |
| ≤ 1 false event per minute of speech or music | clean speech 0.8, clean music 0.3, playback alone 0.2 per minute | yes |
| Median delay ≤ 1.5 s | laughter 1.43 s, scream 0.78 s, gasp 0.70 s | yes |

The confidence intervals are wide: 40 clips per class cannot tell 32% from 45%. Tune any later change on development clips (`em11_eval.py tune`), never on these evaluation clips.

**Decisions:**

1. **Scream level .2** (`detector-v3`, section 6).
2. **Gasp stays at .3, with a known limit.** YAMNet scores heavy breathing, sighs and sneezes like gasps (15 of 40 breathing and cough clips open gasp), and the rule tried in section 5 did not separate them. A viewer breathing hard near the microphone can open a gasp event.
3. **The model stays YAMNet.** The submission's environment and specification sections name PANNs `Cnn14_DecisionLevelMax` at 32 kHz on a GPU worker, while the introduction allows YAMNet or PANNs. YAMNet runs on the CPU in a few milliseconds per window (medians of 4–8 ms in these runs), its weights are versioned in every event (`source.model`), and the whole pipeline, evaluation and EM-26's checks are built on it. Switching before the Dec 3 deadline would need PyTorch, a GPU worker and a new evaluation. The final report states this change from the specification.
4. **R2's evidence** is this evaluation and EM-26's live runs (`docs/em-26-trigger-check.md`, sections 6–7). No recordings of a real audience were made, and that is a known limit: the clips are uploaded sounds, not viewers in a living room with a microphone and an echo canceller.

## 9. EM-11 acceptance criteria

| Criterion | Status |
|---|---|
| Three reaction types from a versioned model, window and thresholds | Built. Each event carries `source.model` with the weights' SHA-256 prefix (`google/yamnet/1@sha256:c59c5744e1b92942`, PR #52) and `source.config` (`detector-v3`, or the fusion string). The window and hop are fixed in `audio.py`. |
| Camera off or no face: audio only; one strong face counts, several weak faces do not add up | Camera off: this evaluation, and EM-26's scripted cases (PR #45). No face found: EM-26's `no-face-*` cases. Several faces: the face worker scores up to 4 faces and keeps the largest smile (`vision.py`, `max(smiles)`), so one strong face counts and weak faces never add up; no test feeds it several real faces. EM-26's cases were not rerun here. |
| Per-class errors and detection delay on separate evaluation input | This document: levels chosen on development clips (section 5) or by the team (section 6), errors and delay on separate evaluation clips (sections 3 and 6). The accuracy target is set in section 8, and `detector-v3` meets it. |

## 10. Reproduce

```sh
# labels: FSD50K.ground_truth.zip from https://zenodo.org/records/4060432, unzipped into CACHE/labels/
python server/tools/em11_eval.py fetch --cache ~/.cache/encore/fsd50k-eval   # 280 clips, ~147 MB
TFHUB_CACHE_DIR=~/.cache/encore/tfhub python server/tools/em11_eval.py run \
    --cache ~/.cache/encore/fsd50k-eval --json results.json      # --detector detector-v1 / detector-v2 for sections 3 / 5

# section 5: development clips, then the scan
python server/tools/em11_eval.py fetch --split dev --per-group 60 --cache ~/.cache/encore/fsd50k-dev \
    --labels ~/.cache/encore/fsd50k-eval/labels/FSD50K.ground_truth    # 415 clips, ~145 MB
TFHUB_CACHE_DIR=~/.cache/encore/tfhub python server/tools/em11_eval.py tune \
    --cache ~/.cache/encore/fsd50k-dev --json tune.json
```

`fetch` needs ffmpeg. `run` and `tune` need `server/requirements-yamnet.txt`. The same labels file and seed pick the same clips (`docs/em-11-clips.json`, `docs/em-11-dev-clips.json`). Results are deterministic apart from the measured processing time, which is a few milliseconds. The scan scored 4 107 windows of dev clips.
