"""_pty_child.main() must work both as a standalone script (default sys.argv)
and when called directly with an explicit argv — the shape run.py's frozen
``--internal-pty-child`` dispatch uses (see pty_runner.py)."""

from unittest import mock

from sshpilot.daemon import _pty_child


def test_main_execs_ssh_with_explicit_argv():
    calls = {}

    def fake_execvpe(file, args, env):
        calls["file"] = file
        calls["args"] = list(args)
        # Real execvpe never returns; simulate that so the surrounding
        # try/except doesn't treat "returned normally" as a failure to
        # report back over the status pipe.
        raise SystemExit(0)

    with mock.patch.object(_pty_child.os, "set_inheritable") as set_inheritable, \
         mock.patch.object(_pty_child.os, "setsid") as setsid, \
         mock.patch.object(_pty_child.os, "login_tty") as login_tty, \
         mock.patch.object(_pty_child.os, "execvpe", side_effect=fake_execvpe):
        try:
            # run.py's --internal-pty-child dispatch calls this with
            # sys.argv[1:], i.e. ['--internal-pty-child', slave_fd, status_fd,
            # '--', *ssh_argv] — the flag stands in for the ignored "script
            # name" slot at index 0.
            _pty_child.main(
                ["--internal-pty-child", "7", "8", "--", "ssh", "-F", "cfg", "host"]
            )
        except SystemExit:
            pass

    set_inheritable.assert_called_once_with(8, False)
    setsid.assert_called_once_with()
    login_tty.assert_called_once_with(7)
    assert calls["file"] == "ssh"
    assert calls["args"] == ["ssh", "-F", "cfg", "host"]


def test_main_reports_launch_failed_on_bad_argv():
    status_writes = []

    def fake_write(fd, data):
        status_writes.append((fd, data))
        return len(data)

    with mock.patch.object(_pty_child.os, "write", side_effect=fake_write):
        # separator not at index 3 -> ValueError before status_fd is even
        # parsed in a way that would let us write to it; this exercises the
        # inner try/except swallowing that gracefully.
        result = _pty_child.main(["prog", "--", "ssh"])

    assert result == 127


def test_main_reports_launch_failed_when_exec_target_missing():
    status_writes = []

    def fake_write(fd, data):
        status_writes.append((fd, data))
        return len(data)

    def fake_execvpe(file, args, env):
        raise FileNotFoundError(file)

    with mock.patch.object(_pty_child.os, "set_inheritable"), \
         mock.patch.object(_pty_child.os, "setsid"), \
         mock.patch.object(_pty_child.os, "login_tty"), \
         mock.patch.object(_pty_child.os, "execvpe", side_effect=fake_execvpe), \
         mock.patch.object(_pty_child.os, "write", side_effect=fake_write):
        result = _pty_child.main(["--internal-pty-child", "7", "8", "--", "ssh", "host"])

    assert result == 127
    assert status_writes == [(8, b"launch_failed")]
