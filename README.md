<p align="center">
<img width="154" height="154" alt="logo" src="https://github.com/user-attachments/assets/42b73dbf-778c-45ff-9361-22a52988f1b3" />
</p>

<p align="center">
<a href='https://flathub.org/apps/io.github.mfat.sshpilot'>
    <img width='240' alt='Get it on Flathub' src='https://flathub.org/api/badge?locale=en'/>
  </a>
</p>

**sshPilot** is a user-friendly, modern and lightweight SSH connection manager for Linux and macOS, with an integrated terminal. 

## Screenshots

<!-- Hero -->
<p align="center">
  <img src="screenshots/main-window.png" width="900" alt="SSH Pilot — main window">
</p>

<p align="center">
  <em>Tabbed terminal, connection list, and quick actions.</em>
</p>

---

## Feature Highlights

<table>
  <tr>
    <td align="center" valign="top">
      <img src="screenshots/tabs-overview.png" width="560" alt="Tabs overview">
      <br><strong>Tab Overview</strong><br>
      Jump between multiple SSH sessions without losing context.
    </td>
    <td align="center" valign="top">
      <img src="screenshots/openwrt-terminal.png" width="560" alt="Built-in terminal">
      <br><strong>Built-in Terminal</strong><br>
      Fast VTE terminal with keyboard-first workflow.
    </td>
  </tr>
  <tr>
    <td align="center" valign="top">
      <img src="screenshots/key-copy-dialog.png" width="560" alt="Key copy dialog">
      <br><strong>Key Generation & Copy</strong><br>
      Create keys and deploy them with one click.
    </td>
    <td align="center" valign="top">
      <img src="screenshots/port-forwarding.png" width="560" alt="Port forwarding UI">
      <br><strong>Port Forwarding</strong><br>
      Manage local/remote/dynamic tunnels visually.
    </td>
  </tr>
</table>



## Features

- Tabbed interface
- Intuitive, minimal UI with keyboard navigation and shortcuts
- File management using SFTP
- Organize servers in groups
- Option to use the built-in terminal or your favorite one
- Broadcast commands to all open tabs
- Full support for Local, Remote and Dynamic port forwarding 
- SCP support for quickly uploading or downloading files to/from remote servers
- Keypair generation and copying to remote servers (ssh-copy-id)
- Support for running remote and local commands upon login
- Secure storage for credentials via libsecret on Linux; no secret (password or passphrase) is copied to clipboard or saved to plain text
- Privacy toggle to show/hide ip addresses/hostnames in the main window
- Light/Dark interface themes
- Customizable terminal font and color schemes
- Load/save standard .ssh/config entries (Or use dedicated configuration file)
- Free software (GPL v3 license)







## Download

- ### DEB/RPM/Flatpak
Latest release can be downloaded from here: https://github.com/mfat/sshpilot/releases/

- ### Arch linux
Arch linux package via AUR: https://aur.archlinux.org/packages/sshpilot

- ### macOS (aarch64)
Download the dmg file from the releases section https://github.com/mfat/sshpilot/releases/

- ### Run from source
You can also run the app from source. Install the modules listed in requirements.txt and a fairly recent version of GNOME and it should run.

`
python3 run.py
`

To enable verbose debugging output, run the app with the `--verbose` flag:

`
python3 run.py --verbose
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
  libsecret-1-0 gir1.2-secret-1 \
  python3-paramiko python3-cryptography sshpass ssh-askpass
```

Fedora / RHEL / CentOS


```
sudo dnf install \
  python3 python3-gobject \
  gtk4 libadwaita \
  vte291-gtk4 \
  libsecret \
  python3-paramiko python3-cryptography sshpass openssh-askpass
```

libsecret handles secure credential storage on Linux via the Secret Service API.

Run from source


```
python3 run.py
```

Enable verbose debugging with:

```
python3 run.py --verbose
```



## Keyboard/mouse navigation and shortcuts

sshPilot is easy to navigate with keyboard. When the app starts up, just press enter to connect to the first host in the list. You can do the same thing by double-clicking the host.
Press Ctrl (⌘ on macOS)+L to quickly switch between hosts, close tabs with Ctrl (⌘)+F4 and switch tabs with Alt+Right/Left arrow.
If you have multiple connections to a single host, doble-clicking the host will cycle through all its open tabs.

## Special Thanks

- [Elibugy](https://www.linkedin.com/in/elham-hesaraki) as the primary sponsor of the project
- Behnam Tavakkoli, Chalist and Kalpase, Ramin Najjarbashi, Farid and Narbeh for testing
- Icon designed by [Blisterexe](https://github.com/Blisterexe)

## Support development
Bitcoin: bc1qqtsyf0ft85zshsnw25jgsxnqy45rfa867zqk4t

Doge: DRzNb8DycFD65H6oHNLuzyTzY1S5avPHHx
