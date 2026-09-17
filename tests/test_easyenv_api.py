"""The API client, against a fake urlopen."""

import io
import json
import urllib.error

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

api = load("easyenv_api")


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class Opener:
    def __init__(self, answers):
        self.answers = list(answers)
        self.requests = []

    def __call__(self, req, timeout):
        self.requests.append(req)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return FakeResponse(json.dumps(answer).encode() if answer is not None else b"")


def http_error(code, body):
    return urllib.error.HTTPError("https://x", code, "err", {}, io.BytesIO(body.encode()))


def test_every_request_carries_the_token_and_the_chosen_account():
    opener = Opener([[]])
    api.Client("tok", "acct", opener=opener).workspaces()
    req = opener.requests[0]
    assert req.get_header("X-service-token") == "tok"
    assert req.get_header("Account-id") == "acct"
    assert req.get_header("User-agent").startswith("sshpilot-easyenv/")


def test_the_account_list_is_asked_for_without_an_account():
    opener = Opener([[]])
    api.Client("tok", "acct", opener=opener).accounts()
    assert opener.requests[0].get_header("Account-id") is None


def test_a_list_is_read_to_its_last_page():
    opener = Opener([
        {"results": [{"uuid": "a"}], "links": {"next": "https://api.example/v1/workspaces/?page=2"}},
        {"results": [{"uuid": "b"}], "links": {"next": None}},
    ])
    items = api.Client("tok", opener=opener, server="https://api.example").workspaces()
    assert [i["uuid"] for i in items] == ["a", "b"]
    assert "page_size=200" in opener.requests[0].full_url
    assert opener.requests[1].full_url == "https://api.example/v1/workspaces/?page=2"


def test_recipes_keep_their_filter_next_to_the_page_size():
    opener = Opener([[{"uuid": "r"}]])
    assert api.Client("tok", opener=opener).recipes() == [{"uuid": "r"}]
    assert opener.requests[0].full_url.endswith("?is_used_in_workspace=true&page_size=200")


def test_a_rejected_token_is_said_in_words_and_flagged():
    opener = Opener([http_error(401, '{"errors": [{"field": "detail", "message": "bad"}]}')])
    with pytest.raises(api.ApiError) as err:
        api.Client("tok", opener=opener).me()
    assert err.value.unauthorized
    assert "did not accept the token" in str(err.value)


def test_a_bad_token_is_rejected_with_a_403_on_the_real_api():
    body = json.dumps({"errors": [{"field": "detail", "message": "Invalid service token"}]})
    with pytest.raises(api.ApiError) as err:
        api.Client("tok", opener=Opener([http_error(403, body)])).me()
    assert err.value.unauthorized
    assert str(err.value) == "EasyEnv did not accept the token"


def test_a_403_about_permissions_is_not_a_rejected_token():
    body = json.dumps({"errors": [{"field": "detail", "message": "Only admins can do that"}]})
    with pytest.raises(api.ApiError) as err:
        api.Client("tok", opener=Opener([http_error(403, body)])).delete("ws")
    assert not err.value.unauthorized
    assert "does not allow" in str(err.value)


def test_running_out_of_hours_is_recognised():
    body = json.dumps({"errors": [{"field": "non_field_errors",
                                   "message": ["Insufficient account total time."]}]})
    opener = Opener([http_error(400, body)])
    with pytest.raises(api.ApiError) as err:
        api.Client("tok", opener=opener).start("ws")
    assert err.value.out_of_time
    assert "Insufficient account total time" in str(err.value)


def test_nested_field_errors_are_flattened():
    body = json.dumps({"errors": [{"field": "boxes", "message": [{"recipe": ["Unknown recipe."]}]}]})
    assert api.error_detail(body) == "boxes.recipe: Unknown recipe."


def test_an_unreachable_server_is_an_api_error_not_a_traceback():
    opener = Opener([urllib.error.URLError("no route")])
    with pytest.raises(api.ApiError, match="could not reach EasyEnv"):
        api.Client("tok", opener=opener).me()
    opener = Opener([ConnectionResetError("reset")])
    with pytest.raises(api.ApiError, match="did not answer"):
        api.Client("tok", opener=opener).me()


def test_ids_are_quoted_into_paths():
    opener = Opener([None])
    api.Client("tok", opener=opener, server="https://h").delete("a/b")
    assert opener.requests[0].full_url == "https://h/v1/workspaces/a%2Fb/"
    assert opener.requests[0].get_method() == "DELETE"
