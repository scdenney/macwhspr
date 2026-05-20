Review recent macwhspr transcription cleanup logs and update the vocabulary/style file.

## What to do

Read these two files:

- `~/.config/macwhspr/cleanup_log.jsonl` - JSONL log of `{ts, raw, cleaned}` pairs
- `~/.config/macwhspr/vocab.md` - current vocabulary and style preferences

If the log is empty or does not exist yet, tell the user and stop.

## Analysis

Look at the last 50 log entries, or all of them if fewer. For each entry,
compare `raw` vs `cleaned`. Identify:

1. **Proper nouns and technical terms** - names, app names, project names, and tools that appear in the text. Are they being handled consistently? Are any missing from `vocab.md`?
2. **Recurring style choices** - capitalization patterns, punctuation habits, paragraph breaks, and formatting the model applied consistently.
3. **Corrections that look wrong** - cases where the cleaned version changed meaning or made a bad edit.
4. **Written-prosody preferences** - sentence rhythm, paragraph density, register, directness, and how much the cleanup model compresses or smooths dictated speech.
5. **High-value vocab candidates** - terms that appear multiple times across entries and are not yet in `vocab.md`.

## Calibration session

Present your findings as a short summary, then walk through proposed changes to
`vocab.md` one at a time:

- State the proposed addition or change.
- Show which log entries support it, quoting briefly.
- Ask: approve, reject, or modify?

After the user responds to each, move to the next. When done, write the updated
`~/.config/macwhspr/vocab.md` with all approved changes incorporated.

Keep it focused. Aim for 5-10 proposals per session, prioritizing the most
recurring patterns.
