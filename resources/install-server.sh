#!/usr/bin/env bash
set -e

DATA_DIR="$(cd "${0%/*}" && echo "$PWD")"

PLATFORM="{{ PLATFORM }}"
COMMIT="{{ COMMIT }}"

VSCODE_DIR="$HOME/.vscode-server"
VSCODE_SERVER="$VSCODE_DIR/cli/servers/Stable-$COMMIT/server"
VSCODE_BIN="$VSCODE_SERVER/bin/code-server"
VSCODE_SERVER_LEGACY="$VSCODE_DIR/bin/$COMMIT"
VSCODE_CLI="$VSCODE_DIR/code-$COMMIT"

DATA_CLI_TAR="$DATA_DIR/cli-$PLATFORM/$COMMIT/vscode-cli-$PLATFORM-cli.tar.gz"
DATA_SERVER_TAR="$DATA_DIR/server-$PLATFORM/$COMMIT/server-$PLATFORM.tar.gz"
DATA_EXTENSION_DIR="$DATA_DIR/extensions"

if [[ ! -f "$VSCODE_CLI" ]]; then
	tempdir=$(mktemp -d)
	tar xzf "$DATA_CLI_TAR" -C "$tempdir"
	mv "$tempdir/code" "$VSCODE_CLI"
	rmdir "$tempdir"
fi

if [[ ! -d "$VSCODE_SERVER" ]]; then
	tempdir=$(mktemp -d)
	tar xzf "$DATA_SERVER_TAR" -C "$tempdir"
	mkdir -p "$(dirname -- "$VSCODE_SERVER")"
	mv "$tempdir/vscode-server-$PLATFORM" "$VSCODE_SERVER"
fi

if [[ -d "$VSCODE_SERVER_LEGACY" && ! -L "$VSCODE_SERVER_LEGACY" ]]; then
	rm -r "$VSCODE_SERVER_LEGACY"
fi

if [[ ! -L "$VSCODE_SERVER_LEGACY" ]]; then
	mkdir -p "$(dirname -- "$VSCODE_SERVER_LEGACY")"
	ln -s "$VSCODE_SERVER" "$VSCODE_SERVER_LEGACY"
fi

extensions=$("$VSCODE_BIN" --list-extensions --show-versions | tr @ -)

install_count=0

for vsix in "$DATA_EXTENSION_DIR"/*.vsix; do
	vsix_name="${vsix##*/}"
	if [[ "$vsix_name" = *"@"* && "$vsix_name" != *"@$PLATFORM.vsix" ]]; then
		continue
	fi
	vsix_id=$(echo "$vsix_name" | sed -e "s/@$PLATFORM//g" -e 's/\.vsix$//gm')

	if ! grep -xiq "$vsix_id" <<<"$extensions"; then
		for i in {1..3}; do
			if "$VSCODE_BIN" --install-extension "$vsix" 2>/dev/null; then
				install_count=$((install_count + 1))
				break
			else
				if [[ "$i" -lt 3 ]]; then
					echo "Failed to install ${vsix_name}. Trying again..."
					sleep 1
				else
					echo "Failed to install ${vsix_name}" >&2
				fi
			fi
		done
	fi
done

echo "Sucessfully installed or updated $install_count extensions."
