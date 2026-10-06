#!/usr/bin/env bash
#
# doctor.sh -- report the size of the transcoded-video cache.
#
# The transcode folder lives at <data_dir>/transcoded, where <data_dir> is
# whichever the app would use: the value resolved from config.toml (and
# MEDIASRV_* env vars), plus the built-in default. Both are checked, and only
# the ones that exist are reported. A warning is printed if any of them is
# larger than LIMIT_GB.
#
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$HERE"

LIMIT_GB="${LIMIT_GB:-20}"
LIMIT_KB=$((LIMIT_GB * 1024 * 1024))

# 1) Data dir as the app resolves it (config.toml / MEDIASRV_DATA env).
CONFIG_DATA=""
if command -v uv >/dev/null 2>&1; then
    CONFIG_DATA="$(cd "$ROOT" && uv run --quiet python -c \
        'from app import config; print(config.DATA_DIR)' 2>/dev/null || true)"
fi

# 2) The app's built-in default.
DEFAULT_DATA="$HOME/.cache/MediaSrv/data"

printf 'Transcode cache doctor (warn over %sG)\n' "$LIMIT_GB"
printf '%-10s %-9s %s\n' "SOURCE" "SIZE" "PATH"
printf -- '--------------------------------------------------------------\n'

check_one() {
    local label="$1" data_dir="$2"
    local trans="$data_dir/transcoded"

    if [ ! -d "$trans" ]; then
        printf '%-10s %-9s %s (not found)\n' "$label" "-" "$trans"
        return 0
    fi

    local kb human
    kb="$(du -sk "$trans" 2>/dev/null | awk '{print $1}')"
    human="$(du -sh "$trans" 2>/dev/null | awk '{print $1}')"
    printf '%-10s %-9s %s\n' "$label" "${human:-?}" "$trans"

    if [ "${kb:-0}" -gt "$LIMIT_KB" ]; then
        printf '\n  !! WARNING: %s is %s, over the %sG limit.\n' "$label" "${human:-?}" "$LIMIT_GB"
        printf '     Clear it with:  rm -rf %q\n' "$trans"
    fi
}

seen=""
for entry in "config:$CONFIG_DATA" "default:$DEFAULT_DATA"; do
    label="${entry%%:*}"
    data="${entry#*:}"
    [ -n "$data" ] || continue
    case " $seen " in *" $data "*) continue ;; esac
    seen="$seen $data"
    check_one "$label" "$data"
done

printf -- '--------------------------------------------------------------\n'
