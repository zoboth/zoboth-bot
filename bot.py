#!/usr/bin/env python3
"""Find work for a GitHub machine-user bot driven by a Claude Code routine.

    python3 bot.py whoami                 # print the login GH_TOKEN belongs to
    python3 bot.py list                   # pending work as JSON
    python3 bot.py done <kind> <repo> <id>  # mark a mention handled (adds the bot's 👀)

Cloud sessions only allow repo-scoped GitHub endpoints (repos/{owner}/{repo}/...)
for the repos configured on the routine, so the notifications API is not
available. Instead each run scans the repos in REPOS directly:
  - issue/PR conversation comments, PR review comments and new issue/PR bodies
    that mention @BOT, updated within LOOKBACK_HOURS
  - open PRs that request a review from BOT and have no BOT review on the
    current head commit

State lives on GitHub, not in the session: a mention is done once the bot has
reacted 👀 to it, and a review request is done once the bot has reviewed the
head commit. The lookback is longer than the schedule so a missed run is
picked up by the next one.

Hard rules enforced here, not in the prompt:
  - only authors whose author_association is MEMBER or OWNER (the repos are
    public: anyone can @ the bot, a stranger must not spend our quota or steer it)
  - never the bot's own comments
  - at most MAX_ITEMS items per run

Env: GH_TOKEN (bot PAT); optional REPOS ("owner/repo,..."; default = the ORG
repos checked out in this session), BOT, ORG, LOOKBACK_HOURS (default 6),
MAX_ITEMS (default 5).
"""
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone

BOT = os.environ.get("BOT", "zoboth")
ORG = os.environ.get("ORG", "modelsphere")
LOOKBACK_HOURS = float(os.environ.get("LOOKBACK_HOURS", "6"))
MAX_ITEMS = int(os.environ.get("MAX_ITEMS", "5"))
TRUSTED = {"MEMBER", "OWNER"}
MARK = "eyes"

# kind -> reactions endpoint for the object with that id
REACTIONS = {
    "comment": "repos/{repo}/issues/comments/{id}/reactions",
    "review_comment": "repos/{repo}/pulls/comments/{id}/reactions",
    "issue_body": "repos/{repo}/issues/{id}/reactions",
}


def gh(path, method="GET", paginate=False, fields=None):
    cmd = ["gh", "api", "-X", method, path]
    if paginate:
        cmd.insert(2, "--paginate")
        cmd += ["--jq", ".[]"]
    for k, v in (fields or {}).items():
        cmd += ["-f", f"{k}={v}"]
    out = subprocess.run(cmd, check=True, capture_output=True, text=True).stdout
    if not out.strip():
        return [] if paginate else None
    if paginate:
        return [json.loads(line) for line in out.splitlines() if line.strip()]
    return json.loads(out)


def checked_out_repos():
    # The routine clones every configured repository next to this one, and only
    # those repos are reachable through the session's GitHub proxy, so the
    # checkouts themselves are the authoritative list.
    here = os.path.dirname(os.path.abspath(__file__))
    found = set()
    for base in {os.getcwd(), os.path.dirname(here), os.path.dirname(os.getcwd())}:
        try:
            names = os.listdir(base)
        except OSError:
            continue
        for name in names:
            d = os.path.join(base, name)
            if not os.path.isdir(os.path.join(d, ".git")):
                continue
            url = subprocess.run(["git", "-C", d, "remote", "get-url", "origin"],
                                 capture_output=True, text=True).stdout.strip()
            m = re.search(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?/?$", url)
            if m:
                found.add(m.group(1))
    return sorted(found)


def repos():
    r = [x.strip() for x in os.environ.get("REPOS", "").split(",") if x.strip()]
    if not r:
        r = [x for x in checked_out_repos() if x.split("/")[0].lower() == ORG.lower()]
    if not r:
        sys.exit(f"no {ORG} repos: set REPOS or add them to the routine's repositories")
    return r


def mentions_bot(body):
    return f"@{BOT}".lower() in (body or "").lower()


def trusted(obj):
    user = (obj.get("user") or {}).get("login", "")
    return user.lower() != BOT.lower() and obj.get("author_association") in TRUSTED


def already_marked(kind, repo, oid):
    path = REACTIONS[kind].format(repo=repo, id=oid) + f"?content={MARK}&per_page=100"
    return any((r.get("user") or {}).get("login", "").lower() == BOT.lower()
               for r in gh(path, paginate=True))


def number_of(url):
    return int(url.rstrip("/").split("/")[-1])


def scan_repo(repo, since):
    items = []

    def add(kind, oid, obj, number, body):
        if not (mentions_bot(body) and trusted(obj)):
            return
        if already_marked(kind, repo, oid):
            return
        items.append({"kind": kind, "repo": repo, "id": oid, "number": number,
                      "author": obj["user"]["login"], "url": obj.get("html_url"),
                      "body": body})

    for c in gh(f"repos/{repo}/issues/comments?since={since}&per_page=100", paginate=True):
        add("comment", c["id"], c, number_of(c["issue_url"]), c.get("body"))
    for c in gh(f"repos/{repo}/pulls/comments?since={since}&per_page=100", paginate=True):
        add("review_comment", c["id"], c, number_of(c["pull_request_url"]), c.get("body"))
    for i in gh(f"repos/{repo}/issues?state=all&since={since}&per_page=100", paginate=True):
        if (i.get("created_at") or "") >= since:
            add("issue_body", i["number"], i, i["number"], i.get("body"))

    for p in gh(f"repos/{repo}/pulls?state=open&per_page=100", paginate=True):
        if not any(u["login"].lower() == BOT.lower() for u in p.get("requested_reviewers", [])):
            continue
        if not trusted(p):
            continue
        head = p["head"]["sha"]
        reviews = gh(f"repos/{repo}/pulls/{p['number']}/reviews?per_page=100", paginate=True)
        if any((r.get("user") or {}).get("login", "").lower() == BOT.lower()
               and r.get("commit_id") == head for r in reviews):
            continue
        items.append({"kind": "review_requested", "repo": repo, "id": p["number"],
                      "number": p["number"], "author": p["user"]["login"],
                      "url": p["html_url"], "head": head, "body": p.get("body") or ""})
    return items


def cmd_list():
    since = (datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    work = []
    served = repos()
    print(f"# scanning {len(served)} repo(s): {', '.join(served)}", file=sys.stderr)
    for repo in served:
        work += scan_repo(repo, since)
    json.dump(work[:MAX_ITEMS], sys.stdout, ensure_ascii=False, indent=1)
    print()
    if len(work) > MAX_ITEMS:
        print(f"# {len(work) - MAX_ITEMS} more item(s) left for the next run", file=sys.stderr)


def cmd_done(kind, repo, oid):
    if kind not in REACTIONS:
        sys.exit(f"done applies to {sorted(REACTIONS)}; a review request is done by reviewing")
    if repo not in repos():
        sys.exit(f"{repo} is not in REPOS")
    gh(REACTIONS[kind].format(repo=repo, id=oid), method="POST", fields={"content": MARK})


def main():
    if not os.environ.get("GH_TOKEN"):
        sys.exit("GH_TOKEN is not set")
    args = sys.argv[1:]
    cmd = args[0] if args else ""
    if cmd == "whoami":
        print(gh("user")["login"])
        return
    if cmd not in ("list", "done"):
        sys.exit(__doc__)
    me = gh("user")["login"]
    if me.lower() != BOT.lower():
        sys.exit(f"GH_TOKEN belongs to {me}, not {BOT}; refusing to act")
    if cmd == "list":
        cmd_list()
    elif len(args) == 4:
        cmd_done(args[1], args[2], args[3])
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
