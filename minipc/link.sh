#!/usr/bin/env bash
# link.sh — point the minipc's live script paths at this checkout, so an edit on
# either side is the same file (commit + push/pull to sync with other machines).
#
#   link.sh          create/repair the symlinks (an identical live file is replaced;
#                    a live file that DIFFERS is left alone and reported)
#   link.sh --check  report only; exit 1 if anything is not linked (stack-backup.sh runs this)
#
# Mapping:  bin/<f>         -> ~/.local/bin/<f>
#           services/<d>/<f> -> ~/services/<d>/<f>   (state/config/secrets stay in ~/services, never here)
#           stack-backup.sh -> ~/.local/bin/stack-backup.sh
#
# Editors that save by write-temp-then-rename replace a symlink with a plain file;
# --check catches that as "drifted": copy the live file over the repo one, then re-run link.sh.
set -uo pipefail
REPO="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
MH="${STACK_OWNER_HOME:-/home/minipc}"
CHECK=0; [ "${1:-}" = "--check" ] && CHECK=1
bad=0

target_for() {
  case "$1" in
    bin/*)           echo "$MH/.local/bin/${1#bin/}";;
    services/*)      echo "$MH/$1";;
    stack-backup.sh) echo "$MH/.local/bin/stack-backup.sh";;
  esac
}

cd "$REPO"
while IFS= read -r rel; do
  src="$REPO/$rel"; dst="$(target_for "$rel")"
  [ -n "$dst" ] || continue
  if [ -L "$dst" ] && [ "$(readlink -f "$dst")" = "$src" ]; then
    continue
  elif [ -e "$dst" ] && ! cmp -s "$src" "$dst"; then
    echo "DRIFTED  $dst differs from $rel (live copy edited? reconcile, then re-run link.sh)"; bad=1
  elif [ "$CHECK" = 1 ]; then
    echo "UNLINKED $dst"; bad=1
  else
    mkdir -p "$(dirname "$dst")" && ln -sfn "$src" "$dst" && echo "linked   $dst -> $rel"
  fi
done < <(find bin services -type f ! -name '*.pyc' 2>/dev/null; echo stack-backup.sh)
exit "$bad"
