#!/usr/bin/env bash
# Script to download all necessary files to use vscode offline.
#
# The following will be downloaded:
# - The current version of the vscode user installer for windows
# - The extension vsix files currently installed in vscode
# - The vscode-server for the currently installed version
#
# It supports downloading files directly from open-vsx.org and then microsoft as a fallback.
set -euo pipefail

extensions_only=0
while [ $# -gt 0 ]; do
	arg=$1
	shift
	case "$arg" in
	--extensions-only | -x)
		extensions_only=1
		;;
	--help | -h)
		echo "Usage: $0 [OPTIONS]"
		echo "Options:"
		echo "  --extensions-only, -x : Only download and package extensions"
		echo "  --help, -h            : Display this help text and exit"
		exit 0
		;;
	*)
		echo "Usage: $0 [OPTIONS]"
		echo "Run '$0 --help' for help"
		exit 1
		;;
	esac
done

root="$(cd "${0%/*}" && echo "$PWD")"
resources=$root/resources
workdir=$root/vscode-extensions

if [ -z "$(type -p jq)" ]; then
	echo "Error: jq is required!" >&2
	exit 2
fi

if [ -z "$(type -p dos2unix)" ]; then
	dos2unix() {
		sed -e 's/\r//g'
	}
fi

if [[ "$OSTYPE" =~ cygwin|msys|win32 ]]; then
	jq() {
		# jq on windows outputs dos line endings
		command jq "$@" | dos2unix
	}
fi

if [ -n "${WSL_DISTRO_NAME:-}" ]; then
	# default code cli in WSL won't show client side extensions

	export WSLENV="ELECTRON_RUN_AS_NODE/w:$WSLENV"
	# don't run vscode-server's code exe
	TEMP_PATH=$(echo "$PATH" | tr : $'\n' | grep -v .vscode-server | tr $'\n' :)
	VSCODE_PATH="$(dirname "$(dirname "$(realpath "$(PATH="$TEMP_PATH" which code)")")")"
	ELECTRON="$VSCODE_PATH/Code.exe"
	CLI=$(wslpath -m "$VSCODE_PATH/resources/app/out/cli.js")

	code() {
		ELECTRON_RUN_AS_NODE=1 "$ELECTRON" "$CLI" "$@" | dos2unix
	}
fi

mkdir -p "$workdir"

####################
# helper functions #
####################

pushcd() {
	pushd "$1" >/dev/null
}
popcd() {
	popd >/dev/null
}

safecd() {
	mkdir -p "$1"
	pushcd "$1"
}

download() {
	local args=()
	if [ -n "$2" ]; then
		args+=(-o "$2")
		mkdir -p "$(dirname "$2")"
		echo "Downloading $2"
	else
		args+=(-O)
		echo "Downloading $(basename "$1")"
	fi
	curl -fsSL "$1" "${args[@]}"
}

cleanup() {
	local target=$1
	shift
	for file in "$@"; do
		if [ "$file" != "$target" ]; then
			echo "Removing $file"
			rm -rf -- "$file"
		fi
	done
}

download_dist() {
	dist=$1
	file="$2"
	safecd "$dist"
	url="https://update.code.visualstudio.com/commit:$commit/$dist/stable"
	if ! [ -f "$file" ]; then
		download "$url" "$file"
	fi
	cleanup "./${file%%/*}" ./*
	popcd
}

copy_resource() {
	local file=$1
	shift
	data=$(cat "$resources/$file")
	while [[ "$#" -gt 0 ]]; do
		IFS='=' read -r name value <<<"$1"
		data=${data//\{\{ $name \}\}/"$value"}
		shift
	done
	echo "$data" >"$file"
}

#########
# setup #
#########

# version, commit, architecture
read -r version commit _ <<<"$(code --version | xargs)"

# publisher.name@version
extensions=$(code --list-extensions --show-versions)

prepend_url_filename() {
	local line
	while read -r line; do
		echo "${line##*/} $line"
	done
}

fetch_download_urls() {
	local spec=$1 pub name vers meta metaurl
	vers="${spec#*@}"
	name="${spec%@*}"
	pub="${name%.*}"
	name="${name#*.}"

	if [[ -f "$pub.$name-$vers.vsix" ]]; then
		echo "$pub.$name-$vers.vsix"
		return
	elif [[ 
		-f "$pub.$name-$vers@win32-x64.vsix" &&
		-f "$pub.$name-$vers@linux-x64.vsix" ]] \
		; then
		echo "$pub.$name-$vers@linux-x64.vsix"
		echo "$pub.$name-$vers@win32-x64.vsix"
		return
	fi
	metaurl="https://open-vsx.org/api/$pub/$name/$vers"
	meta=$(curl -sSL "$metaurl")
	if [[ "$(jq -r 'has("error")' <<<"$meta")" == false ]]; then
		result=$(jq -r '.downloads | (.universal // (.["win32-x64"], .["linux-x64"]))' <<<"$meta" | prepend_url_filename)
		if [[ "$result" == *"null"* ]]; then
			echo "$spec download is null" >&2
			jq .downloads <<<"$meta" >&2
			return
		fi
		echo "$result"
	else
		echo "$pub.$name-$vers.vsix https://$pub.gallery.vsassets.io/_apis/public/gallery/publisher/$pub/extension/$name/$vers/assetbyname/Microsoft.VisualStudio.Services.VSIXPackage"
	fi
}

fetch_downloads() {
	while read -r spec; do
		fetch_download_urls "$spec"
	done <<<"$extensions"
}

download_installer() {
	if [ "$extensions_only" = 1 ]; then
		echo "Skipping installer download" >&2
		return
	fi
	download_dist "win32-x64-user" "VSCodeUserSetup-x64-$version.exe"
}

download_server() {
	if [ "$extensions_only" = 1 ]; then
		echo "Skipping server download" >&2
		return
	fi
	download_dist "server-linux-x64" "$commit/server-linux-x64.tar.gz"
}

download_cli() {
	if [ "$extensions_only" = 1 ]; then
		echo "Skipping cli download" >&2
		return
	fi
	download_dist "cli-linux-x64" "$commit/vscode-cli-linux-x64-cli.tar.gz"
}

download_extensions() {
	safecd "extensions"

	local all_exts=(*.vsix)

	downloads="$(fetch_downloads)"

	while read -r file url; do
		if [[ ! -f "$file" ]]; then
			if [[ "$url" != "https://open-vsx.org"* ]]; then
				echo "$file is not available on openvsx. Downloading from microsoft." >&2
			fi
			file="${file,,}"
			download "$url" "$file" || echo "Failed to download $file"
		fi
		all_exts=("${all_exts[@]/$file/}")
	done <<<"$downloads"

	cleanup "" "${all_exts[@]}"
	popcd
}

write_readme() {
	copy_resource README.txt \
		VERSION="$version" \
		COMMIT="$commit"

	echo "$*" | xargs ls -lh | awk '{ printf "%s %s\n", $5, $9 }' | column -t >>README.txt
}

mkdir -p "$workdir"
cd "$workdir"

download_installer
download_extensions
download_cli
download_server

copy_resource install-server.sh \
	COMMIT="$commit" \
	PLATFORM="linux-x64"
chmod +x install-server.sh

declare -a archiving_files=(
	README.txt
	extensions/*.vsix
)
if [ "$extensions_only" = 0 ]; then
	archiving_files+=(
		install-server.sh
		"win32-x64-user/VSCodeUserSetup-x64-$version.exe"
		"server-linux-x64/$commit/server-linux-x64.tar.gz"
		"cli-linux-x64/$commit/vscode-cli-linux-x64-cli.tar.gz"
	)
fi

write_readme "${archiving_files[@]}"

archive_file="../vscode-extensions.tar.gz"

tar czf "$archive_file" "${archiving_files[@]}"

echo "Wrote archive to $(readlink -f "$archive_file")"
