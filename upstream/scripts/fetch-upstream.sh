#!/usr/bin/env bash
# fetch-upstream.sh — pull latest tracked Android source files from GitHub
# Run from the repo root: ./upstream/scripts/fetch-upstream.sh
#
# Each file is saved to upstream/android/ alongside its current commit SHA.
# After running, use `git diff upstream/android/` to review changes, then
# run check-compat.py to verify critical constants still match.

set -euo pipefail

REPO="permissionlesstech/bitchat-android"
BRANCH="main"
BASE_URL="https://raw.githubusercontent.com/${REPO}/${BRANCH}"
API_URL="https://api.github.com/repos/${REPO}"
OUT_DIR="$(cd "$(dirname "$0")/../android" && pwd)"

# Tracked files: <android-path> <local-filename>
TRACKED=(
    "app/src/main/java/com/bitchat/android/util/AppConstants.kt         AppConstants.kt"
    "app/src/main/java/com/bitchat/android/protocol/BinaryProtocol.kt   BinaryProtocol.kt"
    "app/src/main/java/com/bitchat/android/model/FragmentPayload.kt      FragmentPayload.kt"
    "app/src/main/java/com/bitchat/android/mesh/FragmentManager.kt       FragmentManager.kt"
    "app/src/main/java/com/bitchat/android/mesh/PacketRelayManager.kt    PacketRelayManager.kt"
    "app/src/main/java/com/bitchat/android/mesh/BluetoothGattServerManager.kt  BluetoothGattServerManager.kt"
    "app/src/main/java/com/bitchat/android/mesh/BluetoothGattClientManager.kt  BluetoothGattClientManager.kt"
    "app/src/main/java/com/bitchat/android/model/BitchatMessage.kt       BitchatMessage.kt"
)

echo "Fetching upstream Android sources from ${REPO}@${BRANCH}"
echo ""

for entry in "${TRACKED[@]}"; do
    android_path=$(echo "$entry" | awk '{print $1}')
    local_name=$(echo "$entry" | awk '{print $2}')
    local_file="${OUT_DIR}/${local_name}"

    echo -n "  ${local_name} ... "

    # Fetch file content
    http_code=$(curl -s -o "${local_file}.new" -w "%{http_code}" \
        "${BASE_URL}/${android_path}")

    if [ "$http_code" != "200" ]; then
        echo "FAILED (HTTP ${http_code})"
        rm -f "${local_file}.new"
        continue
    fi

    # Get latest commit SHA for this file
    commit_sha=$(curl -s \
        "${API_URL}/commits?path=${android_path}&per_page=1" \
        | python3 -c "import sys,json; d=json.load(sys.stdin); print(d[0]['sha'][:7] if d else 'unknown')" \
        2>/dev/null || echo "unknown")

    commit_date=$(curl -s \
        "${API_URL}/commits?path=${android_path}&per_page=1" \
        | python3 -c "import sys,json; d=json.load(sys.stdin); print(d[0]['commit']['author']['date'][:10] if d else 'unknown')" \
        2>/dev/null || echo "unknown")

    # Prepend tracking header
    {
        echo "// upstream snapshot — DO NOT EDIT"
        echo "// source: ${android_path}"
        echo "// commit: ${commit_sha}  (${commit_date})"
        echo "// fetched: $(date -u +%Y-%m-%d)"
        echo ""
        cat "${local_file}.new"
    } > "${local_file}"

    rm -f "${local_file}.new"

    # Check if changed
    if git -C "$(dirname "$OUT_DIR")" diff --quiet -- "upstream/android/${local_name}" 2>/dev/null; then
        echo "unchanged (${commit_sha})"
    else
        echo "UPDATED  (${commit_sha}, ${commit_date})"
    fi
done

echo ""
echo "Done. Review changes with:  git diff upstream/android/"
echo "Then verify constants with: python3 upstream/scripts/check-compat.py"
