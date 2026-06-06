# 🗑️ TRASH — safe holding area (nothing here is gone)

Instead of **deleting** old/deprecated files, we **move them here**. That way
everything is recoverable until you explicitly decide to clear it.

## Rules
- When a file/feature is retired, `git mv` it into `TRASH/` (optionally into a
  dated subfolder like `TRASH/2026-06-06-old-mobile/`) instead of deleting it.
- Add a one-line note in this file: what it was, why it was retired, and the date.
- Nothing here is loaded or run by the app — it's parked, not active.

## How to recover something
```bash
git mv TRASH/<file> <original/path>     # bring it back
```
Even after permanent deletion, git history still has it:
```bash
git log --all --oneline -- TRASH/<file>     # find the commit
git checkout <commit> -- <path>             # restore from history
```

## Weekly cleanup
CONTEXT.md tracks a "Trash last reviewed" date. ~Once a week, review the items
below and permanently delete anything you're sure about (then bump the date).

---

## Parked items
_(none yet)_

| Date | Item | Why retired |
|------|------|-------------|
| | | |
