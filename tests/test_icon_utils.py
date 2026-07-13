"""Unit tests for icon_utils caching and alias resolution."""

from sshpilot import icon_utils


def test_is_direct_theme_lookup_matches_filename():
    path = '/io/github/mfat/sshpilot/icons/scalable/actions/folder-symbolic.svg'
    assert icon_utils._is_direct_theme_lookup('folder-symbolic', path)


def test_is_direct_theme_lookup_rejects_alias():
    path = '/io/github/mfat/sshpilot/icons/scalable/actions/network-transmit-receive-symbolic.svg'
    assert not icon_utils._is_direct_theme_lookup('network-receive-symbolic', path)


def test_get_gicon_for_icon_name_caches_by_name():
    icon_utils._gicon_cache.clear()
    sentinel = object()
    icon_utils._gicon_cache['folder-symbolic'] = sentinel  # type: ignore[assignment]
    assert icon_utils.get_gicon_for_icon_name('folder-symbolic') is sentinel


def test_alias_entries_use_file_icon_path():
    path = icon_utils._ICON_RESOURCE_MAP['network-receive-symbolic']
    assert not icon_utils._is_direct_theme_lookup('network-receive-symbolic', path)
