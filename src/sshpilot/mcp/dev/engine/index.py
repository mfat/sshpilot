"""Versioned, incremental SQLite index of the checkout.

Storage
    One SQLite file per checkout and schema version, outside the repository:
    ``$SSHPILOT_MCP_INDEX_DIR`` or ``$XDG_CACHE_HOME/sshpilot-mcp-dev/<name>-<hash>/``.
    Nothing is written into the checkout, so there is nothing to ignore in Git.
    The file holds paths, hashes, symbol names, line ranges, signatures and
    first docstring lines — never file contents, logs or environment data.

Freshness
    Every query first calls :meth:`DevIndex.refresh`, which stats the indexed
    tree (mtime + size), re-hashes only files whose stat changed, re-extracts
    only files whose hash changed, and deletes rows for vanished files.
    Cross-file relationships are stored as *unresolved names* and resolved at
    query time, so a change in one file can never leave a stale resolved edge
    in another — no dependent-row invalidation is needed. Source excerpts are
    re-verified against the stored hash right before they are returned.

Concurrency
    WAL journal, a busy timeout, and one ``BEGIN IMMEDIATE`` transaction per
    refresh: two MCP clients on the same checkout serialise their refreshes
    and the second one finds nothing to do.
"""

from __future__ import annotations

import fnmatch
import hashlib
import os
import re
import sqlite3
import threading
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from ..._scope import SKIP_DIR_NAMES, RepoScope, ScopeError
from .extract import FileExtract, extract_source

#: Bump whenever the table layout or extraction semantics change. The version
#: is part of the database file name, so different server versions never fight
#: over one file; an old file is simply ignored (and can be deleted).
SCHEMA_VERSION = 2

INDEX_DIR_ENV = "SSHPILOT_MCP_INDEX_DIR"
EXCLUDE_ENV = "SSHPILOT_MCP_INDEX_EXCLUDE"
WORKERS_ENV = "SSHPILOT_MCP_INDEX_WORKERS"
#: Optional comma-separated prefixes limiting what is indexed (tests, huge
#: checkouts). Results then only know about those files.
PREFIXES_ENV = "SSHPILOT_MCP_INDEX_PREFIXES"

#: Repository prefixes whose Python files are indexed.
INDEXED_PREFIXES = ("src/", "tests/", "scripts/")
#: Never indexed, whatever their suffix (documented exclusion policy).
DEFAULT_EXCLUDES = (
    ".env",
    ".env.*",
    "*/.env",
    "*/.env.*",
    "*.pem",
    "*.key",
    "*id_rsa*",
    "*id_ecdsa*",
    "*id_ed25519*",
    "*/fixtures/keys/*",
)
MAX_INDEXED_FILE_BYTES = 2 * 1024 * 1024
#: Below this many files to (re)parse, stay in-process.
PARALLEL_THRESHOLD = 64

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS files (
    path TEXT PRIMARY KEY,
    sha256 TEXT NOT NULL,
    size INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    ctime_ns INTEGER NOT NULL,
    module TEXT NOT NULL,
    status TEXT NOT NULL,
    error TEXT NOT NULL DEFAULT '',
    is_test INTEGER NOT NULL,
    indexed_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS symbols (
    sid INTEGER PRIMARY KEY,
    id TEXT NOT NULL UNIQUE,
    path TEXT NOT NULL,
    qualname TEXT NOT NULL,
    name TEXT NOT NULL,
    fqname TEXT NOT NULL,
    kind TEXT NOT NULL,
    line INTEGER NOT NULL,
    end_line INTEGER NOT NULL,
    signature TEXT NOT NULL,
    doc TEXT NOT NULL,
    parent TEXT
);
CREATE INDEX IF NOT EXISTS symbols_fq ON symbols(fqname);
CREATE INDEX IF NOT EXISTS symbols_name ON symbols(name);
CREATE INDEX IF NOT EXISTS symbols_path ON symbols(path, line);
CREATE TABLE IF NOT EXISTS edges (
    src INTEGER NOT NULL,
    kind TEXT NOT NULL,
    target TEXT NOT NULL,
    line INTEGER NOT NULL,
    confidence TEXT NOT NULL,
    mechanism TEXT NOT NULL,
    detail TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS edges_target ON edges(target);
CREATE INDEX IF NOT EXISTS edges_src ON edges(src);
CREATE TABLE IF NOT EXISTS domain_edges (
    src TEXT NOT NULL,
    kind TEXT NOT NULL,
    target TEXT NOT NULL,
    path TEXT NOT NULL,
    line INTEGER NOT NULL,
    confidence TEXT NOT NULL,
    mechanism TEXT NOT NULL,
    detail TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS domain_src ON domain_edges(src);
CREATE INDEX IF NOT EXISTS domain_target ON domain_edges(target);
"""


def symbol_id(path: str, qualname: str) -> str:
    """Stable symbol identifier: ``<repo path>::<qualname>``."""
    return f"{path}::{qualname}"


def default_index_dir(root: Path, env: Optional[Dict[str, str]] = None) -> Path:
    environment = os.environ if env is None else env
    pinned = environment.get(INDEX_DIR_ENV)
    if pinned:
        return Path(pinned).expanduser()
    cache = environment.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    digest = hashlib.sha1(str(root).encode("utf-8")).hexdigest()[:12]
    return Path(cache) / "sshpilot-mcp-dev" / f"{root.name}-{digest}"


def _extract_worker(item: Tuple[str, str]) -> FileExtract:
    return extract_source(*item)


class DevIndex:
    """Incremental symbol/edge index for one :class:`RepoScope`."""

    def __init__(
        self,
        scope: RepoScope,
        index_dir: Optional[Path] = None,
        excludes: Optional[Iterable[str]] = None,
        prefixes: Optional[Iterable[str]] = None,
    ):
        self.scope = scope
        if prefixes is None:
            configured = [item.strip() for item in os.environ.get(PREFIXES_ENV, "").split(",") if item.strip()]
            prefixes = configured or None
        self.prefixes = tuple(p.strip("/") + "/" for p in prefixes) if prefixes else INDEXED_PREFIXES
        for prefix in self.prefixes:
            if not prefix.startswith(INDEXED_PREFIXES):
                raise ScopeError(f"index prefix must be under {', '.join(INDEXED_PREFIXES)}: {prefix!r}")
        self.index_dir = Path(index_dir) if index_dir is not None else default_index_dir(scope.root)
        suffix = "" if self.prefixes == INDEXED_PREFIXES else "-" + hashlib.sha1(",".join(self.prefixes).encode()).hexdigest()[:8]
        self.path = self.index_dir / f"index-v{SCHEMA_VERSION}{suffix}.sqlite3"
        extra = [item.strip() for item in os.environ.get(EXCLUDE_ENV, "").split(",") if item.strip()]
        self.excludes = tuple(excludes) if excludes is not None else DEFAULT_EXCLUDES + tuple(extra)
        self._exclude_re = re.compile("|".join(fnmatch.translate(p) for p in self.excludes) or r"(?!)")
        self._lock = threading.RLock()
        self._db: Optional[sqlite3.Connection] = None
        self.last_refresh: Dict[str, object] = {}

    # -- connection ----------------------------------------------------------
    @property
    def db(self) -> sqlite3.Connection:
        if self._db is None:
            self.index_dir.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(self.index_dir, 0o700)
            except OSError:
                pass
            db = sqlite3.connect(str(self.path), timeout=120, check_same_thread=False, isolation_level=None)
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA busy_timeout=120000")
            db.execute("PRAGMA synchronous=NORMAL")
            db.executescript(_SCHEMA)
            self._migrate(db)
            self._db = db
        return self._db

    def _migrate(self, db: sqlite3.Connection) -> None:
        row = db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        stored = int(row[0]) if row else None
        if stored is None:
            db.execute("INSERT OR REPLACE INTO meta VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
            db.execute("INSERT OR REPLACE INTO meta VALUES ('root', ?)", (str(self.scope.root),))
        elif stored != SCHEMA_VERSION:
            # The version is in the file name, so this only happens if a file
            # was copied by hand. The index is a cache: rebuild it.
            self._clear(db)
            db.execute("INSERT OR REPLACE INTO meta VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
        root_row = db.execute("SELECT value FROM meta WHERE key='root'").fetchone()
        if root_row and root_row[0] != str(self.scope.root):
            # Same cache directory pointed at another checkout: never mix.
            self._clear(db)
            db.execute("INSERT OR REPLACE INTO meta VALUES ('root', ?)", (str(self.scope.root),))

    @staticmethod
    def _clear(db: sqlite3.Connection) -> None:
        for table in ("files", "symbols", "edges", "domain_edges"):
            db.execute(f"DELETE FROM {table}")
        db.execute("DELETE FROM meta WHERE key NOT IN ('schema_version', 'root')")

    def close(self) -> None:
        with self._lock:
            if self._db is not None:
                self._db.close()
                self._db = None

    # -- file selection ------------------------------------------------------
    def is_excluded(self, path: str) -> bool:
        name = path.rsplit("/", 1)[-1]
        return bool(self._exclude_re.match(path) or self._exclude_re.match(name))

    def candidate_files(self) -> List[str]:
        """Repository-relative Python files this index covers (sorted).

        Directory symlinks are never followed (``os.walk`` default); a file
        symlink is accepted only if its target stays inside the checkout.
        """
        root = self.scope.root
        root_text = str(root)
        found: List[str] = []
        for prefix in self.prefixes:
            base = root / prefix.rstrip("/")
            if not base.is_dir() or base.is_symlink():
                continue
            for dirpath, dirnames, filenames in os.walk(base):
                dirnames[:] = sorted(
                    name for name in dirnames if name not in SKIP_DIR_NAMES and not name.startswith(".git")
                )
                for filename in filenames:
                    if not filename.endswith(".py"):
                        continue
                    full = os.path.join(dirpath, filename)
                    if os.path.islink(full) and not self.scope.is_within(Path(full)):
                        continue
                    found.append(full[len(root_text) + 1 :].replace(os.sep, "/"))
        if self.prefixes == INDEXED_PREFIXES:
            for child in root.iterdir():
                if child.suffix == ".py" and child.is_file() and self.scope.is_within(child):
                    found.append(child.name)
        return sorted(path for path in found if not self.is_excluded(path))

    # -- refresh -------------------------------------------------------------
    def refresh(self, full: bool = False, force_paths: Iterable[str] = ()) -> Dict[str, object]:
        """Bring the index up to date with the working tree (incremental).

        Returns statistics: how many files were scanned, unchanged, re-hashed,
        re-extracted and removed, plus timing. ``full=True`` discards the
        index first (explicit rebuild).
        """
        with self._lock:
            started = time.perf_counter()
            db = self.db
            db.execute("BEGIN IMMEDIATE")
            try:
                if full:
                    self._clear(db)
                stats = self._refresh_locked(db, frozenset(force_paths))
                db.execute("INSERT OR REPLACE INTO meta VALUES ('refreshed_at', ?)", (repr(time.time()),))
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise
            stats["full"] = full
            stats["duration_ms"] = round((time.perf_counter() - started) * 1000, 1)
            self.last_refresh = stats
            return stats

    def _refresh_locked(self, db: sqlite3.Connection, force: frozenset = frozenset()) -> Dict[str, object]:
        known = {
            row[0]: (row[1], row[2], row[3], row[4])
            for row in db.execute("SELECT path, sha256, size, mtime_ns, ctime_ns FROM files")
        }
        candidates = self.candidate_files()
        present = set(candidates)
        removed = sorted(set(known) - present)
        for path in removed:
            self._delete_file(db, path)

        to_extract: List[Tuple[str, str, str, int, int]] = []  # path, source, sha, size, mtime
        rehashed = 0
        skipped: List[str] = []
        unindexable: List[str] = []
        for path in candidates:
            full_path = self.scope.root / path
            try:
                stat = full_path.stat()
            except OSError as error:
                # Every transition into a non-indexable state drops the file's
                # old symbols and edges: a stale definition must never outlive
                # the file contents it described.
                self._mark_unindexable(db, path, "unreadable", f"stat failed: {error.strerror or error}")
                unindexable.append(path)
                continue
            previous = known.get(path)
            # ctime is part of the signature: chmod/chown change it without
            # touching size or mtime, and may make the file unreadable.
            if (
                previous
                and previous[1] == stat.st_size
                and previous[2] == stat.st_mtime_ns
                and previous[3] == stat.st_ctime_ns
                and path not in force
            ):
                continue
            if stat.st_size > MAX_INDEXED_FILE_BYTES:
                self._mark_unindexable(db, path, "skipped_too_large", "file exceeds index size cap", stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
                skipped.append(path)
                continue
            try:
                raw = full_path.read_bytes()
            except OSError as error:
                self._mark_unindexable(db, path, "unreadable", f"read failed: {error.strerror or error}")
                unindexable.append(path)
                continue
            sha = hashlib.sha256(raw).hexdigest()
            rehashed += 1
            if previous and previous[0] == sha:
                db.execute(
                    "UPDATE files SET size=?, mtime_ns=?, ctime_ns=? WHERE path=?",
                    (stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, path),
                )
                continue
            try:
                source = raw.decode("utf-8")
            except UnicodeDecodeError:
                self._delete_file(db, path)
                self._store_file(db, path, sha, stat.st_size, stat.st_mtime_ns, "decode_error", "not UTF-8", stat.st_ctime_ns)
                continue
            to_extract.append((path, source, sha, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns))

        results = self._extract_many([(item[0], item[1]) for item in to_extract])
        for (path, _source, sha, size, mtime, ctime), extract in zip(to_extract, results):
            self._delete_file(db, path)
            self._store_file(db, path, sha, size, mtime, extract.status, extract.error, ctime)
            self._store_extract(db, path, extract)

        domain_rebuilt = False
        if to_extract or removed or not self._domain_current(db):
            from .domain import rebuild_domain_edges

            domain_rebuilt = rebuild_domain_edges(self, db)

        return {
            "scanned": len(candidates),
            "reindexed": len(to_extract),
            "rehashed": rehashed,
            "removed": len(removed),
            "removed_paths": removed[:20],
            "reindexed_paths": [item[0] for item in to_extract][:20],
            "skipped_too_large": skipped,
            "unreadable": unindexable,
            "domain_rebuilt": domain_rebuilt,
        }

    def _extract_many(self, items: List[Tuple[str, str]]) -> List[FileExtract]:
        workers_setting = os.environ.get(WORKERS_ENV, "")
        # Serial by default: spawn-based workers re-import the caller's
        # ``__main__`` and are fragile inside hosts; parallelism is opt-in.
        workers = int(workers_setting) if workers_setting.isdigit() else 1
        if len(items) < PARALLEL_THRESHOLD or workers <= 1:
            return [extract_source(path, source) for path, source in items]
        try:
            import multiprocessing

            context = multiprocessing.get_context("spawn")
            with ProcessPoolExecutor(max_workers=workers, mp_context=context) as pool:
                return list(pool.map(_extract_worker, items, chunksize=16))
        except Exception:
            # Spawning can fail in constrained sandboxes; correctness first.
            return [extract_source(path, source) for path, source in items]

    def _mark_unindexable(self, db, path, status, error, size=-1, mtime=-1, ctime=-1) -> None:
        """Drop *path*'s symbols/edges and record why it is not indexed.

        size/mtime of -1 never match a real stat, so the file is retried (and
        recovers) on the next refresh once it is readable again.
        """
        self._delete_file(db, path)
        self._store_file(db, path, "", size, mtime, status, error, ctime)

    @staticmethod
    def _delete_file(db: sqlite3.Connection, path: str) -> None:
        db.execute("DELETE FROM edges WHERE src IN (SELECT sid FROM symbols WHERE path=?)", (path,))
        db.execute("DELETE FROM symbols WHERE path=?", (path,))
        db.execute("DELETE FROM files WHERE path=?", (path,))

    @staticmethod
    def _store_file(db, path, sha, size, mtime, status, error, ctime=-1) -> None:
        from .extract import module_name_for

        db.execute(
            "INSERT OR REPLACE INTO files VALUES (?,?,?,?,?,?,?,?,?,?)",
            (path, sha, size, mtime, ctime, module_name_for(path), status, error, int(path.startswith("tests/")), time.time()),
        )

    @staticmethod
    def _store_extract(db: sqlite3.Connection, path: str, extract: FileExtract) -> None:
        sids: Dict[str, int] = {}
        for s in extract.symbols:
            cursor = db.execute(
                "INSERT INTO symbols (id, path, qualname, name, fqname, kind, line, end_line, signature, doc, parent) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    symbol_id(path, s.qualname),
                    path,
                    s.qualname,
                    s.name,
                    s.fqname,
                    s.kind,
                    s.line,
                    s.end_line,
                    s.signature,
                    s.doc,
                    symbol_id(path, s.parent) if s.parent else None,
                ),
            )
            sids[s.qualname] = cursor.lastrowid
        db.executemany(
            "INSERT INTO edges VALUES (?,?,?,?,?,?,?)",
            [
                (sids[e.src], e.kind, e.target, e.line, e.confidence, e.mechanism, e.detail)
                for e in extract.edges
                if e.src in sids
            ],
        )

    def _domain_current(self, db: sqlite3.Connection) -> bool:
        from .domain import domain_inputs_digest

        row = db.execute("SELECT value FROM meta WHERE key='domain_digest'").fetchone()
        return bool(row) and row[0] == domain_inputs_digest(self, db)

    # -- freshness -----------------------------------------------------------
    def file_is_current(self, path: str) -> bool:
        """Re-hash *path* now and compare with the indexed hash."""
        row = self.db.execute("SELECT sha256 FROM files WHERE path=?", (path,)).fetchone()
        if row is None:
            return False
        try:
            raw = self.scope.resolve(path).read_bytes()
        except (OSError, ScopeError):
            return False
        return hashlib.sha256(raw).hexdigest() == row[0]

    def freshness(self) -> Dict[str, object]:
        from .._git import GitError, GitInspector

        db = self.db
        counts = dict(db.execute("SELECT status, COUNT(*) FROM files GROUP BY status").fetchall())
        refreshed = db.execute("SELECT value FROM meta WHERE key='refreshed_at'").fetchone()
        domain = db.execute("SELECT value FROM meta WHERE key='domain_status'").fetchone()
        git = GitInspector(self.scope.root)
        try:
            head = git.head()
        except GitError:
            head = None
        last = {
            key: value
            for key, value in self.last_refresh.items()
            if key in ("scanned", "reindexed", "removed", "duration_ms", "full")
        }
        return {
            "index_schema": SCHEMA_VERSION,
            "git_head": head,
            "files_indexed": sum(counts.values()),
            "files_by_status": counts,
            "refreshed_at": float(refreshed[0]) if refreshed else None,
            "verified_current": bool(self.last_refresh),
            "last_refresh": last,
            "domain_extractors": domain[0] if domain else "not-built",
        }

    # -- small query helpers ---------------------------------------------------
    def parse_errors(self, paths: Optional[Iterable[str]] = None) -> List[dict]:
        db = self.db
        rows = db.execute("SELECT path, status, error FROM files WHERE status != 'ok' ORDER BY path").fetchall()
        wanted = set(paths) if paths is not None else None
        return [
            {"path": path, "status": status, "error": error}
            for path, status, error in rows
            if wanted is None or path in wanted
        ]
