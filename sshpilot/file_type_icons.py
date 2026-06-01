"""File-type detection and icon resolution for the SFTP file manager.

The SFTP backend only exposes a name and an ``is_dir`` flag for each entry, so
type detection is done purely from the filename:

1. Multi-segment compound extensions (``foo.tar.gz``, ``bundle.tar.xz``) are
   matched first so archives are classified correctly.
2. The filename is checked against a list of well-known names (``Makefile``,
   ``Dockerfile``, ``.bashrc`` …).
3. The last extension is looked up in a curated table.
4. ``mimetypes.guess_type`` provides a final, broad fallback.

Each branch returns a canonical Adwaita-style icon name (e.g. ``text-x-script``)
together with the symbolic variant used when the user picks the system icon
theme. The icon names are intentionally restricted to the set we vendor in
``resources/icons/scalable/mimetypes`` so they render identically everywhere.
"""

from __future__ import annotations

import mimetypes
import os


# Canonical (non-symbolic) Adwaita mimetype icon names we vendor.
ICON_FOLDER = "inode-directory"
ICON_SYMLINK = "inode-symlink"
ICON_GENERIC = "text-x-generic"
ICON_TEXT = "text-x-generic"
ICON_SCRIPT = "text-x-script"
ICON_HTML = "text-html"
ICON_PREVIEW = "text-x-preview"
ICON_IMAGE = "image-x-generic"
ICON_AUDIO = "audio-x-generic"
ICON_VIDEO = "video-x-generic"
ICON_FONT = "font-x-generic"
ICON_PACKAGE = "package-x-generic"
ICON_REPO = "x-package-repository"
ICON_EXECUTABLE = "application-x-executable"
ICON_LIBRARY = "application-x-sharedlib"
ICON_FIRMWARE = "application-x-firmware"
ICON_ADDON = "application-x-addon"
ICON_CERT = "application-certificate"
ICON_BINARY = "application-x-generic"
ICON_DOCUMENT = "x-office-document"
ICON_DOCUMENT_TEMPLATE = "x-office-document-template"
ICON_SPREADSHEET = "x-office-spreadsheet"
ICON_SPREADSHEET_TEMPLATE = "x-office-spreadsheet-template"
ICON_PRESENTATION = "x-office-presentation"
ICON_PRESENTATION_TEMPLATE = "x-office-presentation-template"
ICON_DRAWING = "x-office-drawing"
ICON_ADDRESSBOOK = "x-office-addressbook"
ICON_MODEL = "model"


# Filenames (lowercased, no path) that should classify regardless of extension.
_BY_NAME = {
    # Build/CI
    "makefile": ICON_SCRIPT,
    "gnumakefile": ICON_SCRIPT,
    "dockerfile": ICON_SCRIPT,
    "containerfile": ICON_SCRIPT,
    "vagrantfile": ICON_SCRIPT,
    "rakefile": ICON_SCRIPT,
    "gemfile": ICON_SCRIPT,
    "gemfile.lock": ICON_TEXT,
    "procfile": ICON_SCRIPT,
    "jenkinsfile": ICON_SCRIPT,
    "cmakelists.txt": ICON_SCRIPT,
    "meson.build": ICON_SCRIPT,
    "build": ICON_SCRIPT,
    "build.gradle": ICON_SCRIPT,
    "pom.xml": ICON_TEXT,
    "package.json": ICON_TEXT,
    "package-lock.json": ICON_TEXT,
    "yarn.lock": ICON_TEXT,
    "pnpm-lock.yaml": ICON_TEXT,
    "pyproject.toml": ICON_TEXT,
    "poetry.lock": ICON_TEXT,
    "requirements.txt": ICON_TEXT,
    "pipfile": ICON_TEXT,
    "pipfile.lock": ICON_TEXT,
    "cargo.toml": ICON_TEXT,
    "cargo.lock": ICON_TEXT,
    "go.mod": ICON_TEXT,
    "go.sum": ICON_TEXT,
    # Docs / metadata
    "readme": ICON_TEXT,
    "readme.txt": ICON_TEXT,
    "readme.md": ICON_TEXT,
    "license": ICON_TEXT,
    "licence": ICON_TEXT,
    "copying": ICON_TEXT,
    "copyright": ICON_TEXT,
    "authors": ICON_TEXT,
    "contributors": ICON_TEXT,
    "changelog": ICON_TEXT,
    "changes": ICON_TEXT,
    "news": ICON_TEXT,
    "notice": ICON_TEXT,
    "install": ICON_TEXT,
    "todo": ICON_TEXT,
    # Shell / dotfiles
    ".bashrc": ICON_SCRIPT,
    ".bash_profile": ICON_SCRIPT,
    ".bash_logout": ICON_SCRIPT,
    ".profile": ICON_SCRIPT,
    ".zshrc": ICON_SCRIPT,
    ".zprofile": ICON_SCRIPT,
    ".zshenv": ICON_SCRIPT,
    ".inputrc": ICON_TEXT,
    ".gitconfig": ICON_TEXT,
    ".gitignore": ICON_TEXT,
    ".gitattributes": ICON_TEXT,
    ".gitmodules": ICON_TEXT,
    ".editorconfig": ICON_TEXT,
    ".env": ICON_TEXT,
    ".vimrc": ICON_TEXT,
    ".tmux.conf": ICON_TEXT,
    ".ssh": ICON_FOLDER,
    "authorized_keys": ICON_CERT,
    "known_hosts": ICON_CERT,
    "id_rsa": ICON_CERT,
    "id_ed25519": ICON_CERT,
    "id_ecdsa": ICON_CERT,
}


# Compound extensions ("foo.tar.gz") that must be matched before the single
# extension lookup so we don't classify ``foo.tar.gz`` as ``.gz``.
_COMPOUND_EXTS = {
    ".tar.gz": ICON_PACKAGE,
    ".tar.bz2": ICON_PACKAGE,
    ".tar.xz": ICON_PACKAGE,
    ".tar.zst": ICON_PACKAGE,
    ".tar.lz": ICON_PACKAGE,
    ".tar.lzma": ICON_PACKAGE,
}


# Single-extension lookup. Keys are lowercase and include the leading dot.
_BY_EXT = {
    # --- Folders (handled before reaching this table, listed for clarity) ---

    # --- Plain text / docs ---
    ".txt": ICON_TEXT,
    ".md": ICON_TEXT,
    ".markdown": ICON_TEXT,
    ".rst": ICON_TEXT,
    ".adoc": ICON_TEXT,
    ".asciidoc": ICON_TEXT,
    ".log": ICON_TEXT,
    ".csv": ICON_SPREADSHEET,
    ".tsv": ICON_SPREADSHEET,
    ".rtf": ICON_DOCUMENT,
    ".tex": ICON_TEXT,
    ".bib": ICON_TEXT,

    # --- Source code (script-like) ---
    ".py": ICON_SCRIPT,
    ".pyw": ICON_SCRIPT,
    ".pyx": ICON_SCRIPT,
    ".pyi": ICON_SCRIPT,
    ".rb": ICON_SCRIPT,
    ".pl": ICON_SCRIPT,
    ".pm": ICON_SCRIPT,
    ".sh": ICON_SCRIPT,
    ".bash": ICON_SCRIPT,
    ".zsh": ICON_SCRIPT,
    ".fish": ICON_SCRIPT,
    ".ksh": ICON_SCRIPT,
    ".csh": ICON_SCRIPT,
    ".ps1": ICON_SCRIPT,
    ".bat": ICON_SCRIPT,
    ".cmd": ICON_SCRIPT,
    ".lua": ICON_SCRIPT,
    ".tcl": ICON_SCRIPT,
    ".awk": ICON_SCRIPT,
    ".sed": ICON_SCRIPT,
    ".vim": ICON_SCRIPT,
    ".applescript": ICON_SCRIPT,

    # --- Source code (compiled, treated as plain text but distinct) ---
    ".c": ICON_TEXT,
    ".h": ICON_TEXT,
    ".cc": ICON_TEXT,
    ".cpp": ICON_TEXT,
    ".cxx": ICON_TEXT,
    ".hpp": ICON_TEXT,
    ".hh": ICON_TEXT,
    ".hxx": ICON_TEXT,
    ".m": ICON_TEXT,
    ".mm": ICON_TEXT,
    ".swift": ICON_TEXT,
    ".java": ICON_TEXT,
    ".kt": ICON_TEXT,
    ".kts": ICON_SCRIPT,
    ".scala": ICON_TEXT,
    ".clj": ICON_TEXT,
    ".cljs": ICON_TEXT,
    ".rs": ICON_TEXT,
    ".go": ICON_TEXT,
    ".dart": ICON_TEXT,
    ".cs": ICON_TEXT,
    ".fs": ICON_TEXT,
    ".vb": ICON_TEXT,
    ".php": ICON_TEXT,
    ".php3": ICON_TEXT,
    ".php4": ICON_TEXT,
    ".php5": ICON_TEXT,
    ".phtml": ICON_TEXT,
    ".erl": ICON_TEXT,
    ".ex": ICON_TEXT,
    ".exs": ICON_SCRIPT,
    ".hs": ICON_TEXT,
    ".ml": ICON_TEXT,
    ".mli": ICON_TEXT,
    ".r": ICON_SCRIPT,
    ".jl": ICON_TEXT,
    ".groovy": ICON_TEXT,
    ".gradle": ICON_TEXT,

    # --- Web ---
    ".html": ICON_HTML,
    ".htm": ICON_HTML,
    ".xhtml": ICON_HTML,
    ".shtml": ICON_HTML,
    ".js": ICON_TEXT,
    ".mjs": ICON_TEXT,
    ".cjs": ICON_TEXT,
    ".jsx": ICON_TEXT,
    ".ts": ICON_TEXT,
    ".tsx": ICON_TEXT,
    ".vue": ICON_TEXT,
    ".svelte": ICON_TEXT,
    ".astro": ICON_TEXT,
    ".css": ICON_TEXT,
    ".scss": ICON_TEXT,
    ".sass": ICON_TEXT,
    ".less": ICON_TEXT,
    ".styl": ICON_TEXT,

    # --- Markup / data / config ---
    ".xml": ICON_TEXT,
    ".xsl": ICON_TEXT,
    ".xsd": ICON_TEXT,
    ".dtd": ICON_TEXT,
    ".json": ICON_TEXT,
    ".jsonc": ICON_TEXT,
    ".json5": ICON_TEXT,
    ".geojson": ICON_TEXT,
    ".yaml": ICON_TEXT,
    ".yml": ICON_TEXT,
    ".toml": ICON_TEXT,
    ".ini": ICON_TEXT,
    ".cfg": ICON_TEXT,
    ".conf": ICON_TEXT,
    ".config": ICON_TEXT,
    ".properties": ICON_TEXT,
    ".env": ICON_TEXT,
    ".sql": ICON_TEXT,
    ".nix": ICON_TEXT,
    ".dockerfile": ICON_SCRIPT,
    ".tf": ICON_TEXT,
    ".tfvars": ICON_TEXT,
    ".hcl": ICON_TEXT,
    ".proto": ICON_TEXT,
    ".graphql": ICON_TEXT,
    ".gql": ICON_TEXT,

    # --- Diff/patch ---
    ".diff": ICON_TEXT,
    ".patch": ICON_TEXT,

    # --- Images ---
    ".png": ICON_IMAGE,
    ".jpg": ICON_IMAGE,
    ".jpeg": ICON_IMAGE,
    ".jpe": ICON_IMAGE,
    ".jfif": ICON_IMAGE,
    ".gif": ICON_IMAGE,
    ".bmp": ICON_IMAGE,
    ".tif": ICON_IMAGE,
    ".tiff": ICON_IMAGE,
    ".webp": ICON_IMAGE,
    ".svg": ICON_IMAGE,
    ".svgz": ICON_IMAGE,
    ".ico": ICON_IMAGE,
    ".icns": ICON_IMAGE,
    ".heic": ICON_IMAGE,
    ".heif": ICON_IMAGE,
    ".avif": ICON_IMAGE,
    ".raw": ICON_IMAGE,
    ".cr2": ICON_IMAGE,
    ".nef": ICON_IMAGE,
    ".dng": ICON_IMAGE,
    ".psd": ICON_IMAGE,
    ".xcf": ICON_IMAGE,
    ".kra": ICON_IMAGE,
    ".ai": ICON_DRAWING,
    ".eps": ICON_DRAWING,

    # --- Audio ---
    ".mp3": ICON_AUDIO,
    ".wav": ICON_AUDIO,
    ".flac": ICON_AUDIO,
    ".ogg": ICON_AUDIO,
    ".oga": ICON_AUDIO,
    ".opus": ICON_AUDIO,
    ".m4a": ICON_AUDIO,
    ".aac": ICON_AUDIO,
    ".wma": ICON_AUDIO,
    ".aiff": ICON_AUDIO,
    ".aif": ICON_AUDIO,
    ".alac": ICON_AUDIO,
    ".mid": ICON_AUDIO,
    ".midi": ICON_AUDIO,
    ".ape": ICON_AUDIO,
    ".dsf": ICON_AUDIO,

    # --- Video ---
    ".mp4": ICON_VIDEO,
    ".m4v": ICON_VIDEO,
    ".mkv": ICON_VIDEO,
    ".webm": ICON_VIDEO,
    ".mov": ICON_VIDEO,
    ".avi": ICON_VIDEO,
    ".wmv": ICON_VIDEO,
    ".flv": ICON_VIDEO,
    ".mpg": ICON_VIDEO,
    ".mpeg": ICON_VIDEO,
    ".mpe": ICON_VIDEO,
    ".3gp": ICON_VIDEO,
    ".3g2": ICON_VIDEO,
    # .ts intentionally omitted here — TypeScript wins over MPEG-TS in dev tools.
    ".vob": ICON_VIDEO,
    ".ogv": ICON_VIDEO,
    ".rm": ICON_VIDEO,
    ".rmvb": ICON_VIDEO,

    # --- Fonts ---
    ".ttf": ICON_FONT,
    ".otf": ICON_FONT,
    ".woff": ICON_FONT,
    ".woff2": ICON_FONT,
    ".eot": ICON_FONT,
    ".pfb": ICON_FONT,
    ".pfm": ICON_FONT,
    ".bdf": ICON_FONT,
    ".pcf": ICON_FONT,

    # --- Archives / packages ---
    ".zip": ICON_PACKAGE,
    ".tar": ICON_PACKAGE,
    ".gz": ICON_PACKAGE,
    ".bz2": ICON_PACKAGE,
    ".xz": ICON_PACKAGE,
    ".zst": ICON_PACKAGE,
    ".lz": ICON_PACKAGE,
    ".lzma": ICON_PACKAGE,
    ".7z": ICON_PACKAGE,
    ".rar": ICON_PACKAGE,
    ".cab": ICON_PACKAGE,
    ".ar": ICON_PACKAGE,
    ".cpio": ICON_PACKAGE,
    ".tgz": ICON_PACKAGE,
    ".tbz": ICON_PACKAGE,
    ".tbz2": ICON_PACKAGE,
    ".txz": ICON_PACKAGE,
    ".tzst": ICON_PACKAGE,
    ".jar": ICON_PACKAGE,
    ".war": ICON_PACKAGE,
    ".ear": ICON_PACKAGE,
    ".apk": ICON_PACKAGE,
    ".aab": ICON_PACKAGE,
    ".ipa": ICON_PACKAGE,
    ".iso": ICON_PACKAGE,
    ".img": ICON_PACKAGE,
    ".dmg": ICON_PACKAGE,
    ".vhd": ICON_PACKAGE,
    ".vhdx": ICON_PACKAGE,
    ".vmdk": ICON_PACKAGE,
    ".qcow2": ICON_PACKAGE,

    # --- Distro / repo packages ---
    ".deb": ICON_REPO,
    ".rpm": ICON_REPO,
    ".pkg": ICON_REPO,
    ".msi": ICON_REPO,
    ".flatpak": ICON_REPO,
    ".flatpakref": ICON_REPO,
    ".snap": ICON_REPO,
    ".appimage": ICON_REPO,
    ".whl": ICON_REPO,

    # --- Executables / libraries / firmware ---
    ".exe": ICON_EXECUTABLE,
    ".com": ICON_EXECUTABLE,
    ".elf": ICON_EXECUTABLE,
    ".bin": ICON_FIRMWARE,
    ".rom": ICON_FIRMWARE,
    ".fw": ICON_FIRMWARE,
    ".hex": ICON_FIRMWARE,
    ".uf2": ICON_FIRMWARE,
    ".dll": ICON_LIBRARY,
    ".so": ICON_LIBRARY,
    ".dylib": ICON_LIBRARY,
    ".a": ICON_LIBRARY,
    ".lib": ICON_LIBRARY,
    ".o": ICON_LIBRARY,
    ".obj": ICON_LIBRARY,
    ".class": ICON_LIBRARY,
    ".pyc": ICON_LIBRARY,
    ".pyo": ICON_LIBRARY,

    # --- Add-ons / plugins ---
    ".xpi": ICON_ADDON,
    ".crx": ICON_ADDON,
    ".vsix": ICON_ADDON,

    # --- Certificates / keys ---
    ".pem": ICON_CERT,
    ".crt": ICON_CERT,
    ".cer": ICON_CERT,
    ".der": ICON_CERT,
    ".key": ICON_CERT,
    ".pub": ICON_CERT,
    ".p12": ICON_CERT,
    ".pfx": ICON_CERT,
    ".jks": ICON_CERT,
    ".gpg": ICON_CERT,
    ".asc": ICON_CERT,
    ".sig": ICON_CERT,

    # --- Office: documents ---
    ".doc": ICON_DOCUMENT,
    ".docx": ICON_DOCUMENT,
    ".odt": ICON_DOCUMENT,
    ".pages": ICON_DOCUMENT,
    ".pdf": ICON_DOCUMENT,
    ".epub": ICON_DOCUMENT,
    ".mobi": ICON_DOCUMENT,
    ".azw": ICON_DOCUMENT,
    ".azw3": ICON_DOCUMENT,
    ".djvu": ICON_DOCUMENT,
    ".fb2": ICON_DOCUMENT,
    ".dot": ICON_DOCUMENT_TEMPLATE,
    ".dotx": ICON_DOCUMENT_TEMPLATE,
    ".ott": ICON_DOCUMENT_TEMPLATE,

    # --- Office: spreadsheets ---
    ".xls": ICON_SPREADSHEET,
    ".xlsx": ICON_SPREADSHEET,
    ".ods": ICON_SPREADSHEET,
    ".numbers": ICON_SPREADSHEET,
    ".xlt": ICON_SPREADSHEET_TEMPLATE,
    ".xltx": ICON_SPREADSHEET_TEMPLATE,
    ".ots": ICON_SPREADSHEET_TEMPLATE,

    # --- Office: presentations ---
    ".ppt": ICON_PRESENTATION,
    ".pptx": ICON_PRESENTATION,
    ".odp": ICON_PRESENTATION,
    ".key.zip": ICON_PRESENTATION,
    ".pot": ICON_PRESENTATION_TEMPLATE,
    ".potx": ICON_PRESENTATION_TEMPLATE,
    ".otp": ICON_PRESENTATION_TEMPLATE,

    # --- Office: drawings / addressbook ---
    ".odg": ICON_DRAWING,
    ".vsd": ICON_DRAWING,
    ".vsdx": ICON_DRAWING,
    ".vcf": ICON_ADDRESSBOOK,
    ".vcard": ICON_ADDRESSBOOK,

    # --- 3D / models ---
    # Note: .obj is ambiguous (C object file vs Wavefront 3D); .obj is mapped
    # to ICON_LIBRARY above since C/C++ object files are vastly more common in
    # a dev/server context.
    ".stl": ICON_MODEL,
    ".gltf": ICON_MODEL,
    ".glb": ICON_MODEL,
    ".fbx": ICON_MODEL,
    ".dae": ICON_MODEL,
    ".3ds": ICON_MODEL,
    ".blend": ICON_MODEL,
    ".ply": ICON_MODEL,
}


def _ext_lookup(name_lower: str) -> str:
    """Match the *last* extension(s) of *name_lower* against the tables."""
    for compound, icon in _COMPOUND_EXTS.items():
        if name_lower.endswith(compound):
            return icon
    _, ext = os.path.splitext(name_lower)
    if ext and ext in _BY_EXT:
        return _BY_EXT[ext]
    return ""


def _mimetype_lookup(name: str) -> str:
    """Fallback to :mod:`mimetypes` when extension table missed."""
    guess, _ = mimetypes.guess_type(name)
    if not guess:
        return ""
    if guess.startswith("text/html"):
        return ICON_HTML
    if guess.startswith("text/"):
        return ICON_TEXT
    if guess.startswith("image/"):
        return ICON_IMAGE
    if guess.startswith("audio/"):
        return ICON_AUDIO
    if guess.startswith("video/"):
        return ICON_VIDEO
    if guess.startswith("font/"):
        return ICON_FONT
    if guess in {"application/json", "application/xml", "application/javascript",
                 "application/x-yaml", "application/x-sh", "application/x-python"}:
        return ICON_TEXT if "json" in guess or "xml" in guess or "yaml" in guess else ICON_SCRIPT
    if guess in {"application/pdf", "application/epub+zip"}:
        return ICON_DOCUMENT
    if guess in {"application/zip", "application/x-tar", "application/gzip",
                 "application/x-bzip2", "application/x-xz", "application/x-7z-compressed",
                 "application/x-rar", "application/x-zstd"}:
        return ICON_PACKAGE
    if guess == "application/x-executable":
        return ICON_EXECUTABLE
    if guess == "application/x-sharedlib":
        return ICON_LIBRARY
    if guess == "application/x-x509-ca-cert":
        return ICON_CERT
    return ICON_BINARY


def get_icon_for_name(name: str, is_dir: bool) -> str:
    """Return the canonical (non-symbolic) icon name for *name*.

    *is_dir* is honoured first: directories always get the folder icon.
    """
    if is_dir:
        return ICON_FOLDER

    if not name:
        return ICON_GENERIC

    name_lower = name.lower()

    by_name = _BY_NAME.get(name_lower)
    if by_name is not None:
        return by_name

    ext_icon = _ext_lookup(name_lower)
    if ext_icon:
        return ext_icon

    mime_icon = _mimetype_lookup(name)
    if mime_icon:
        return mime_icon

    return ICON_GENERIC
