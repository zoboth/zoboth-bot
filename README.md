# zoboth: a GitHub bot that answers @mentions

A machine-user account (`zoboth`, member of the org with read access) plus a
scheduled Claude Code routine. Each run scans the served repos with
`bot.py`, handles them, and replies as the bot through `gh`.

## Setup (claude.ai/code)

Cloud sessions only allow repo-scoped GitHub API calls for the repos that are
configured on the routine, so the bot cannot use the notifications API. It
scans the repos it serves instead: every org repo added to the routine's
repository list is served automatically (bot.py reads the checkouts), and a
repo that is not on the list cannot be reached at all.

1. **Environment**: add
   - `GH_TOKEN` = the bot's classic PAT (`public_repo` or `repo`, `read:org`)
   - optional `REPOS` = comma-separated `owner/repo` list, to serve fewer repos
     than are checked out
   Network access: Trusted (default).
2. **Routine**: repositories = this repo + every org repo the bot should serve,
   environment = the one above, trigger = schedule (hourly is the minimum),
   prompt = below.
3. Test: mention the bot in an issue of one of those repos, then **Run now**.

## Routine prompt

The routine's own prompt is one fixed line; the real instructions live in
[PROMPT.md](PROMPT.md), so changing them only needs a commit here:

```
Read PROMPT.md in the zoboth-bot repository checkout and follow it exactly.
```

## Safety

`bot.py list` enforces the hard rules (only the served org repos, MEMBER/OWNER
authors only, never the bot's own comments, at most `MAX_ITEMS` items) and
refuses to run when `GH_TOKEN` is not the bot's token. State is kept on
GitHub: a handled mention carries the bot's 👀 reaction, a handled review
request has a bot review on the head commit.
