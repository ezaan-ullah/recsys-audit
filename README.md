# Short-video recommendation audit: collection harness

Sock-puppet harness for YouTube Shorts. Each account scrolls its Shorts feed and
lingers (or doesn't) on each item according to a fixed, logged policy. It records
every item it is shown. Exposure is measured later, offline, by the labeling pipeline.

## Design guarantees

**Passive only.** The bot never likes, comments, shares, follows, subscribes, or
searches. Watch time is its only signal, so the study never boosts real creators' content.

**Treatment never seeks harm.** Treatment accounts linger only on mental-health-adjacent
items (`keywords/adjacent.txt`) and always skip items matching `keywords/exclude.txt`.
Any drift toward harmful content therefore comes from the recommender, not the bot.

**Controls ignore content.** Neutral mode chooses dwell from a fixed probability that
never looks at the item. Both modes use identical random draws per item (tested).

**Frozen, provable policy.** Every row carries `policy_fp`, a hash of the policy
settings and both keyword files. Pre-register that value and it shows the treatment
rule never changed.

**Researcher protection.** Pages are blurred and muted on your screen (`browser.blur`).
The video still plays normally, so the platform sees ordinary viewing.

## Setup (once)

    pip install -r requirements.txt
    playwright install chrome              # skip if Google Chrome is already installed

Get a YouTube Data API v3 key (Google Cloud console) and set it:

    export YT_API_KEY=...                  # Windows: set YT_API_KEY=...

Without a key, the harness falls back to titles only, which makes treatment triggering weaker.

**Accounts.** Edit `accounts.yaml` to match your accounts, then sign each in by hand:

    python login.py acct01                 # repeat for every account
    python login.py --check                # all should say signed_in=True

If Google refuses the sign-in in this window, close it and open real Chrome yourself with
`--user-data-dir=<repo>/profiles/acct01`. Sign in there, close Chrome, and the harness
will reuse that profile.

After setup, never use these accounts by hand.

## Workflow

**1. Dry run (feasibility + calibration).** Use two throwaway accounts, not study accounts:

    python run_slot.py --phase treatment --session 1 --dry-run --mode treatment \
        --accounts acct01,acct02 --n-items 15

Check that items were collected, `playing` is near 1.0, and `meta` coverage is high.
The printed long-dwell rate is your candidate for `policy.neutral_long_prob`. Run a few
dry sessions, since the rate rises as treatment feeds shift.

**2. Freeze.** Set `neutral_long_prob`, finalize the keyword files, note the `policy_fp`
printed on the next run, and pre-register.

**3. Assign groups** (once; seeded and balanced):

    python assign_groups.py

**4. Collect.** Run two slots per day, with sessions numbered continuously:

    python run_slot.py --phase baseline  --session 1    # day 1 (sessions 1-2)
    python run_slot.py --phase treatment --session 3    # days 2-5 (sessions 3-10)
    python run_slot.py --phase washout   --session 11   # days 6-7 (sessions 11-14)

Keep windows open, not minimized, since minimized windows can pause playback. Covering
them with other windows is fine. Keep a lab log of anything unusual (crashes, re-runs
with `--force`, CAPTCHAs).

## Output

`data/raw/<phase>_sNN/<account>.jsonl` has one row per feed item:
- account, group, mode, phase, session, and position
- arrival time and video ID
- title, channel, tags, and duration
- the policy decision and matched term
- planned and actual dwell
- whether the video was playing
- `policy_fp`

`_session_log.jsonl` in each slot folder has one row per account per slot: status
(`ok`, `not_signed_in`, `signed_out`, `blocked`, `stuck`, `no_feed`, or `error`) and
summary rates.

`data/meta_cache.jsonl` holds full metadata, including descriptions, for later labeling.

## Security

`profiles/` contains live Google session cookies. It is git-ignored; never share it.
The same goes for `data/`, which contains harmful-content metadata.

## Known limitations

These belong in your threats-to-validity section:

- All accounts share one IP address and may be linked by the platform.
- Sponsored items are skipped and not logged.
- Watch-time matching between groups is approximate, because the treatment trigger rate
  changes as the feed shifts. Report total watch time per group.
- YouTube changes its front end often. The harness relies only on the `/shorts/<id>` URL
  and keyboard navigation to minimize breakage.
