sshPilot
========

SSH connection manager with integrated terminal, tunneling, key management and scp upload support.

<img width="1260" height="833" alt="Main window" src="https://github.com/user-attachments/assets/743bb1fb-22de-4537-ba91-775cea48d57a" />

<img width="722" height="822" alt="Connection settings" src="https://github.com/user-attachments/assets/55fad9a6-9d4d-4c15-bfac-8c19c6df15c5" />



Features

- Load/save standard .ssh/config entries (it loads you current configuration)
- Full support for Local, Remote and Dynamic port forwarding 
- Intuitive, minimal UI with keyboard navigation and shortcuts 
-- Press ctrl+L to quickly switch between hosts), close tabs with ctrl+w and move between tabs with alt+right/left arrow
- SCP support for quicly uploading a file to remote server
- Generate keypairs and add them to remote servers
- Toggle to show/hide ip addresses/hostnames in main UI
- Light/Dark themes
- Customizable terminal font and color schemes
- Free software (GPL v3 license)


Runtime dependencies
--------------------

Install system GTK/libadwaita/VTE GI bindings (do not use pip for these).

Debian/Ubuntu (minimum versions)
~~~~~~~~~~~~~

```
sudo apt update
sudo apt install \
  python3-gi python3-gi-cairo \
  libgtk-4-1 (>= 4.6) gir1.2-gtk-4.0 (>= 4.6) \
  libadwaita-1-0 (>= 1.4) gir1.2-adw-1 (>= 1.4) \
  libvte-2.91-gtk4-0 (>= 0.70) gir1.2-vte-3.91 (>= 0.70) \
  sshpass python3-paramiko python3-cryptography python3-secretstorage python3-matplotlib
# Optional for keyring
sudo apt install gnome-keyring libsecret-1-0
```

Fedora
~~~~~~

```
sudo dnf install \
  python3-gobject \
  gtk4 gtk4-libadwaita \
  vte291-gtk4 \
  openssh-clients sshpass \
  python3-paramiko python3-cryptography python3-secretstorage python3-matplotlib
# Optional keyring (GNOME)
sudo dnf install gnome-keyring libsecret
```

Run from source
---------------

```
python3 run.py
```

Build Debian package
--------------------

```
./build-deb.sh
# Install the generated deb from the parent directory
sudo dpkg -i ../sshpilot_*_all.deb || sudo apt -f install
```


