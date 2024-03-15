This directory contains all the files needed to use vscode offline.

To install, follow these steps:

1. Run win32-x64-user/VSCodeUserSetup-x64-{{ VERSION }}.exe to ensure you are using
the correct version of vscode.

2. Install all the extensions in the extensions folder by drag and dropping
onto the vscode extensions panel or running code --install-extension
extensions/*.vsix

3. Extract the vscode-server-linux-x64 folder from
server-linux-x64/{{ COMMIT }}/server-linux-x64.tar.gz to
~/.vscode-server/bin/{{ COMMIT }} on the remote server.

3a. Alternatively, you can run the script 'install-server.sh' to automate
installing both the server and all extensions. Only updated or new extensions
will be installed.

=======

This archive contains the following files
