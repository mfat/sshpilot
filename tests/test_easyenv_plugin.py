"""The plugin's wiring: what each click asks of the API and of sshPilot.

Needs sshPilot importable (its SDK is the plugin's base class) but no display:
the page is a recording fake, and worker threads run inline.
"""

import os

import pytest


import importlib.util as _ilu
import os as _os
import sys as _sys

from sshpilot.plugins import registry as _registry

# The plugin ships as an example beside the SDK; its modules are loaded from
# there, the way sshPilot's loader loads a user plugin.
_ROOT = _os.path.join(_os.path.dirname(_registry.__file__), "examples", "easyenv_workspaces")


def load(name):
    spec = _ilu.spec_from_file_location(f"easyenv_example_{name}",
                                        _os.path.join(_ROOT, f"{name}.py"))
    module = _ilu.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_plugin():
    package = "sshpilot_easyenv_example_under_test"
    if package in _sys.modules:
        return _sys.modules[package]
    spec = _ilu.spec_from_file_location(package, _os.path.join(_ROOT, "__init__.py"),
                                        submodule_search_locations=[_ROOT])
    module = _ilu.module_from_spec(spec)
    _sys.modules[package] = module
    spec.loader.exec_module(module)
    return module

plugin_mod = load_plugin()
from importlib import import_module  # noqa: E402

api = import_module(f"{plugin_mod.__name__}.easyenv_api")
proxy = import_module(f"{plugin_mod.__name__}.cli_config")
ApiError = api.ApiError

ACCOUNT = {"uuid": "acct", "title": "me@example.com",
           "total_time_seconds": 3600 * 30,
           "current_plan": {"plan": {"monthly_compute_seconds": 3600 * 40}}}
BOX = {"uuid": "Box1", "title": "node", "recipe": {"uuid": "r1", "title": "Ubuntu"}}


def running(uuid="ws1", title="dev", **extra):
    d = {"uuid": uuid, "title": title, "status": "active", "boxes": [dict(BOX)],
         "remaining_time": 3_600_000}
    d.update(extra)
    return d


class FakeClient:
    """Answers from class attributes; records every call."""
    calls = []
    accounts_answer = [ACCOUNT]
    workspaces_answer = []
    details = {}
    fail = {}
    keys = []

    def __init__(self, token, account=None, server=None):
        self.token, self.account, self.server = token, account, server

    def _call(self, name, *args):
        FakeClient.calls.append((name, self.token, self.account) + args)
        if name in FakeClient.fail:
            raise FakeClient.fail[name]

    def me(self):
        self._call("me")
        return {"email": "me@example.com", "personal_account": {"uuid": "acct"}}

    def accounts(self):
        self._call("accounts")
        return list(FakeClient.accounts_answer)

    def workspaces(self):
        self._call("workspaces")
        return list(FakeClient.workspaces_answer)

    def workspace(self, uuid):
        self._call("workspace", uuid)
        return FakeClient.details[uuid]

    def recipes(self):
        self._call("recipes")
        return [{"uuid": "r1", "title": "Ubuntu"}]

    def templates(self):
        self._call("templates")
        return [{"uuid": "k8s", "title": "Kubernetes", "account": None,
                 "recipes": [{"uuid": "k3s", "title": "K3s"}],
                 "template_recipes": [{"recipe": "k3s"}],
                 "stacks": [{"stack_recipe": "helm", "stack_title": "Helm",
                             "recipe": "k3s", "vars": {}}]}]

    def stack_recipes(self):
        self._call("stack_recipes")
        return [{"name": "python", "title": "Python", "tags": ["language"],
                 "vars_schema": [{"key": "version", "type": "select",
                                  "options": ["3.13", "3.12"], "defaultValue": "3.13"}]},
                {"name": "ansible", "title": "Ansible", "tags": ["script"],
                 "vars_schema": [{"key": "playbook", "type": "code"}]}]

    def create_workspace(self, body):
        self._call("create", body)
        return {"uuid": "new1", **body}

    def start(self, uuid):
        self._call("start", uuid)

    def stop(self, uuid):
        self._call("stop", uuid)

    def delete(self, uuid):
        self._call("delete", uuid)

    def ssh_keys(self):
        self._call("ssh_keys")
        return list(FakeClient.keys)

    def add_ssh_key(self, public_key, label=""):
        self._call("add_ssh_key", public_key, label)
        return {"uuid": "k1"}


class Page:
    def __init__(self):
        self.state = None
        self.events = []

    def show(self, state):
        self.state = state
        self.events.append(("show", state))

    def show_signed_out(self, message=""):
        self.state = None
        self.events.append(("signed_out", message))

    def signing_in_failed(self, message):
        self.events.append(("signin_failed", message))

    def set_status(self, text):
        self.events.append(("status", text))

    def set_busy(self, uuid, on):
        self.events.append(("busy", uuid, on))

    def set_refreshing(self, on):
        pass

    def kinds(self):
        return [e[0] for e in self.events]


class Info:
    def __init__(self, nickname):
        self.nickname = nickname


class Store(dict):
    def get(self, key, default=None):
        return super().get(key, default)

    def set(self, key, value):
        self[key] = value

    def delete(self, key):
        self.pop(key, None)


class Ctx:
    def __init__(self):
        self.notes = []
        self.saved = {}
        self.opened = []
        self.settings = Store()
        self.secrets = Store()
        self.ui = self
        self.events = self

    # ui / events
    def register_page(self, *a, **k):
        self.page_args = a

    def subscribe(self, event, fn):
        self.started = fn

    def notify(self, text, timeout=3):
        self.notes.append(text)

    def run_on_ui_thread(self, fn, *args):
        fn(*args)

    # connections
    def list_connections(self):
        return [Info(n) for n in self.saved]

    def add_connection(self, data):
        self.saved[data["nickname"]] = data

    def update_connection(self, nickname, data):
        if nickname not in self.saved:
            return False
        self.saved[nickname] = data
        return True

    def open_connection(self, nickname):
        self.opened.append(nickname)
        return True

    def create_group(self, name):
        return "g"

    def add_connection_to_group(self, nickname, group):
        return True


class InlineThread:
    def __init__(self, target, daemon=None, name=None):
        self.target = target

    def start(self):
        self.target()


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    for var in ("EASYENV_SERVER", "EASYENV_TOKEN", "EASYENV_ACCOUNT", "EASYENV_WEB_URL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(plugin_mod.threading, "Thread", InlineThread)
    FakeClient.calls = []
    FakeClient.accounts_answer = [ACCOUNT]
    FakeClient.workspaces_answer = []
    FakeClient.details = {}
    FakeClient.fail = {}
    FakeClient.keys = []
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(plugin_mod.Plugin, "client_class", FakeClient)
    p = plugin_mod.Plugin()
    ctx = Ctx()
    p.activate(ctx)
    p.page = Page()
    return p, ctx


def sign_in_file(**values):
    proxy.write_config({"service_token": "tok", "default_account": "acct", **values})


def test_with_no_token_the_page_asks_to_sign_in(env):
    p, _ = env
    p.refresh()
    assert p.page.events == [("signed_out", "")]
    assert FakeClient.calls == []


def test_refresh_shows_the_account_and_fetches_details_only_for_live_workspaces(env):
    p, _ = env
    sign_in_file()
    FakeClient.workspaces_answer = [
        running(), {"uuid": "old", "title": "old", "status": "stopped", "boxes": []}]
    detail = running()
    detail["boxes"][0].update(vm_password="pw", ssh_username="dev", status="started")
    FakeClient.details = {"ws1": detail}
    p.refresh()
    state = p.page.state
    assert state["account"]["credit"]["text"] == "1d 6h left"
    assert [(w["uuid"], w["state"]) for w in state["workspaces"]] == [
        ("ws1", "running"), ("old", "ended")]
    assert state["workspaces"][0]["machines"][0]["password"] == "pw"
    assert [c[0] for c in FakeClient.calls] == ["accounts", "workspaces", "workspace", "ssh_keys"]
    # Listed with the saved account.
    assert FakeClient.calls[1][2] == "acct"


def test_a_saved_account_that_is_gone_is_replaced_in_the_config_too(env):
    p, _ = env
    sign_in_file(default_account="gone")
    p.refresh()
    assert proxy.read_config()["default_account"] == "acct"
    assert p.page.state["account"]["uuid"] == "acct"


def test_a_token_the_api_stopped_accepting_signs_the_page_out(env):
    p, _ = env
    sign_in_file()
    FakeClient.fail = {"accounts": ApiError("EasyEnv did not accept the token", 401)}
    p.refresh()
    kind, message = p.page.events[-1]
    assert kind == "signed_out" and "no longer accepts" in message


def test_a_failed_refresh_says_why_and_the_next_one_still_runs(env):
    p, _ = env
    sign_in_file()
    FakeClient.fail = {"workspaces": ApiError("could not reach EasyEnv: timeout")}
    p.refresh()
    assert ("status", "Could not refresh: could not reach EasyEnv: timeout") in p.page.events
    FakeClient.fail = {}
    p.refresh()
    assert p.page.state["account"]["uuid"] == "acct"


def test_a_rejected_token_is_not_saved(env):
    p, _ = env
    sign_in_file(service_token="good")
    FakeClient.fail = {"me": ApiError("no", 401)}
    p.sign_in("bad")
    assert p.page.kinds() == ["signin_failed"]
    assert "did not accept that token" in p.page.events[0][1]
    assert proxy.read_config()["service_token"] == "good"


def test_a_good_token_is_saved_for_the_cli_and_the_list_shown(env):
    p, ctx = env
    p.sign_in("fresh")
    config = proxy.read_config()
    assert (config["service_token"], config["default_account"]) == ("fresh", "acct")
    assert oct(os.stat(proxy.config_path()).st_mode & 0o777) == "0o600"
    assert ctx.notes == ["Signed in to EasyEnv as me@example.com"]
    assert p.page.kinds()[-1] == "show"


def test_sign_out_removes_the_token_and_keeps_the_rest(env):
    p, _ = env
    sign_in_file(server="https://api.example")
    p.sign_out()
    config = proxy.read_config()
    assert "service_token" not in config
    assert config["server"] == "https://api.example"
    assert p.page.events[-1] == ("signed_out", "")


def test_a_result_for_the_account_before_a_switch_is_dropped(env, monkeypatch):
    p, _ = env
    sign_in_file()
    FakeClient.accounts_answer = [ACCOUNT, {**ACCOUNT, "uuid": "team", "title": "team"}]
    held = []
    monkeypatch.setattr(plugin_mod.threading, "Thread",
                        lambda target, daemon=None, name=None: held.append(target) or InlineThread(lambda: None))
    p.refresh()                  # for acct, still in flight
    p.switch_account("team")     # asks again, queued behind the first
    held[0]()                    # the first one lands
    assert p.page.state is None
    assert len(held) == 2        # and the queued refresh for team went out
    held[1]()
    assert p.page.state["account"]["uuid"] == "team"


def test_ssh_saves_the_machine_through_the_broker_and_opens_it(env):
    p, ctx = env
    sign_in_file()
    detail = running(title="My lab")
    detail["boxes"][0].update(vm_password="pw", ssh_username="dev", status="started")
    FakeClient.details = {"ws1": detail}
    view = plugin_mod.model.workspace_view(running(title="My lab"))
    p.ssh(view, view["machines"][0])
    assert ctx.opened == ["My-lab"]
    data = ctx.saved["My-lab"]
    assert data["extra_ssh_config"].splitlines()[0].endswith("-W %h:%p easyenv@ssh.easyenv.io")
    assert data["hostname"] == "Box1.box.easyenv.io"
    assert (data["username"], data["password"], data["auth_method"]) == ("dev", "", 0)
    # Opened again: updated, not added twice.
    p.ssh(view, view["machines"][0])
    assert ctx.opened == ["My-lab", "My-lab"]
    assert list(ctx.saved) == ["My-lab"]


def test_a_connection_sshpilot_will_not_save_is_said_not_raised(env):
    p, ctx = env
    sign_in_file()
    FakeClient.details = {"ws1": running()}

    def refuse(data):
        raise RuntimeError("daemon is busy")

    ctx.add_connection = refuse
    view = plugin_mod.model.workspace_view(running())
    p.ssh(view, view["machines"][0])
    assert ctx.opened == []
    assert ctx.notes[-1] == "Could not save node as a connection: daemon is busy"


def test_create_creates_then_starts(env):
    p, ctx = env
    sign_in_file()
    p.create("dev", [{"recipe": {"uuid": "r1", "title": "Ubuntu"}, "stacks": [("python", {"version": "3.12"})], "size": None}], 2, "hours")
    names = [c[0] for c in FakeClient.calls]
    assert names[:2] == ["create", "start"]
    assert FakeClient.calls[0][3]["duration"] == 2
    assert "workspace_template" not in FakeClient.calls[0][3]
    assert FakeClient.calls[0][3]["boxes"][0]["stacks"] == [
        {"stack_recipe": "python", "vars": {"version": "3.12"}}]
    assert FakeClient.calls[1][3] == "new1"
    assert ctx.notes[0] == "dev is starting"


def test_out_of_hours_on_start_says_so_and_refreshes_the_hours(env):
    p, ctx = env
    sign_in_file()
    FakeClient.fail = {"start": ApiError("EasyEnv answered 400: Insufficient account total time.", 400)}
    p.create("dev", [{"uuid": "r1", "title": "Ubuntu"}], 1, "hours")
    assert ctx.notes[0] == "Created dev but could not start it: this account has no lab hours left"
    assert "accounts" in [c[0] for c in FakeClient.calls]


def test_run_again_copies_the_machines_and_duration(env):
    p, _ = env
    sign_in_file()
    ended = {"uuid": "old", "title": "dev", "status": "stopped", "duration": 4,
             "duration_unit": "hours",
             "boxes": [{"uuid": "b1", "title": "web", "recipe": {"uuid": "r1"}},
                       {"uuid": "b2", "title": "db", "recipe": "r2"}]}
    p.run_again(plugin_mod.model.workspace_view(ended))
    body = FakeClient.calls[0][3]
    assert body == {"title": "dev", "duration": 4, "duration_unit": "hours", "boxes": [
        {"title": "web", "recipe": "r1", "position": 0},
        {"title": "db", "recipe": "r2", "position": 1}]}
    assert FakeClient.calls[1][:1] == ("start",)


def test_stop_marks_the_workspace_busy_until_the_api_answers(env):
    p, ctx = env
    sign_in_file()
    view = plugin_mod.model.workspace_view(running())
    p.stop(view)
    assert ("stop", "tok", "acct", "ws1") in FakeClient.calls
    assert ("busy", "ws1", True) in p.page.events
    assert ("busy", "ws1", False) in p.page.events
    assert ctx.notes[0] == "Stopping dev"


def test_the_first_versions_keyring_token_is_moved_once(env):
    p, ctx = env
    ctx.settings.set("account_uuid", "acct")
    ctx.secrets.set("service_token", "from-keyring")
    ctx.started(None)
    assert proxy.read_config()["service_token"] == "from-keyring"
    assert "service_token" not in ctx.secrets
    ctx.secrets.set("service_token", "again")
    ctx.started(None)
    assert ctx.secrets["service_token"] == "again"


def test_a_profile_the_first_version_never_signed_in_does_not_touch_the_keyring(env):
    p, ctx = env

    class Loud(Store):
        def get(self, key, default=None):
            raise AssertionError("keyring read")

    ctx.secrets = Loud()
    ctx.started(None)


def test_the_catalog_is_fetched_once_per_account(env):
    p, _ = env
    sign_in_file()
    got = []
    p.load_catalog(lambda *a: got.append(a))
    p.load_catalog(lambda *a: got.append(a))
    assert [c[0] for c in FakeClient.calls] == ["recipes", "stack_recipes", "templates"]
    assert got[0] == got[1]
    recipes, stacks, templates, error = got[0]
    assert recipes == [{"uuid": "r1", "title": "Ubuntu"}] and error is None
    assert [s["name"] for s in stacks] == ["python"]
    assert [t["title"] for t in templates] == ["Kubernetes"]
    assert templates[0]["machines"][0]["stacks"] == [("helm", "Helm", {})]


def test_a_stack_catalog_that_fails_leaves_recipes_working(env):
    p, _ = env
    sign_in_file()
    FakeClient.fail = {"stack_recipes": ApiError("EasyEnv answered 500", 500),
                       "templates": ApiError("EasyEnv answered 500", 500)}
    got = []
    p.load_catalog(lambda *a: got.append(a))
    assert got == [([{"uuid": "r1", "title": "Ubuntu"}], [], [], None)]


def test_details_carry_sizes_and_stacks(env):
    p, _ = env
    sign_in_file()
    detail = running()
    detail["boxes"][0].update(cpu=2, ram_mb=4096, storage_gb=16,
                              stacks=[{"stack_recipe": "python", "vars": {"version": "3.13"}}])
    FakeClient.details = {"ws1": detail}
    got = []
    p.details({"uuid": "ws1"}, lambda v, e: got.append((v, e)))
    view, error = got[0]
    m = view["machines"][0]
    assert error is None
    assert m["size"] == {"cpu": 2, "ram_mb": 4096, "storage_gb": 16}
    assert [(s["title"], s["version"]) for s in m["stacks"]] == [("Python", "3.13")]
    assert m["stacks"][0]["logo"] == "https://dashboard.easyenv.io/images/stacks/python.svg"


ED = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIHH6Sdcy0UKpv2dY1JZcL+BrDH9QBudJrV5tW4R8k4u1 me@host"


def test_the_page_is_told_when_this_computers_key_is_not_on_the_profile(env, tmp_path):
    p, _ = env
    sign_in_file()
    (tmp_path / ".ssh").mkdir()
    (tmp_path / ".ssh" / "id_ed25519.pub").write_text(ED + "\n")
    p.refresh()
    assert p.page.state["key_status"] == "upload"
    FakeClient.keys = [{"fingerprint": plugin_mod.model.key_fingerprint(ED)}]
    p.refresh()
    assert p.page.state["key_status"] == "ok"


def test_a_key_list_the_api_will_not_give_does_not_break_the_page(env):
    p, _ = env
    sign_in_file()
    FakeClient.fail = {"ssh_keys": ApiError("EasyEnv answered 500", 500)}
    p.refresh()
    assert p.page.state["key_status"] == "unknown"


def test_adding_this_computers_key_sends_the_public_half(env, tmp_path):
    p, ctx = env
    sign_in_file()
    (tmp_path / ".ssh").mkdir()
    (tmp_path / ".ssh" / "id_ed25519.pub").write_text(ED + "\n")
    (tmp_path / ".ssh" / "id_ed25519").write_text("PRIVATE KEY\n")
    p.add_this_key()
    call = next(c for c in FakeClient.calls if c[0] == "add_ssh_key")
    assert call[3] == ED
    assert "PRIVATE" not in repr(FakeClient.calls)
    assert ctx.notes[0].startswith("Added id_ed25519.pub to your EasyEnv profile")
