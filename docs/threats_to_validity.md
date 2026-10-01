# Threats to validity

What the design controls, what it only measures, and what remains. Numbers in brackets
refer to the original 27-point review of the first harness.

## Internal validity

| Threat | How it is handled | What remains |
|---|---|---|
| Treatment never triggers on a multilingual feed [1, 10] | Live Gemini classifier (title, tags, description, thumbnail) with multilingual keyword fallback; matched seed session gets past the cold start | Trigger accuracy is measured against the human gold set (`validate_labels.py`), not assumed |
| Dose confounded with outcome [2] | Yoked control: identical number and position of long dwells, identical skip lengths (shared draws), same start time | Long-dwell *length* follows each item's own duration; ads differ. The dose table in the report shows the residual gap |
| Asymmetric safety rule [3] | Exclusion applies to every arm and phase | – |
| Ads logged as feed items [7] | DOM ad markers + ad-channel pattern; ads are skipped in both arms, excluded from the yoke index and from the analysis | DOM markers can break when YouTube changes its UI; checked in the pilot |
| Dwell depends on metadata source or API latency [11, 13] | Long dwell uses the *player's* duration; skips always outlast the 2.5 s decision deadline; dwell is timed from playback start; every fallback is logged | – |
| "Playing" not verified [14] | The active player is polled every 0.5 s: `watched_s`, `loops`, `playing_frac`, `muted` per item | – |
| Wrong or signed-out account [16] | DATASYNC_ID recorded at sign-in and checked every session; history-off and bot-challenge checks | – |
| Missed or repeated sessions [19, 21] | Protocol order and spacing enforced; re-runs are whole pairs, logged with a reason, superseded rows kept aside; Ctrl-C still logs | A re-run account has seen extra items; `attempt` lets the analysis flag it |
| Procedure drift during collection [12] | Protocol fingerprint over config, prompts, keyword terms, seeds, code, and lock file; real runs refuse on mismatch | – |

## Construct validity

- **Outcome is a classifier's judgment [8].** It uses a separate prompt and richer inputs
  than the trigger. It is validated against two human labelers (Cohen's κ, then an
  adjudicated gold set), and precision and recall are reported in the report. A keyword
  measure is shown as a sensitivity check.
- **Partial circularity.** Gemini both triggers (treatment) and measures. Mitigations: a
  different prompt and taxonomy; the human gold set; and the harmful class, which the bot
  never lingers on, so drift toward it cannot come from the bot's own selection.
- **Exclusion narrows the question [4].** Because no account lingers on harmful content,
  the study measures drift *from adjacent content toward* harmful content. It does not
  measure how fast the feed amplifies harmful content that is engaged with directly.
- **Watch time is the only signal.** The study says nothing about likes, follows or search.

## External validity

- **Desktop web Shorts, not the mobile app [20].** Ranking signals may differ, though the
  same recommender family serves both.
- **YouTube only.** TikTok and Instagram Reels are not covered yet.
- **Personas.** Adult accounts with identical declared age and gender, in Pakistan.
  Platforms apply extra protections to teen accounts, so results do not carry over to minors.
- **Fresh accounts.** These are not representative of long-lived real accounts.

## Statistical conclusion validity

- **2–3 pairs [9].** The account (pair) is the unit of analysis; items within a session are
  not independent. With n pairs the exact sign-flip test cannot go below p = 2/2ⁿ (0.25 for 3
  pairs), so MVP results are **descriptive**.
- **Power for scale-up.** For a confirmatory study, plan the number of pairs from the
  between-pair SD of the DiD observed in the MVP. As a rough guide, detecting a DiD of
  about 1 SD at α = 0.05 with 80% power needs around 10 pairs (paired t-test).

## Remaining confounds

- **Shared IP and device [5].** All accounts share one IP and one machine (fonts, GPU,
  screen), so the platform may link them. Signals leaking between accounts push treatment
  and control together, which **biases toward no difference** (a conservative direction).
  Distinct residential IPs would remove this at scale.
- **Bot detection [17].** Automated playback may be detected and down-weighted. No evasion
  is added beyond hiding the automation flag. The `interstitial` and `blocked` statuses
  show challenges, but silent down-weighting can't be observed.
- **Platform experiments.** YouTube runs its own A/B tests on accounts. Randomizing pairs
  spreads this out; it is not controlled.
- **Front-end changes.** All selectors live in `audit/dom.py`, and the session log's
  `active_video_rate` reveals breakage.
- **Chrome updates [25].** The Chrome version is logged per session. Hold the package during
  collection.
