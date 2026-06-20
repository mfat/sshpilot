"""SSH Key Audit — key age, type, and certificate expiry at a glance.

A non-protocol sshPilot plugin. Scans ``~/.ssh`` for public keys and
certificates and shows each one's age, type/size, fingerprint, comment, and
(for certs) expiry — so "which keys are older than a year?" and "when does this
cert expire?" are one page away. A security-hygiene companion to the Host Health
Dashboard.

It reads only ``*.pub`` / ``*-cert.pub`` files and ``ssh-keygen`` metadata —
**never private key contents**. "Age" is the public-key file's modification time
(a proxy for when the key was generated).

Capabilities exercised (all from ``sshpilot.plugins.api``):
* a UI page (``ctx.ui.register_page``) + a startup warning toast (``ctx.events``)
* per-plugin settings (``ctx.settings``) for the age threshold
* running ``ssh-keygen`` (process) and reading ``~/.ssh`` (filesystem)

Pure parsing/age logic has no GTK import and is unit-tested without a display;
``gi`` is imported lazily inside the page factory.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import threading
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from sshpilot.plugins.api import Events, PluginContext, SshPilotPlugin

logger = logging.getLogger(__name__)

DEFAULT_THRESHOLD_DAYS = 365
_FP_RE = re.compile(r"^\s*(\d+)\s+(\S+)\s+(.*?)\s+\(([^)]+)\)\s*$")


# --- Flatpak helpers --------------------------------------------------------

def _is_flatpak() -> bool:
    return bool(os.environ.get("FLATPAK_ID")) or os.path.exists("/.flatpak-info")


# --- pure logic (no GTK) ----------------------------------------------------

def parse_fingerprint(line: str) -> Optional[Dict[str, Any]]:
    """Parse one ``ssh-keygen -lf`` line: ``<bits> <fp> <comment> (<TYPE>)``.

    The comment may contain spaces (or be empty); the type is the trailing
    parenthesised token. Returns None if the line doesn't match."""
    match = _FP_RE.match(line or "")
    if not match:
        return None
    bits, fingerprint, comment, key_type = match.groups()
    try:
        bits_int = int(bits)
    except ValueError:
        return None
    return {
        "bits": bits_int,
        "fingerprint": fingerprint,
        "comment": comment.strip(),
        "type": key_type.strip(),
    }


def parse_cert_validity(text: str) -> Optional[Dict[str, Any]]:
    """Parse the ``Valid:`` line from ``ssh-keygen -L -f``. Returns
    ``{"forever": bool, "valid_from": str|None, "valid_to": str|None}`` or None
    if there's no validity line."""
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line.startswith("Valid:"):
            continue
        rest = line[len("Valid:"):].strip()
        if rest.lower() == "forever":
            return {"forever": True, "valid_from": None, "valid_to": None}
        # "from <ts> to <ts>"
        match = re.match(r"from\s+(\S+)\s+to\s+(\S+)", rest)
        if match:
            return {"forever": False,
                    "valid_from": match.group(1), "valid_to": match.group(2)}
        return None
    return None


def _parse_ts(value: str) -> Optional[datetime]:
    if not value:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def cert_expiry_days(valid_to: Optional[str], now: Optional[datetime] = None) -> Optional[int]:
    """Days until a cert's ``valid_to`` timestamp (negative if already expired).
    None if unparseable / forever."""
    ts = _parse_ts(valid_to or "")
    if ts is None:
        return None
    now = now or datetime.now()
    return int((ts - now).total_seconds() // 86400)


def age_days(mtime_epoch: float, now_epoch: Optional[float] = None) -> int:
    now_epoch = time.time() if now_epoch is None else now_epoch
    return max(0, int((now_epoch - mtime_epoch) // 86400))


def classify(age: int, threshold: int) -> str:
    return "old" if age >= max(1, threshold) else "ok"


# --- plugin -----------------------------------------------------------------

class Plugin(SshPilotPlugin):
    def activate(self, ctx: PluginContext) -> None:
        self.ctx = ctx
        self._threshold = self._read_threshold()
        self._stop = threading.Event()
        self._list_box = None
        self._status_label = None
        self._threshold_entry = None
        self._warn_row = None

        ctx.ui.register_page(
            "keyaudit", "Key Audit", "channel-secure-symbolic", self._build_page)
        # Always subscribe; whether to actually warn is checked at event time so
        # the UI toggle takes effect without needing a re-subscribe.
        ctx.events.subscribe(Events.APP_STARTED, self._on_app_started)

    def deactivate(self) -> None:
        self._stop.set()
        logger.info("key-audit: deactivate")

    def _read_threshold(self) -> int:
        try:
            return max(1, int(self.ctx.settings.get("threshold_days",
                                                    DEFAULT_THRESHOLD_DAYS)))
        except (TypeError, ValueError):
            return DEFAULT_THRESHOLD_DAYS

    # --- startup warning --------------------------------------------------
    def _on_app_started(self, _payload) -> None:
        if not self.ctx.settings.get("warn_on_start", True):
            return

        def worker():
            keys = self._scan()
            old = [k for k in keys if k.get("status") == "old"]
            if old and not self._stop.is_set():
                self.ctx.run_on_ui_thread(
                    self.ctx.ui.notify,
                    f"{len(old)} SSH key(s) older than {self._threshold} days")
        threading.Thread(target=worker, daemon=True).start()

    # --- scanning (filesystem + ssh-keygen; impure) -----------------------
    def _ssh_dir(self) -> str:
        return os.path.join(os.path.expanduser("~"), ".ssh")

    def _keygen_run(self, args: List[str]) -> Optional[str]:
        keygen = shutil.which("ssh-keygen")
        argv: List[str]
        if keygen:
            argv = [keygen, *args]
        elif _is_flatpak() and shutil.which("flatpak-spawn"):
            argv = ["flatpak-spawn", "--host", "ssh-keygen", *args]
        else:
            return None
        try:
            result = subprocess.run(
                argv, capture_output=True, text=True, timeout=10, check=False,
                stdin=subprocess.DEVNULL)
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            return None
        return result.stdout if result.returncode == 0 else None

    def _scan(self) -> List[Dict[str, Any]]:
        ssh_dir = self._ssh_dir()
        keys: List[Dict[str, Any]] = []
        try:
            names = sorted(os.listdir(ssh_dir))
        except OSError:
            return keys
        now = time.time()
        for name in names:
            if not name.endswith(".pub"):
                continue
            path = os.path.join(ssh_dir, name)
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                continue
            age = age_days(mtime, now)
            entry: Dict[str, Any] = {
                "name": name,
                "age_days": age,
                "status": classify(age, self._threshold),
                "is_cert": name.endswith("-cert.pub"),
                "bits": None, "type": "?", "fingerprint": "", "comment": "",
                "cert_expiry_days": None, "cert_valid_to": None,
            }
            fp_out = self._keygen_run(["-lf", path])
            if fp_out:
                parsed = parse_fingerprint(fp_out.strip().splitlines()[0]
                                           if fp_out.strip() else "")
                if parsed:
                    entry.update(parsed)
            if entry["is_cert"]:
                cert_out = self._keygen_run(["-L", "-f", path])
                validity = parse_cert_validity(cert_out or "")
                if validity and not validity["forever"]:
                    entry["cert_valid_to"] = validity["valid_to"]
                    entry["cert_expiry_days"] = cert_expiry_days(
                        validity["valid_to"])
            keys.append(entry)
        return keys

    # --- UI (gi imported lazily) ------------------------------------------
    def _build_page(self):
        import gi
        gi.require_version("Gtk", "4.0")
        gi.require_version("Adw", "1")
        from gi.repository import Adw, Gtk

        self._Gtk = Gtk
        self._Adw = Adw

        outer = Gtk.ScrolledWindow()
        outer.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        for fn in (box.set_margin_top, box.set_margin_bottom,
                   box.set_margin_start, box.set_margin_end):
            fn(18)
        outer.set_child(box)

        title = Gtk.Label(label="SSH Key Audit")
        title.add_css_class("title-2")
        title.set_halign(Gtk.Align.START)
        box.append(title)

        subtitle = Gtk.Label(
            label="Public keys and certificates in ~/.ssh. Age is the file's "
                  "modified time; private keys are never read.")
        subtitle.add_css_class("dim-label")
        subtitle.set_halign(Gtk.Align.START)
        subtitle.set_wrap(True)
        subtitle.set_xalign(0)
        box.append(subtitle)

        controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        controls.append(Gtk.Label(label="Warn after (days):"))
        self._threshold_entry = Gtk.Entry()
        self._threshold_entry.set_text(str(self._threshold))
        self._threshold_entry.set_max_width_chars(6)
        self._threshold_entry.connect("activate", self._on_threshold_changed)
        controls.append(self._threshold_entry)
        refresh = Gtk.Button(label="Rescan")
        refresh.connect("clicked", lambda _b: self._refresh())
        controls.append(refresh)
        box.append(controls)

        warn_group = Adw.PreferencesGroup()
        self._warn_row = Adw.SwitchRow(
            title="Warn at startup",
            subtitle="Show a toast on launch if any key is past the threshold")
        self._warn_row.set_active(bool(self.ctx.settings.get("warn_on_start", True)))
        self._warn_row.connect("notify::active", self._on_warn_toggled)
        warn_group.add(self._warn_row)
        box.append(warn_group)

        self._list_box = Gtk.ListBox()
        self._list_box.set_selection_mode(Gtk.SelectionMode.NONE)
        self._list_box.add_css_class("boxed-list")
        box.append(self._list_box)

        self._status_label = Gtk.Label(label="")
        self._status_label.add_css_class("dim-label")
        self._status_label.set_halign(Gtk.Align.START)
        box.append(self._status_label)

        self._refresh()
        return outer

    def _on_warn_toggled(self, row, _param) -> None:
        self.ctx.settings.set("warn_on_start", bool(row.get_active()))

    def _on_threshold_changed(self, _entry) -> None:
        try:
            value = max(1, int(self._threshold_entry.get_text().strip()))
        except (TypeError, ValueError):
            self._set_status("Threshold must be a whole number of days.")
            return
        self._threshold = value
        self.ctx.settings.set("threshold_days", value)
        self._refresh()

    def _refresh(self) -> None:
        self._set_status("Scanning ~/.ssh…")

        def worker():
            keys = self._scan()
            if not self._stop.is_set():
                self.ctx.run_on_ui_thread(self._render, keys)
        threading.Thread(target=worker, daemon=True).start()

    def _render(self, keys: List[Dict[str, Any]]) -> None:
        Gtk = self._Gtk
        while child := self._list_box.get_first_child():
            self._list_box.remove(child)

        if not keys:
            row = Gtk.ListBoxRow()
            row.set_child(Gtk.Label(label="No public keys found in ~/.ssh.",
                                    margin_top=8, margin_bottom=8))
            self._list_box.append(row)
            self._set_status("")
            return

        old = 0
        for key in keys:
            row = Gtk.ListBoxRow()
            line = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            for fn in (line.set_margin_top, line.set_margin_bottom,
                       line.set_margin_start, line.set_margin_end):
                fn(8)
            header = Gtk.Label(xalign=0)
            bits = f"{key['bits']}-bit " if key.get("bits") else ""
            header.set_markup(
                f"<b>{_esc(key['name'])}</b>  "
                f"{_esc(bits + (key.get('type') or '?'))}")
            line.append(header)

            detail = f"{key['age_days']} days old"
            if key.get("status") == "old":
                detail += f"  ⚠ older than {self._threshold}d"
                old += 1
            if key.get("comment"):
                detail += f"  ·  {key['comment']}"
            if key.get("is_cert"):
                exp = key.get("cert_expiry_days")
                if exp is None:
                    detail += "  ·  cert: no expiry"
                elif exp < 0:
                    detail += f"  ·  cert EXPIRED {abs(exp)}d ago"
                else:
                    detail += f"  ·  cert expires in {exp}d"
            sub = Gtk.Label(label=detail, xalign=0)
            sub.add_css_class("dim-label")
            sub.add_css_class("caption")
            line.append(sub)

            if key.get("fingerprint"):
                fp = Gtk.Label(label=key["fingerprint"], xalign=0)
                fp.add_css_class("dim-label")
                fp.add_css_class("caption")
                line.append(fp)

            row.set_child(line)
            self._list_box.append(row)

        self._set_status(
            f"{len(keys)} key(s); {old} older than {self._threshold} days.")

    def _set_status(self, text: str) -> None:
        if self._status_label is not None:
            self._status_label.set_text(text)


def _esc(text: str) -> str:
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
