This directory contains all the files needed to use vscode offline.

To install, follow these steps:

1. Install dist/{{ CLIENT_DIST }} to ensure you are using the
correct version of vscode.

2. Install all the extensions in the extensions folder by drag and dropping
onto the vscode extensions panel or running code --install-extension
extensions/*.vsix

3. Extract dist/{{ COMMIT }}/{{ SERVER_DIST }}
to ~/{{ SERVER_HOME }}/bin/{{ COMMIT }}
on the remote server.

3a. Alternatively, you can run the script 'install-server.sh' to automate
installing both the server and all extensions. Only updated or new extensions
will be installed.

=======

This archive contains the following files
