import os
import subprocess
import sys
import textwrap

from sshpilot.agent_client import AgentClient


def test_flatpak_agent_command_handles_cwd_with_spaces(tmp_path, monkeypatch):
    client = AgentClient()

    agent_script = tmp_path / "mock_agent.py"
    agent_script.write_text(
        textwrap.dedent(
            """\
            import os
            import sys
            from pathlib import Path

            def main():
                args = sys.argv[1:]
                cwd_value = None
                for index, value in enumerate(args):
                    if value == '--cwd' and index + 1 < len(args):
                        cwd_value = args[index + 1]
                        break

                output_path = Path(os.environ['SSHPILOT_TEST_OUTPUT'])
                output_path.write_text(cwd_value or '')

            if __name__ == '__main__':
                main()
            """
        )
    )

    monkeypatch.setattr(
        client,
        'find_agent',
        lambda: (sys.executable, str(agent_script)),
    )

    flatpak_spawn = tmp_path / "flatpak-spawn"
    flatpak_spawn.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import os
            import subprocess
            import sys

            def main():
                args = sys.argv[1:]
                env = os.environ.copy()
                index = 0

                while index < len(args):
                    value = args[index]
                    if value == '--host':
                        index += 1
                        continue

                    if value.startswith('--env='):
                        key, val = value[6:].split('=', 1)
                        env[key] = val
                        index += 1
                        continue

                    break

                cmd = args[index:]
                subprocess.run(cmd, check=True, env=env)

            if __name__ == '__main__':
                main()
            """
        )
    )
    flatpak_spawn.chmod(0o755)

    monkeypatch.setenv('PATH', f"{tmp_path}:{os.environ.get('PATH', '')}")

    cwd_path = tmp_path / "dir with spaces"
    output_path = tmp_path / "result.txt"

    cmd = client._build_flatpak_agent_command(
        rows=24,
        cols=80,
        cwd=str(cwd_path),
        verbose=False,
    )

    assert cmd is not None

    run_env = os.environ.copy()
    run_env['SSHPILOT_TEST_OUTPUT'] = str(output_path)

    subprocess.run(cmd, check=True, env=run_env)

    assert output_path.read_text() == str(cwd_path)
