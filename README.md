<p align="center">
<img width="154" height="154" alt="logo" src="https://github.com/user-attachments/assets/42b73dbf-778c-45ff-9361-22a52988f1b3" />
</p>

**sshPilot** is a user-friendly, modern and lightweight SSH connection manager for Linux, with an integrated terminal. It's a free (as in freedom) alternative to Putty and Termius.

<img width="1057" height="705" alt="Screenshot From 2025-08-20 18-32-09" src="https://github.com/user-attachments/assets/f57b25a9-c3ce-4355-891e-caad17a906f9" />

<img width="1212" height="778" alt="Screenshot From 2025-08-15 01-22-02" src="https://github.com/user-attachments/assets/6b79a06a-d900-49eb-969f-a8f7a4c31b02" />

<img width="762" height="995" alt="Screenshot From 2025-08-15 01-18-57" src="https://github.com/user-attachments/assets/aec20f9a-1fb5-44bb-a13a-bb5a36445431" />

<img width="722" height="622" alt="Screenshot From 2025-08-15 01-17-38" src="https://github.com/user-attachments/assets/b72fe4df-f5ac-48e2-9ba0-af728901e1c8" />

<img width="562" height="569" alt="Screenshot From 2025-08-20 13-49-59" src="https://github.com/user-attachments/assets/eb8de65b-ce0e-449e-a7e3-dcc6bf1e43bb" />


## Features

- Tabbed interface
- Intuitive, minimal UI with keyboard navigation and shortcuts
- File management in standard file managers using SFTP
- Organize servers in groups
- Option to use the built-in terminal or your favorite one
- Broadcast commands to all open tabs
- Full support for Local, Remote and Dynamic port forwarding 
- SCP support for quicly uploading a file to remote server
- Keypair generation and copying to remote servers (ssh-copy-id)
- Support for running remote and local commands upon login
- Secure storage for credentials, no secret (password or passphrase) is copied to clipboard or saved to plain text
- Privacy toggle to show/hide ip addresses/hostnames in the main window
- Light/Dark interface themes
- Customizable terminal font and color schemes
- Load/save standard .ssh/config entries
- Free software (GPL v3 license)







## Download

- ### DEB/RPM/Flatpak
Latest release can be downloaded from here: https://github.com/mfat/sshpilot/releases/

- ### Arch linux
Arch linux package via AUR: https://aur.archlinux.org/packages/sshpilot

- ### macOS (not extensively tested)
(WIP) On the [Mac branch](https://github.com/mfat/sshpilot/tree/mac) there are [instructions](https://github.com/mfat/sshpilot/blob/mac/INSTALL-macos.md) for running sshPilot on macOS

- ### Run from source
You can also run the app from source. Install the modules listed in requirements.txt and a fairly recent version of GNOME and it should run.

`
python3 run.py
`




Runtime dependencies
--------------------

Install system GTK/libadwaita/VTE GI bindings (do not use pip for these).

Debian/Ubuntu (minimum versions)

```
sudo apt update
sudo apt install \
  python3 python3-gi python3-gi-cairo \
  libgtk-4-1 (>= 4.6) gir1.2-gtk-4.0 (>= 4.6) \
  libadwaita-1-0 (>= 1.4) gir1.2-adw-1 (>= 1.4) \
  libvte-2.91-gtk4-0 (>= 0.70) gir1.2-vte-3.91 (>= 0.70) \
  python3-paramiko python3-cryptography python3-secretstorage sshpass ssh-askpass
```

Fedora / RHEL / CentOS


```
sudo dnf install \
  python3 python3-gobject \
  gtk4 libadwaita \
  vte291-gtk4 \
  libsecret \
  python3-paramiko python3-cryptography python3-secretstorage sshpass openssh-askpass
```

Run from source


```
python3 run.py
```



## Keyboard/mouse navigation and shortcuts

sshPilot is easy to navigate with keyboard. When the app starts up, just press enter to connect to the first host in the list. You can do the same thing by double-clicking the host.
Press ctrl+L to quickly switch between hosts, close tabs with ctrl+F4 and switch tabs with alt+right/left arrow.
If you have multiple connections to a single host, doble-clicking the host will cycle through all its open tabs.

## Special Thanks

- [Elibugy](https://www.linkedin.com/in/elham-hesaraki) as the primary sponsor of the project
- Behnam Tavakkoli, Chalist and Kalpase for testing
- Icon designed by [Blisterexe](https://github.com/Blisterexe)

## Support development
Bitcoin: bc1qqtsyf0ft85zshsnw25jgsxnqy45rfa867zqk4t

Doge: DRzNb8DycFD65H6oHNLuzyTzY1S5avPHHx
