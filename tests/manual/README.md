# Manual test harnesses

These scripts exercise the SFTP file manager against a **live SSH host** and are
run by hand, not by pytest:

```
python3 tests/manual/test_file_manager.py --connection NICKNAME [--remote-dir PATH]
python3 tests/manual/test_sftp_comprehensive.py --connection NICKNAME [--remote-dir PATH]
```

They require a reachable server configured in `~/.ssh/config` and will create
and delete files under the chosen remote directory.
