"""Concrete :class:`~sshpilot.identity.IdentityProvider` implementations.

Ships the system ssh-agent and file-key providers. Kept import-light so importing
this package (and the base abstraction) stays cheap — import the concrete
provider modules directly::

    from sshpilot.providers.system_agent import SystemAgentProvider
    from sshpilot.providers.file_key import FileKeyProvider
"""
