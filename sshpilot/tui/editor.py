from __future__ import annotations

import copy
from typing import Any, Callable, Dict, List, Optional

PromptFunc = Callable[[str], str]
PrintFunc = Callable[[str], None]


class ConnectionEditSession:
    """
    Text-mode editor for modifying connection attributes that normally live in the GTK dialog.

    The session interacts through ``input_func``/``print_func`` so it can run both interactively
    and under tests.
    """

    def __init__(
        self,
        connection,
        *,
        input_func: PromptFunc = input,
        print_func: PrintFunc = print,
    ):
        self.connection = connection
        self.input = input_func
        self.print = print_func

    def run(self) -> Optional[Dict[str, Any]]:
        """
        Prompt the user for edits. Returns a dictionary with updated fields or ``None`` on cancel.
        """

        try:
            return self._run()
        except KeyboardInterrupt:
            self.print("\nEdit cancelled.")
            return None

    def _run(self) -> Optional[Dict[str, Any]]:
        conn = self.connection
        self.print("\n--- Connection editor ---")
        self.print("Press Enter to keep current values, '-' to clear a field, Ctrl+C to abort.\n")

        nickname = self._ask_text("Nickname", getattr(conn, "nickname", ""), required=True)
        hostname = self._ask_text("Hostname", getattr(conn, "hostname", ""))
        username = self._ask_text("Username", getattr(conn, "username", ""))
        port = self._ask_port(getattr(conn, "port", 22))

        local_cmd = self._ask_text("Local command", getattr(conn, "local_command", ""), allow_clear=True)
        remote_cmd = self._ask_text("Remote command", getattr(conn, "remote_command", ""), allow_clear=True)

        rules = copy.deepcopy(getattr(conn, "forwarding_rules", []) or [])
        if self._confirm("Edit port forwarding rules? [y/N] ", default=False):
            rules = self._edit_forwarding_rules(rules)
            if rules is None:
                return None

        rules = self._sanitize_rules(rules)

        payload: Dict[str, Any] = {
            "nickname": nickname,
            "hostname": hostname,
            "username": username,
            "port": port,
            "local_command": local_cmd,
            "remote_command": remote_cmd,
            "forwarding_rules": rules,
        }

        source = getattr(conn, "source", "")
        if source:
            payload["source"] = source

        return payload

    # ------------------------------------------------------------------ helpers
    def _ask_text(self, label: str, current: str, *, required: bool = False, allow_clear: bool = False) -> str:
        base_prompt = f"{label}"
        if current:
            base_prompt += f" [{current}]"
        if allow_clear and current:
            base_prompt += " (type '-' to clear)"
        base_prompt += ": "

        while True:
            resp = self.input(base_prompt)
            if resp is None:
                resp = ""
            resp = resp.strip()
            if not resp:
                if current or not required:
                    return current
                self.print(f"{label} is required.")
                continue
            if allow_clear and resp == "-":
                return ""
            return resp

    def _ask_port(self, current: Any) -> int:
        prompt = f"Port [{current}]: "
        while True:
            resp = self.input(prompt)
            if resp is None:
                resp = ""
            resp = resp.strip()
            if not resp:
                try:
                    return int(current)
                except (TypeError, ValueError):
                    current = 22
                    prompt = f"Port [{current}]: "
                    continue
            try:
                value = int(resp)
                if 1 <= value <= 65535:
                    return value
            except ValueError:
                pass
            self.print("Port must be a number between 1 and 65535.")

    def _confirm(self, prompt: str, *, default: bool) -> bool:
        while True:
            resp = self.input(prompt)
            if resp is None:
                resp = ""
            resp = resp.strip().lower()
            if not resp:
                return default
            if resp in ("y", "yes"):
                return True
            if resp in ("n", "no"):
                return False
            self.print("Please respond with 'y' or 'n'.")

    # ---------------------------------------------------------- forwarding menu
    def _edit_forwarding_rules(self, initial_rules: List[Dict[str, Any]]) -> Optional[List[Dict[str, Any]]]:
        rules = [copy.deepcopy(rule) for rule in (initial_rules or [])]
        while True:
            self.print("\nCurrent forwarding rules:")
            if not rules:
                self.print("  (none)")
            else:
                for idx, rule in enumerate(rules, 1):
                    status = "enabled" if rule.get("enabled", True) else "disabled"
                    summary = self._summarize_rule(rule)
                    self.print(f"  {idx}. [{status}] {summary}")

            action = self.input("Choose action: [a]dd  [e]dit  [d]elete  [t]oggle  [enter=done] ").strip().lower()
            if not action:
                return rules
            if action not in {"a", "e", "d", "t"}:
                self.print("Unknown action. Choose a/e/d/t or press Enter to finish.")
                continue

            if action == "a":
                new_rule = self._prompt_rule()
                if new_rule:
                    rules.append(new_rule)
            else:
                idx = self._ask_rule_index(rules)
                if idx is None:
                    continue
                if action == "e":
                    updated = self._prompt_rule(existing=rules[idx])
                    if updated:
                        rules[idx] = updated
                elif action == "d":
                    del rules[idx]
                elif action == "t":
                    rules[idx]["enabled"] = not rules[idx].get("enabled", True)

    def _ask_rule_index(self, rules: List[Dict[str, Any]]) -> Optional[int]:
        if not rules:
            self.print("No rules available.")
            return None
        prompt = f"Select rule [1-{len(rules)}]: "
        while True:
            resp = self.input(prompt)
            if resp is None:
                resp = ""
            resp = resp.strip()
            if not resp:
                return None
            try:
                idx = int(resp) - 1
                if 0 <= idx < len(rules):
                    return idx
            except ValueError:
                pass
            self.print("Invalid index.")

    def _prompt_rule(self, existing: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        try:
            current_type = (existing or {}).get("type", "local")
            rule_type = self._ask_choice(
                "Rule type [l=local, r=remote, d=dynamic]",
                choices={"l": "local", "r": "remote", "d": "dynamic"},
                current=current_type,
            )

            listen_addr_default = (existing or {}).get("listen_addr", "localhost") or "localhost"
            addr_label = "Bind address" if rule_type == "local" else "Listen address"
            listen_addr = self._ask_text(addr_label, listen_addr_default, allow_clear=True) or "localhost"

            default_listen_port = (existing or {}).get("listen_port")
            if not default_listen_port:
                default_listen_port = 8080 if rule_type == "local" else 2222 if rule_type == "remote" else 1080
            listen_port = self._ask_int("Listen port", default_listen_port)
            enabled = bool((existing or {}).get("enabled", True))
            enabled = self._confirm(
                f"Enable this rule? [{'Y' if enabled else 'y'}/{'n' if enabled else 'N'}] ",
                default=enabled,
            )

            rule: Dict[str, Any] = {
                "type": rule_type,
                "listen_addr": listen_addr,
                "listen_port": listen_port,
                "enabled": enabled,
            }

            if rule_type == "dynamic":
                return rule

            if rule_type == "remote":
                dest_host_default = (existing or {}).get("local_host") or (existing or {}).get("remote_host") or "localhost"
                dest_port_default = (existing or {}).get("local_port") or (existing or {}).get("remote_port") or 22
                dest_host = self._ask_text("Destination host", dest_host_default or "localhost")
                dest_port = self._ask_int("Destination port", dest_port_default)
                rule["local_host"] = dest_host
                rule["local_port"] = dest_port
            else:
                remote_host_default = (existing or {}).get("remote_host") or "localhost"
                remote_port_default = (existing or {}).get("remote_port") or 22
                remote_host = self._ask_text("Remote host", remote_host_default)
                remote_port = self._ask_int("Remote port", remote_port_default)
                rule["remote_host"] = remote_host
                rule["remote_port"] = remote_port

            return rule
        except KeyboardInterrupt:
            self.print("\nRule edit cancelled.")
            return None

    def _ask_choice(self, prompt: str, *, choices: Dict[str, str], current: str) -> str:
        normalized_current = None
        for key, value in choices.items():
            if value == current:
                normalized_current = key
                break
        prompt_txt = f"{prompt} [{normalized_current or next(iter(choices))}]: "
        while True:
            resp = self.input(prompt_txt)
            if resp is None:
                resp = ""
            resp = resp.strip().lower()
            if not resp:
                return choices.get(normalized_current, current) or next(iter(choices.values()))
            if resp in choices:
                return choices[resp]
            self.print(f"Invalid choice. Expected one of: {', '.join(choices)}")

    def _ask_int(self, label: str, current: Any) -> int:
        current_val = current or 0
        prompt = f"{label} [{current_val}]: "
        while True:
            resp = self.input(prompt)
            if resp is None:
                resp = ""
            resp = resp.strip()
            if not resp:
                value = current_val or 0
            else:
                try:
                    value = int(resp)
                except ValueError:
                    self.print(f"{label} must be a number.")
                    continue
            if 1 <= value <= 65535:
                return value
            self.print(f"{label} must be between 1 and 65535.")

    @staticmethod
    def _summarize_rule(rule: Dict[str, Any]) -> str:
        rtype = rule.get("type", "local")
        listen = f"{rule.get('listen_addr', 'localhost')}:{rule.get('listen_port', '')}"
        if rtype == "dynamic":
            return f"Dynamic SOCKS on {listen}"
        if rtype == "remote":
            dest = f"{rule.get('local_host') or rule.get('remote_host') or 'localhost'}:{rule.get('local_port') or rule.get('remote_port', '')}"
            return f"Remote {listen} -> {dest}"
        dest = f"{rule.get('remote_host', 'localhost')}:{rule.get('remote_port', '')}"
        return f"Local {listen} -> {dest}"

    @staticmethod
    def _sanitize_rules(rules: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        sanitized: List[Dict[str, Any]] = []
        for raw in rules or []:
            try:
                rule_type = raw.get("type", "local")
                listen_addr = (raw.get("listen_addr") or "").strip() or "localhost"
                listen_port = int(raw.get("listen_port") or 0)
                if listen_port <= 0 or listen_port > 65535:
                    continue
                enabled = bool(raw.get("enabled", True))

                if rule_type == "dynamic":
                    sanitized.append(
                        {
                            "type": "dynamic",
                            "enabled": enabled,
                            "listen_addr": listen_addr,
                            "listen_port": listen_port,
                        }
                    )
                    continue

                if rule_type == "remote":
                    dest_host = (raw.get("local_host") or raw.get("remote_host") or "").strip() or "localhost"
                    dest_port = int(raw.get("local_port") or raw.get("remote_port") or 0)
                    if dest_port <= 0 or dest_port > 65535:
                        continue
                    sanitized.append(
                        {
                            "type": "remote",
                            "enabled": enabled,
                            "listen_addr": listen_addr,
                            "listen_port": listen_port,
                            "local_host": dest_host,
                            "local_port": dest_port,
                        }
                    )
                    continue

                # Default to local forwarding
                remote_host = (raw.get("remote_host") or "").strip() or "localhost"
                remote_port = int(raw.get("remote_port") or 0)
                if remote_port <= 0 or remote_port > 65535:
                    continue
                sanitized.append(
                    {
                        "type": "local",
                        "enabled": enabled,
                        "listen_addr": listen_addr,
                        "listen_port": listen_port,
                        "remote_host": remote_host,
                        "remote_port": remote_port,
                    }
                )
            except Exception:
                continue
        return sanitized


__all__ = ["ConnectionEditSession"]
