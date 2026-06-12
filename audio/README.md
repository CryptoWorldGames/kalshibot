# Audio assets

Drop sound files here. They are served by Flask at `/audio/<filename>`.

## Cha-ching (cash register) sound

Put your cash-register sound here named **exactly**:

```
chaching.mp3
```

The bot plays `/audio/chaching.mp3` on a **profitable sell**, immediately before the
spoken announcement ("you sold N contracts for X each, for a total of …, and made … profit").

- Supported: `.mp3` (recommended), also `.wav`/`.ogg` if you rename the code reference.
- If `chaching.mp3` is missing, the bot falls back to a synthesized cha-ching — nothing breaks.
- A per-browser custom sound (uploaded via the in-app Sounds settings, stored in localStorage
  under `customSound_profit`) takes priority over this file if set.

## Other sounds (optional, per-browser via the app's Sound settings)
- Buy beep: `customSound_buy`
- Profit: `customSound_profit`
- Loss: `customSound_loss`

These live in browser localStorage, not here. This folder is the shared/committed sound that
syncs across machines via git.
