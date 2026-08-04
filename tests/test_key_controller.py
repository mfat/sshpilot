"""GTK-free SSH-key controller tests."""

import ast
import threading
from pathlib import Path

import pytest

from sshpilot.api.models.keys import (
    GenerateKeyRequest,
    GenerateKeyResult,
    KeyId,
    KeyList,
    KeyStoreScope,
    KeySummary,
    PublicKeyResult,
    ReadPublicKeyRequest,
)
from sshpilot.gtk.key_controller import KeyController

def _summary(key_id="key-1", name="id_ed25519"):
    private_path = f"/home/user/.ssh/{name}"
    return KeySummary(
        key_id=KeyId(key_id),
        name=name,
        private_path=private_path,
        public_path=private_path + ".pub",
        public_key_available=True,
    )


class _FakeClient:
    def __init__(self):
        self.list_scopes: list[KeyStoreScope] = []
        self.read_requests: list[ReadPublicKeyRequest] = []
        self.generate_requests: list[GenerateKeyRequest] = []
        self.key_list = KeyList(keys=(_summary(),))
        self.read_result = PublicKeyResult(
            key_id=KeyId("key-1"),
            text="ssh-ed25519 AAAA-public\n",
        )
        self.generate_result = GenerateKeyResult(key=_summary("key-2", "new_key"))
        self.fail_list = False
        self.fail_read = False
        self.fail_generate = False
        self.blocked = None  # threading.Event to hold an RPC open

    def list_keys(self, request):
        self.list_scopes.append(request.scope)
        if self.blocked is not None:
            self.blocked.wait(5)
        if self.fail_list:
            raise RuntimeError("list boom")
        return self.key_list

    def read_public_key(self, request):
        self.read_requests.append(request)
        if self.blocked is not None:
            self.blocked.wait(5)
        if self.fail_read:
            raise RuntimeError("read boom")
        return self.read_result

    def generate_key(self, request):
        self.generate_requests.append(request)
        if self.blocked is not None:
            self.blocked.wait(5)
        if self.fail_generate:
            raise RuntimeError("generate boom")
        return self.generate_result


def _controller(client, scope=KeyStoreScope.DEFAULT):
    return KeyController(client, scope)


# ---------------------------------------------------------------------------
# Request shaping
# ---------------------------------------------------------------------------
def test_list_keys_sends_scope():
    client = _FakeClient()
    controller = _controller(client)
    result = controller.list_keys()
    assert client.list_scopes == [KeyStoreScope.DEFAULT]
    assert result.keys[0].key_id == "key-1"


def test_list_keys_uses_isolated_scope():
    client = _FakeClient()
    controller = _controller(client, scope=KeyStoreScope.ISOLATED)
    controller.list_keys()
    assert client.list_scopes == [KeyStoreScope.ISOLATED]


def test_read_public_key_sends_scope_and_id():
    client = _FakeClient()
    controller = _controller(client)
    result = controller.read_public_key(KeyId("key-1"))
    assert client.read_requests == [
        ReadPublicKeyRequest(key_id=KeyId("key-1"), scope=KeyStoreScope.DEFAULT)
    ]
    assert result.text == "ssh-ed25519 AAAA-public\n"


def test_generate_key_request_fields():
    client = _FakeClient()
    controller = _controller(client)
    controller.generate_key(
        name="my_key",
        key_type="rsa",
        key_size=3072,
        comment="work",
        passphrase="secret",
    )
    assert client.generate_requests == [
        GenerateKeyRequest(
            name="my_key",
            key_type="rsa",
            key_size=3072,
            comment="work",
            passphrase="secret",
            scope=KeyStoreScope.DEFAULT,
        )
    ]


# ---------------------------------------------------------------------------
# Caching and snapshot
# ---------------------------------------------------------------------------
def test_list_keys_caches_and_exposes_read_only_snapshot():
    client = _FakeClient()
    controller = _controller(client)
    controller.list_keys()
    snapshot = controller.key_snapshot()
    assert [k.key_id for k in snapshot] == ["key-1"]
    assert type(snapshot) is tuple


def test_snapshot_empty_before_first_load():
    client = _FakeClient()
    controller = _controller(client)
    assert controller.key_snapshot() == ()


def test_generate_refreshes_cached_list_without_duplicates():
    client = _FakeClient()
    controller = _controller(client)
    controller.list_keys()
    controller.generate_key(name="new_key")
    ids = [k.key_id for k in controller.key_snapshot()]
    assert ids == ["key-1", "key-2"]
    # A second generation of the same key refreshes rather than duplicating.
    client.generate_result = GenerateKeyResult(key=_summary("key-2", "new_key"))
    controller.generate_key(name="new_key")
    ids = [k.key_id for k in controller.key_snapshot()]
    assert ids == ["key-1", "key-2"]


# ---------------------------------------------------------------------------
# Overlap rejection
# ---------------------------------------------------------------------------
def test_overlapping_calls_are_rejected():
    client = _FakeClient()
    controller = _controller(client)
    client.blocked = threading.Event()
    results = []

    def worker():
        try:
            results.append(("list", controller.list_keys()))
        except BaseException as exc:  # pragma: no cover - thread bookkeeping
            results.append(("error", exc))

    thread = threading.Thread(target=worker)
    thread.start()
    try:
        with pytest.raises(RuntimeError):
            controller.read_public_key(KeyId("key-1"))
        with pytest.raises(RuntimeError):
            controller.generate_key(name="x")
        with pytest.raises(RuntimeError):
            controller.list_keys()
    finally:
        client.blocked.set()
        thread.join(5)


def test_busy_flag_reset_after_list_error():
    client = _FakeClient()
    client.fail_list = True
    controller = _controller(client)
    with pytest.raises(RuntimeError):
        controller.list_keys()
    # The next operation must be allowed.
    client.fail_list = False
    assert controller.list_keys().keys[0].key_id == "key-1"


def test_busy_flag_reset_after_read_error():
    client = _FakeClient()
    client.fail_read = True
    controller = _controller(client)
    with pytest.raises(RuntimeError):
        controller.read_public_key(KeyId("key-1"))
    client.fail_read = False
    assert controller.read_public_key(KeyId("key-1")).key_id == "key-1"


def test_busy_flag_reset_after_generate_error():
    client = _FakeClient()
    client.fail_generate = True
    controller = _controller(client)
    with pytest.raises(RuntimeError):
        controller.generate_key(name="x")
    client.fail_generate = False
    controller.generate_key(name="x")
    assert any(k.name == "new_key" for k in controller.key_snapshot())


# ---------------------------------------------------------------------------
# Passphrase handling
# ---------------------------------------------------------------------------
def test_passphrase_not_retained_on_controller():
    client = _FakeClient()
    controller = _controller(client)
    controller.generate_key(name="my_key", passphrase="super-secret")
    assert "super-secret" not in repr(controller)
    assert "passphrase" not in vars(controller)
    # The GenerateKeyRequest is not stored on the controller either.
    assert not any("request" in key for key in vars(controller))


def test_result_repr_has_no_secret():
    client = _FakeClient()
    controller = _controller(client)
    result = controller.generate_key(name="my_key", passphrase="super-secret")
    assert "super-secret" not in repr(result)
    assert "super-secret" not in str(result)


# ---------------------------------------------------------------------------
# No filesystem or GTK imports
# ---------------------------------------------------------------------------
def test_controller_has_no_filesystem_or_gtk_imports():
    path = Path(__file__).resolve().parents[1] / "src" / "sshpilot" / "gtk" / "key_controller.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    forbidden = {"gi", "Gtk", "GLib", "os", "pathlib", "subprocess", "shutil"}
    hits = sorted(name for name in imported if name.split(".")[0] in forbidden)
    assert not hits, f"key_controller.py imports forbidden modules: {hits}"
