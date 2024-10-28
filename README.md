# dlvsix

This project is intended to automate packaging and installing of vscode,
vscode-server, and extensions for use in an offline environment.

## Requirements

Usage requires an internet connected computer with VSCode installed, with all extensions you wish to install to the offline host.

The following requirements are needed on the online host.

- [Python 3.9+](https://www.python.org/downloads)
- [VS Code](https://code.visualstudio.org) (derivatives are untested).

No additional dependencies are required on the offline host. (only bash, tar, sed, and grep are used for automated installs)

## Usage

On the online host, run `python3 ./dlvsix.py` with no arguments from a bash session to start the download. All downloaded files will be in the `vscode-extensions/` directory.

Additionally, the folder will be archived to `vscode-extensions.zip` as a convenience. This file should be transfered to the offline host using any method available.

On the offline host, extract `vscode-extensions.zip` using the `unzip` command. This will create the folder `vscode-extensions`.

```sh
unzip vscode-extensions.zip
```

### Automated installation

After extracting the zip file on the offline host, you can run the install scripts for an automated install process.

- `install-server.sh`: Installs the vscode server.
- `install-extensions.py`: Installs the extensions in bulk, both client and server (Requires `--install-server`). This is faster than installing each extension individually.

If you wish to install manually, continue reading.

### Client

From here on for the client, installation is straight-forward. Simply run the vscode user setup from `dist/[COMMIT]/` as on any other host.

After installing, you can drag + drop the files from the `extensions/` directory to the extensions sidebar inside vscode.

**Note**: Some extensions may have been downloaded for multiple platforms. Be sure to just install your platform's extension. **VSCode will not warn you** when you install the wrong one.

### Server

If you are running vscode-server with the Remote Development extension, vscode will try to download the code server from Microsoft. This obviously won't work offline.

The server will also be downloaded if you have a remote development extension installed. `install-server.sh` will also generate inside `vscode-extensions`. This script will automatically extract the downloaded vscode server to `~/.vscode-server/bin/` and install all the extensions. Afterwards, you will be able to connect to the remote host in vscode.
