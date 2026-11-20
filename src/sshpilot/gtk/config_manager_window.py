"""Presentation-only advanced SSH configuration manager.

All structure, edits, persistence and OpenSSH evaluation come from SshPilotClient.
Only form values and the unsaved document returned by the daemon live here.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from gettext import gettext as _

from gi.repository import Adw, GLib, Gtk, Pango

from sshpilot.api.capabilities import Capability
from sshpilot.api.models.connections import SaveSshConfigTextRequest, SshConfigManagerRequest
from sshpilot.api.models.operations import OperationState, OperationSummary
from sshpilot.api.transport.config_manager import config_preview_from_wire


_DIRECTIVES = ('User', 'HostName', 'Port', 'ProxyJump', 'IdentityFile', 'IdentitiesOnly',
               'ForwardAgent', 'ServerAliveInterval', 'ServerAliveCountMax', 'LocalForward',
               'RemoteForward', 'DynamicForward', 'ProxyCommand', 'ControlMaster',
               'ControlPath', 'ControlPersist', 'StrictHostKeyChecking', 'UserKnownHostsFile')


def _box(vertical=True, spacing=12):
    return Gtk.Box(orientation=Gtk.Orientation.VERTICAL if vertical else Gtk.Orientation.HORIZONTAL, spacing=spacing)


def _label(text, css=None):
    label = Gtk.Label(label=text, xalign=0, wrap=True)
    if css:
        label.add_css_class(css)
    return label


def _button(text, callback, icon=None):
    button = Gtk.Button()
    if icon:
        child = _box(False, 6)
        child.append(Gtk.Image.new_from_icon_name(icon))
        child.append(Gtk.Label(label=text))
        button.set_child(child)
    else:
        button.set_label(text)
    button.connect('clicked', lambda *_: callback())
    return button


def _clear(box):
    while box.get_first_child() is not None:
        box.remove(box.get_first_child())


def _scroll(child):
    scroll = Gtk.ScrolledWindow(hexpand=True, vexpand=True)
    scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
    scroll.set_child(child)
    return scroll


class ConfigManagerWindow(Adw.Window):
    def __init__(self, *, client, parent=None):
        super().__init__(title=_('SSH Pilot – SSH Configuration Manager'),
                         default_width=1300, default_height=980, transient_for=parent)
        self.client = client
        self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix='sshpilot-config-manager')
        self._view = None
        self._saved_text = ''
        self._closed = False
        self._busy = False
        self._operation = None
        self._cancel_pending = False
        self._poll_source = 0
        self._preview = None
        self._host = None
        self._header_entry = None
        self._directive_entries = []
        self._rendering = False
        self._save_after_preview = False
        self._saving = False
        self.add_css_class('config-manager')
        self._css = Gtk.CssProvider()
        self._css.load_from_data(b'''
            .config-manager entry, .config-manager combobox button { border: 1px solid alpha(currentColor,.2); background: alpha(@window_bg_color,.8); }
            .config-manager .manager-panel { border: 1px solid alpha(currentColor,.13); border-radius: 10px; }
            .config-manager .manager-heading { font-size: 18px; font-weight: 700; }
            .config-manager .manager-section { font-weight: 700; }
            .config-manager .manager-block:selected { background: alpha(@accent_bg_color,.2); }
            .config-manager .manager-block { padding: 12px 10px; border-radius: 8px; }
            .config-manager .manager-notice { padding: 14px; border-radius: 9px; background: alpha(@accent_bg_color,.08); }
            .config-manager .manager-success { padding: 14px; border-radius: 9px; background: alpha(@success_color,.12); }
            .config-manager .manager-preview { padding: 14px; font-family: monospace; background: alpha(currentColor,.035); }
            .config-manager .manager-table-row { padding: 8px; border-bottom: 1px solid alpha(currentColor,.1); }
        ''')
        Gtk.StyleContext.add_provider_for_display(self.get_display(), self._css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        layout = _box(spacing=0)
        header = Adw.HeaderBar()
        header.set_title_widget(Adw.WindowTitle(title=_('SSH Configuration Manager'), subtitle=_('SSH Pilot')))
        layout.append(header)
        self.content = _box(spacing=14)
        for edge in ('top', 'bottom', 'start', 'end'):
            getattr(self.content, 'set_margin_' + edge)(16)
        layout.append(self.content)
        self.content.set_vexpand(True)
        toolbar = _box(False, 10)
        self.file_label = Gtk.MenuButton(label=_('Loading configuration…'))
        self.file_label.add_css_class('title-3')
        file_popover = Gtk.Popover()
        file_box = _box(spacing=8)
        file_box.append(_label(_('Active SSH configuration')))
        file_box.append(_button(_('Reload from Disk'), lambda: (file_popover.popdown(), self._reload_clicked())))
        file_popover.set_child(file_box)
        self.file_label.set_popover(file_popover)
        toolbar.append(self.file_label)
        self.path_label = _label('', 'dim-label')
        self.path_label.set_hexpand(True)
        self.path_label.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        self.path_label.set_wrap(False)
        toolbar.append(self.path_label)
        toolbar.append(self._add_menu(_('Add')))
        self.up = _button(_('Move Up'), lambda: self._move(-1), 'go-up-symbolic')
        self.down = _button(_('Move Down'), lambda: self._move(1), 'go-down-symbolic')
        toolbar.append(self.up)
        toolbar.append(self.down)
        toolbar.append(_button(_('Validate'), self._preview_clicked, 'emblem-ok-symbolic'))
        self.save_button = _button(_('Save'), self._save_clicked, 'document-save-symbolic')
        self.save_button.add_css_class('suggested-action')
        toolbar.append(self.save_button)
        more = Gtk.MenuButton(icon_name='view-more-symbolic', tooltip_text=_('More options'))
        popover = Gtk.Popover()
        more_box = _box(spacing=6)
        more_box.append(_button(_('Reload from Disk'), lambda: (popover.popdown(), self._reload_clicked())))
        more_box.append(_button(_('Delete Selected Block'), lambda: (popover.popdown(), self._delete_clicked())))
        popover.set_child(more_box)
        more.set_popover(popover)
        toolbar.append(more)
        self.content.append(toolbar)
        columns = _box(False, 16)
        columns.set_vexpand(True)
        self.content.append(columns)
        left = _box(spacing=12)
        left.set_size_request(290, -1)
        left.set_hexpand(False)
        columns.append(left)
        left_panel = _box(spacing=8)
        left_panel.add_css_class('manager-panel')
        title = _label(_('Configuration (evaluation order)'), 'manager-section')
        title.set_margin_top(14)
        title.set_margin_start(12)
        title.set_margin_bottom(6)
        left_panel.append(title)
        left_panel.append(Gtk.Separator())
        self.block_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.block_list.add_css_class('navigation-sidebar')
        self.block_list.connect('row-selected', self._row_selected)
        left_panel.append(_scroll(self.block_list))
        left.append(left_panel)
        left.append(self._add_menu(_('Add Block')))
        notice = _label(_('The order of blocks matters. OpenSSH uses the first obtained value for most directives.'), 'manager-notice')
        notice.set_max_width_chars(32)
        left.append(notice)
        columns.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))
        self.editor = _box(spacing=18)
        self.editor.set_size_request(365, -1)
        editor_scroll = _scroll(self.editor)
        columns.append(editor_scroll)
        columns.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))
        right = _box(spacing=14)
        right.set_size_request(410, -1)
        right.set_hexpand(True)
        columns.append(right)
        self.stack = Gtk.Stack(vexpand=True)
        tabs = Gtk.StackSwitcher(stack=self.stack, halign=Gtk.Align.FILL)
        right.append(tabs)
        right.append(Gtk.Separator())
        self.impact = _box(spacing=16)
        self.effective = _box(spacing=12)
        self.matches = _box(spacing=12)
        self.stack.add_titled(_scroll(self.impact), 'impact', _('Impact'))
        self.stack.add_titled(_scroll(self.effective), 'effective', _('Effective Config'))
        self.stack.add_titled(_scroll(self.matches), 'matches', _('Matches'))
        right.append(self.stack)
        right.append(Gtk.Separator())
        right.append(_label(_('Change preview'), 'manager-section'))
        change = _box(False, 10)
        change.add_css_class('manager-notice')
        change.append(_label(_('See the effect of edits before saving.')))
        change.append(_button(_('Preview Changes…'), self._preview_clicked, 'media-playback-start-symbolic'))
        right.append(change)
        self.status_row = _box(False, 12)
        self.status_row.set_margin_start(16)
        self.status_row.set_margin_end(16)
        self.status_row.set_margin_bottom(12)
        self.status = _label('')
        self.status.set_hexpand(True)
        self.status.set_selectable(True)
        self.status_row.append(self.status)
        self.cancel_button = _button(_('Cancel'), self._cancel_preview)
        self.cancel_button.set_visible(False)
        self.status_row.append(self.cancel_button)
        layout.append(self.status_row)
        self.set_content(layout)
        self.connect('close-request', self._close_requested)
        self._load()

    def _add_menu(self, title):
        menu = Gtk.MenuButton(label=title)
        popover = Gtk.Popover()
        box = _box(spacing=6)
        for kind, label in [('Host', _('Host / Pattern Rule')), ('Match', _('Conditional Rule (Match)')), ('Include', _('Include Files'))]:
            box.append(_button(label, lambda k=kind: (popover.popdown(), self._add_clicked(k))))
        popover.set_child(box)
        menu.set_popover(popover)
        return menu

    def _set_busy(self, busy, message=''):
        self._busy = busy
        self.content.set_sensitive(not busy)
        if message:
            self.status.set_text(message)

    def _submit(self, function, callback):
        future = self._executor.submit(function)
        def done(f):
            try:
                value, error = f.result(), None
            except Exception as exc:
                value, error = None, exc
            if self._closed:
                if isinstance(value, OperationSummary):
                    self.client.cancel_operation(value.operation_id)
                return
            GLib.idle_add(self._deliver, callback, value, error)
        future.add_done_callback(done)

    def _deliver(self, callback, value, error):
        if not self._closed:
            if error:
                self._set_busy(False, str(error))
                self._save_after_preview = False
                self._saving = False
                if self._view is None:
                    self.content.set_sensitive(False)
                    self.file_label.set_label(_("Configuration unavailable"))
                self.cancel_button.set_visible(False)
            else:
                callback(value)
        return GLib.SOURCE_REMOVE

    def _load(self):
        self._set_busy(True, _('Loading configuration…'))
        def loaded(view):
            self._saved_text = view.document.text
            self._accept(view)
            self._begin_preview()
        def load():
            if not self.client.get_capabilities().supports(Capability.CONNECTIONS_CONFIG_MANAGER):
                raise RuntimeError(_('The background service does not support Advanced Configuration Manager.'))
            return self.client.edit_ssh_config(SshConfigManagerRequest())
        self._submit(load, loaded)

    def _accept(self, view):
        self._view = view
        self._header_entry = None
        self._directive_entries = []
        self._preview = None
        self._set_busy(False, _('Unsaved changes') if self._dirty() else _('Configuration loaded'))
        self._render()

    def _pending(self):
        if not self._view or not self._view.blocks:
            return []
        block = self._view.blocks[self._view.selected]
        changes = []
        if self._header_entry and self._header_entry.get_text() != block.argument:
            changes.append(dict(action='header', value=self._header_entry.get_text()))
        for i, (key, value) in enumerate(self._directive_entries):
            original = block.directives[i]
            if key.get_text().lower() != original.keyword.lower() or value.get_text() != original.value:
                changes.append(dict(action='directive', directive=i, keyword=key.get_text(), value=value.get_text()))
        return changes

    def _dirty(self):
        return self._view is not None and (self._view.document.text != self._saved_text or bool(self._pending()))

    def _sync(self, action=None, then=None):
        if self._busy or not self._view:
            return
        requests = self._pending()
        if action:
            requests.append(action)
        view = self._view
        if not requests:
            if then:
                then()
            return
        self._set_busy(True, _('Updating draft…'))
        def work():
            current = view
            for fields in requests:
                current = self.client.edit_ssh_config(SshConfigManagerRequest(
                    text=current.document.text, expected_revision=current.document.revision,
                    block=current.selected, **fields))
            return current
        def complete(current):
            self._accept(current)
            if then:
                then()
        self._submit(work, complete)

    def _render(self):
        self._rendering = True
        self._header_entry = None
        self._directive_entries = []
        _clear(self.block_list)
        self.file_label.set_label(self._view.document.display_name)
        self.path_label.set_text(self._view.document.display_name)
        for i, block in enumerate(self._view.blocks):
            row = Gtk.ListBoxRow()
            row.add_css_class('manager-block')
            body = _box(False, 12)
            body.append(_label(str(i+1), 'dim-label'))
            body.append(Gtk.Image.new_from_icon_name('folder-symbolic' if block.kind == 'include' else 'network-server-symbolic'))
            labels = _box(spacing=3)
            heading = _label(self._block_title(block), 'manager-section')
            heading.set_ellipsize(Pango.EllipsizeMode.END)
            heading.set_max_width_chars(26)
            heading.set_wrap(False)
            labels.append(heading)
            labels.append(_label(self._kind_label(block), 'dim-label'))
            if block.included_files:
                expander = Gtk.Expander(label=_('%d files') % len(block.included_files), expanded=True)
                files = _box(spacing=6)
                for path in block.included_files:
                    file_label = _label('/'.join(path.replace('\\', '/').split('/')[-2:]))
                    file_label.set_max_width_chars(26)
                    file_label.set_tooltip_text(path)
                    files.append(file_label)
                expander.set_child(files)
                labels.append(expander)
            body.append(labels)
            row.set_child(body)
            self.block_list.append(row)
        row = self.block_list.get_row_at_index(self._view.selected)
        if row:
            self.block_list.select_row(row)
        _clear(self.editor)
        count = len(self._view.blocks)
        self.up.set_sensitive(self._view.selected > 0 and self._view.document.writable)
        self.down.set_sensitive(self._view.selected < count-1 and self._view.document.writable)
        self.save_button.set_sensitive(self._view.document.writable)
        if count:
            self._render_editor(self._view.blocks[self._view.selected])
        else:
            self.editor.append(_label(_('No blocks yet. Add a Host, Match or Include block.')))
        self.editor.set_sensitive(self._view.document.writable)
        self._render_impact()
        self._rendering = False

    @staticmethod
    def _block_title(block):
        return _('Global directives') if block.kind == 'global' else block.kind.title() + ' ' + block.argument

    @staticmethod
    def _kind_label(block):
        if block.kind == 'include':
            return _('Include')
        if block.kind == 'match':
            return _('Conditional rule')
        if block.kind == 'global' or block.argument == '*':
            return _('Global rule')
        if any(c in block.argument for c in '*?!'):
            return _('Pattern rule')
        return _('Connection')

    def _render_editor(self, block):
        heading = _box(False)
        title = _label(self._block_title(block), 'manager-heading')
        title.set_hexpand(True)
        heading.append(title)
        badge = _label(self._kind_label(block), 'dim-label')
        heading.append(badge)
        heading.append(_label('#%d' % (self._view.selected+1), 'dim-label'))
        self.editor.append(heading)
        self.editor.append(Gtk.Separator())
        if block.kind != 'global':
            self.editor.append(_label({'host': _('Host patterns'), 'match': _('Match conditions'), 'include': _('Include patterns')}[block.kind], 'manager-section'))
            self._header_entry = Gtk.Entry(text=block.argument, hexpand=True)
            self._header_entry.connect('changed', self._form_changed)
            self._header_entry.connect('activate', lambda *_: self._sync(then=self._begin_preview))
            self.editor.append(self._header_entry)
            if block.kind == 'host':
                self.editor.append(_label(_('Space-separated patterns, e.g. prod-*  *.example.com  !prod-test'), 'dim-label'))
            self.editor.append(Gtk.Separator())
        self.editor.append(_label(_('Directives'), 'manager-section'))
        directives = _box(spacing=8)
        for i, directive in enumerate(block.directives):
            row = _box(False, 8)
            combo = Gtk.ComboBoxText.new_with_entry()
            for name in _DIRECTIVES:
                combo.append_text(name)
            key = combo.get_child()
            key.set_width_chars(12)
            key.set_text(next((n for n in _DIRECTIVES if n.lower() == directive.keyword), directive.keyword))
            value = Gtk.Entry(text=directive.value, hexpand=True)
            value.set_width_chars(15)
            for entry in (key, value):
                entry.connect('changed', self._form_changed)
                entry.connect('activate', lambda *_: self._sync(then=self._begin_preview))
            row.append(combo)
            row.append(value)
            remove = Gtk.Button(icon_name='user-trash-symbolic', tooltip_text=_('Remove Directive'))
            remove.connect('clicked', lambda _b, index=i: self._sync(dict(action='remove_directive', directive=index), self._begin_preview))
            row.append(remove)
            self._directive_entries.append((key, value))
            directives.append(row)
        directives.append(_button(_('Add Directive'), self._add_directive, 'list-add-symbolic'))
        self.editor.append(directives)
        self.editor.append(Gtk.Separator())
        self.editor.append(_label(_('Block position'), 'manager-section'))
        self.editor.append(_label(_('Position in file: %d') % (self._view.selected+1)))
        moves = _box(False, 6)
        moves.append(_button(_('Move Up'), lambda: self._move(-1), 'go-up-symbolic'))
        moves.append(_button(_('Move Down'), lambda: self._move(1), 'go-down-symbolic'))
        move_to = Gtk.DropDown.new_from_strings([_('Move To…')] + [str(i+1) for i in range(len(self._view.blocks))])
        move_to.connect('notify::selected', lambda widget, *_: self._sync(dict(action='move', position=widget.get_selected()-1), self._begin_preview) if widget.get_selected() else None)
        moves.append(move_to)
        self.editor.append(moves)
        self.editor.append(Gtk.Separator())
        preview_title = _box(False)
        preview_label = _label(_('SSH config preview (this block)'), 'manager-section')
        preview_label.set_hexpand(True)
        preview_title.append(preview_label)
        preview_title.append(_button(_('Copy'), self._copy_block, 'edit-copy-symbolic'))
        self.editor.append(preview_title)
        from sshpilot.text_editor import GtkSource, _get_language_manager
        if GtkSource is not None:
            buffer = GtkSource.Buffer()
            language_manager = _get_language_manager()
            language = language_manager.get_language('sshconfig')
            if language is not None:
                buffer.set_language(language)
            code = GtkSource.View(buffer=buffer, show_line_numbers=True,
                                  editable=False, cursor_visible=False, monospace=True,
                                  wrap_mode=Gtk.WrapMode.WORD_CHAR)
        else:
            code = Gtk.TextView(editable=False, cursor_visible=False, monospace=True,
                                wrap_mode=Gtk.WrapMode.WORD_CHAR)
        code.add_css_class('manager-preview')
        code.get_buffer().set_text(block.text)
        self.editor.append(code)

    def _form_changed(self, *_args):
        if self._rendering or self._busy:
            return
        self._preview = None
        self._render_impact()
        self.status.set_text(_('Unsaved changes — preview to update effective values'))

    def _copy_block(self):
        self._sync(then=lambda: self.get_clipboard().set(self._view.blocks[self._view.selected].text))

    def _row_selected(self, _list, row):
        if self._rendering or row is None or self._busy or not self._view:
            return
        index = row.get_index()
        if index != self._view.selected:
            # Selection changes must commit the previous form first.
            self._sync(dict(action='inspect', position=index), lambda: self._select(index))

    def _select(self, index):
        from dataclasses import replace
        self._view = replace(self._view, selected=index)
        self._render()
        self._begin_preview()

    def _move(self, delta):
        if self._view and 0 <= self._view.selected+delta < len(self._view.blocks):
            self._sync(dict(action='move', position=self._view.selected+delta), self._begin_preview)

    def _prompt(self, title, fields, callback):
        dialog = Adw.MessageDialog(transient_for=self, modal=True, heading=title)
        box = _box()
        entries = []
        for label, value in fields:
            box.append(_label(label))
            entry = Gtk.Entry(text=value, activates_default=True)
            box.append(entry)
            entries.append(entry)
        dialog.set_extra_child(box)
        dialog.add_response('cancel', _('Cancel'))
        dialog.add_response('add', _('Add'))
        dialog.set_response_appearance('add', Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response('add')
        dialog.set_close_response('cancel')
        dialog.connect('response', lambda _d, response: callback(*(e.get_text() for e in entries)) if response == 'add' else None)
        dialog.present()

    def _add_clicked(self, kind):
        self._prompt(_('Add Block'), [(kind, '*' if kind == 'Host' else 'all' if kind == 'Match' else 'config.d/*.conf')],
                     lambda value: self._sync(dict(action='add', keyword=kind, value=value, position=len(self._view.blocks)), self._begin_preview))

    def _add_directive(self):
        self._prompt(_('Add Directive'), [(_('Directive'), 'User'), (_('Value'), '')],
                     lambda key, value: self._sync(dict(action='directive', keyword=key, value=value), self._begin_preview))

    def _delete_clicked(self):
        if self._view and self._view.blocks:
            self._confirm(_('Delete Selected Block?'), _('The block will be removed from the draft. Save to apply this change.'),
                          _('Delete'), lambda: self._sync(dict(action='remove'), self._begin_preview))

    def _preview_clicked(self):
        self._sync(then=self._begin_preview)

    def _save_clicked(self):
        self._sync(then=lambda: self._begin_preview(save_after=True))

    def _begin_preview(self, save_after=False):
        if self._busy or not self._view:
            return
        self._save_after_preview = save_after
        self._cancel_pending = False
        self._set_busy(True, _('Resolving with OpenSSH…'))
        self.cancel_button.set_visible(True)
        request = SshConfigManagerRequest(self._view.document.text, self._view.document.revision, block=self._view.selected)
        def started(operation):
            self._operation = operation.operation_id
            if self._cancel_pending:
                self._cancel_preview()
            self._poll(operation)
        self._submit(lambda: self.client.start_ssh_config_preview(request), started)

    def _poll(self, operation):
        if operation.state in {OperationState.QUEUED, OperationState.RUNNING}:
            self.status.set_text(_('Resolving with OpenSSH… %d%%') % int((operation.progress or 0)*100))
            self._poll_source = GLib.timeout_add(150, self._poll_again)
            return
        self._operation = None
        self.cancel_button.set_visible(False)
        self._set_busy(False)
        if operation.state is not OperationState.SUCCEEDED:
            self.status.set_text(_('Preview cancelled') if operation.state is OperationState.CANCELLED else
                                 (operation.failure.message if operation.failure else _('Preview failed')))
            self._save_after_preview = False
            return
        try:
            self._preview = config_preview_from_wire(operation.result)
        except (ValueError, TypeError):
            self.status.set_text(_('The background service returned an invalid preview.'))
            self._save_after_preview = False
            return
        self._render_impact()
        self.status.set_text(_('OpenSSH validation passed') if self._preview.valid else '\n'.join(self._preview.diagnostics))
        if self._save_after_preview and self._preview.valid:
            self._save_after_preview = False
            self._save()

    def _poll_again(self):
        self._poll_source = 0
        if self._operation and not self._closed:
            operation_id = self._operation
            self._submit(lambda: self.client.get_operation(operation_id), self._poll)
        return GLib.SOURCE_REMOVE

    def _cancel_preview(self):
        self._cancel_pending = True
        if self._operation:
            operation_id = self._operation
            self.cancel_button.set_sensitive(False)
            self._submit(lambda: self.client.cancel_operation(operation_id), lambda _result: self.cancel_button.set_sensitive(True))

    def _save(self):
        self._saving = True
        self._set_busy(True, _('Saving configuration…'))
        request = SaveSshConfigTextRequest(self._view.document.text, self._view.document.revision)
        def saved(document):
            self._saving = False
            from dataclasses import replace
            self._saved_text = document.text
            self._view = replace(self._view, document=document)
            self._set_busy(False, _('Configuration saved'))
        self._submit(lambda: self.client.save_ssh_config_text(request), saved)

    def _render_impact(self):
        for box in (self.impact, self.effective, self.matches):
            _clear(box)
        if not self._preview:
            self.impact.append(_label(_('Preview this block to see its effect on saved connections.'), 'manager-notice'))
            return
        if not self._preview.valid:
            self.impact.append(_label('\n'.join(self._preview.diagnostics), 'error'))
            return
        hosts = self._preview.hosts
        matched = [h for h in hosts if h.matched]
        banner = _box(spacing=4)
        banner.add_css_class('manager-success')
        banner.append(_label(_('Matches %d of %d saved connections') % (len(matched), len(hosts)), 'manager-section'))
        banner.append(_label(_('Effective values resolved by OpenSSH.')))
        self.impact.append(banner)
        for title, subset in [(_('Matching connections'), matched), (_('Other saved connections'), [h for h in hosts if not h.matched])]:
            self.impact.append(_label(title, 'manager-section'))
            group = _box(spacing=0)
            group.add_css_class('manager-panel')
            for host in subset:
                group.append(self._host_row(host))
            if not subset:
                group.append(_label(_('None'), 'dim-label'))
            self.impact.append(group)
        self.impact.append(Gtk.Separator())
        chosen = next((h for h in hosts if h.host == self._host), next(iter(matched or list(hosts)), None))
        if chosen:
            self._host = chosen.host
            self.impact.append(_label(_('Directive contributions (for %s)') % chosen.host, 'manager-section'))
            table = _box(spacing=0)
            table.add_css_class('manager-panel')
            self._table_row(table, _('Directive'), _('Effective value'), _('From'), heading=True)
            relevant = set(chosen.contributions) | {'user', 'hostname', 'port', 'proxyjump', 'identityfile'}
            for line in chosen.after:
                key, separator, value = line.partition(' ')
                if key in relevant:
                    origin = (self._block_title(self._view.blocks[self._view.selected]) + ' (#%d)' % (self._view.selected+1)) if key in chosen.contributions else _('OpenSSH')
                    self._table_row(table, key, value, origin)
            self.impact.append(table)
            table.set_tooltip_text(_('OpenSSH also supplies inherited and default values. Sources are shown for confirmed contributions.'))
            self.effective.append(_label(_('Effective configuration for %s') % chosen.host, 'manager-section'))
            code = Gtk.TextView(editable=False, monospace=True, wrap_mode=Gtk.WrapMode.WORD_CHAR)
            code.get_buffer().set_text('\n'.join(chosen.after))
            self.effective.append(code)
            self.effective.append(_label(_('Changes from saved configuration'), 'manager-section'))
            before, after = set(chosen.before), set(chosen.after)
            changes = ['− '+line for line in chosen.before if line not in after] + ['+ '+line for line in chosen.after if line not in before]
            self.effective.append(_label('\n'.join(changes) if changes else _('No effective changes.')))
        for host in hosts:
            self.matches.append(self._host_row(host))

    def _host_row(self, host):
        button = Gtk.Button()
        button.add_css_class('flat')
        row = _box(False, 10)
        row.append(Gtk.Image.new_from_icon_name('network-server-symbolic'))
        labels = _box(spacing=2)
        labels.set_hexpand(True)
        labels.append(_label(host.host, 'manager-section'))
        labels.append(_label(host.hostname, 'dim-label'))
        row.append(labels)
        row.append(_label(_('Effective') if host.contributions else _('Matched') if host.matched else _('Not matched'), 'success' if host.matched else 'dim-label'))
        row.append(Gtk.Image.new_from_icon_name('go-next-symbolic'))
        button.set_child(row)
        def choose(*_args):
            self._host = host.host
            self._render_impact()
            self.stack.set_visible_child_name('effective')
        button.connect('clicked', choose)
        return button

    @staticmethod
    def _table_row(table, key, value, source, heading=False):
        row = _box(False, 8)
        row.add_css_class('manager-table-row')
        for text, width in ((key, 12), (value, 18), (source, 12)):
            label = _label(text, 'manager-section' if heading else None)
            label.set_hexpand(True)
            label.set_max_width_chars(width)
            label.set_width_chars(width)
            label.set_selectable(True)
            row.append(label)
        table.append(row)

    def _confirm(self, heading, body, accept, callback):
        dialog = Adw.MessageDialog(transient_for=self, modal=True, heading=heading, body=body)
        dialog.add_response('cancel', _('Cancel'))
        dialog.add_response('accept', accept)
        dialog.set_response_appearance('accept', Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response('cancel')
        dialog.set_close_response('cancel')
        dialog.connect('response', lambda _d, response: callback() if response == 'accept' else None)
        dialog.present()

    def _reload_clicked(self):
        if self._dirty():
            self._confirm(_('Discard Unsaved Changes?'), _('Reload the configuration from disk.'), _('Discard'), self._load)
        else:
            self._load()

    def _close_requested(self, *_args):
        if self._closed:
            return False
        if self._saving:
            self.status.set_text(_('Saving configuration…'))
            return True
        if self._dirty():
            self._confirm(_('Discard Unsaved Changes?'), _('Your configuration changes have not been saved.'), _('Discard'), self._finish_close)
            return True
        self._finish_close()
        return True

    def _finish_close(self):
        self._closed = True
        if self._poll_source:
            GLib.source_remove(self._poll_source)
            self._poll_source = 0
        if self._operation:
            self._executor.submit(self.client.cancel_operation, self._operation)
        self._executor.shutdown(wait=False, cancel_futures=False)
        Gtk.StyleContext.remove_provider_for_display(self.get_display(), self._css)
        self.destroy()
