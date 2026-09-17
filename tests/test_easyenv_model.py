"""What the page shows."""

import os
import shutil
import subprocess
from datetime import datetime, timedelta, timezone

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

model = load("model")

NOW = datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc)
HOUR = 3600


def account(total_seconds=None, included_hours=40, unlimited=False, usage=None, **extra):
    plan = {"plan": {"abbreviation": "pro", "monthly_compute_seconds": included_hours * HOUR},
            "is_unlimited_time": unlimited}
    if usage is not None:
        plan["usage"] = usage
    a = {"uuid": "acct", "title": "me@example.com", "type": "personal", "current_plan": plan}
    if total_seconds is not None:
        a["total_time_seconds"] = total_seconds
    a.update(extra)
    return a


# --- credit ----------------------------------------------------------------

def test_hours_left_are_read_from_the_account_total_not_the_plan_balance():
    a = account(total_seconds=30 * HOUR)
    a["current_plan"]["remaining_time_seconds"] = 0  # bought hours are not in here
    assert model.credit(a) == {"state": "ok", "remaining": 30 * HOUR, "text": "1d 6h left"}


def test_an_account_with_nothing_left_is_empty():
    assert model.credit(account(total_seconds=0))["state"] == "empty"
    assert model.credit(account(total_seconds=0))["text"] == "No hours left"
    assert model.credit(account(total_seconds=-50))["state"] == "empty"


def test_eighty_five_percent_used_is_low():
    assert model.credit(account(total_seconds=6 * HOUR, included_hours=40))["state"] == "low"
    assert model.credit(account(total_seconds=7 * HOUR, included_hours=40))["state"] == "ok"


def test_bought_hours_beyond_the_plan_are_not_low():
    assert model.credit(account(total_seconds=100 * HOUR, included_hours=40))["state"] == "ok"


def test_the_usage_block_wins_when_the_api_sends_one():
    a = account(total_seconds=0, usage={"remaining": 5 * HOUR, "total": 10 * HOUR})
    assert model.credit(a)["state"] == "ok"
    assert model.credit(account(usage={"unlimited": True}))["state"] == "unlimited"


def test_unlimited_and_unknown():
    assert model.credit(account(unlimited=True, total_seconds=0))["text"] == "Unlimited hours"
    assert model.credit({})["state"] == "unknown"
    assert model.credit(None)["state"] == "unknown"


def test_the_personal_account_is_picked_unless_one_was_chosen():
    accounts = [{"uuid": "team"}, {"uuid": "mine"}]
    profile = {"personal_account": {"uuid": "mine"}}
    assert model.pick_account(profile, accounts) == "mine"
    assert model.pick_account(profile, accounts, "team") == "team"
    assert model.pick_account(profile, accounts, "gone") == "mine"
    assert model.pick_account(None, accounts, "gone") == "team"
    assert model.pick_account(None, []) is None


def test_account_view_carries_what_the_header_shows():
    v = model.account_view(account(total_seconds=HOUR))
    assert (v["title"], v["plan"], v["initials"]) == ("me@example.com", "pro", "ME")


# --- workspaces ------------------------------------------------------------

def ws(uuid, status, title=None, boxes=None, **extra):
    d = {"uuid": uuid, "title": title or uuid, "status": status,
         "boxes": boxes if boxes is not None else [
             {"uuid": f"{uuid}-box", "title": "node", "recipe": {"uuid": "r", "title": "Ubuntu"}}]}
    d.update(extra)
    return d


def test_api_statuses_map_to_the_page_states():
    assert model.workspace_state("active") == model.RUNNING
    assert model.workspace_state("in_progress") == model.STARTING
    assert model.workspace_state("not_started") == model.NOT_STARTED
    assert model.workspace_state("failed") == model.FAILED
    assert model.workspace_state("stopped") == model.ENDED
    assert model.workspace_state(None) == model.ENDED


def test_running_workspaces_come_first_and_ended_ones_are_hidden():
    views = [model.workspace_view(w, now=NOW) for w in (
        ws("b", "stopped"), ws("z", "not_started"), ws("y", "in_progress"), ws("x", "active"))]
    assert [v["uuid"] for v in model.visible(views)] == ["x", "y", "z"]
    assert [v["uuid"] for v in model.visible(views, show_ended=True)] == ["x", "y", "z", "b"]


def test_search_looks_at_title_recipe_and_owner():
    views = [model.workspace_view(ws("api-dev", "active", creator={"email": "sara@x.io"}), now=NOW),
             model.workspace_view(ws("db", "active"), now=NOW)]
    assert [v["uuid"] for v in model.visible(views, "API")] == ["api-dev"]
    assert [v["uuid"] for v in model.visible(views, "ubuntu")] == ["api-dev", "db"]
    assert [v["uuid"] for v in model.visible(views, "sara")] == ["api-dev"]


def test_a_machine_without_its_own_status_follows_its_workspace():
    v = model.workspace_view(ws("a", "active"), now=NOW)
    assert v["machines"][0]["state"] == model.RUNNING
    boxes = [{"uuid": "m", "title": "n", "status": "in_progress", "ssh_username": "dev",
              "vm_password": "pw"}]
    m = model.workspace_view(ws("a", "active", boxes=boxes), now=NOW)["machines"][0]
    assert (m["state"], m["user"], m["password"]) == (model.STARTING, "dev", "pw")


def test_time_left_counts_down_from_what_the_api_said():
    v = model.workspace_view(ws("a", "active", remaining_time=2 * HOUR * 1000), fetched_at=NOW)
    assert model.time_text(v, NOW) == "2h left"
    assert model.time_text(v, NOW + timedelta(minutes=30)) == "1h 30m left"
    assert model.time_text(v, NOW + timedelta(hours=3)) == "ending"


def test_time_left_falls_back_to_start_and_duration():
    start = (NOW - timedelta(hours=1)).isoformat()
    v = model.workspace_view(ws("a", "active", start_time=start, duration=3,
                                duration_unit="hours"), fetched_at=NOW)
    assert model.time_text(v, NOW) == "2h left"


def test_an_ended_workspace_says_how_long_it_ran():
    v = model.workspace_view(ws("a", "stopped", start_time="2026-09-16T08:00:00Z",
                                stop_time="2026-09-16T09:30:00Z"), now=NOW)
    assert model.time_text(v, NOW) == "ran 1h 30m"


def test_create_body_makes_one_machine_per_picked_recipe():
    ubuntu = {"uuid": "u", "title": "Ubuntu"}
    docker = {"uuid": "d", "title": "Docker"}
    body = model.create_body("lab", [ubuntu, docker, ubuntu], amount=8)
    assert body == {"title": "lab", "duration": 8, "duration_unit": "hours", "boxes": [
        {"title": "Ubuntu 1", "recipe": "u", "position": 0},
        {"title": "Docker", "recipe": "d", "position": 1},
        {"title": "Ubuntu 2", "recipe": "u", "position": 2}]}
    assert "public_ip_requested" not in str(body)


def test_unavailable_recipes_are_listed_last_with_their_reason():
    out = model.recipe_choices([
        {"uuid": "a", "title": "GPU", "is_available": False, "unavailable_reason": "Pro only"},
        {"uuid": "b", "title": "Ubuntu"},
        {"title": "no id"},
    ])
    assert [(r["uuid"], r["available"], r["reason"]) for r in out] == [
        ("b", True, ""), ("a", False, "Pro only")]


def test_logos_come_from_the_recipe_config_and_only_as_web_urls():
    logo = "https://static.easyenv.io/recipe/base/ubuntu_24_04/ubuntu.svg"
    assert model.recipe_choices([{"uuid": "u", "config": {"logo": logo}}])[0]["logo"] == logo
    assert model.recipe_logo({"config": {"logo": "file:///etc/passwd"}}) == ""
    assert model.recipe_logo({"config": None}) == ""


def test_a_card_knows_its_logos_progress_and_subtitle():
    logo = "https://static.easyenv.io/x.svg"
    ubuntu = {"uuid": "r", "title": "Ubuntu", "config": {"logo": logo}}
    boxes = [{"uuid": "1", "title": "a", "recipe": ubuntu},
             {"uuid": "2", "title": "b", "recipe": ubuntu},
             {"uuid": "3", "title": "c", "recipe": {"uuid": "k", "title": "K8s"}}]
    v = model.workspace_view(ws("w", "in_progress", boxes=boxes, progress=42.0,
                                creator={"first_name": "Mohammad", "last_name": "Efazati"},
                                created_at=(NOW - timedelta(hours=3)).isoformat()), now=NOW)
    assert v["logos"] == [("Ubuntu", logo), ("K8s", "")]
    assert v["progress"] == 0.42
    assert model.time_text(v, NOW) == "42%"
    assert model.subtitle(v, NOW) == "3 machines  ·  by Mohammad Efazati  ·  3h ago"
    assert v["label"] == "Starting up"


# --- SSH keys ------------------------------------------------------------------

ED = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIHH6Sdcy0UKpv2dY1JZcL+BrDH9QBudJrV5tW4R8k4u1 me@host"


def test_fingerprints_match_ssh_keygen():
    assert model.key_fingerprint("nonsense") is None
    assert model.key_fingerprint("ssh-ed25519 !!!") is None
    if shutil.which("ssh-keygen"):
        out = subprocess.run(["ssh-keygen", "-lf", "-"], input=ED, capture_output=True,
                             text=True, check=True).stdout
        assert model.key_fingerprint(ED) == out.split()[1]


def test_local_keys_prefer_ed25519_and_skip_junk(tmp_path):
    (tmp_path / "id_rsa.pub").write_text(ED.replace("me@host", "rsa-named") + "\n")
    (tmp_path / "id_ed25519.pub").write_text(ED + "\n")
    (tmp_path / "notes.pub").write_text("not a key\n")
    (tmp_path / "id_ed25519").write_text("PRIVATE\n")
    keys = model.local_public_keys(str(tmp_path))
    assert [os.path.basename(k[0]) for k in keys] == ["id_ed25519.pub", "id_rsa.pub"]
    assert model.local_public_keys(str(tmp_path / "absent")) == []


def test_key_status():
    fp = model.key_fingerprint(ED)
    local = [("/h/.ssh/id_ed25519.pub", ED, fp)]
    assert model.key_status([{"fingerprint": fp}], local) == "ok"
    assert model.key_status([{"fingerprint": "SHA256:other"}], local) == "upload"
    assert model.key_status([], []) == "none"


def test_dashboard_links_for_keys_and_a_workspace():
    assert model.keys_url({}) == "https://dashboard.easyenv.io/dashboard/profile"
    assert model.workspace_url({}, "NzY8WOFs", "ws1") == \
        "https://dashboard.easyenv.io/dashboard/NzY8WOFs/workspaces/ws1"


# --- the dashboard's address -------------------------------------------------

def test_the_dashboard_is_next_to_the_api():
    assert model.web_url({}) == "https://dashboard.easyenv.io"
    assert model.web_url({"server": "https://api.staging.easyenv.io/"}) == \
        "https://dashboard.staging.easyenv.io"
    assert model.web_url({"server": "http://localhost:8051"}) == "http://localhost:3000"
    assert model.web_url({"web_url": "https://custom/"}) == "https://custom"


def test_token_and_buy_hours_links():
    assert model.token_url({}) == "https://dashboard.easyenv.io/integration"
    assert model.buy_hours_url({}, "NzY8WOFs") == \
        "https://dashboard.easyenv.io/dashboard/NzY8WOFs/usage"
    assert model.buy_hours_url({}, None) == \
        "https://dashboard.easyenv.io/dashboard/upgrade-account"


def test_humanize():
    assert [model.humanize(s) for s in (5, 60, 3600, 3660, 86400, 90000, -3)] == \
        ["5s", "1m", "1h", "1h 1m", "1d", "1d 1h", "0s"]


def test_one_logo_chip_per_logo():
    logo = "https://static.easyenv.io/ubuntu.svg"
    boxes = [{"uuid": "1", "title": "a", "recipe": {"uuid": "u24", "title": "Ubuntu 24.04", "config": {"logo": logo}}},
             {"uuid": "2", "title": "b", "recipe": {"uuid": "u26", "title": "Ubuntu 26.04", "config": {"logo": logo}}},
             {"uuid": "3", "title": "c", "recipe": {"uuid": "x", "title": "Custom"}},
             {"uuid": "4", "title": "d", "recipe": {"uuid": "x", "title": "Custom"}}]
    v = model.workspace_view(ws("w", "active", boxes=boxes), now=NOW)
    assert v["logos"] == [("Ubuntu 24.04, Ubuntu 26.04", logo), ("Custom", "")]


def test_a_new_machine_starts_as_ubuntu():
    choices = model.recipe_choices([{"uuid": "ansible", "title": "Ansible"},
                                    {"uuid": "ubuntu_24_04", "title": "Ubuntu 24.04 LTS"}])
    assert choices[model.default_choice(choices)]["uuid"] == "ubuntu_24_04"
    only = model.recipe_choices([{"uuid": "a", "title": "A", "is_available": False},
                                 {"uuid": "b", "title": "B"}])
    assert only[model.default_choice(only)]["uuid"] == "b"


def test_stacks_are_read_from_the_list_and_the_detail_once_each():
    w = ws("w", "active", stacks=[{"name": "claude", "title": "Claude Code", "kind": "catalog"}])
    w["boxes"][0]["stacks"] = [{"stack_recipe": "claude"}, {"stack_recipe": "ansible"},
                               {"stack_recipe": "../evil"}]
    v = model.workspace_view(w, now=NOW, web="https://dashboard.example")
    assert v["stacks"] == [
        ("Claude Code", "https://dashboard.example/images/stacks/claude.svg"),
        ("Ansible", "https://dashboard.example/images/stacks/ansible.svg"),
        ("../evil", "")]
    assert model.workspace_view(ws("x", "active"), now=NOW)["stacks"] == []



# --- sizes and stacks --------------------------------------------------------------

def test_size_limits_read_the_plan_the_way_the_backend_does():
    a = account(total_seconds=1)
    a["current_plan"].update(effective_max_cpu_cores=12, effective_max_ram_mb=48000,
                             effective_max_disk_gb=-1)
    a["current_plan"]["plan"].update(max_cpu_cores=4, maximum_boxes_per_workspace=-1)
    assert model.size_limits(a) == {"cpu": 12, "ram_mb": 48000, "storage_gb": 2000,
                                    "custom": True, "max_machines": None}
    assert model.account_view(a)["limits"]["cpu"] == 12


def test_zero_caps_mean_sizes_are_fixed_on_the_plan():
    # The personal plan on production: all three 0, five machines.
    a = account(total_seconds=1)
    a["current_plan"].update(effective_max_cpu_cores=0, effective_max_ram_mb=0,
                             effective_max_disk_gb=0)
    a["current_plan"]["plan"].update(max_cpu_cores=4, maximum_boxes_per_workspace=5)
    limits = model.size_limits(a)
    assert limits["custom"] is False and limits["max_machines"] == 5
    assert model.size_limits({})["custom"] is False


def test_the_plans_own_caps_are_used_when_the_subscription_has_none():
    a = account(total_seconds=1)
    a["current_plan"]["plan"].update(max_cpu_cores=4, max_ram_mb=8192, max_disk_gb=0)
    assert model.size_limits(a) == {"cpu": 4, "ram_mb": 8192, "storage_gb": 0,
                                    "custom": True, "max_machines": None}


def test_size_text_and_recipe_size():
    assert model.size_text({"cpu": 2, "ram_mb": 4096, "storage_gb": 16}) == \
        "2 vCPU  ·  4 GB RAM  ·  16 GB disk"
    assert model.size_text({"cpu": 1, "ram_mb": 512, "storage_gb": 8}) == \
        "1 vCPU  ·  512 MB RAM  ·  8 GB disk"
    assert model.size_text(None) == ""
    assert model.recipe_size({"resources": {"cpu": 4, "ram_mb": 8192, "storage_gb": 40}}) == \
        {"cpu": 4, "ram_mb": 8192, "storage_gb": 40}
    assert model.recipe_choices([{"uuid": "u"}])[0]["size"] == \
        {"cpu": 2, "ram_mb": 2048, "storage_gb": 20}


CATALOG = [
    {"name": "python", "title": "Python", "tags": ["language"], "description": "Py",
     "vars_schema": [{"key": "version", "type": "select", "options": ["3.14", "3.13"],
                      "defaultValue": "3.13"},
                     {"key": "script", "defaultValue": "#!/bin/sh"}]},
    {"name": "claude", "title": "Claude Code", "tags": ["tools"],
     "vars_schema": [{"key": "script", "defaultValue": "#!/bin/sh"}]},
    {"name": "helm", "title": "Helm", "tags": ["tools"],
     "default_compatibility": {"recipes": ["k3s", "k8s_cluster"]},
     "vars_schema": [{"key": "playbook", "defaultValue": "- hosts: localhost"}]},
    {"name": "vscode", "title": "VS Code", "tags": ["tools"],
     "default_compatibility": {"base_images": ["ubuntu_24_04"], "excluded_recipes": ["vscode"]},
     "vars_schema": []},
    {"name": "ansible", "title": "Ansible", "tags": ["script"],
     "vars_schema": [{"key": "playbook", "type": "code"}]},
    {"name": "old", "title": "Old", "is_active": False, "vars_schema": []},
]


def test_only_stacks_that_need_no_typing_are_offered_languages_first():
    choices = model.stack_choices(CATALOG, "https://d")
    assert [c["name"] for c in choices] == ["python", "claude", "helm", "vscode"]
    py = choices[0]
    assert py["version"] == {"key": "version", "options": ["3.14", "3.13"], "default": "3.13"}
    assert py["logo"] == "https://d/images/stacks/python.svg"
    assert choices[1]["version"] is None


def test_stacks_fit_only_the_recipes_they_are_made_for():
    by = {c["name"]: c for c in model.stack_choices(CATALOG)}
    ubuntu = {"uuid": "ubuntu_24_04", "base_image": "ubuntu_24_04"}
    debian = {"uuid": "debian_13", "base_image": "debian_13"}
    k3s = {"uuid": "k3s", "base_image": "ubuntu_24_04"}
    assert model.stack_fits(by["python"], debian)
    assert not model.stack_fits(by["helm"], ubuntu) and model.stack_fits(by["helm"], k3s)
    assert model.stack_fits(by["vscode"], ubuntu) and not model.stack_fits(by["vscode"], debian)
    assert not model.stack_fits(by["vscode"], {"uuid": "vscode", "base_image": "ubuntu_24_04"})


def test_stack_vars_send_only_the_version():
    by = {c["name"]: c for c in model.stack_choices(CATALOG)}
    assert model.stack_vars(by["python"]) == {"version": "3.13"}
    assert model.stack_vars(by["python"], "3.14") == {"version": "3.14"}
    assert model.stack_vars(by["claude"]) == {}


def test_create_body_carries_stacks_and_only_a_changed_size():
    ubuntu = {"uuid": "u", "title": "Ubuntu", "size": {"cpu": 2, "ram_mb": 2048, "storage_gb": 20}}
    body = model.create_body("lab", [
        {"recipe": ubuntu, "stacks": [("python", {"version": "3.14"}), ("claude", {})],
         "size": {"cpu": 4, "ram_mb": 8192, "storage_gb": 40}},
        {"recipe": ubuntu, "stacks": [], "size": None},
        {"recipe": ubuntu, "stacks": [], "size": dict(ubuntu["size"])},
    ])
    first, second, third = body["boxes"]
    assert first == {"title": "Ubuntu 1", "recipe": "u", "position": 0,
                     "stacks": [{"stack_recipe": "python", "vars": {"version": "3.14"}},
                                {"stack_recipe": "claude", "vars": {}}],
                     "cpu": 4, "ram_mb": 8192, "storage_gb": 40}
    assert second == {"title": "Ubuntu 2", "recipe": "u", "position": 1}
    assert "cpu" not in third


def test_a_machine_shows_its_size_and_stacks_from_the_detail():
    box = {"uuid": "m", "title": "n", "cpu": 2, "ram_mb": 4096, "storage_gb": 16,
           "stacks": [{"stack_recipe": "python", "vars": {"version": "3.13"}},
                      {"inline_title": "My setup", "inline_runner": "bash"},
                      "junk"]}
    m = model.machine_view(box, "https://d")
    assert m["size"] == {"cpu": 2, "ram_mb": 4096, "storage_gb": 16}
    assert [(s["title"], s["version"], s["logo"]) for s in m["stacks"]] == [
        ("Python", "3.13", "https://d/images/stacks/python.svg"), ("My setup", "", "")]
    assert model.machine_view({"uuid": "x"})["size"] is None


TEMPLATES = [
    {"uuid": "mine", "title": "EasyEnv Fleet", "account": {"uuid": "acct"},
     "recipes": [{"uuid": "ubuntu_26_04", "title": "Ubuntu 26.04 LTS",
                  "config": {"logo": "https://s/ubuntu.svg"},
                  "resources": {"cpu": 2, "ram_mb": 2048, "storage_gb": 20}}],
     "template_recipes": [{"recipe": "ubuntu_26_04"}], "stacks": []},
    {"uuid": "cicd", "title": "CI/CD", "account": None,
     "recipes": [{"uuid": "jenkins", "title": "Jenkins"}, {"uuid": "gitea", "title": "Gitea"}],
     "template_recipes": [{"recipe": "gitea"}, {"recipe": "jenkins"}],
     "stacks": [{"stack_recipe": "docker-run", "stack_title": "Docker Image",
                 "recipe": "jenkins", "vars": {"image": "nginx"}},
                {"stack_recipe": "claude", "stack_title": "Claude Code", "recipe": None, "vars": {}}]},
    {"uuid": "empty", "title": "Nothing", "recipes": []},
]


def test_templates_become_machines_in_the_templates_order_with_their_stacks():
    out = model.template_choices(TEMPLATES, "https://d")
    assert [t["uuid"] for t in out] == ["mine", "cicd"]
    mine, cicd = out
    assert mine["owned"] and not cicd["owned"]
    assert [m["recipe"]["uuid"] for m in cicd["machines"]] == ["gitea", "jenkins"]
    assert cicd["machines"][0]["stacks"] == []
    # Stacks that belong to the workspace, not a machine, are dropped as the dashboard does.
    assert cicd["machines"][1]["stacks"] == [("docker-run", "Docker Image", {"image": "nginx"})]
    assert cicd["logos"] == [("Gitea", ""), ("Jenkins", ""),
                             ("Docker Image", "https://d/images/stacks/docker-run.svg")]
    assert mine["machines"][0]["recipe"]["size"] == {"cpu": 2, "ram_mb": 2048, "storage_gb": 20}
    assert model.template_summary(cicd) == "2 machines  ·  Docker Image"
    assert model.template_summary(mine) == "1 machine  ·  this account's"


def test_create_body_names_the_template_it_came_from():
    ubuntu = {"uuid": "u", "title": "Ubuntu"}
    body = model.create_body("lab", [{"recipe": ubuntu}], template="mine")
    assert body["workspace_template"] == "mine"
    assert "workspace_template" not in model.create_body("lab", [{"recipe": ubuntu}])


def test_the_filter_shows_active_ended_or_all_and_counts_each():
    views = [model.workspace_view(w, now=NOW) for w in (
        ws("a", "active", title="api"), ws("b", "stopped", title="api-old"),
        ws("c", "failed", title="db"), ws("d", "stopped", title="db-old"))]
    assert [v["uuid"] for v in model.visible(views, show="active")] == ["a", "c"]
    assert [v["uuid"] for v in model.visible(views, show="ended")] == ["b", "d"]
    assert len(model.visible(views, show="all")) == 4
    assert model.filter_counts(views) == {"active": 2, "ended": 2, "all": 4}
    assert model.filter_counts(views, "api") == {"active": 1, "ended": 1, "all": 2}


def test_the_upgrade_link_is_the_dashboards_plans_page():
    assert model.upgrade_url({}) == "https://dashboard.easyenv.io/dashboard/upgrade-account"
