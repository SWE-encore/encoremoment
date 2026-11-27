# EM-11: YAMNet against AST on a larger FSD50K evaluation

Status (2026-11-27): the tooling and the two Kaggle notebooks are ready and tested without models
(`server/tests/test_em11_compare.py`). The models themselves have not run yet: the first Kaggle smoke
run is the first time the AST code meets torch. Section 6 will hold the results.

`docs/em-11-evaluation.md` measured the server's detector (YAMNet, `detector-v3`) on 280 FSD50K eval
clips. Laughter recall is weak there (13 of 40). This comparison asks three questions on a much
larger set of clips:

1. Does a stronger audio model, AST (Audio Spectrogram Transformer), rank laughter, scream and gasp
   better than YAMNet?
2. At the false-alarm budget the detector already uses, how many more reactions would it find, and
   how much later or sooner?
3. How well could either model detect three candidate reactions: cheering, applause and shout/yell?

This is an evaluation only. The server, the detector and the contract still know laughter, scream
and gasp, and YAMNet stays the production model for the demo (team decision, 2026-11-26). The
candidate reactions are measured, not added.

## 1. What is compared

| Variant | What runs | Where |
|---|---|---|
| `yamnet` | The server's own model on each 0.975 s window (`encore_server.models.YamnetModel`) | CPU, as on the server |
| `ast-pad10s` | AST on each window, its features padded to the 10.24 s input AST was trained on | T4 GPU |
| `ast-cut1s` | AST on each window with 100 feature frames, position embeddings cut to the middle 9 time patches (the original AST code's way to take short input). Not retrained. | T4 GPU |
| `ast-clip` | AST on the whole clip in 10.24 s pieces, highest score of the pieces. No windows, so no server path: ranking only | T4 GPU |

AST is `MIT/ast-finetuned-audioset-10-10-0.4593` from Hugging Face transformers, scores the sigmoid
of its 527 AudioSet outputs. YAMNet's scores are its own 521 class scores. Both use AudioSet's class
names, so each reaction is the highest score among the same classes for both models:

| Reaction | AudioSet classes | FSD50K labels that make a clip a positive |
|---|---|---|
| laughter | Laughter, Baby laughter, Giggle, Snicker, Belly laugh, Chuckle/chortle | Laughter, Giggle, Chuckle_and_chortle |
| scream | Screaming | Screaming |
| gasp | Gasp | Gasp |
| cheering | Cheering | Cheering |
| applause | Applause, Clapping | Applause, Clapping |
| shout | Shout, Bellow, Whoop, Yell, Children shouting | Shout, Yell |

The first three are the server's classes (`YAMNET_CLASSES`). The others were fixed before any result
was seen (`em11_compare.CLASSES`).

## 2. Data

- **Source:** FSD50K ([Zenodo 4060432](https://zenodo.org/records/4060432)). The eval split is measured;
  the dev split is only used to choose levels. FSD50K keeps uploaders apart between the two.
- **Clips:** `docs/em-11-compare-clips.json`, made by `em11_compare.py select` from the labels
  (deterministic, seed 11). A clip joins the first group it fits, in the order below; clips carrying
  two reactions of different groups (say laughter and screaming) join none. The 8 ESC-50 clips EM-26
  ran stay out.

| Group | Labels (any of) | Not if labeled | Eval clips | Dev clips |
|---|---|---|---|---|
| laughter | Laughter, Giggle, Chuckle_and_chortle | Screaming, Gasp, Music | 236 | 300 |
| scream | Screaming | laughter labels, Gasp, Music | 113 | 252 |
| gasp | Gasp | laughter labels, Screaming, Music | 43 | 57 |
| cheering | Cheering | laughter labels, Screaming, Gasp, Music | 85 | 214 |
| applause | Applause, Clapping | the above, Cheering | 100 | 300 |
| shout | Shout, Yell | the above, Cheering, Applause, Clapping | 102 | 167 |
| speech | Speech | any reaction label, Music | 630 | 300 |
| music | Music | any reaction label, Human_voice | 300 (a fixed sample) | 300 |
| breath_cough | Breathing, Cough, Sigh, Sneeze | any reaction label, Music | 294 | 300 |
| everyday | every other clip with no reaction label | | 8 281 | 600 (a sample) |

  Eval holds every eligible clip: 1 903 in the groups and 8 281 everyday sounds (about 28 hours of
  eval audio in all). Dev holds up to 300 clips per group. The 280 clips of `docs/em-11-clips.json`
  are all inside the eval selection.
- **Streams:** as in `em11_eval.py`. Each clip alone, with 2 s of silence before and 3 s after
  (*clean*). Each eval reaction clip also over speech and over music, at +10 dB and 0 dB (679 clips,
  2 716 streams).
- **Where the audio comes from:** the Kaggle dataset `yousirui1/fsd50k` (version 1, 21.39 GB,
  licence shown as "Unknown", no source given). Because its origin is not stated, it is used only if
  its audio matches. `prepare` decodes clips from the copy and compares the decoded samples (SHA-256
  of 16-bit PCM at the file's own rate) with:
  1. the 695 clips EM-11 already downloaded from the Hugging Face mirror
     (`docs/em-11-compare-reference.json`); and
  2. 30 other clips fetched from the mirror `Fhrozen/FSD50k` at a pinned revision.

  If any clip differs, or none could be checked, the copy is not used and every clip comes from the
  mirror. A clip the copy lacks also comes from the mirror. `verify-report.md` records the outcome,
  every source and format, and whether the 16 kHz files equal the reference ones.
- **Licences:** each clip keeps its Freesound licence (CC0, CC-BY, CC-BY-NC or Sampling+). No audio is
  committed; the prepared clips live only in the Kaggle notebook's private output.

## 3. Method

The score stage runs each model once per distinct analysis window: exactly the windows
`ServerSession` makes from the stream (0.975 s every 0.48 s; a test checks they are the same), and
keeps the six reaction scores in a *bank*. The analysis then answers the questions without running
a model again.

1. **Ranking without thresholds.** A clip's score for a reaction is its highest window score while
   it plays. Average precision (AP) and ROC AUC per reaction on the eval clips, once on the curated
   groups and once with the everyday clips added as negatives. For each AST variant, the 95 %
   interval of its AP difference from YAMNet (2 000 paired bootstrap resamples, positives and
   negatives resampled apart, seed 11).
2. **Levels chosen on dev clips.** `em11_eval.py`'s rule, per model and reaction: the level (from
   .01 to .9) that finds the most dev clips of the reaction while no other dev group, everyday
   sounds included, opens it on more than 10 % of its clips. A clip that carries the reaction's own
   labels (a cheering clip that is also applause) does not count against it. Ties: fewest false
   opens, then the highest level. Scores are rounded to 3 decimals before the comparison, as the
   detector does.
3. **Eval clips through ServerSession.** Every eval stream is replayed through the real
   `ServerSession` with the bank as the model. Inside the analysis processes only, the detector
   tracks all six reactions. Four configurations:
   - `yamnet@v3`: the server today (detector-v3 levels; YAMNet's dev levels for the candidates);
   - `yamnet@dev`, `ast-pad10s@dev`, `ast-cut1s@dev`: each model at its own dev levels.

   For each: clips found (with a Wilson 95 % interval), found over playback, clips that opened the
   wrong reaction, false events per minute of speech, music and breathing, events per hour of
   everyday sound, events while only playback sounds, and the delay from the sound's onset to OPEN.
   The delay includes the model's measured time per window (YAMNet on 2 CPU threads, AST on a T4).
   The exact McNemar test compares each configuration with `yamnet@dev` on the same clips.
4. **Time per window.** One window at a time after a warm-up: YAMNet on 2 CPU threads (the cloud
   server) and on all threads; AST on a T4 (full and half precision) and on 2 CPU threads.
5. **Precision.** Everything is scored in full precision except `ast-pad10s` on the everyday clips,
   which uses half precision on the T4 to save quota. 2 000 windows are scored both ways; the report
   gives the largest difference and how many decisions would flip at the dev levels.
6. **Sanity check.** `em11_eval.py` (detector-v3) runs on the original 280 clips from this run's
   16 kHz files. Every row's opened reactions must equal the rows the laptop produced
   (`docs/em-11-compare-sanity.json`).

**Limits.** FSD50K clips are uploaded recordings, not an audience in a living room, and the labels are
per clip, so a delay is an upper bound. AST's delay assumes a GPU, which the cloud server does not
have (section 4 of the report gives its CPU time too). `ast-cut1s` uses AST outside the input length
it was trained on. The candidates' levels are chosen the same way as the product reactions', but
they are measurements, not product settings.

## 4. Pins

| What | Pin |
|---|---|
| FSD50K labels | `FSD50K.ground_truth.zip`, Zenodo 4060432, md5 `ca27382c195e37d2269c4c866dd73485` |
| Kaggle copy | `yousirui1/fsd50k`, version 1 (checked on every run, section 2) |
| Mirror | `Fhrozen/FSD50k`, revision `ccf1acaff12f3f4a4c10052dddafbb6d22152c9b` |
| YAMNet | `https://tfhub.dev/google/yamnet/1`, directory sha256 `c59c5744e1b92942f67d9c0c8c53cb69757d002007870c394e5d126af98c0b59` |
| AST | `MIT/ast-finetuned-audioset-10-10-0.4593`, revision `f826b80d28226b62986cc218e5cec390b1096902`, `model.safetensors` sha256 `ae0c1e2ad4e1381d851fa9bf298ba13ebc9c5a914cdee2dbe427a6583869924d` (346 404 948 bytes) |
| Python | 3.12, in fresh `uv` environments (uv 0.9.5) |
| YAMNet packages | `server/requirements-yamnet.txt` (TensorFlow 2.18.1, the server's) |
| AST packages | `server/tools/kaggle/requirements-ast.txt` (torch 2.5.1, transformers 4.46.3) |

Every run saves each environment's full package list (`results/env-*.txt`), the code commit, the GPU,
CPU and driver, and each stage's times next to the results.

## 5. Running it on Kaggle

Two notebooks in `server/tools/kaggle/`. Notebook 1 prepares the clips on a CPU and keeps them as a
versioned output. Notebook 2 runs the models on two T4 GPUs and writes the report. Splitting them
keeps the hour of clip preparation off the GPU quota, and lets notebook 2 be re-run on the same
prepared clips.

**Before the first run**

- A Kaggle account with a verified phone number (Kaggle requires it for GPUs and internet access).
- This branch pushed to GitHub: the notebooks clone the public repository.

**Notebook 1: `em11-prepare.ipynb`** (CPU, no GPU quota)

1. Kaggle → Create → New Notebook → File → Import Notebook → upload `server/tools/kaggle/em11-prepare.ipynb`.
2. Settings: Accelerator *None*; Internet *On*; Environment *Pin to original environment*.
3. Add Input → Datasets → `yousirui1/fsd50k` (version 1).
4. Leave `SMOKE = True`. Save Version → *Save & Run All (Commit)*. It takes a few minutes.
5. Open the version's output: `verify-report.md` should say **Copy used: yes** and every reference clip
   should match. If the copy is not used, the clips come from the mirror instead (slower, about
   11 GB of downloads in the full run) and the report says why.
6. Set `SMOKE = False` and save a new version (about 30–60 minutes, more if the mirror is used).

**Notebook 2: `em11-compare.ipynb`** (GPU T4 ×2)

1. Import `server/tools/kaggle/em11-compare.ipynb` the same way.
2. Settings: Accelerator *GPU T4 x2*; Internet *On*; Environment *Pin to original environment*.
3. Add Input → Your Work → the **smoke** version of notebook 1's output. Do not attach FSD50K here.
4. Save Version → *Save & Run All (Commit)*. The smoke run takes about 20–30 minutes, most of it
   installing packages. Its last cell prints each bank's windows per second: a full run scores about
   400 000 distinct windows per window model, so this gives the full run's length.
5. Switch the input to notebook 1's **full** version and save a new version. The estimate is 2–4 GPU
   hours; the free quota is about 30 GPU hours a week, and Kaggle shows what is left. A session may
   run for up to 12 hours.
6. From the version's output, download `results/report.md`, `results/report.json` and
   `results/verify-report.md`.

If a stage fails, the notebook stops, and `logs/NAME.log` in the output says why. The banks of every
finished stage are kept in `banks/`, so the analysis can be re-run on a CPU from them.

**One stage at a time, anywhere** (for reference; each takes `--help`):

```bash
python server/tools/em11_compare.py prepare --source /path/to/fsd50k --out DATA --limit 3
python server/tools/em11_compare.py score --data DATA --model yamnet --out banks/yamnet.npz
python server/tools/em11_compare.py score --data DATA --model ast-cut1s --weights AST_DIR --device cuda:0 --shard 0/2 --out banks/cut1s-0.npz
python server/tools/em11_compare.py timing --data DATA --model yamnet --threads 2 --out timing/yamnet-cpu2.json
python server/tools/em11_compare.py sanity --data DATA --work /tmp/sanity --out results/sanity.json
python server/tools/em11_compare.py analyze --data DATA --bank yamnet=banks/yamnet.npz --bank ast-cut1s=banks/cut1s-0.npz,banks/cut1s-1.npz --timing timing/yamnet-cpu2.json --out results
```

## 6. Results

Pending the Kaggle run. This section will hold the tables of `report.md` and what they mean for R2
and for the candidate reactions.
