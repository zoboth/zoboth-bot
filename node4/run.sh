#!/usr/bin/env bash
# One bot cycle on the self-hosted runner (cron calls this).
#   1. pull the latest bot.py / PROMPT.md
#   2. bot.py list -- deterministic, no model call; stop if nothing is pending
#   3. only then run Claude Code headless with the bot.py helpers allowed
# Billing: claude -p authenticates with ANTHROPIC_API_KEY from the Console
# organization linked to the Max plan, so it draws the plan's monthly API
# credits; when they run out the API refuses and the run fails, it never
# spills into paid usage unless the org buys credits.
#
# Secrets live in $CONF/env (mode 0600), passed with --env-file so they never
# appear on a command line (ps on this shared host shows docker's arguments):
#   GH_TOKEN=<zoboth classic PAT: public_repo/repo, notifications, read:org>
#   ANTHROPIC_API_KEY=<API key of the linked Console organization>
set -euo pipefail
BASE=${BASE:-$HOME/zoboth}
CONF=${CONF:-$HOME/.config/zoboth}
IMAGE=${IMAGE:-zoboth-runner:latest}
MODEL=${MODEL:-claude-sonnet-5}                    # everyone else
MODEL_PREMIUM=${MODEL_PREMIUM:-claude-opus-5-5}    # requests from PREMIUM_REQUESTERS
PREMIUM_REQUESTERS=${PREMIUM_REQUESTERS:-zhanghaohit}
LOG=${BASE}/log/$(date +%Y%m%d).log
mkdir -p "${BASE}/log"
exec >>"${LOG}" 2>&1
echo "=== $(date '+%F %T') cycle"

# One cycle at a time: a long review must not overlap the next cron tick.
exec 9>"${BASE}/.lock"
flock -n 9 || { echo "previous cycle still running"; exit 0; }

git -C "${BASE}/zoboth-bot" pull -q --ff-only || echo "WARN: git pull failed, using the current checkout"

run() {
  docker run --rm -i \
    --env-file "${CONF}/env" \
    -e ORG=modelsphere -e ALL_ORG_REPOS=1 -e LOOKBACK_HOURS="${LOOKBACK_HOURS:-6}" \
    -v "${BASE}/zoboth-bot:/work" -w /work \
    "${IMAGE}" "$@"
}

pending=$(run python3 bot.py list)
if [ "$(printf '%s' "${pending}" | tr -d '[:space:]')" = "[]" ]; then
  echo "nothing pending"; exit 0
fi
echo "pending: ${pending}"

# Model per requester: requests from PREMIUM_REQUESTERS get MODEL_PREMIUM, everyone else MODEL.
# Each run sees only its own items (bot.py list filters on ONLY_/EXCLUDE_REQUESTERS).
review() {  # <model> <env filter>
  docker run --rm -i --env-file "${CONF}/env" -e ORG=modelsphere -e ALL_ORG_REPOS=1 \
    -e LOOKBACK_HOURS="${LOOKBACK_HOURS:-6}" -e "$2" \
    -v "${BASE}/zoboth-bot:/work" -w /work "${IMAGE}" \
    claude -p "$(cat "${BASE}/zoboth-bot/PROMPT.md")" \
      --model "$1" --max-turns 60 \
      --allowedTools "Bash(python3 bot.py:*)" "Edit(//tmp/**)" "Write(//tmp/**)" "Read(//tmp/**)" \
      --output-format text
}
for tier in premium default; do
  if [ "$tier" = premium ]; then f="ONLY_REQUESTERS=${PREMIUM_REQUESTERS}"; m="${MODEL_PREMIUM}"
  else f="EXCLUDE_REQUESTERS=${PREMIUM_REQUESTERS}"; m="${MODEL}"; fi
  items=$(docker run --rm --env-file "${CONF}/env" -e ORG=modelsphere -e ALL_ORG_REPOS=1 \
    -e LOOKBACK_HOURS="${LOOKBACK_HOURS:-6}" -e "$f" -v "${BASE}/zoboth-bot:/work" -w /work \
    "${IMAGE}" python3 bot.py list 2>/dev/null)
  [ "$(printf '%s' "${items}" | tr -d '[:space:]')" = "[]" ] && continue
  echo "--- ${tier}: ${m}"
  review "$m" "$f"
done
echo "=== $(date '+%F %T') done"
