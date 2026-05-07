#!/bin/bash
# post-requirements-source-resolve.sh
#
# PostToolUse hook (Write|Edit): after the requirements step writes its
# step-result.json, automatically run resolve_source.py to discover and
# clone repos from discovered_repos.json, then update the progress file
# to un-defer (or skip) source-dependent steps.
#
# This makes post-requirements source resolution deterministic — the LLM
# no longer needs to remember to re-run resolve_source.py.
#
# Exit codes: always 0 (hooks must not block the LLM).
# All diagnostic output goes to stderr.

set -u

INPUT=$(cat)

FILE_PATH=$(echo "$INPUT" | jq -r '.tool_input.file_path // .tool_input.file // empty' 2>/dev/null)

if [ -z "$FILE_PATH" ]; then
  exit 0
fi

# Fast bail: only trigger on requirements step-result.json
case "$FILE_PATH" in
  */requirements/step-result.json) ;;
  *) exit 0 ;;
esac

BASE_PATH="${FILE_PATH%/requirements/step-result.json}"

if [ ! -d "$BASE_PATH" ]; then
  exit 0
fi

# Idempotency: skip if already resolved
STAMP="${BASE_PATH}/requirements/.source-resolved"
if [ -f "$STAMP" ]; then
  exit 0
fi

# Find the progress file
shopt -s nullglob
PROGRESS_FILES=("${BASE_PATH}"/workflow/docs-workflow_*.json)
shopt -u nullglob

if [ ${#PROGRESS_FILES[@]} -eq 0 ]; then
  exit 0
fi

PROGRESS_FILE="${PROGRESS_FILES[0]}"

# Bail if source is already set (was provided explicitly or resolved pre-flight)
SOURCE_SET=$(jq -r '.options.source.repo_path // empty' "$PROGRESS_FILE" 2>/dev/null)
if [ -n "$SOURCE_SET" ]; then
  touch "$STAMP"
  exit 0
fi

# Read plugin root from conf file
CONF_FILE="${CLAUDE_PROJECT_DIR:-.}/.claude/hooks/docs-orchestrator.conf"
if [ ! -f "$CONF_FILE" ]; then
  echo "post-requirements-source-resolve: no conf file at $CONF_FILE" >&2
  exit 0
fi

# shellcheck source=/dev/null
source "$CONF_FILE"

if [ -z "${PLUGIN_ROOT:-}" ]; then
  echo "post-requirements-source-resolve: PLUGIN_ROOT not set in $CONF_FILE" >&2
  exit 0
fi

RESOLVE_SCRIPT="${PLUGIN_ROOT}/skills/docs-orchestrator/scripts/resolve_source.py"
if [ ! -f "$RESOLVE_SCRIPT" ]; then
  echo "post-requirements-source-resolve: resolve_source.py not found at $RESOLVE_SCRIPT" >&2
  exit 0
fi

echo "post-requirements-source-resolve: requirements completed, resolving source repos..." >&2

RESULT_FILE=$(mktemp)
trap 'rm -f "$RESULT_FILE"' EXIT

python3 "$RESOLVE_SCRIPT" \
  --base-path "$BASE_PATH" \
  --scan-requirements \
  > "$RESULT_FILE" 2>&2
RESOLVE_EXIT=$?

if [ "$RESOLVE_EXIT" -eq 0 ]; then
  # Source resolved — update progress file
  REPO_PATH=$(jq -r '.repo_path // empty' "$RESULT_FILE" 2>/dev/null)
  REPO_URL=$(jq -r '.repo_url // empty' "$RESULT_FILE" 2>/dev/null)
  REF=$(jq -r '.ref // null' "$RESULT_FILE" 2>/dev/null)
  SCOPE=$(jq -r '.scope // null' "$RESULT_FILE" 2>/dev/null)

  if [ -n "$REPO_PATH" ]; then
    # Update options.source
    jq --arg rp "$REPO_PATH" \
       --arg ru "$REPO_URL" \
       --argjson ref "$( [ "$REF" = "null" ] && echo 'null' || echo "\"$REF\"")" \
       --argjson scope "$( [ "$SCOPE" = "null" ] && echo 'null' || echo "\"$SCOPE\"")" \
       '.options.source = {repo_path: $rp, repo_url: $ru, ref: $ref, scope: $scope}' \
       "$PROGRESS_FILE" > "${PROGRESS_FILE}.tmp" && mv "${PROGRESS_FILE}.tmp" "$PROGRESS_FILE"

    # Handle additional_repos if present
    ADDITIONAL=$(jq -r '.additional_repos // empty' "$RESULT_FILE" 2>/dev/null)
    if [ -n "$ADDITIONAL" ] && [ "$ADDITIONAL" != "null" ]; then
      jq --argjson add "$(jq '.additional_repos' "$RESULT_FILE")" \
         '.options.additional_sources = $add' \
         "$PROGRESS_FILE" > "${PROGRESS_FILE}.tmp" && mv "${PROGRESS_FILE}.tmp" "$PROGRESS_FILE"
    fi

    # Un-defer: change deferred steps to pending
    jq '(.steps | to_entries | map(select(.value.status == "deferred")) | .[].key) as $k |
        .steps[$k].status = "pending"' \
       "$PROGRESS_FILE" > "${PROGRESS_FILE}.tmp" && mv "${PROGRESS_FILE}.tmp" "$PROGRESS_FILE"

    echo "post-requirements-source-resolve: source resolved to $REPO_PATH — deferred steps now pending" >&2
  fi

elif [ "$RESOLVE_EXIT" -eq 2 ]; then
  # No source found — skip deferred steps
  jq '(.steps | to_entries | map(select(.value.status == "deferred")) | .[].key) as $k |
      .steps[$k].status = "skipped"' \
     "$PROGRESS_FILE" > "${PROGRESS_FILE}.tmp" && mv "${PROGRESS_FILE}.tmp" "$PROGRESS_FILE"

  echo "post-requirements-source-resolve: no source repo discovered — deferred steps skipped" >&2

else
  # Error — leave state unchanged
  echo "post-requirements-source-resolve: resolve_source.py failed (exit $RESOLVE_EXIT), leaving state unchanged" >&2
fi

# Write stamp regardless of outcome (prevent re-runs)
touch "$STAMP"

exit 0
