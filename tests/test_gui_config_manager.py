"""Real GTK window -> daemon -> OpenSSH configuration-manager acceptance tests."""
import time

import pytest

pytestmark = pytest.mark.gui


def pump(until, seconds=20):
    from gi.repository import GLib
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        while GLib.MainContext.default().pending():
            GLib.MainContext.default().iteration(False)
        if until():
            return
        time.sleep(.01)
    raise AssertionError('GTK configuration-manager action did not complete')


@pytest.fixture
def manager(gui, tmp_path):
    from sshpilot.api import DaemonClient
    from sshpilot.core.connection_application_service import ConnectionApplicationService
    from sshpilot.core.connections.repository import ConnectionRepository
    from sshpilot.core.connections.ssh_config_store import SshConfigStore
    from sshpilot.daemon import DaemonServer
    from sshpilot.gtk.config_manager_window import ConfigManagerWindow

    root = tmp_path/'config'
    root.write_text('Host *\n  ServerAliveInterval 30\n'
                    'Host prod-*\n  User deploy\n  ProxyJump bastion\n  IdentityFile ~/.ssh/deploy_ed25519\n'
                    'Host prod-web\n  HostName production.example.com\n'
                    'Host prod-db\n  HostName prod-db.example.com\n'
                    'Host staging\n  HostName staging.example.com\n'
                    'Match host *.internal\n  Port 2222\n')
    state = tmp_path/'state'
    state.mkdir()
    repo = ConnectionRepository(ssh_store=SshConfigStore(root, isolated=True),
                                state_path=state/'connections.json', legacy_config_path=state/'config.json', isolated=True)
    service = ConnectionApplicationService(repo)
    server = DaemonServer(lambda: service, socket_path=tmp_path/'daemon.sock')
    server.start_in_thread()
    client = DaemonClient(socket_path=tmp_path/'daemon.sock')
    window = None
    try:
        window = ConfigManagerWindow(client=client)
        window.present()
        pump(lambda: window._preview is not None and not window._busy)
        yield window, root
    finally:
        if window and not window._closed:
            window._finish_close()
        client.close()
        server.shutdown()
        assert server.wait_stopped()


def test_window_edits_previews_moves_and_saves_through_real_daemon(manager):
    window, root = manager
    original = root.read_text()
    window.block_list.select_row(window.block_list.get_row_at_index(1))
    pump(lambda: not window._busy and window._view.selected == 1 and window._preview is not None)
    assert len([h for h in window._preview.hosts if h.matched]) == 2
    window._directive_entries[0][1].set_text('release')
    window._preview_clicked()
    pump(lambda: not window._busy and window._preview is not None)
    assert root.read_text() == original
    assert all('user release' in h.after for h in window._preview.hosts if h.matched)
    window._move(1)
    pump(lambda: not window._busy and window._view.selected == 2)
    assert window._view.blocks[2].argument == 'prod-*'
    window._save_clicked()
    pump(lambda: not window._busy and window.status.get_text() == 'Configuration saved')
    assert 'User release' in root.read_text()
    assert not window._dirty()
    assert window.stack.get_page(window.stack.get_child_by_name('effective')).get_title() == 'Effective Config'


def test_invalid_draft_is_not_saved_and_remains_editable(manager):
    window, root = manager
    original = root.read_text()
    window._directive_entries[0][0].set_text('InvalidSshDirective')
    window._save_clicked()
    pump(lambda: not window._busy)
    assert not window._preview.valid
    assert root.read_text() == original
    assert window.content.get_sensitive()
    assert window._dirty()


def test_add_and_delete_dialogs_and_unsaved_close_confirmation(manager):
    from gi.repository import Adw, Gtk
    window, root = manager
    original = root.read_text()
    def dialog():
        return next(w for w in Gtk.Window.get_toplevels() if isinstance(w, Adw.MessageDialog) and w.get_transient_for() == window and w.get_visible())
    def click_response(prompt, label):
        # MessageDialog.response() only emits the signal; exercise the actual
        # button so GTK also closes the modal as it does for a user click.
        def descendants(widget):
            yield widget
            child = widget.get_first_child()
            while child is not None:
                yield from descendants(child)
                child = child.get_next_sibling()
        button = next(w for w in descendants(prompt) if isinstance(w, Gtk.Button) and w.get_label() == label)
        button.emit('clicked')
    window._add_clicked('Host')
    prompt = dialog()
    entry = prompt.get_extra_child().get_last_child()
    entry.set_text('new-connection')
    click_response(prompt, 'Add')
    pump(lambda: not window._busy and window._view.blocks[-1].argument == 'new-connection')
    assert root.read_text() == original
    window._delete_clicked()
    assert dialog().get_heading() == 'Delete Selected Block?'
    click_response(dialog(), 'Delete')
    pump(lambda: not window._busy and window._view.document.text == original)
    window._directive_entries[0][1].set_text('2223')
    window.close()
    prompt = dialog()
    assert prompt.get_heading() == 'Discard Unsaved Changes?'
    click_response(prompt, 'Cancel')
    assert not window._closed
    assert window._dirty()
