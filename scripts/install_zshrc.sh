#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd -- "$SCRIPT_DIR/.." && pwd)"
ZSHRC="${ZSHRC:-$HOME/.zshrc}"
if [[ -n "${CODEXTOP_BIN_DIR:-}" ]]; then
  BIN_DIR="$CODEXTOP_BIN_DIR"
elif [[ -d "$HOME/.npm-global/bin" ]]; then
  BIN_DIR="$HOME/.npm-global/bin"
else
  BIN_DIR="$HOME/.local/bin"
fi
MARKER_BEGIN="# >>> codextop initialize >>>"
MARKER_END="# <<< codextop initialize <<<"

print_codextop_banner() {
  local cols padding line
  cols="$(tput cols 2>/dev/null || printf '80')"
  while IFS= read -r line; do
    padding=$(( (cols - ${#line}) / 2 ))
    if (( padding < 0 )); then
      padding=0
    fi
    printf '%*s%s\n' "$padding" "" "$line"
  done <<'EOF'
 ██████╗ ██████╗ ██████╗ ███████╗██╗  ██╗████████╗ ██████╗ ██████╗
██╔════╝██╔═══██╗██╔══██╗██╔════╝╚██╗██╔╝╚══██╔══╝██╔═══██╗██╔══██╗
██║     ██║   ██║██║  ██║█████╗   ╚███╔╝    ██║   ██║   ██║██████╔╝
██║     ██║   ██║██║  ██║██╔══╝   ██╔██╗    ██║   ██║   ██║██╔═══╝
╚██████╗╚██████╔╝██████╔╝███████╗██╔╝ ██╗   ██║   ╚██████╔╝██║
 ╚═════╝ ╚═════╝ ╚═════╝ ╚══════╝╚═╝  ╚═╝   ╚═╝    ╚═════╝ ╚═╝
EOF
}

mkdir -p "$(dirname -- "$ZSHRC")"
mkdir -p "$BIN_DIR"
touch "$ZSHRC"

SNIPPET="$(cat <<EOF
$MARKER_BEGIN
export CODEXTOP_HOME="$REPO_DIR"
export CODEXTOP_CODEX_DIR="\${CODEXTOP_CODEX_DIR:-\$HOME/.codex}"

CODEXTOP() {
  command python3 "\$CODEXTOP_HOME/src/codextop/codextop.py" "\$@"
}

CHECK_CODEX_QUOTA() {
  command python3 "\$CODEXTOP_HOME/src/codextop/check_codex_quota.py" "\$@"
}

CODEXAUTH() {
  command python3 "\$CODEXTOP_HOME/src/codextop/codex_auth.py" "\$@"
}

SWITCH_CODEX_PROVIDER() {
  command python3 "\$CODEXTOP_HOME/src/codextop/codex_auth.py" "\$@"
}

if [[ -x "\$CODEXTOP_HOME/scripts/start_codextop_backend.sh" ]]; then
  "\$CODEXTOP_HOME/scripts/start_codextop_backend.sh" >/dev/null 2>&1
fi
$MARKER_END
EOF
)"

python3 - "$ZSHRC" "$MARKER_BEGIN" "$MARKER_END" "$SNIPPET" <<'PY'
from __future__ import annotations

import sys
from pathlib import Path

zshrc = Path(sys.argv[1])
begin = sys.argv[2]
end = sys.argv[3]
snippet = sys.argv[4].rstrip() + "\n"

content = zshrc.read_text(encoding="utf-8") if zshrc.exists() else ""
start = content.find(begin)
if start >= 0:
    stop = content.find(end, start)
    if stop >= 0:
        stop += len(end)
        content = content[:start].rstrip() + "\n\n" + snippet + content[stop:].lstrip("\n")
    else:
        content = content.rstrip() + "\n\n" + snippet
else:
    content = content.rstrip() + "\n\n" + snippet
zshrc.write_text(content, encoding="utf-8")
PY

write_wrapper() {
  local name="$1"
  local module="$2"
  local target="$BIN_DIR/$name"
  cat > "$target" <<EOF
#!/usr/bin/env bash
export CODEXTOP_HOME="$REPO_DIR"
export CODEXTOP_CODEX_DIR="\${CODEXTOP_CODEX_DIR:-\${CODEX_HOME:-\$HOME/.codex}}"
exec python3 "$REPO_DIR/src/codextop/$module.py" "\$@"
EOF
  chmod +x "$target"
}

write_wrapper CODEXTOP codextop
write_wrapper CHECK_CODEX_QUOTA check_codex_quota
write_wrapper CODEXAUTH codex_auth
write_wrapper SWITCH_CODEX_PROVIDER codex_auth

"$REPO_DIR/scripts/start_codextop_backend.sh"
echo "Installed CODEXTOP shell hook in $ZSHRC"
echo "Installed CODEXTOP commands in $BIN_DIR"
print_codextop_banner
