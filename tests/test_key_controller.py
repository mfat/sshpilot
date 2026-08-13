"""GTK-free SSH-key controller tests."""

import ast
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from sshpilot.api.events import EventType
from sshpilot.api.models import InteractionId, InteractionType, SessionId
from sshpilot.api.models.keys import (
    GenerateKeyRequest,
    GenerateKeyResult,
    KeyId,
    KeyList,
    KeyStoreScope,
    KeySummary,
    PublicKeyResult,
    ReadPublicKeyRequest,
    VerifyKeyPassphraseResult,
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
        self.verify_requests = []
        self.store_requests = []
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
        self._event_callback = None
        self.sent_secrets = []
        self.fail_subscribe = False
        self.fail_verify = False

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

    def subscribe_events(self, callback):
        if self.fail_subscribe:
            raise RuntimeError("subscription failed")
        self._event_callback = callback
        return SimpleNamespace(close=lambda: None)

    def verify_key_passphrase(self, request):
        self.verify_requests.append(request)
        self._publish_passphrase_interaction(request.interaction_scope_id)
        for _ in range(100):
            if self.sent_secrets:
                break
            threading.Event().wait(0.01)
        if self.fail_verify:
            raise RuntimeError("verification failed")
        return VerifyKeyPassphraseResult(valid=bool(self.sent_secrets))

    def store_key_passphrase(self, request):
        self.store_requests.append(request)
        self._publish_passphrase_interaction(request.interaction_scope_id)
        for _ in range(100):
            if self.sent_secrets:
                break
            threading.Event().wait(0.01)
        return bool(self.sent_secrets)

    def _publish_passphrase_interaction(self, scope_id):
        summary = SimpleNamespace(
            id=InteractionId("interaction-key-1"),
            session_id=scope_id,
            type=InteractionType.PRIVATE_KEY_PASSPHRASE,
        )
        self._event_callback(
            SimpleNamespace(type=EventType.INTERACTION_CREATED, payload=summary)
        )

    def claim_interaction(self, interaction_id):
        return SimpleNamespace(nonce="nonce-key-1")

    def respond_to_interaction(self, request):
        return None

    def send_interaction_secret(self, interaction_id, nonce, secret):
        self.sent_secrets.append(bytes(secret))
        secret[:] = b"\0" * len(secret)
        secret.clear()


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
        encrypted=True,
        interaction_scope_id=SessionId("key-operation-generate-1"),
    )
    assert client.generate_requests == [
        GenerateKeyRequest(
            name="my_key",
            key_type="rsa",
            key_size=3072,
            comment="work",
            encrypted=True,
            interaction_scope_id=SessionId("key-operation-generate-1"),
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
# Request-model construction failures must not poison the busy flag
# ---------------------------------------------------------------------------
def test_invalid_generate_name_raises_and_leaves_controller_usable():
    client = _FakeClient()
    controller = _controller(client)

    with pytest.raises(ValueError):
        controller.generate_key(name="bad\\name")
    # A failed request-model construction makes no client call.
    assert client.generate_requests == []
    # The controller remains usable afterwards.
    assert controller.list_keys().keys[0].key_id == "key-1"
    controller.generate_key(name="fine_name")
    assert any(k.name == "new_key" for k in controller.key_snapshot())


def test_invalid_generate_type_or_size_leaves_controller_usable():
    client = _FakeClient()
    controller = _controller(client)

    with pytest.raises(ValueError):
        controller.generate_key(name="x", key_type="dsa")
    with pytest.raises(ValueError):
        controller.generate_key(name="x", key_type="rsa", key_size=512)
    with pytest.raises(TypeError):
        controller.generate_key(name="x", key_size=True)
    assert client.generate_requests == []
    # Still usable for every operation type.
    assert controller.list_keys().keys[0].key_id == "key-1"
    assert controller.read_public_key(KeyId("key-1")).key_id == "key-1"


def test_validation_failure_makes_no_client_call():
    client = _FakeClient()
    controller = _controller(client)

    with pytest.raises(ValueError):
        controller.generate_key(name="a/b")
    with pytest.raises(ValueError):
        controller.generate_key(name="..")
    with pytest.raises(ValueError):
        controller.generate_key(name="x", comment="\x00")

    assert client.generate_requests == []
    assert client.list_scopes == []


def test_bad_returned_generation_dto_does_not_poison_busy():
    """A commit failure on the returned DTO resets the busy flag too."""
    client = _FakeClient()
    controller = _controller(client)

    # The client returns a broken payload: the summary upsert must fail and the
    # busy flag must still be released.
    client.generate_result = object()  # no .key attribute
    with pytest.raises(AttributeError):
        controller.generate_key(name="x")

    # Recover: a healthy result commits and refreshes the cache.
    client.generate_result = GenerateKeyResult(key=_summary("key-2", "new_key"))
    result = controller.generate_key(name="x")
    assert result.key.key_id == "key-2"
    assert [k.key_id for k in controller.key_snapshot()] == ["key-2"]


# ---------------------------------------------------------------------------
# Passphrase handling
# ---------------------------------------------------------------------------
def test_generate_request_carries_no_passphrase_field():
    client = _FakeClient()
    controller = _controller(client)
    controller.generate_key(name="my_key")
    assert "passphrase" not in client.generate_requests[0].__dataclass_fields__
    assert "passphrase" not in vars(controller)


def test_verify_sends_bytearray_only_through_secret_frame_and_clears_it():
    client = _FakeClient()
    controller = _controller(client)
    secret = bytearray(b"KEY_PASSPHRASE_SENTINEL_8F1C29")

    assert controller.verify_key_passphrase("/home/user/.ssh/id", secret) is True

    assert secret == bytearray()
    assert client.sent_secrets == [b"KEY_PASSPHRASE_SENTINEL_8F1C29"]
    assert len(client.verify_requests) == 1
    request = client.verify_requests[0]
    assert "passphrase" not in request.__dataclass_fields__
    assert request.interaction_scope_id.startswith("key-operation-")


def test_store_sends_bytearray_only_through_secret_frame_and_clears_it():
    client = _FakeClient()
    controller = _controller(client)
    secret = bytearray(b"KEY_PASSPHRASE_SENTINEL_8F1C29")

    assert controller.store_key_passphrase("/home/user/.ssh/id", secret) is True

    assert secret == bytearray()
    assert client.sent_secrets == [b"KEY_PASSPHRASE_SENTINEL_8F1C29"]
    request = client.store_requests[0]
    assert "passphrase" not in request.__dataclass_fields__
    assert request.interaction_scope_id.startswith("key-operation-")


@pytest.mark.parametrize("failure", ["subscribe", "verify"])
def test_verify_clears_secret_and_busy_state_on_transport_failures(failure):
    client = _FakeClient()
    client.fail_subscribe = failure == "subscribe"
    client.fail_verify = failure == "verify"
    controller = _controller(client)
    secret = bytearray(b"KEY_PASSPHRASE_SENTINEL_8F1C29")

    with pytest.raises(RuntimeError):
        controller.verify_key_passphrase("/home/user/.ssh/id", secret)

    assert secret == bytearray()
    assert controller.list_keys().keys


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
