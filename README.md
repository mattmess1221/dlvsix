# dlvsix

This project is intended to automate packaging and installing of vscode,
vscode-server, and extensions for use in an offline environment.

## Requirements

Usage requires an internet connected computer with VSCode installed, with all extensions you wish to install to the offline host.

The following requirements are needed on the online host.

- Bash via WSL, cygwin, or msysgit
- [vscode](https://code.visualstudio.org)
- [jq](https://jqlang.github.io/jq/)
- [curl](https://curl.se/)

No additional dependencies are required on the offline host. (only bash, tar, sed, and grep are used)

## Usage

On the online host, run `./dlvsix.sh` with no arguments from a bash session to start the download. All downloaded files will be in the `vscode-extensions/` directory.

Additionally, the folder will be archived to `vscode-extensions.tar.gz` as a convenience. This file should be transfered to the offline host using any method available.

On the offline host, extract `vscode-extensions.tar.gz` using the `tar` command. This will create the folder `vscode-extensions`.

```sh
tar xzf vscode-extensions.tar.gz
```

### Client

From here on for the client, installation is straight-forward. Simply run the vscode user setup from `win32-x64-user/` as on any other host.

After installing, you can drag + drop the files from the `extensions/` directory to the extensions sidebar inside vscode.

**Note**: Some extensions may have been downloaded for multiple platforms. Be sure to just install your platform's extension. **VSCode will not warn you** when you install the wrong one.

### Server

If you are running vscode-server with the Remote Development extension, vscode will try to download the code server from Microsoft. This obviously won't work offline.

Luckily, the `dlvsix.sh` script downloaded the server too. There is even a script `install-server.sh` which gets generated inside `vscode-extensions`.

This script will automatically extract the downloaded vscode server to `~/.vscode-server/bin/` and install all the extensions. Afterwards, you will be able to connect to the remote host in vscode.
