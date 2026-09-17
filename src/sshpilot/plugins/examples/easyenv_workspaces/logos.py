"""Recipe logos, fetched once and kept on disk.

The API names each recipe's logo by URL (an SVG on static.easyenv.io). The
page shows the same few dozen over and over, so each is downloaded once into
the cache directory and read from there afterwards, across restarts.
"""

from __future__ import annotations

import hashlib
import os
import threading
import urllib.parse
import urllib.request

USER_AGENT = "sshpilot-easyenv/2.6"
TIMEOUT_SECONDS = 15
#: A logo is a small SVG; anything bigger is not one.
MAX_BYTES = 512 * 1024
EXTENSIONS = (".svg", ".png", ".jpg", ".jpeg", ".webp")


def cache_dir():
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return os.path.join(base, "sshpilot-easyenv", "logos")


def cache_path(url, directory=None):
    ext = os.path.splitext(urllib.parse.urlsplit(url).path)[1].lower()
    if ext not in EXTENSIONS:
        ext = ".img"
    name = hashlib.sha256(url.encode("utf-8")).hexdigest()[:32] + ext
    return os.path.join(directory or cache_dir(), name)


def fetch(url, directory=None, opener=urllib.request.urlopen):
    """The logo's path on disk, downloading it the first time. None on failure."""
    if not url.startswith(("https://", "http://")):
        return None
    path = cache_path(url, directory)
    if os.path.exists(path):
        return path
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with opener(req, timeout=TIMEOUT_SECONDS) as resp:
            data = resp.read(MAX_BYTES + 1)
    except Exception:  # noqa: BLE001 - a missing logo is not worth a message
        return None
    if not data or len(data) > MAX_BYTES:
        return None
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp-{os.getpid()}-{threading.get_ident()}"
    with open(tmp, "wb") as fh:
        fh.write(data)
    os.replace(tmp, path)
    return path


class Loader:
    """Fetches off the UI thread; each URL once, however many cards ask."""

    def __init__(self, deliver, directory=None, opener=urllib.request.urlopen):
        self._deliver = deliver          # deliver(callback, path) on the UI thread
        self._directory = directory
        self._opener = opener
        self._lock = threading.Lock()
        self._done = {}                  # url -> path or None
        self._waiting = {}               # url -> [callback]

    def get(self, url, callback):
        with self._lock:
            if url in self._done:
                done = self._done[url]
            else:
                done = False
                first = url not in self._waiting
                self._waiting.setdefault(url, []).append(callback)
        if done is not False:
            callback(done)
            return
        if first:
            threading.Thread(target=self._run, args=(url,), daemon=True,
                             name="easyenv-logo").start()

    def _run(self, url):
        path = fetch(url, self._directory, self._opener)
        with self._lock:
            self._done[url] = path
            callbacks = self._waiting.pop(url, [])
        for cb in callbacks:
            self._deliver(cb, path)
