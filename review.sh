#!/usr/bin/env bash
# 互動式審閱 DISCOVER 提案
# 用法：bash review.sh
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
"$DIR/.venv/bin/python" "$DIR/scripts/review_shadow_proposals.py" "$@"
