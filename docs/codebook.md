# Outcome codebook (v1)

Used word for word by the Gemini outcome labeler (`prompts/outcome_v1.md`) and by the
human labelers (`label.html`). Judge each Short by its **overall theme and mood**, using
the thumbnail, the frames, and the text together.

## Primary label (choose exactly one)

| Label | Meaning | Examples |
|---|---|---|
| `none` | No sadness or mental-health theme. | Comedy, pranks, sport, gaming, cooking, religion, music, romance without pain, motivational/"mindset" clips, heartwarming kindness edits. |
| `adjacent` | Sad or emotionally painful mood, without explicit mental-health distress. | Sad shayari/poetry, "sad status", sad edits, breakup or betrayal (bewafa), loneliness, missing someone, grief, nostalgia that hurts. |
| `mh_distress` | Explicit mental-health struggle or distress: the person (or the edit) presents depression, anxiety, panic, hopelessness, numbness, breakdown, "nobody cares about me", or discusses a mental illness. | "Depression is real" edits, crying-breakdown vlogs, overthinking at 3 a.m., "I feel empty". Awareness content that dwells on the struggle. |
| `harmful` | Any reference to suicide, self-harm, wanting to die or disappear, methods of self-injury, or glorifying/romanticizing them; pro-eating-disorder content. | "I want to sleep forever", cutting imagery, "unalive" jokes, thinspiration. |

If two labels apply, choose the more severe one (`harmful` > `mh_distress` > `adjacent` > `none`).
If there is too little information to tell, choose `none` and set `unclear`.

## Flags

- `is_ad`: the Short is an advertisement or paid promotion.
- `supportive`: the Short points viewers toward help (helplines, therapy, recovery, "you are not alone").
- `unclear`: the label is a guess because the Short could not be understood (language, missing image).

## Language

The main language of the text: `en`, `ur` (Urdu script), `ur-Latn` (Roman Urdu), `hi`, `pa`, `bn`, `ar`, `other`, or `mixed`.
