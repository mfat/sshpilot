"""Manual SFTP/file-manager scripts.

These files are CLI utilities (each has its own ``if __name__ == "__main__"``
entry point) that require a live SSH server to be useful. They are not
pytest-style tests, so we tell pytest to skip them during collection.
"""

collect_ignore_glob = ["test_*.py"]
