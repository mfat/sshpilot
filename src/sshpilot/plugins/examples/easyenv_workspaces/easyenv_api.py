"""The EasyEnv REST API, as far as this plugin needs it.

Standard library only. Every call is blocking, so the plugin makes them off the
UI thread.

Auth is the same pair of headers the easyenv CLI sends: ``X-Service-Token`` on
every request, and ``Account-ID`` once an account has been chosen.
"""

from __future__ import annotations

import http.client
import json
import urllib.error
import urllib.parse
import urllib.request

USER_AGENT = "sshpilot-easyenv/2.6"
TIMEOUT_SECONDS = 30

#: The API pages its lists (ten a page by default, two hundred at most). The
#: first version of this plugin read the first page only, so an account with
#: eleven workspaces had one that never showed up.
PAGE_SIZE = 200
MAX_PAGES = 20


class ApiError(RuntimeError):
    """A request that did not succeed, with the API's own words when it gave any."""

    def __init__(self, message, status=None, detail=""):
        super().__init__(message)
        self.status = status
        self.detail = detail

    @property
    def unauthorized(self):
        return _rejects_token(self.status, self.detail)

    @property
    def out_of_time(self):
        text = f"{self} {self.detail}".lower()
        return ("insufficient account total time" in text
                or "no remaining time" in text)


def error_detail(body):
    """The readable part of the backend's error envelope.

    ``{"errors": [{"field": "boxes", "message": ...}]}``, where a message may
    be a string, a list, or a nested object for a serializer inside a list.
    """
    try:
        payload = json.loads(body)
    except (TypeError, ValueError):
        return (body or "").strip()[:300]
    parts = []

    def walk(prefix, value):
        if isinstance(value, str):
            text = value.strip()
            if text:
                parts.append(f"{prefix}: {text}" if prefix else text)
        elif isinstance(value, list):
            for item in value:
                walk(prefix, item)
        elif isinstance(value, dict):
            for key, item in value.items():
                walk(prefix if key in ("non_field_errors", "detail") else
                     (f"{prefix}.{key}" if prefix else key), item)

    if isinstance(payload, dict) and isinstance(payload.get("errors"), list):
        for item in payload["errors"]:
            if isinstance(item, dict):
                field = item.get("field") or ""
                walk("" if field in ("non_field_errors", "detail") else field,
                     item.get("message"))
    else:
        walk("", payload)
    return "; ".join(parts)[:500]


class Client:
    def __init__(self, token, account=None, server="https://api.easyenv.io",
                 opener=urllib.request.urlopen):
        self.token = token or ""
        self.account = account or ""
        self.server = (server or "https://api.easyenv.io").rstrip("/")
        self._open = opener

    # --- transport ---------------------------------------------------------

    def _url(self, path_or_url):
        if path_or_url.startswith(("http://", "https://")):
            return path_or_url
        return self.server + path_or_url

    def request(self, method, path, body=None, account=True):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(self._url(path), data=data, method=method)
        req.add_header("X-Service-Token", self.token)
        if account and self.account:
            req.add_header("Account-ID", self.account)
        req.add_header("Accept", "application/json")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        # Cloudflare in front of the API turns away urllib's default agent.
        req.add_header("User-Agent", USER_AGENT)
        try:
            with self._open(req, timeout=TIMEOUT_SECONDS) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            try:
                detail = error_detail(exc.read().decode("utf-8", "replace"))
            except Exception:  # noqa: BLE001 - the status alone still says something
                detail = ""
            raise ApiError(_status_sentence(exc.code, detail), exc.code, detail) from exc
        except urllib.error.URLError as exc:
            raise ApiError(f"could not reach EasyEnv: {exc.reason}") from exc
        except (http.client.HTTPException, OSError) as exc:
            raise ApiError(f"EasyEnv did not answer: {exc}") from exc
        if not raw:
            return None
        try:
            return json.loads(raw)
        except ValueError as exc:
            raise ApiError("EasyEnv answered with something that is not json") from exc

    def list_all(self, path, account=True):
        """Every item of a list, following the API's page links."""
        sep = "&" if "?" in path else "?"
        url = f"{path}{sep}page_size={PAGE_SIZE}"
        items = []
        for _ in range(MAX_PAGES):
            page = self.request("GET", url, account=account)
            if isinstance(page, list):
                return items + page
            if not isinstance(page, dict):
                return items
            items += [x for x in (page.get("results") or []) if isinstance(x, dict)]
            nxt = (page.get("links") or {}).get("next") or page.get("next")
            if not nxt:
                return items
            url = nxt
        return items

    # --- calls -------------------------------------------------------------

    def me(self):
        return self.request("GET", "/v1/profiles/user/me/", account=False) or {}

    def accounts(self):
        return self.list_all("/v1/accounts/", account=False)

    def recipes(self):
        return self.list_all("/v1/recipes/?is_used_in_workspace=true")

    def templates(self):
        return self.list_all("/v1/workspace_templates/")

    def stack_recipes(self):
        return self.list_all("/v1/stack-recipes/")

    def workspaces(self):
        return self.list_all("/v1/workspaces/")

    def workspace(self, uuid):
        return self.request("GET", f"/v1/workspaces/{_seg(uuid)}/") or {}

    def create_workspace(self, body):
        return self.request("POST", "/v1/workspaces/", body) or {}

    def start(self, uuid):
        return self.request("POST", f"/v1/workspaces/{_seg(uuid)}/start/", {})

    def stop(self, uuid):
        return self.request("POST", f"/v1/workspaces/{_seg(uuid)}/stop/", {})

    def delete(self, uuid):
        return self.request("DELETE", f"/v1/workspaces/{_seg(uuid)}/")

    def ssh_keys(self):
        """The SSH keys on the signed-in person's profile: what the gateway checks."""
        return self.list_all("/v1/profiles/me/ssh-keys/", account=False)

    def add_ssh_key(self, public_key, label=""):
        body = {"public_key": public_key}
        if label:
            body["label"] = label[:100]
        return self.request("POST", "/v1/profiles/me/ssh-keys/", body, account=False) or {}


def _seg(value):
    return urllib.parse.quote(str(value), safe="")


#: What the API says when the token itself is the problem. It says so with a
#: 403, not a 401: DRF answers 403 when the authentication class sends no
#: WWW-Authenticate header, and the service-token one sends none. Read as
#: "this account does not allow that", a bad token looked like a permission
#: problem and a revoked one never signed the page out.
_TOKEN_REJECTIONS = ("invalid service token", "user inactive or deleted",
                     "authentication credentials were not provided")


def _rejects_token(status, detail):
    if status == 401:
        return True
    return status == 403 and any(t in (detail or "").lower() for t in _TOKEN_REJECTIONS)


def _status_sentence(status, detail):
    if _rejects_token(status, detail):
        return "EasyEnv did not accept the token"
    if status == 403:
        return "this account does not allow that" + (f": {detail}" if detail else "")
    if status == 404:
        return "EasyEnv could not find it" + (f": {detail}" if detail else "")
    return f"EasyEnv answered {status}" + (f": {detail}" if detail else "")
