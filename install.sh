#!/bin/sh
# Puts a `heavy` command on your PATH that runs this copy of HeavyGate. Nothing runs as root.
#   ./install.sh [bin folder]        default: ~/.local/bin
set -eu
here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
bin=${1:-"$HOME/.local/bin"}
command -v python3 >/dev/null || { echo "HeavyGate needs python3 (3.9 or newer)" >&2; exit 1; }
python3 -c 'import sys; sys.exit(sys.version_info < (3, 9))' || { echo "HeavyGate needs Python 3.9 or newer" >&2; exit 1; }
mkdir -p "$bin"
printf '#!/bin/sh\nexec python3 "%s/heavygate.py" "$@"\n' "$here" > "$bin/heavy"
chmod +x "$bin/heavy"
echo "Installed $bin/heavy"
case ":$PATH:" in
  *":$bin:"*) ;;
  *) echo "Add it to your PATH:  export PATH=\"$bin:\$PATH\"" ;;
esac
