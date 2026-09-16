"""What the page shows, worked out from API answers.

No GTK and no network in this module, so every decision the page makes can be
tested on its own: which workspaces come first, what a status is called, how
much lab time is left and whether to offer more, where the dashboard is.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import os
import re
import urllib.parse
from datetime import datetime, timezone

DEFAULT_SERVER = "https://api.easyenv.io"

# --- workspace and machine states ------------------------------------------

RUNNING = "running"
STARTING = "starting"
NOT_STARTED = "not_started"
FAILED = "failed"
ENDED = "ended"

#: The dashboard's words for each state.
LABELS = {
    RUNNING: "Running",
    STARTING: "Starting up",
    NOT_STARTED: "Not started",
    FAILED: "Failed",
    ENDED: "Terminated",
}

#: Order on the page: what can be used now, then what is on its way.
RANK = {RUNNING: 0, STARTING: 1, NOT_STARTED: 2, FAILED: 3, ENDED: 4}

#: The CSS class for each state's badge and card strip (colours in page.py,
#: taken from the dashboard's card).
STYLE = {RUNNING: "ee-running", STARTING: "ee-starting", NOT_STARTED: "ee-not-started",
         FAILED: "ee-failed", ENDED: "ee-ended"}


def workspace_state(status):
    """The API's workspace status, as one of the states above.

    The API says ``active``, ``in_progress``, ``not_started``, ``stopped`` and
    ``failed``. ``stopped`` is the end: the machines are gone and the API
    refuses to start that workspace again.
    """
    return {
        "active": RUNNING, "started": RUNNING, "running": RUNNING,
        "in_progress": STARTING, "starting": STARTING, "pending": STARTING,
        "not_started": NOT_STARTED,
        "failed": FAILED, "error": FAILED,
    }.get(str(status or "").lower(), ENDED)


def machine_state(status):
    """A box's status (``started``, ``in_progress``, ``not_started``,
    ``stopped``, ``failed``, ``terminated``) as one of the states above."""
    return {
        "started": RUNNING, "active": RUNNING,
        "in_progress": STARTING,
        "not_started": NOT_STARTED,
        "failed": FAILED,
    }.get(str(status or "").lower(), ENDED)


# --- time --------------------------------------------------------------------

def humanize(seconds):
    """``45s``, ``12m``, ``3h 5m``, ``2d 4h``."""
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes}m" if minutes else f"{hours}h"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h" if hours else f"{days}d"


def parse_time(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def remaining_seconds(ws, fetched_at, now):
    """Seconds a running workspace has left.

    The API's own ``remaining_time`` (milliseconds) when it sends one, counted
    down from when it was fetched, so the card ticks without asking again.
    Worked out from the start time and duration otherwise.
    """
    ms = ws.get("remaining_time")
    if isinstance(ms, (int, float)):
        return int(ms / 1000 - (now - fetched_at).total_seconds())
    start = parse_time(ws.get("start_time"))
    if start is None:
        return None
    unit = {"minutes": 60, "hours": 3600, "days": 86400}.get(
        str(ws.get("duration_unit") or "hours").lower(), 3600)
    end = start.timestamp() + (ws.get("duration") or 0) * unit
    return int(end - now.timestamp())


#: Choices in the New workspace dialog: (label, amount, unit).
DURATIONS = (
    ("1 hour", 1, "hours"),
    ("2 hours", 2, "hours"),
    ("4 hours", 4, "hours"),
    ("8 hours", 8, "hours"),
    ("1 day", 24, "hours"),
)


# --- credit --------------------------------------------------------------------

def credit(account):
    """Lab time left on an account, read the way the dashboard's usage chip does.

    The subscription's ``usage`` block when the API sends one. Otherwise the
    account's ``total_time_seconds``, which is the plan's balance plus invited
    and bought time; not ``current_plan.remaining_time_seconds``, which is the
    plan's balance alone and would tell someone living on bought hours to buy
    more.

    ``state`` is ``unlimited``, ``ok``, ``low`` (85% or more used), ``empty``,
    or ``unknown`` when the API said nothing usable.
    """
    account = account if isinstance(account, dict) else {}
    sub = account.get("current_plan") if isinstance(account.get("current_plan"), dict) else {}
    usage = sub.get("usage") if isinstance(sub.get("usage"), dict) else None
    if usage is not None:
        unlimited = bool(usage.get("unlimited"))
        remaining = usage.get("remaining")
        total = usage.get("total")
    else:
        unlimited = bool(sub.get("is_unlimited_time"))
        seconds = account.get("total_time_seconds")
        if not isinstance(seconds, (int, float)):
            seconds = sub.get("remaining_time_seconds")
        remaining = int(seconds) if isinstance(seconds, (int, float)) else None
        included = int(((sub.get("plan") or {}).get("monthly_compute_seconds")) or 0)
        total = None if remaining is None else max(0, included - max(0, remaining)) + max(0, remaining)

    if unlimited:
        return {"state": "unlimited", "remaining": None, "text": "Unlimited hours"}
    if not isinstance(remaining, (int, float)):
        return {"state": "unknown", "remaining": None, "text": ""}
    remaining = max(0, int(remaining))
    total = int(total or 0)
    if remaining <= 0:
        state = "empty"
    elif total > 0 and (total - remaining) * 100 // total >= 85:
        state = "low"
    else:
        state = "ok"
    text = f"{humanize(remaining)} left" if remaining else "No hours left"
    return {"state": state, "remaining": remaining, "text": text}


def account_view(account):
    account = account if isinstance(account, dict) else {}
    title = str(account.get("title") or (account.get("owner") or {}).get("email") or "")
    plan = str((((account.get("current_plan") or {}).get("plan") or {}).get("abbreviation"))
               or "")
    return {
        "uuid": str(account.get("uuid") or ""),
        "title": title,
        "kind": str(account.get("type") or "").title(),
        "plan": plan,
        "initials": ((title.split("@")[0] or "ee")[:2]).upper(),
        "credit": credit(account),
        "limits": size_limits(account),
    }


# --- machine size -------------------------------------------------------------

#: What "unlimited" (-1) is shown as: the spin rows need an upper bound.
UNLIMITED = {"cpu": 64, "ram_mb": 256 * 1024, "storage_gb": 2000}

#: Only for tests and for an account the API said nothing about.
FALLBACK_LIMITS = {"cpu": 0, "ram_mb": 0, "storage_gb": 0, "custom": False, "max_machines": None}


def _cap(sub, plan, eff, own):
    value = sub.get(eff)
    if not isinstance(value, int):
        value = plan.get(own)
    return value if isinstance(value, int) else 0


def size_limits(account):
    """What the account's plan allows a machine to be.

    ``{cpu, ram_mb, storage_gb, custom, max_machines}``. The caps are the
    subscription's ``effective_max_*`` (the plan's, or the account's override),
    read the way the backend reads them: 0 means sizes are fixed on this plan,
    -1 means no cap. ``custom`` is whether any of them may be changed at all;
    when it is False the recipe's size is what the machine gets, which the
    dialog says rather than offering spin buttons the API would refuse.

    ``max_machines`` is the plan's ``maximum_boxes_per_workspace`` (None for
    no limit).
    """
    sub = (account or {}).get("current_plan") if isinstance(account, dict) else None
    sub = sub if isinstance(sub, dict) else {}
    plan = sub.get("plan") if isinstance(sub.get("plan"), dict) else {}
    out = {}
    for key, eff, own in (("cpu", "effective_max_cpu_cores", "max_cpu_cores"),
                          ("ram_mb", "effective_max_ram_mb", "max_ram_mb"),
                          ("storage_gb", "effective_max_disk_gb", "max_disk_gb")):
        value = _cap(sub, plan, eff, own)
        out[key] = UNLIMITED[key] if value < 0 else value
    out["custom"] = any(out[k] > 0 for k in UNLIMITED)
    boxes = plan.get("maximum_boxes_per_workspace")
    out["max_machines"] = boxes if isinstance(boxes, int) and boxes > 0 else None
    return out


def recipe_size(recipe):
    """A recipe's default (and smallest) machine: {cpu, ram_mb, storage_gb}."""
    res = (recipe or {}).get("resources") if isinstance(recipe, dict) else None
    res = res if isinstance(res, dict) else {}
    return {"cpu": int(res.get("cpu") or 2), "ram_mb": int(res.get("ram_mb") or 2048),
            "storage_gb": int(res.get("storage_gb") or 20)}


def gb(mb):
    value = mb / 1024
    return f"{value:g} GB" if value >= 1 else f"{mb} MB"


def size_text(size):
    """``2 vCPU · 4 GB RAM · 20 GB disk``, or "" without a size."""
    if not size or not size.get("cpu"):
        return ""
    return (f"{size['cpu']} vCPU  ·  {gb(size.get('ram_mb') or 0)} RAM  ·  "
            f"{size.get('storage_gb') or 0} GB disk")


def pick_account(profile, accounts, wanted=None):
    """The account to act in: the one already chosen, else the personal one.

    The personal account is what ``easyenv auth login`` picks, so the CLI and
    the plugin agree about where a new workspace goes.
    """
    uuids = [str(a.get("uuid")) for a in accounts if isinstance(a, dict) and a.get("uuid")]
    if wanted and wanted in uuids:
        return wanted
    personal = str(((profile or {}).get("personal_account") or {}).get("uuid") or "")
    if personal in uuids:
        return personal
    return uuids[0] if uuids else None


# --- workspaces --------------------------------------------------------------------

def _recipe(box):
    recipe = box.get("recipe")
    return recipe if isinstance(recipe, dict) else {"uuid": recipe}


def recipe_logo(recipe):
    """The recipe's logo, an SVG URL, when the API gave one."""
    url = ((recipe or {}).get("config") or {}).get("logo") if isinstance(recipe, dict) else None
    return url if isinstance(url, str) and url.startswith(("https://", "http://")) else ""


def _recipe_title(box):
    recipe = box.get("recipe")
    if isinstance(recipe, dict):
        return str(recipe.get("title") or recipe.get("uuid") or "")
    return str(recipe or "")


#: Stacks the dashboard knows, by id, with the names it shows. Their logos
#: are the dashboard's own files, /images/stacks/<id>.svg; the API names a
#: stack by id only.
STACK_NAMES = {
    "python": "Python", "nodejs": "Node.js", "typescript": "TypeScript", "golang": "Go",
    "rust": "Rust", "java": "Java", "kotlin": "Kotlin", "ruby": "Ruby", "php": "PHP",
    "cpp": "C / C++", "github": "GitHub", "claude": "Claude", "bash": "Bash",
    "ansible": "Ansible", "vscode": "VS Code", "docker-run": "Docker Image",
    "docker-build": "Dockerfile", "docker-compose": "Docker Compose", "zsh": "Zsh",
    "helm": "Helm", "kustomize": "Kustomize", "argocd": "ArgoCD", "fluxcd": "FluxCD",
    "traefik": "Traefik", "cert-manager": "cert-manager", "longhorn": "Longhorn",
    "cilium": "Cilium", "calico": "Calico",
    "kubernetes-gateway-api": "Kubernetes Gateway API",
    "istio-ingress-gateway": "Istio Ingress Gateway",
    "prometheus-grafana": "Prometheus + Grafana", "k9s": "k9s",
    "custom-commands": "Commands", "custom-ports": "Ports",
}

DEFAULT_WEB = "https://dashboard.easyenv.io"


def stack_logo(name, web=DEFAULT_WEB):
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", name or ""):
        return ""
    return f"{web.rstrip('/')}/images/stacks/{name}.svg"


def stacks_of(ws, web=DEFAULT_WEB):
    """The workspace's stacks as [(title, logo)], once each.

    The list API puts them on the workspace ({name, title}); the detail API
    puts them on each machine ({stack_recipe}). Both are read.
    """
    found = {}
    for s in ws.get("stacks") or []:
        if isinstance(s, dict) and s.get("name"):
            found.setdefault(s["name"], s.get("title") or STACK_NAMES.get(s["name"], s["name"]))
    for box in ws.get("boxes") or []:
        for s in (box.get("stacks") or []) if isinstance(box, dict) else []:
            name = s.get("stack_recipe") if isinstance(s, dict) else None
            if name:
                found.setdefault(name, STACK_NAMES.get(name, name))
    return [(title, stack_logo(name, web)) for name, title in found.items()]


def machine_stacks(box, web=DEFAULT_WEB):
    """What goes onto one machine: [{name, title, logo, version}] (detail API)."""
    out = []
    for s in box.get("stacks") or []:
        if not isinstance(s, dict):
            continue
        name = s.get("stack_recipe") or ""
        title = s.get("inline_title") or STACK_NAMES.get(name, name)
        if not title:
            continue
        version = (s.get("vars") or {}).get("version") if isinstance(s.get("vars"), dict) else None
        out.append({"name": name, "title": str(title), "logo": stack_logo(name, web),
                    "version": str(version) if isinstance(version, (str, int, float)) else ""})
    return out


def machine_view(box, web=DEFAULT_WEB):
    cpu = box.get("cpu")
    size = ({"cpu": int(cpu), "ram_mb": int(box.get("ram_mb") or 0),
             "storage_gb": int(box.get("storage_gb") or 0)}
            if isinstance(cpu, (int, float)) and cpu > 0 else None)
    return {
        "size": size,
        "stacks": machine_stacks(box, web),
        "uuid": str(box.get("uuid") or ""),
        "title": str(box.get("title") or _recipe_title(box) or box.get("uuid") or "machine"),
        "recipe": _recipe_title(box),
        "recipe_uuid": str(_recipe(box).get("uuid") or ""),
        "logo": recipe_logo(_recipe(box)),
        # Absent from the list API, which is why the page fetches a running
        # workspace's details: without a state the machine is taken to be
        # up if its workspace is.
        "state": machine_state(box["status"]) if box.get("status") else None,
        "user": str(box.get("ssh_username") or "easyenv"),
        "password": box.get("vm_password") or "",
    }


def workspace_view(ws, fetched_at=None, now=None, web=DEFAULT_WEB):
    now = now or datetime.now(timezone.utc)
    fetched_at = fetched_at or now
    state = workspace_state(ws.get("status"))
    machines = [machine_view(b, web) for b in (ws.get("boxes") or []) if isinstance(b, dict)]
    for m in machines:
        if m["state"] is None:
            m["state"] = state
    creator = ws.get("creator") or {}
    owner = (f"{creator.get('first_name', '')} {creator.get('last_name', '')}".strip()
             or creator.get("email") or "")
    recipes = sorted({m["recipe"] for m in machines if m["recipe"]})
    # One chip per logo, not per recipe: Ubuntu 24.04 and 26.04 share one.
    logos = []
    for m in machines:
        known = next((i for i, (_t, url) in enumerate(logos)
                      if m["logo"] and url == m["logo"]), None)
        if known is not None:
            title, url = logos[known]
            if m["recipe"] and m["recipe"] not in title.split(", "):
                logos[known] = (f"{title}, {m['recipe']}", url)
        elif not any(t == m["recipe"] for t, _u in logos):
            logos.append((m["recipe"], m["logo"]))
    progress = ws.get("progress")
    return {
        "uuid": str(ws.get("uuid") or ""),
        "title": str(ws.get("title") or ws.get("uuid") or "workspace"),
        "state": state,
        "label": LABELS[state],
        "style": STYLE[state],
        "machines": machines,
        "recipes": ", ".join(recipes),
        "logos": logos,
        "stacks": stacks_of(ws, web),
        "progress": (max(0.0, min(1.0, float(progress) / 100))
                     if isinstance(progress, (int, float)) else None),
        "owner": owner,
        "created_at": str(ws.get("created_at") or ""),
        "raw": ws,
        "fetched_at": fetched_at,
    }


def ago(iso, now):
    when = parse_time(iso)
    if when is None:
        return ""
    seconds = (now - when).total_seconds()
    if seconds < 60:
        return "just now"
    return f"{humanize(seconds).split(' ')[0]} ago"


def subtitle(view, now):
    """The card's second line, as the dashboard writes it."""
    count = len(view["machines"])
    parts = [f"{count} machine" + ("" if count == 1 else "s")]
    if view["owner"]:
        owner = view["owner"]
        parts.append(f"by {owner[:18] + '...' if len(owner) > 18 else owner}")
    when = ago(view["raw"].get("start_time") or view["created_at"], now)
    if when:
        parts.append(when)
    return "  ·  ".join(parts)


def time_text(view, now):
    """The line in a card's corner."""
    state = view["state"]
    if state == RUNNING:
        left = remaining_seconds(view["raw"], view["fetched_at"], now)
        if left is None:
            return ""
        return f"{humanize(left)} left" if left > 0 else "ending"
    if state == STARTING:
        progress = view.get("progress")
        return f"{int(progress * 100)}%" if progress else ""
    started = parse_time(view["raw"].get("start_time"))
    stopped = parse_time(view["raw"].get("stop_time"))
    if started and stopped:
        return f"ran {humanize((stopped - started).total_seconds())}"
    return ""


#: The list's filter: (key, label). "Active" is everything that has not ended.
FILTERS = (("active", "Active"), ("ended", "Ended"), ("all", "All"))


def _matches(v, query):
    return not query or query in " ".join([v["title"], v["recipes"], v["owner"]]).lower()


def visible(views, search="", show_ended=False, show="active"):
    """What the list shows, running first, then starting, then the rest.

    ``show`` is one of FILTERS; ``show_ended`` is the older switch and means
    "all".
    """
    if show_ended:
        show = "all"
    query = (search or "").strip().lower()
    out = []
    for v in views:
        ended = v["state"] == ENDED
        if (show == "active" and ended) or (show == "ended" and not ended):
            continue
        if _matches(v, query):
            out.append(v)
    return sorted(out, key=lambda v: (RANK[v["state"]], v["title"].lower()))


def filter_counts(views, search=""):
    """How many workspaces each filter would show, for the filter's labels."""
    query = (search or "").strip().lower()
    matching = [v for v in views if _matches(v, query)]
    ended = sum(1 for v in matching if v["state"] == ENDED)
    return {"active": len(matching) - ended, "ended": ended, "all": len(matching)}


def create_body(title, machines, amount=1, unit="hours", template=None):
    """The ``POST /v1/workspaces/`` body.

    ``machines`` is a list of machine specs, one per machine:
    ``{"recipe": <recipe_choices item>, "stacks": [(name, vars)], "size":
    {cpu, ram_mb, storage_gb} or None}``. The same recipe may appear more
    than once. Each machine is called after its recipe, as the dashboard
    does; repeats are numbered. A size is sent only when it differs from the
    recipe's own, which is also when the API checks it against the plan.
    ``template`` is the workspace template the machines came from, if any.

    No ``public_ip_requested``: machines are reached through the gateway.
    """
    specs = [m if "recipe" in m else {"recipe": m} for m in machines]
    counts = {}
    for m in specs:
        counts[m["recipe"]["title"]] = counts.get(m["recipe"]["title"], 0) + 1
    seen = {}
    boxes = []
    for i, m in enumerate(specs):
        r = m["recipe"]
        name = r["title"]
        if counts[name] > 1:
            seen[name] = seen.get(name, 0) + 1
            name = f"{name} {seen[name]}"
        box = {"title": name, "recipe": r["uuid"], "position": i}
        stacks = [{"stack_recipe": n, "vars": dict(v or {})} for n, v in m.get("stacks") or []]
        if stacks:
            box["stacks"] = stacks
        size = m.get("size")
        if size and size != r.get("size"):
            box.update({"cpu": int(size["cpu"]), "ram_mb": int(size["ram_mb"]),
                        "storage_gb": int(size["storage_gb"])})
        boxes.append(box)
    body = {"title": title, "duration": int(amount), "duration_unit": unit, "boxes": boxes}
    if template:
        body["workspace_template"] = template
    return body


#: What a new machine starts as in the dialog: a plain Ubuntu, not whatever
#: sorts first (that was "Ansible Dev Env").
DEFAULT_RECIPES = ("ubuntu_24_04", "ubuntu_26_04")


def default_choice(choices):
    """The index of the recipe a new machine row starts on."""
    for wanted in DEFAULT_RECIPES:
        for i, r in enumerate(choices):
            if r["uuid"] == wanted and r["available"]:
                return i
    return next((i for i, r in enumerate(choices) if r["available"]), 0)


def recipe_choices(recipes):
    """Recipes the dialog offers, available ones first, with why the rest are not."""
    out = []
    for r in recipes:
        if not isinstance(r, dict) or not r.get("uuid"):
            continue
        out.append({
            "uuid": str(r["uuid"]),
            "title": str(r.get("title") or r["uuid"]),
            "available": r.get("is_available", True) is not False,
            "reason": str(r.get("unavailable_reason") or ""),
            "logo": recipe_logo(r),
            "base_image": str(r.get("base_image") or ""),
            "size": recipe_size(r),
        })
    return sorted(out, key=lambda r: (not r["available"], r["title"].lower()))


# --- stacks ---------------------------------------------------------------------

#: Order of the stack groups in the dialog, by the catalog's tags.
STACK_GROUPS = (("language", "Languages"), ("tools", "Tools"), ("docker", "Docker"),
                ("script", "Scripts"))


def stack_choices(stack_recipes, web=DEFAULT_WEB):
    """Stacks the dialog offers: [{name, title, description, logo, tag,
    compatibility, version: {key, options, default} or None}].

    Only stacks that work without being written by hand: every field of the
    form has a default (a script the catalog ships) or is a choice. An
    Ansible playbook, a Dockerfile or a list of commands has to be typed,
    which belongs on the dashboard.
    """
    out = []
    for s in stack_recipes or []:
        if not isinstance(s, dict) or not s.get("name") or s.get("is_active") is False:
            continue
        version = None
        usable = True
        for field in s.get("vars_schema") or []:
            if not isinstance(field, dict):
                continue
            if field.get("type") == "select" and field.get("options"):
                if version is None:
                    version = {"key": str(field.get("key") or "version"),
                               "options": [str(o) for o in field["options"]],
                               "default": str(field.get("defaultValue") or field["options"][0])}
            elif field.get("defaultValue") in (None, ""):
                usable = False
        if not usable:
            continue
        tags = s.get("tags") or []
        out.append({
            "name": str(s["name"]),
            "title": str(s.get("title") or STACK_NAMES.get(s["name"], s["name"])),
            "description": str(s.get("description") or ""),
            "logo": stack_logo(s["name"], web),
            "tag": next((t for t, _l in STACK_GROUPS if t in tags), "tools"),
            "compatibility": s.get("default_compatibility") or {},
            "version": version,
        })
    order = {t: i for i, (t, _l) in enumerate(STACK_GROUPS)}
    return sorted(out, key=lambda c: (order.get(c["tag"], 9), c["title"].lower()))


def stack_fits(stack, recipe):
    """Whether a stack can go on a machine of this recipe (the catalog's
    ``default_compatibility``: recipe list, base images, exclusions)."""
    compat = stack.get("compatibility") or {}
    uuid = (recipe or {}).get("uuid")
    if uuid in (compat.get("excluded_recipes") or []):
        return False
    if compat.get("recipes") and uuid not in compat["recipes"]:
        return False
    if compat.get("base_images") and (recipe or {}).get("base_image") not in compat["base_images"]:
        return False
    return True


def template_choices(templates, web=DEFAULT_WEB):
    """Templates the dialog offers, the account's own first (the API's order).

    Each is ``{uuid, title, description, owned, logos, machines}``, where a
    machine is ``{recipe: <recipe_choices item>, stacks: [(name, title, vars)]}``,
    one per recipe in the template, with the template's stacks for that
    recipe. That is how the dashboard builds a workspace from a template: the
    API takes the machines in the request, not from the template.
    """
    out = []
    for t in templates or []:
        if not isinstance(t, dict) or not t.get("uuid"):
            continue
        recipes = recipe_choices([r for r in t.get("recipes") or [] if isinstance(r, dict)])
        by_uuid = {r["uuid"]: r for r in recipes}
        order = [str(tr.get("recipe")) for tr in t.get("template_recipes") or []
                 if isinstance(tr, dict) and str(tr.get("recipe")) in by_uuid]
        order = order or [r["uuid"] for r in recipes]
        machines = []
        for uuid in order:
            stacks = [(str(s["stack_recipe"]),
                       str(s.get("stack_title") or STACK_NAMES.get(s["stack_recipe"], s["stack_recipe"])),
                       dict(s.get("vars") or {}))
                      for s in t.get("stacks") or []
                      if isinstance(s, dict) and s.get("stack_recipe") and s.get("recipe") == uuid]
            machines.append({"recipe": by_uuid[uuid], "stacks": stacks})
        if not machines:
            continue
        logos = []
        for m in machines:
            pair = (m["recipe"]["title"], m["recipe"]["logo"])
            if pair not in logos:
                logos.append(pair)
        names = []
        for m in machines:
            for name, title, _v in m["stacks"]:
                if name not in [n for n, _t in names]:
                    names.append((name, title))
        out.append({
            "uuid": str(t["uuid"]),
            "title": str(t.get("title") or t["uuid"]),
            "description": str(t.get("description") or ""),
            "owned": bool(t.get("account")),
            "logos": logos + [(title, stack_logo(n, web)) for n, title in names],
            "machines": machines,
        })
    return out


def template_summary(template):
    count = len(template["machines"])
    parts = [f"{count} machine" + ("" if count == 1 else "s")]
    stacks = sorted({t for m in template["machines"] for _n, t, _v in m["stacks"]})
    if stacks:
        parts.append(", ".join(stacks))
    if template["owned"]:
        parts.append("this account's")
    return "  ·  ".join(parts)


def stack_vars(stack, version=None):
    """The ``vars`` to send: only the chosen version. The script itself is the
    catalog's, which the backend lays over whatever is stored."""
    if not stack.get("version"):
        return {}
    return {stack["version"]["key"]: version or stack["version"]["default"]}


#: The most machines the New workspace dialog offers. The plan may allow
#: fewer; the API says so when it does.
MAX_MACHINES = 10


# --- SSH keys ---------------------------------------------------------------

def key_fingerprint(line):
    """``SHA256:...`` for one ``authorized_keys``-style public key line, as
    ``ssh-keygen -l`` and the API print it; None for anything else."""
    parts = (line or "").split()
    if len(parts) < 2:
        return None
    try:
        blob = base64.b64decode(parts[1], validate=True)
    except (ValueError, binascii.Error):
        return None
    digest = base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")
    return f"SHA256:{digest}"


def local_public_keys(ssh_dir):
    """The public keys in ``~/.ssh``: [(path, line, fingerprint)], newest type first."""
    order = ("id_ed25519", "id_ecdsa", "id_rsa")
    found = []
    try:
        names = sorted(os.listdir(ssh_dir))
    except OSError:
        return []
    for name in names:
        if not name.endswith(".pub"):
            continue
        path = os.path.join(ssh_dir, name)
        try:
            with open(path, encoding="utf-8") as fh:
                line = fh.readline().strip()
        except (OSError, UnicodeDecodeError):
            continue
        fp = key_fingerprint(line)
        if fp:
            found.append((path, line, fp))
    rank = {n: i for i, n in enumerate(order)}
    return sorted(found, key=lambda k: rank.get(os.path.basename(k[0])[:-4], len(order)))


def key_status(profile_keys, local_keys):
    """Whether SSH from this computer can work.

    ``ok`` when a key here is on the profile; ``upload`` when there is a key
    here that is not; ``none`` when this computer has no key at all (an agent
    or a hardware key may still do, so this is advice, not a block).
    """
    registered = {k.get("fingerprint") for k in profile_keys or [] if isinstance(k, dict)}
    if any(fp in registered for _p, _l, fp in local_keys):
        return "ok"
    return "upload" if local_keys else "none"


def color_class(name):
    """A stable avatar colour for a name."""
    digest = int(hashlib.md5((name or "").encode("utf-8")).hexdigest(), 16)
    return f"ee-c{digest % 8}"


# --- the dashboard's address --------------------------------------------------------

def web_url(config):
    """The dashboard's address, from the API's.

    ``api.X`` is served by ``dashboard.X``. The easyenv CLI guesses ``X``
    instead, which answers 404 on easyenv.io. ``web_url`` in the config wins.
    """
    if config.get("web_url"):
        return str(config["web_url"]).rstrip("/")
    server = str(config.get("server") or DEFAULT_SERVER).rstrip("/")
    parts = urllib.parse.urlsplit(server)
    host = parts.hostname or ""
    if host.startswith("api."):
        netloc = "dashboard." + host[len("api."):]
        if parts.port:
            netloc += f":{parts.port}"
        return urllib.parse.urlunsplit((parts.scheme, netloc, "", "", ""))
    if parts.port == 8051:  # a local backend next to a local frontend
        return server.replace(":8051", ":3000")
    return server


def token_url(config):
    """The dashboard page that shows a signed-in person their token."""
    return web_url(config) + "/integration"


def keys_url(config):
    """The dashboard page where SSH keys are added."""
    return web_url(config) + "/dashboard/profile"


def workspace_url(config, account_uuid, workspace_uuid):
    return (f"{web_url(config)}/dashboard/{urllib.parse.quote(account_uuid or '')}"
            f"/workspaces/{urllib.parse.quote(workspace_uuid)}")


def upgrade_url(config):
    """The dashboard's plans page, where a plan that fixes sizes is changed."""
    return web_url(config) + "/dashboard/upgrade-account"


def buy_hours_url(config, account_uuid):
    base = web_url(config)
    if account_uuid:
        return f"{base}/dashboard/{urllib.parse.quote(account_uuid)}/usage"
    return f"{base}/dashboard/upgrade-account"
