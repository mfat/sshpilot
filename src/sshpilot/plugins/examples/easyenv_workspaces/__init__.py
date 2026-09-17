"""EasyEnv Workspaces for sshPilot.

Sign in with the token the EasyEnv dashboard shows, see how much lab time the
account has left (and buy more when it runs out), create workspaces, and open
their machines as ordinary sshPilot SSH connections through EasyEnv's
connection broker, with no VPN, no public IP and no easyenv binary.

The modules, in the order a request goes through them:

- ``easyenv_api``: the REST API, standard library only.
- ``model``: what the page shows, worked out from the API's answers.
- ``page``: the widgets.
- ``connections``: a machine as an sshPilot connection, through EasyEnv's
  ssh gateway.
- ``cli_config``: the easyenv CLI's config file, where the token lives.
- ``logos``: recipe logos, cached on disk.

This file ties them together. Every network call runs on a worker thread and
comes back to the UI through ``ctx.run_on_ui_thread``.

**Where the token lives.** In the easyenv CLI's config file
(``~/.config/easyenv/config.yaml``, mode 0600), not in sshPilot's keyring. ssh
and the plugin share one login that way, and two copies of one credential
drift apart. Signing in here signs the CLI in, and ``easyenv auth login``
signs this plugin in.

**SSH needs a key on the profile.** The gateway admits a key registered on the
person's EasyEnv profile and nothing else. The page compares the keys in
``~/.ssh`` with the profile's and offers to add this computer's.
"""

from __future__ import annotations

import logging
import os
import threading
from datetime import datetime, timezone

from sshpilot.plugins.api import Events, PluginContext, SshPilotPlugin

from . import cli_config, connections, logos, model
from .easyenv_api import ApiError, Client

logger = logging.getLogger(__name__)


def friendly(exc):
    """One sentence for a toast."""
    if isinstance(exc, ApiError):
        if exc.out_of_time:
            return "this account has no lab hours left"
        return str(exc)
    return str(exc) or exc.__class__.__name__


class Plugin(SshPilotPlugin):
    #: How the plugin makes an API client. A test hands in a fake.
    client_class = Client

    def activate(self, ctx: PluginContext) -> None:
        self.ctx = ctx
        self.page = None
        self._lock = threading.Lock()
        self._refreshing = False
        self._again = False
        self._recipes = None
        self._generation = 0   # bumped on sign-in, sign-out and account switch
        self._logos = logos.Loader(lambda cb, path: ctx.run_on_ui_thread(cb, path))
        ctx.ui.register_page("workspaces", "EasyEnv Workspaces",
                             "network-server-symbolic", self._build_page)
        ctx.events.subscribe(Events.APP_STARTED, lambda _p: self._migrate_keyring_token())

    def _build_page(self):
        from .page import Page
        self.page = Page(self)
        self.refresh()
        return self.page.root

    # --- config ------------------------------------------------------------------
    # Read fresh every time: `easyenv auth login` or `easyenv account use` in a
    # terminal takes effect here on the next refresh, with nothing to restart.

    def config(self):
        try:
            return cli_config.read_config()
        except Exception:  # noqa: BLE001 - an unreadable file is a signed-out one
            logger.warning("could not read the easyenv config", exc_info=True)
            return {}

    def client(self, config=None, token=None):
        config = config if config is not None else self.config()
        return self.client_class(token or config.get("service_token"),
                                 None if token else config.get("default_account"),
                                 config.get("server") or model.DEFAULT_SERVER)

    def token_url(self):
        return model.token_url(self.config())

    def upgrade_url(self):
        return model.upgrade_url(self.config())

    def buy_hours_url(self):
        config = self.config()
        return model.buy_hours_url(config, config.get("default_account"))

    def web_url(self):
        return model.web_url(self.config())

    def keys_url(self):
        return model.keys_url(self.config())

    def workspace_url(self, uuid):
        config = self.config()
        return model.workspace_url(config, config.get("default_account"), uuid)

    def logo(self, url, callback):
        """``callback(path or None)`` on the UI thread, once the logo is on disk."""
        self._logos.get(url, callback)

    def ssh_dir(self):
        return os.path.join(os.path.expanduser("~"), ".ssh")

    def notify(self, text):
        self.ctx.ui.notify(text)

    # --- threads ----------------------------------------------------------------

    def _background(self, work, done=None, failed=None, always=None):
        """Run ``work()`` on a thread; ``done(result)`` or ``failed(exc)`` on the UI.

        A result that comes back after a sign-out or an account switch is
        dropped: it belongs to an account the page no longer shows.
        ``always()`` runs first either way, for bookkeeping that must not be
        dropped with it.
        """
        generation = self._generation

        def run():
            try:
                result = work()
            except Exception as exc:  # noqa: BLE001 - every failure is shown
                if not isinstance(exc, ApiError):
                    logger.exception("easyenv: background work failed")
                self.ctx.run_on_ui_thread(self._finish, generation, always, failed, exc)
                return
            self.ctx.run_on_ui_thread(self._finish, generation, always, done, result)

        threading.Thread(target=run, daemon=True, name="easyenv").start()

    def _finish(self, generation, always, callback, value):
        if always is not None:
            always()
        if generation != self._generation or callback is None:
            return
        if isinstance(value, ApiError) and value.unauthorized:
            self._signed_out("EasyEnv no longer accepts the saved token. "
                             "Copy a new one from the dashboard.")
            return
        callback(value)

    # --- signing in and out ------------------------------------------------------

    def sign_in(self, token):
        """Check the token, then save it.

        Checked first: a token that does not work must not replace one that
        does, here or in the CLI.
        """
        config = self.config()

        def work():
            client = self.client(config, token=token)
            profile = client.me()
            accounts = client.accounts()
            chosen = model.pick_account(profile, accounts, config.get("default_account"))
            if not chosen:
                raise ApiError("this token has no EasyEnv accounts")
            cli_config.write_config({"service_token": token, "default_account": chosen})
            return profile

        def done(profile):
            self._generation += 1
            self._recipes = None
            self.notify(f"Signed in to EasyEnv as {profile.get('email') or 'you'}")
            self.refresh()

        def failed(exc):
            if isinstance(exc, ApiError) and exc.unauthorized:
                message = ("EasyEnv did not accept that token. Copy it again from "
                           "the dashboard's Integration page.")
            else:
                message = f"Could not sign in: {friendly(exc)}"
            if self.page:
                self.page.signing_in_failed(message)

        # Straight to the thread, not through _background: a rejected token is
        # an answer about the pasted one, not a reason to sign out.
        generation = self._generation

        def run():
            try:
                result = work()
            except Exception as exc:  # noqa: BLE001
                # Bound now: Python unbinds `exc` when this block ends.
                self.ctx.run_on_ui_thread(
                    lambda e=exc: generation == self._generation and failed(e))
                return
            self.ctx.run_on_ui_thread(
                lambda: generation == self._generation and done(result))

        threading.Thread(target=run, daemon=True, name="easyenv-signin").start()

    def sign_out(self):
        try:
            cli_config.write_config({"service_token": None})
        except OSError as exc:
            self.notify(f"Could not sign out: {exc}")
            return
        self.notify("Signed out of EasyEnv (the easyenv CLI too)")
        self._signed_out("")

    def _signed_out(self, message):
        self._generation += 1
        self._recipes = None
        if self.page:
            self.page.show_signed_out(message)

    def switch_account(self, account_uuid):
        if account_uuid == self.config().get("default_account"):
            return
        try:
            cli_config.write_config({"default_account": account_uuid})
        except OSError as exc:
            self.notify(f"Could not switch account: {exc}")
            return
        self._generation += 1
        self._recipes = None
        self.refresh()

    def _migrate_keyring_token(self):
        """Move a token the first version kept in the keyring into the config.

        Moved rather than copied, so there is one credential and not two that
        disagree after the next sign-in, and only when the config has none: a
        token `easyenv auth login` wrote since is the newer one.

        At most once, and only for a profile the first version signed in (it
        saved the chosen account in settings). With no secret service on the
        bus, sshPilot's daemon waits two minutes for one on a keyring read,
        and every connection request queues behind it.
        """
        settings, secrets = self.ctx.settings, self.ctx.secrets
        try:
            if settings.get("keyring_token_migrated", False):
                return
            account = settings.get("account_uuid", None)
            if not account:
                return
            settings.set("keyring_token_migrated", True)
        except Exception:  # noqa: BLE001
            return

        def work():
            old = secrets.get("service_token")
            if not old:
                return False
            if not self.config().get("service_token"):
                cli_config.write_config({"service_token": old, "default_account": account})
            secrets.delete("service_token")
            return True

        self._background(work, lambda moved: moved and self.refresh(),
                         lambda exc: logger.warning("easyenv: keyring move failed: %s", exc))

    # --- the list ------------------------------------------------------------------

    def refresh(self):
        """Fetch the account and its workspaces, and show them.

        One refresh at a time; a request during one is remembered and made
        when it ends, so a click is never lost and never doubled.
        """
        if self.page is None:
            return
        config = self.config()
        if not config.get("service_token"):
            self._signed_out("")
            return
        with self._lock:
            if self._refreshing:
                self._again = True
                return
            self._refreshing = True
        self.page.set_refreshing(True)

        def work():
            client = self.client(config)
            accounts = client.accounts()
            chosen = model.pick_account(None, accounts, config.get("default_account"))
            if not chosen:
                raise ApiError("this token has no EasyEnv accounts")
            if chosen != config.get("default_account"):
                # The saved account is gone (left the team, say): use the one
                # the list offers, and say so to the CLI too.
                cli_config.write_config({"default_account": chosen})
            client.account = chosen
            listed = client.workspaces()
            fetched_at = datetime.now(timezone.utc)
            web = model.web_url(config)
            views = []
            for ws in listed:
                # The list leaves out the machines' own state and ssh user, and a
                # running or starting workspace needs both.
                if model.workspace_state(ws.get("status")) in (model.RUNNING, model.STARTING):
                    try:
                        ws = client.workspace(ws["uuid"])
                    except ApiError as exc:
                        if exc.status != 404:
                            raise
                        continue
                views.append(model.workspace_view(ws, fetched_at, web=web))
            account = next(a for a in accounts if str(a.get("uuid")) == chosen)
            local = model.local_public_keys(self.ssh_dir())
            try:
                key_status = model.key_status(client.ssh_keys(), local)
            except ApiError as exc:
                if exc.unauthorized:
                    raise
                key_status = "unknown"
            return {
                "key_status": key_status,
                "local_key": local[0] if local else None,
                "account": model.account_view(account),
                "accounts": [model.account_view(a) for a in accounts],
                "workspaces": views,
                "fetched_at": fetched_at,
            }

        def done(state):
            self.page.set_status("")
            self.page.show(state)

        def failed(exc):
            if self.page.state is None:
                # Nothing to show yet: the dashboard, empty, with the reason.
                self.page.show({"account": {}, "accounts": [], "workspaces": []})
            self.page.set_status(f"Could not refresh: {friendly(exc)}")

        self._background(work, done, failed, always=self._refresh_ended)

    def _refresh_ended(self):
        with self._lock:
            self._refreshing = False
            again, self._again = self._again, False
        if self.page:
            self.page.set_refreshing(False)
        if again:
            self.refresh()

    # --- actions -------------------------------------------------------------------

    def _action(self, view, verb, work, success):
        uuid = view["uuid"]
        self.page.set_busy(uuid, True)

        def done(_result):
            self.page.set_busy(uuid, False)
            if success:
                self.notify(success)
            self.refresh()

        def failed(exc):
            self.page.set_busy(uuid, False)
            self._report(f"Could not {verb} {view['title']}", exc)
            self.refresh()

        self._background(work, done, failed)

    def _report(self, what, exc):
        text = f"{what}: {friendly(exc)}"
        self.notify(text)
        self.page.set_status(text)
        if isinstance(exc, ApiError) and exc.out_of_time:
            # The account row and banner show the hours from before; fetch
            # them again so the Buy hours button appears.
            self.refresh()

    def ssh(self, view, machine):
        """Save the machine as a connection and open a terminal on it.

        The workspace is fetched again first, so the machine and its user are
        current even if the page has been open since before it was rebuilt.
        """
        client = self.client()
        uuid = view["uuid"]
        self.page.set_busy(uuid, True)

        def done(ws):
            self.page.set_busy(uuid, False)
            fresh = model.workspace_view(ws)
            target = next((m for m in fresh["machines"] if m["uuid"] == machine["uuid"]), None)
            if target is None:
                self.notify(f"{machine['title']} is no longer part of {view['title']}")
                self.refresh()
                return
            try:
                names = connections.save_workspace(self.ctx, fresh, only=target["uuid"])
            except Exception as exc:  # noqa: BLE001 - sshPilot's daemon has its own reasons
                logger.exception("easyenv: could not save %s", target["uuid"])
                self._report(f"Could not save {machine['title']} as a connection", exc)
                return
            nickname = names[target["uuid"]]
            if not self.ctx.open_connection(nickname):
                self.notify(f"Saved {nickname}; open it from the sidebar")

        def failed(exc):
            self.page.set_busy(uuid, False)
            self._report(f"Could not open {machine['title']}", exc)

        self._background(lambda: client.workspace(uuid), done, failed)

    def start(self, view):
        client = self.client()
        self._action(view, "start", lambda: client.start(view["uuid"]),
                     f"Starting {view['title']}")

    def stop(self, view):
        client = self.client()
        self._action(view, "stop", lambda: client.stop(view["uuid"]),
                     f"Stopping {view['title']}")

    def delete(self, view):
        client = self.client()
        self._action(view, "delete", lambda: client.delete(view["uuid"]),
                     f"Deleted {view['title']}")

    def run_again(self, view):
        """A new workspace with the same machines, started.

        The API does not start a workspace that has stopped, so "again" means
        a copy: same title, recipes and duration.
        """
        raw = view["raw"]
        boxes = [{"title": b.get("title") or "machine",
                  "recipe": (b.get("recipe") or {}).get("uuid") if isinstance(b.get("recipe"), dict)
                  else b.get("recipe"),
                  "position": i}
                 for i, b in enumerate(raw.get("boxes") or [])]
        boxes = [b for b in boxes if b["recipe"]]
        if not boxes:
            self.notify(f"{view['title']} has no machines to run again")
            return
        body = {"title": view["title"], "duration": raw.get("duration") or 1,
                "duration_unit": raw.get("duration_unit") or "hours", "boxes": boxes}
        self._create(body, view)

    def create(self, title, machines, amount, unit, template=None):
        """``machines``: model.create_body's machine specs."""
        self._create(model.create_body(title, machines, amount, unit, template), None)

    def add_this_key(self):
        """Put this computer's public key on the EasyEnv profile.

        Only the public half, read from ``~/.ssh``. Machines built from now on
        accept it; one that is already running learned its keys when it was
        built, which is said, since that is the first thing anyone would try.
        """
        local = model.local_public_keys(self.ssh_dir())
        if not local:
            self.notify("No SSH key in ~/.ssh. Create one with ssh-keygen first.")
            return
        path, line, _fp = local[0]
        client = self.client()
        label = f"sshPilot on {os.uname().nodename}" if hasattr(os, "uname") else "sshPilot"

        def done(_key):
            self.notify(f"Added {os.path.basename(path)} to your EasyEnv profile. "
                        "Machines started from now on accept it.")
            self.refresh()

        self._background(lambda: client.add_ssh_key(line, label), done,
                         lambda exc: self._report("Could not add the key", exc))

    def _create(self, body, view):
        """Create, then start: a new workspace waits until it is told to."""
        client = self.client()
        title = body["title"]
        self.page.set_status(f"Creating {title}...")

        def work():
            ws = client.create_workspace(body)
            try:
                client.start(ws["uuid"])
            except ApiError as exc:
                exc.created = ws
                raise
            return ws

        def done(_ws):
            self.notify(f"{title} is starting")
            self.page.set_status("")
            self.refresh()

        def failed(exc):
            created = getattr(exc, "created", None)
            what = (f"Created {title} but could not start it" if created
                    else f"Could not create {title}")
            self._report(what, exc)
            if created:
                self.refresh()

        self._background(work, done, failed)
        if view is not None:
            self.notify(f"Running {title} again")

    def load_catalog(self, callback):
        """``callback(recipes, stacks, templates, error)``: what the New
        workspace dialog offers, fetched once per account."""
        if self._recipes is not None:
            callback(*self._recipes, None)
            return
        client = self.client()
        web = model.web_url(self.config())

        def optional(fetch, what):
            # The dialog still works without stacks or templates.
            try:
                return fetch()
            except ApiError as exc:
                if exc.unauthorized:
                    raise
                logger.warning("easyenv: no %s: %s", what, exc)
                return []

        def work():
            recipes = client.recipes()
            stacks = model.stack_choices(optional(client.stack_recipes, "stack catalog"), web)
            templates = model.template_choices(optional(client.templates, "templates"), web)
            return recipes, stacks, templates

        def done(result):
            self._recipes = result
            callback(*result, None)

        self._background(work, done, lambda exc: callback([], [], [], friendly(exc)))

    def details(self, view, callback):
        """``callback(workspace view or None, error)`` with the machines'
        sizes and stacks, which only the workspace's own page carries."""
        client = self.client()
        web = model.web_url(self.config())
        self._background(lambda: model.workspace_view(client.workspace(view["uuid"]), web=web),
                         lambda fresh: callback(fresh, None),
                         lambda exc: callback(None, friendly(exc)))
