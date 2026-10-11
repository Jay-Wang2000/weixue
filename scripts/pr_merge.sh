#!/usr/bin/env bash
# Pushes a task branch from its worktree, opens the PR and squash-merges it; exits non-zero unless GitHub reports MERGED.
# usage: scripts/pr_merge.sh <worktree-dir> <branch> "<title>" <body-file>
set -euo pipefail
WT=$1; BRANCH=$2; TITLE=$3; BODY_FILE=$4
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

cd "$WT"
if ! git rebase main >/dev/null 2>&1; then
  git rebase --abort || true
  echo "pr_merge: rebase onto main conflicts; resolve in $WT first" >&2
  exit 2
fi
git push -u -f -q origin "$BRANCH"
if ! gh pr view "$BRANCH" --json number -q .number >/dev/null 2>&1; then
  gh pr create --base main --head "$BRANCH" --title "$TITLE" --body-file "$BODY_FILE" >/dev/null
fi
NUM=$(gh pr view "$BRANCH" --json number -q .number)

for _ in $(seq 1 12); do
  M=$(gh pr view "$NUM" --json mergeable -q .mergeable)
  [ "$M" != "UNKNOWN" ] && break
  sleep 10
done
if [ "$M" != "MERGEABLE" ]; then
  echo "pr_merge: PR #$NUM not mergeable ($M)" >&2
  exit 3
fi
gh pr merge "$NUM" --squash --delete-branch --subject "$TITLE" >/dev/null 2>&1 || true
STATE=$(gh pr view "$NUM" --json state -q .state)
if [ "$STATE" != "MERGED" ]; then
  echo "pr_merge: PR #$NUM state is $STATE, not MERGED" >&2
  exit 4
fi
cd "$REPO_DIR"
git pull -q --ff-only origin main
echo "pr_merge: PR #$NUM merged; main at $(git rev-parse --short HEAD)"
