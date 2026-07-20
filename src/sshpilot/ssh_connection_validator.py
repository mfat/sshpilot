"""Form-input validation for the connection dialog.

Extracted verbatim from connection_dialog.py into a leaf module so the
validation logic can be imported and tested without the GTK dialog. These
validate user-entered fields (connection name, hostname/IP, port, username) and
verify a key passphrase with a *local* ``ssh-keygen -y`` — they never open an
SSH connection, so they are unrelated to the single connection/auth path.
"""

import os
import re
import logging
import ipaddress
import subprocess

# Initialize gettext
try:
    from . import gettext as _
except ImportError:
    # Fallback for when gettext is not available
    _ = lambda s: s

logger = logging.getLogger(__name__)


class ValidationResult:
    def __init__(self, is_valid: bool = True, message: str = "", severity: str = "info"):
        self.is_valid = is_valid
        self.message = message
        self.severity = severity  # "error", "warning", "info"

class SSHConnectionValidator:
    def __init__(self):
        self.reserved_usernames = {
            'root', 'daemon', 'bin', 'sys', 'sync', 'games', 'man', 'lp', 'mail',
            'news', 'uucp', 'proxy', 'www-data', 'backup', 'list', 'irc', 'gnats',
            'nobody', 'systemd-timesync', 'systemd-network', 'systemd-resolve'
        }
        self.common_ssh_ports = {22, 2222, 222, 2022}
        self.system_ports = set(range(1, 1024))
        self.service_ports = {
            21: "FTP", 22: "SSH", 23: "Telnet", 25: "SMTP", 53: "DNS",
            80: "HTTP", 110: "POP3", 143: "IMAP", 443: "HTTPS",
            993: "IMAPS", 995: "POP3S", 3389: "RDP", 5432: "PostgreSQL",
            3306: "MySQL", 27017: "MongoDB", 6379: "Redis", 5672: "RabbitMQ"
        }
        self.existing_names: set[str] = set()
        self.valid_tlds = {
            'com','org','net','edu','gov','mil','int','biz','info','name','pro','aero','coop','museum',
            'local','localhost','test','invalid',
            'us','uk','ca','au','de','fr','jp','cn','ru','br','in','it','es','mx','kr','nl','se','no','dk','fi','ch','at','be','ie'
        }

    def set_existing_names(self, names: set[str]):
        self.existing_names = {str(n).strip().lower() for n in (names or set())}

    def validate_connection_name(self, name: str) -> 'ValidationResult':
        if not name or not name.strip():
            return ValidationResult(False, _("Connection name is required"), "error")
        name = name.strip()
        if re.search(r"\s", name):
            return ValidationResult(False, _("Connection name cannot contain whitespace"), "error")
        if name.strip().lower() in self.existing_names:
            return ValidationResult(False, _("Nickname already exists"), "error")
        return ValidationResult(True, _("Valid connection name"))

    def _validate_ip_address(self, ip_str: str) -> 'ValidationResult':
        try:
            ip = ipaddress.ip_address(ip_str)
            if ip.is_loopback:
                return ValidationResult(True, _("Loopback address (localhost)"), "info")
            elif ip.is_private:
                return ValidationResult(True, _("Private network address"), "info")
            elif ip.is_multicast:
                return ValidationResult(True, _("Multicast address"), "warning")
            elif getattr(ip, 'is_reserved', False):
                return ValidationResult(True, _("Reserved IP address"), "warning")
            elif ip.version == 4 and str(ip).startswith('169.254.'):
                return ValidationResult(True, _("Link-local address"), "warning")
            return ValidationResult(True, _("Valid IPv{ver} address").format(ver=ip.version))
        except ValueError:
            return ValidationResult(False, _("Invalid IP address format"), "error")

    def _validate_hostname(self, hostname: str) -> 'ValidationResult':
        if len(hostname) > 253:
            return ValidationResult(False, _("Hostname too long (max 253 characters)"), "error")
        # Reject leading/trailing dot and consecutive dots
        if hostname.startswith('.'):
            return ValidationResult(False, _("Hostname cannot start with dot"), "error")
        if hostname.endswith('.'):
            return ValidationResult(False, _("Hostname cannot end with dot"), "error")
        if '..' in hostname:
            return ValidationResult(False, _("Hostname cannot contain consecutive dots"), "error")
        if not re.match(r'^[a-zA-Z0-9]([a-zA-Z0-9\-\.]*[a-zA-Z0-9])?$', hostname):
            return ValidationResult(False, _("Invalid hostname format"), "error")
        labels = hostname.split('.')
        for label in labels:
            if not label:
                return ValidationResult(False, _("Empty hostname segment"), "error")
            if len(label) > 63:
                return ValidationResult(False, _("Hostname segment too long (max 63 chars)"), "error")
            if label.startswith('-') or label.endswith('-'):
                return ValidationResult(False, _("Hostname segment cannot start/end with hyphen"), "error")
            # Disallow all-digit TLDs and label of only digits for TLD
            # We'll check the last label separately as TLD
        if hostname.lower() in ['localhost', '127.0.0.1', '::1']:
            return ValidationResult(True, _("Local hostname"), "info")
        if '.' not in hostname:
            return ValidationResult(True, _("Consider using fully qualified domain name"), "warning")
        # Validate TLD: must start with a letter, not all-digit
        tld = labels[-1]
        if not re.match(r'^[A-Za-z][A-Za-z0-9-]{1,}$', tld):
            return ValidationResult(False, _("Invalid top-level domain"), "error")
        if re.fullmatch(r'\d+', tld):
            return ValidationResult(False, _("Invalid top-level domain"), "error")
        # Warn if TLD unknown/uncommon (alphabetic, not in list, not 2-letter ccTLD)
        if tld.isalpha() and tld.lower() not in self.valid_tlds and len(tld) != 2:
            return ValidationResult(True, _("Unknown or uncommon top-level domain"), "warning")
        return ValidationResult(True, _("Valid hostname"))

    def validate_hostname(self, hostname: str, allow_empty: bool = False) -> 'ValidationResult':
        if not hostname or not hostname.strip():
            if allow_empty:
                return ValidationResult(True, "")
            return ValidationResult(False, _("Hostname is required"), "error")
        hostname = hostname.strip()
        ip_result = self._validate_ip_address(hostname)
        if ip_result.is_valid or not ip_result.message.startswith("Invalid IP"):
            return ip_result
        # If looks like numeric IPv4 but invalid, treat as error explicitly
        if re.fullmatch(r"[0-9.]+", hostname):
            return ValidationResult(False, _("Invalid IPv4 address format"), "error")
        # Pure structural validation (avoid DNS on typing to reduce lag)
        return self._validate_hostname(hostname)

    def validate_port(self, port: str, context: str = "SSH") -> 'ValidationResult':
        if not port or not str(port).strip():
            return ValidationResult(False, _("Port is required"), "error")
        try:
            port_num = int(str(port).strip())
        except ValueError:
            return ValidationResult(False, _("Port must be a number"), "error")
        if not (1 <= port_num <= 65535):
            return ValidationResult(False, _("Port must be between 1-65535"), "error")
        if port_num in self.system_ports:
            if port_num in self.service_ports:
                service = self.service_ports[port_num]
                if context == "SSH" and port_num in self.common_ssh_ports:
                    return ValidationResult(True, _("Standard {svc} port").format(svc=service), "info")
                else:
                    return ValidationResult(True, _("System port for {svc} service").format(svc=service), "warning")
            else:
                return ValidationResult(True, _("System port - requires administrator privileges"), "warning")
        if context == "SSH" and port_num not in self.common_ssh_ports:
            if port_num in self.service_ports:
                service = self.service_ports[port_num]
                return ValidationResult(True, _("Unusual for SSH - typically used for {svc}").format(svc=service), "warning")
            elif port_num > 49152:
                return ValidationResult(True, _("Dynamic port range"), "info")
        return ValidationResult(True, _("Valid port number"))

    def validate_username(self, username: str) -> 'ValidationResult':
        if not username or not username.strip():
            return ValidationResult(False, _("Username is required"), "error")
        return ValidationResult(True, _("Valid username"))

    def verify_key_passphrase(self, key_path: str, passphrase: str) -> bool:
        """Verify that the passphrase matches the private key using ssh-keygen -y"""
        if not key_path or not os.path.exists(key_path):
            return False

        try:
            # Run ssh-keygen -y to test the passphrase
            result = subprocess.run([
                'ssh-keygen', '-y', '-P', passphrase, '-f', key_path
            ], capture_output=True, text=True, timeout=10)

            # Exit code 0 means the passphrase is valid
            return result.returncode == 0
        except subprocess.TimeoutExpired:
            logger.error(f"Timeout verifying passphrase for key: {key_path}")
            return False
        except subprocess.CalledProcessError:
            # This shouldn't happen since we're capturing output, but handle it
            return False
        except Exception as e:
            logger.error(f"Error verifying passphrase for key {key_path}: {e}")
            return False
