# Short-video recommendation audit (YouTube Shorts MVP)

An external black-box audit of a short-video feed using sock-puppet accounts. Each account
sends one controlled signal, watch time, and we measure how fast and how strongly the feed
shifts toward sadness and mental-health content, and whether it escalates toward harmful
content the accounts never lingered on.

## Design

- **Yoked pairs.** Fresh accounts are randomly paired; one member of each pair is treatment,
  the other control. Partners run at the same time, from the same machine, with identical
  random draws.
- **Phases** (`protocol.yaml`, ~4 days at 3 slots/day):
  baseline ×2 → seed ×1 → treatment ×6 → washout ×2.
  - *Baseline and washout:* both partners follow the same content-blind dwell schedule.
  - *Seed:* treatment watches a frozen list of ~10 sad/lonely Shorts; control watches a
    duration-matched neutral list. Opened by direct URL; no searching.
  - *Treatment:* the treatment account lingers on mental-health-adjacent items, decided
    live by Gemini with a multilingual keyword fallback. The control lingers exactly where
    its partner lingered (yoked), so watch time, timing, and network are matched and only
    the **content** lingered on differs.
- **Outcome.** The share of served items with a sadness or mental-health theme, and the
  share that is harmful. It is labeled offline with a codebook (`docs/codebook.md`) and
  validated against two human labelers.

## Guarantees

- **Passive only.** The bot never likes, comments, shares, follows, subscribes, or searches.
- **Symmetric safety rule.** No account, in any arm or phase, lingers on suicide or
  self-harm content: a keyword exclude match, a Gemini `harmful` label, or a Gemini
  refusal all lead to a skip. Any drift toward such content therefore comes from the
  recommender.
- **Frozen, provable procedure.** Every row carries `policy_fp`, a hash of the policy,
  protocol, keyword terms, seed lists, classifier model and prompt, the procedure's source
  code, and `requirements.lock`. Real runs refuse to start unless it equals
  `preregistered_fp` in `config.yaml`.
- **Every decision is logged.** For each item the row records:
  - which source decided (`gemini` or a `keyword_*` fallback, with the reason)
  - what the player actually did (`watched_s`, `loops`, `playing_frac`, `muted`)
  - whether it was an ad
  - any warning screen or crisis panel YouTube showed (`interstitial`)
- **Researcher protection.** Windows are covered by an opaque overlay and audio is muted at
  the browser level. Images in the review and labeling pages stay blurred until clicked.

## Setup

    uv pip install -r requirements.lock     # or: pip install -r requirements.lock
    playwright install chrome               # skip if Google Chrome is installed
    sudo apt-mark hold google-chrome-stable # keep Chrome's version fixed during the study

Keys:

    export YT_API_KEY=...       # YouTube Data API v3 key (Google Cloud console)
    export GEMINI_API_KEY=...   # Google AI Studio key

In `config.yaml`, set the following from what your AI Studio project shows:
- `classifier.model` and `labeler.model`: a Flash-Lite or Flash model ID
- `classifier.rpm` and `classifier.rpd`: your project's requests per minute and per day

Rate limits are per project. Treatment pairs run at once only as far as the RPM allows.
Any item Gemini can't decide in time falls back to keywords, and this is logged.

**Accounts.** `acct01` and `acct02` are pilot accounts (they already have dry-run history).
Add your fresh study accounts to `accounts.yaml` as `role: study`, with persona fields that
are identical across accounts. Then sign each one in by hand:

    python login.py acct03          # sign in in the window; records the account's identity
    python login.py --check         # every account: signed in, identity ok, history on
    python assign_groups.py         # seeded pairs; random treatment/control within each pair

After setup, never use the accounts by hand.

## Workflow

1. **Seed lists.** Run `python build_seeds.py` and open `data/seeds/review.html`. Tick about
   10 Shorts per arm and export. Paste the two lists into `seeds/treatment.txt` and
   `seeds/control.txt`.
2. **Pilot** (pilot accounts only; writes to `data/dryrun/`):

       python run_slot.py --session 4 --dry-run --n-items 20
       python run_slot.py --session 3 --dry-run            # check the seed session too

   Check the printed line and `data/dryrun/*/_session_log.jsonl`:
   - `active_video_rate` ≈ 1
   - `gemini_rate` ≥ 0.9
   - `muted_rate` = 0
   - `frames_rate` > 0
   - ads detected (`ads`)
   - no `flags`

   If `active_video_rate` drops, YouTube changed its front end: see `audit/dom.py`.
3. **Freeze.** Copy the printed `policy_fp` into `config.yaml: preregistered_fp`, fill in
   `docs/preregistration.md`, and register it with your supervisor (date-stamped).
4. **Collect.** Run one session per slot, at least `min_gap_hours` apart:

       python run_slot.py --session 1
       ...
       python run_slot.py --session 11
       python protocol_status.py                     # what has run, what failed

   If a session fails, re-run **the whole pair** and give a reason, which is written to
   `data/lab_log.jsonl`:

       python run_slot.py --session 5 --force --accounts acct03,acct04 --reason "Chrome crashed"

5. **Label and validate.**

       python label_items.py                         # Gemini outcome labels (resumable)
       python make_label_sample.py                   # ~150 items -> data/labels/label.html
       # Two people label in label.html; save their downloads in data/labels/human/
       python validate_labels.py                     # kappa, adjudication.csv, accuracy
       # Fill the gold column of data/labels/adjudication.csv together, then re-run

6. **Report.** Run `python analyze.py`, which writes `reports/mvp_report.html`. It contains
   the drift curves, the per-pair difference-in-differences, the validity check, the dose
   check and collection quality.

## Output

`data/raw/<phase>_sNN/<account>.jsonl` has one row per item shown. Fields:

- **Identity:** account, pair, group, mode, phase, session, attempt
- **Position:** `position` and `organic_index` (`organic_index` excludes ads; it is the
  yoke index)
- **Metadata:** video ID, title, channel, tags, duration (metadata and player), category,
  audio language, `meta_source`
- **Ads:** `is_ad`, `ad_signal`
- **Trigger:** `trigger_label`, `trigger_source`, `trigger_latency_s`, `kw_adjacent`, `kw_exclude`
- **Yoke:** `partner_long`, `yoke_wait_s`
- **Decision:** `long`, `reason`, `planned_dwell_s`
- **Player:** `start_delay_s`, `dwell_s`, `watched_s`, `loops`, `playing_frac`,
  `active_video_frac`, `muted`, `volume`
- **Warnings:** `interstitial`, `interstitial_snippet`
- **Navigation:** `left_early`, `advance_attempts`, `exposure_s`
- **Labeling:** `frames_saved`
- **Fingerprint:** `policy_fp`

`_session_log.jsonl` in each slot holds one row per account per attempt:
- status: `ok`, `not_signed_in`, `signed_out`, `wrong_account`, `history_off`, `blocked`,
  `stuck`, `no_feed`, `partner_aborted`, `ad_flood`, `interrupted` or `error`
- summary rates and quality `flags`
- the git commit and the Chrome, Playwright and Python versions

Other output:
- `data/meta_cache.jsonl`: full metadata
- `data/trigger_cache.jsonl`: live trigger verdicts
- `data/media/`: thumbnails and two player frames per video, captured because flagged
  videos are often removed before labeling

## Tests

    venv/bin/python -m pytest -q

No network or browser needed. The tests cover:
- the policy and the symmetric exclusion rule
- yoking, including what happens when a partner aborts
- the session loop, against a scripted fake browser on a virtual clock
- classifier fallbacks
- fingerprint stability
- protocol order, re-run and cancellation rules
- the metrics, and the full analysis on synthetic data with a planted effect

## Security and ethics

`profiles/` contains live Google session cookies. `data/` and `reports/` contain
harmful-content metadata and imagery. All three are git-ignored; never share them. See
`docs/ETHICS.md` and `docs/threats_to_validity.md`.
