"""Unit tests for icon_utils caching and bundled-icon resolution."""

from sshpilot import icon_utils


class _RecordingFileIcon:
    """Stand-in for Gio.FileIcon that records the resource file it was built from."""

    def __init__(self, file_obj):
        self.file_obj = file_obj

    @classmethod
    def new(cls, file_obj):
        return cls(file_obj)


class _RecordingThemedIcon:
    """Stand-in for Gio.ThemedIcon that records the name it was built from."""

    def __init__(self, name):
        self.name = name

    @classmethod
    def new(cls, name):
        return cls(name)


def _patch_gio(monkeypatch):
    monkeypatch.setattr(icon_utils.Gio, 'FileIcon', _RecordingFileIcon)
    monkeypatch.setattr(icon_utils.Gio, 'ThemedIcon', _RecordingThemedIcon)
    monkeypatch.setattr(
        icon_utils.Gio, 'File',
        type('_File', (), {'new_for_uri': staticmethod(lambda uri: uri)}),
    )


def test_mapped_name_resolves_to_its_bundled_resource_file(monkeypatch):
    icon_utils._gicon_cache.clear()
    _patch_gio(monkeypatch)
    path = icon_utils._ICON_RESOURCE_MAP['folder-symbolic']
    gicon = icon_utils.get_gicon_for_icon_name('folder-symbolic')
    assert isinstance(gicon, _RecordingFileIcon)
    assert gicon.file_obj == f'resource://{path}'


def test_alias_entries_resolve_to_their_mapped_resource_not_their_own_name(monkeypatch):
    # 'network-receive-symbolic' has no SVG of its own; it must load the
    # bundled 'network-transmit-receive-symbolic' resource directly rather
    # than going through Gio.ThemedIcon (which could resolve to an unrelated
    # system icon of that name and silently shadow the bundled artwork).
    icon_utils._gicon_cache.clear()
    _patch_gio(monkeypatch)
    path = icon_utils._ICON_RESOURCE_MAP['network-receive-symbolic']
    gicon = icon_utils.get_gicon_for_icon_name('network-receive-symbolic')
    assert isinstance(gicon, _RecordingFileIcon)
    assert gicon.file_obj == f'resource://{path}'


def test_bundled_name_never_falls_back_to_themed_icon_lookup(monkeypatch):
    # Regression test: every name in _ICON_RESOURCE_MAP must bypass
    # Gio.ThemedIcon (system icon-theme name lookup), because GTK checks the
    # user's configured icon theme before any app-registered resource path,
    # so a same-named system icon would silently shadow our bundled artwork.
    icon_utils._gicon_cache.clear()
    _patch_gio(monkeypatch)
    for name in icon_utils._ICON_RESOURCE_MAP:
        gicon = icon_utils.get_gicon_for_icon_name(name)
        assert isinstance(gicon, _RecordingFileIcon), name


def test_unmapped_name_falls_back_to_themed_icon(monkeypatch):
    icon_utils._gicon_cache.clear()
    _patch_gio(monkeypatch)
    gicon = icon_utils.get_gicon_for_icon_name('some-unbundled-system-icon-symbolic')
    assert isinstance(gicon, _RecordingThemedIcon)
    assert gicon.name == 'some-unbundled-system-icon-symbolic'


def test_get_gicon_for_icon_name_caches_by_name():
    icon_utils._gicon_cache.clear()
    sentinel = object()
    icon_utils._gicon_cache['folder-symbolic'] = sentinel  # type: ignore[assignment]
    assert icon_utils.get_gicon_for_icon_name('folder-symbolic') is sentinel
