# Ethics and data handling

**Approval status:** _fill in: supervisor sign-off / institutional review (who approved it and when)._

## Why the study is justified

Algorithmic audits of recommender systems serve the public interest. They show whether
platforms steer distressed users toward harmful content, which neither users nor regulators
can observe directly. A black-box audit with sock puppets is the established method for
this (Sandvig et al. 2014; Ribeiro et al. 2020; Haroon et al. 2022; Amnesty International 2023).

## Harm minimisation built into the code

- **No amplification of creators.** The accounts never like, comment, share, follow or
  subscribe. Watch time from a handful of accounts is negligible next to real traffic.
- **The bot never seeks harm.** No account lingers on suicide, self-harm or pro-eating-disorder
  content, in any arm or phase. Seed videos are reviewed by a person and must not be harmful.
- **No searching from study accounts.** Seed candidates come from the researcher's API key.
- **No real users involved.** No personal data about real people is collected beyond
  public video metadata.

## Terms of service

Automated access to YouTube conflicts with its Terms of Service. Courts and scholars have
argued that research audits of this kind are legitimate (e.g. *Sandvig v. Barr*, 2020, in the
US). Your institution's position still applies. Record the supervisor's decision above
before collecting data. Keep the scale minimal (MVP: 4–6 accounts) and don't publish
account identities.

## Data handling

- `profiles/` holds live Google session cookies, and `data/` and `reports/` hold
  harmful-content metadata and imagery. All are git-ignored. Keep them on an encrypted disk.
  Don't upload them to shared drives or chat.
- Publish only aggregates and video IDs needed for replication, never thumbnails or frames.
- Delete the sock-puppet accounts and `data/media/` when the project ends (or after the
  retention period your institution requires).

## Researcher and labeler wellbeing

- Collection windows are covered by an opaque overlay and are muted. Review and labeling
  pages keep images blurred until clicked.
- Labelers agree beforehand, can stop at any time, and label in short sessions.
- Have a contact ready for anyone affected by the material. In Pakistan: Umang helpline,
  0311-7786264 (check that the number is current before use).
