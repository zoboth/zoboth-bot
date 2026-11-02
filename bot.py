#!/usr/bin/env python3
"""Notification helper for a GitHub machine-user bot driven by a Claude Code routine.

    python3 bot.py whoami          # print the login GH_TOKEN belongs to
    python3 bot.py list            # print pending work as JSON (one object per thread)
    python3 bot.py done <thread>   # mark a notification thread read

All filtering lives here, not in the prompt, so it is deterministic:
  - only threads in ORG repos, with reason mention / review_requested / assign
  - only requests written by org MEMBER/OWNER (the repos are public: anyone can
    @ the bot, and a stranger must not be able to spend our quota or steer it)
  - never the bot's own comments (no talking to itself)
  - at most MAX_ITEMS threads per run
Threads that carry no acceptable request are marked read here, so they do not
come back every run.

Needs only `gh` on PATH and GH_TOKEN set to the bot's classic PAT
(scopes: repo or public_repo, notifications, read:org).
"""
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

BOT = os.environ.get("BOT", "zoboth")
ORG = os.environ.get("ORG", "modelsphere")
MAX_ITEMS = int(os.environ.get("MAX_ITEMS", "5"))
REASONS = {"mention", "review_requested", "assign"}
TRUSTED = {"MEMBER", "OWNER"}


def gh(path, method="GET", paginate=False):
    cmd = ["gh", "api", "-X", method, path]
    if paginate:
        cmd.insert(2, "--paginate")
        cmd += ["--jq", ".[]"]
    out = subprocess.run(cmd, check=True, capture_output=True, text=True).stdout
    if not out.strip():
        return [] if paginate else None
    if paginate:
        return [json.loads(line) for line in out.splitlines() if line.strip()]
    return json.loads(out)


def since_of(thread):
    # Comments newer than the last time the bot read this thread; a first-time
    # thread looks back one day.
    ts = thread.get("last_read_at")
    if ts:
        return ts
    return (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")


def requests_in(thread):
    repo = thread["repository"]["full_name"]
    subj = thread["subject"]
    if subj["type"] not in ("Issue", "PullRequest") or not subj.get("url"):
        return None, []
    number = subj["url"].rstrip("/").split("/")[-1]
    issue = gh(f"repos/{repo}/issues/{number}")
    since = since_of(thread)
    found = []

    def take(kind, obj, body):
        user = (obj.get("user") or {}).get("login", "")
        if user.lower() == BOT.lower():
            return
        if obj.get("author_association") not in TRUSTED:
            return
        found.append({"kind": kind, "id": obj.get("id"), "author": user,
                      "url": obj.get("html_url"), "body": body or ""})

    mention = f"@{BOT}".lower()
    if thread["reason"] == "mention":
        if (issue.get("created_at") or "") >= since and mention in (issue.get("body") or "").lower():
            take("issue_body", issue, issue.get("body"))
        for c in gh(f"repos/{repo}/issues/{number}/comments?since={since}&per_page=100", paginate=True):
            if mention in (c.get("body") or "").lower():
                take("comment", c, c.get("body"))
        if subj["type"] == "PullRequest":
            for c in gh(f"repos/{repo}/pulls/{number}/comments?since={since}&per_page=100", paginate=True):
                if mention in (c.get("body") or "").lower():
                    take("review_comment", c, c.get("body"))
    else:
        # review_requested / assign: GitHub only lets triage+ users do this, but
        # still require the issue/PR author to be trusted before acting on it.
        take(thread["reason"], issue, issue.get("body"))

    meta = {"thread": thread["id"], "reason": thread["reason"], "repo": repo,
            "number": int(number), "type": subj["type"], "title": subj.get("title"),
            "html_url": issue.get("html_url")}
    return meta, found


def cmd_list():
    threads = gh("notifications?participating=true&per_page=50", paginate=True)
    work = []
    for t in threads:
        if len(work) >= MAX_ITEMS:
            break
        repo_owner = t["repository"]["owner"]["login"]
        if repo_owner.lower() != ORG.lower() or t["reason"] not in REASONS:
            gh(f"notifications/threads/{t['id']}", method="PATCH")
            continue
        meta, found = requests_in(t)
        if not meta or not found:
            gh(f"notifications/threads/{t['id']}", method="PATCH")
            continue
        meta["requests"] = found
        work.append(meta)
    json.dump(work, sys.stdout, ensure_ascii=False, indent=1)
    print()


def main():
    if not os.environ.get("GH_TOKEN"):
        sys.exit("GH_TOKEN is not set")
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "whoami":
        print(gh("user")["login"])
    elif cmd == "list":
        me = gh("user")["login"]
        if me.lower() != BOT.lower():
            sys.exit(f"GH_TOKEN belongs to {me}, not {BOT}; refusing to act")
        cmd_list()
    elif cmd == "done" and len(sys.argv) == 3:
        gh(f"notifications/threads/{sys.argv[2]}", method="PATCH")
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
