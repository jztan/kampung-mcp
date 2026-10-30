#!/usr/bin/env bash
# Check BRANCH against the gitflow prefixes and the author, committer and
# trailers of each commit in RANGE.
#
# Usage: scripts/check-commits.sh BRANCH RANGE
#   e.g. scripts/check-commits.sh feature/x origin/develop..HEAD
set -euo pipefail

branch=$1
range=$2
status=0

case $branch in
  develop | master | feature/?* | release/?* | hotfix/?*) ;;
  *)
    echo "::error::branch '$branch' must be develop, master, feature/*, release/* or hotfix/*"
    status=1
    ;;
esac

identity='(noreply@anthropic\.com|^Claude$)'
trailer='^(Co-Authored-By:.*(Claude|anthropic)|Claude-Session:|.*Generated with \[Claude Code\])'

for sha in $(git rev-list "$range"); do
  short=$(git rev-parse --short "$sha")
  for who in "%an" "%ae" "%cn" "%ce"; do
    if git log -1 --format="$who" "$sha" | grep -qiE "$identity"; then
      echo "::error::$short has a disallowed author or committer ($(git log -1 --format='%an <%ae>' "$sha"))"
      status=1
      break
    fi
  done
  if git log -1 --format=%B "$sha" | grep -qiE "$trailer"; then
    echo "::error::$short has a disallowed trailer"
    status=1
  fi
done

exit $status
