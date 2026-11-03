#!/usr/bin/env python3
"""Find work for a GitHub machine-user bot driven by a Claude Code routine.

    python3 bot.py whoami                 # print the login GH_TOKEN belongs to
    python3 bot.py list                   # pending work as JSON
    python3 bot.py done <kind> <repo> <id>  # mark a mention handled (adds the bot's 👀)
    python3 bot.py show <repo> <number>     # issue/PR metadata, conversation and (PR) diff
    python3 bot.py comment <repo> <number> <file>  # post <file> as a comment
    python3 bot.py review <repo> <number> <file> [comment|approve|request-changes]
                                            # post <file> as a PR review (default comment)

The session's GitHub proxy blocks GraphQL, and `gh pr view/diff/review/comment`
and `gh issue view/comment` are GraphQL underneath, so all reads and writes go
through these REST helpers.

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


def fold_mentions_into_reviews(work):
    # A PR that both requests the bot's review and mentions it would otherwise
    # be reviewed twice. Keep one review_requested item and carry the mentions
    # on it; they still need `done` after the review is posted.
    reviews = {(i["repo"], i["number"]): i for i in work if i["kind"] == "review_requested"}
    out = []
    for i in work:
        r = reviews.get((i["repo"], i["number"]))
        if r is not None and i["kind"] != "review_requested":
            r.setdefault("mentions", []).append(
                {k: i[k] for k in ("kind", "repo", "id", "author", "url", "body")})
            continue
        out.append(i)
    return out


def cmd_list():
    since = (datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    work = []
    served = repos()
    print(f"# scanning {len(served)} repo(s): {', '.join(served)}", file=sys.stderr)
    for repo in served:
        work += scan_repo(repo, since)
    work = fold_mentions_into_reviews(work)
    json.dump(work[:MAX_ITEMS], sys.stdout, ensure_ascii=False, indent=1)
    print()
    if len(work) > MAX_ITEMS:
        print(f"# {len(work) - MAX_ITEMS} more item(s) left for the next run", file=sys.stderr)


def served_or_exit(repo):
    if repo not in repos():
        sys.exit(f"{repo} is not a served repo")


def cmd_done(kind, repo, oid):
    if kind not in REACTIONS:
        sys.exit(f"done applies to {sorted(REACTIONS)}; a review request is done by reviewing")
    served_or_exit(repo)
    gh(REACTIONS[kind].format(repo=repo, id=oid), method="POST", fields={"content": MARK})
    if not already_marked(kind, repo, oid):
        sys.exit(f"marking {kind} {repo} {oid} did not stick")
    print(f"marked {kind} {repo} {oid}")


def cmd_show(repo, number):
    served_or_exit(repo)
    i = gh(f"repos/{repo}/issues/{number}")
    is_pr = "pull_request" in i
    print(f"# {'PR' if is_pr else 'Issue'} {repo}#{number}: {i['title']}")
    print(f"# author {i['user']['login']} state {i['state']} url {i['html_url']}")
    print(i.get("body") or "")
    for c in gh(f"repos/{repo}/issues/{number}/comments?per_page=100", paginate=True):
        print(f"\n## comment {c['id']} by {c['user']['login']} at {c['created_at']}\n{c.get('body') or ''}")
    if is_pr:
        p = gh(f"repos/{repo}/pulls/{number}")
        print(f"\n# base {p['base']['ref']} head {p['head']['ref']} {p['head']['sha']}")
        for c in gh(f"repos/{repo}/pulls/{number}/comments?per_page=100", paginate=True):
            print(f"\n## review comment {c['id']} by {c['user']['login']} on {c['path']}:{c.get('line')}\n{c.get('body') or ''}")
        diff = subprocess.run(["gh", "api", f"repos/{repo}/pulls/{number}",
                               "-H", "Accept: application/vnd.github.diff"],
                              check=True, capture_output=True, text=True).stdout
        print("\n# diff\n" + diff)


def read_body(path):
    body = open(path, encoding="utf-8").read().strip()
    if not body:
        sys.exit(f"{path} is empty")
    return body


def cmd_comment(repo, number, path):
    served_or_exit(repo)
    c = gh(f"repos/{repo}/issues/{number}/comments", method="POST", fields={"body": read_body(path)})
    print(f"commented {c['html_url']}")


EVENTS = {"comment": "COMMENT", "approve": "APPROVE", "request-changes": "REQUEST_CHANGES"}


def cmd_review(repo, number, path, verdict="comment"):
    served_or_exit(repo)
    if verdict not in EVENTS:
        sys.exit(f"verdict must be one of {sorted(EVENTS)}")
    if verdict == "approve":
        # The bot's approval is meant to be advisory. GitHub counts approvals
        # only from users with write access, so refuse to approve if the bot
        # ever gains it -- that would let a PR author merge on the bot's word.
        perms = gh(f"repos/{repo}").get("permissions") or {}
        if perms.get("push") or perms.get("maintain") or perms.get("admin"):
            sys.exit(f"{BOT} can push to {repo}; its approval would count, refusing to approve")
    head = gh(f"repos/{repo}/pulls/{number}")["head"]["sha"]
    r = gh(f"repos/{repo}/pulls/{number}/reviews", method="POST",
           fields={"body": read_body(path), "event": EVENTS[verdict], "commit_id": head})
    print(f"reviewed ({r['state']}) {r['html_url']} at {head[:7]}")


def main():
    if not os.environ.get("GH_TOKEN"):
        sys.exit("GH_TOKEN is not set")
    args = sys.argv[1:]
    cmd = args[0] if args else ""
    if cmd == "whoami":
        print(gh("user")["login"])
        return
    if cmd not in ("list", "done", "show", "comment", "review"):
        sys.exit(__doc__)
    me = gh("user")["login"]
    if me.lower() != BOT.lower():
        sys.exit(f"GH_TOKEN belongs to {me}, not {BOT}; refusing to act")
    if cmd == "list" and len(args) == 1:
        cmd_list()
    elif cmd == "done" and len(args) == 4:
        cmd_done(*args[1:])
    elif cmd == "show" and len(args) == 3:
        cmd_show(*args[1:])
    elif cmd == "comment" and len(args) == 4:
        cmd_comment(*args[1:])
    elif cmd == "review" and len(args) in (4, 5):
        cmd_review(*args[1:])
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
