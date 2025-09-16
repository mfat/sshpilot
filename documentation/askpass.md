The askpass helper doesn’t log through the main logger.
It writes debug information to a log file in an accessible runtime directory:

- If `SSHPILOT_ASKPASS_LOG_DIR` is set, that directory is used.
- Otherwise `XDG_RUNTIME_DIR` is used when available.
- If neither is set, the system temporary directory is used.

The log file is named `sshpilot-askpass.log` within that directory, which is created if needed. No messages appear in the normal console or application log unless you inspect this file.