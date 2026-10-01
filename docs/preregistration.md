# Pre-registration (fill in before session 1)

- **Date registered:**
- **Registered with:** _(supervisor name; e-mail timestamp or OSF link)_
- **Protocol fingerprint (`policy_fp`):**
- **Git commit:**

## Hypotheses

H1. During the treatment phase, the share of MH-themed items (`adjacent`, `mh_distress`,
`harmful`) in treatment accounts' feeds rises relative to their yoked controls, compared with
baseline (pair-level DiD > 0).

H2. The share of `harmful` items rises in treatment feeds relative to controls, although no
account ever lingers on harmful items (harmful DiD > 0).

H3 (exploratory). The difference persists into washout (persistence > 0).

## Design

- Platform: YouTube Shorts, desktop web, `hl=en`, accounts and IP in Pakistan.
- Units: _N_ pairs of fresh accounts, paired by persona, with treatment randomized within
  each pair (seed `study.seed`).
- Protocol: `protocol.yaml` (baseline ×2, seed ×1, treatment ×6, washout ×2; at least
  `min_gap_hours` between slots), with `items_per_session` organic items per session.
- Treatment rule: Gemini model and prompt as fingerprinted, deadline 2.5 s, with multilingual
  keyword fallback. Control: yoked.
- Seed lists: `seeds/treatment.txt`, `seeds/control.txt` (fingerprinted).

## Outcome and analysis

- Outcome label: `label_items.py` with codebook v1 (`docs/codebook.md`), model:
  _(labeler.model)_.
- Validation: about 150 items labeled independently by two people, then adjudicated. Report
  Cohen's κ and the outcome labeler's precision and recall on the gold set.
- Primary estimand: pair-level DiD of the MH-themed share (treatment − control, treatment
  phase minus baseline), averaged over pairs.
- Secondary: harmful-share DiD; washout persistence; first treatment session where the
  difference exceeds the largest baseline difference by `analysis.drift_margin`.
- Inference: exact sign-flip test across pairs. Results are reported as descriptive when
  the number of pairs is below 5.
- Exclusions: ads; the seed session; sessions with status ≠ `ok` (reported, not imputed);
  sessions flagged `gemini_rate<0.9` (reported as a sensitivity analysis, not dropped).

## Deviations

Every `--force` run is recorded with its reason in `data/lab_log.jsonl`. Summarize them here
after collection.
