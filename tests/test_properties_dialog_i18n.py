"""Localized FileManager properties text."""

from sshpilot.file_manager import format_utils
from sshpilot.file_manager import properties_dialog as properties


def test_folder_states_translate_before_formatting(monkeypatch):
    translations = {
        "Folder": "Dossier",
        "Empty folder": "Dossier vide",
        "{items} (size unavailable)": "taille inconnue pour {items}",
        "Size unavailable": "Taille indisponible",
        "{size} Free": "{size} libres",
        "{count} item, with size {size}": "{count} élément, taille {size}",
        "{count} items, totalling {size}": "{count} éléments, total {size}",
    }
    monkeypatch.setattr(properties, "_", translations.__getitem__)
    monkeypatch.setattr(
        properties,
        "ngettext",
        lambda singular, plural, count: translations[singular if count == 1 else plural],
    )
    monkeypatch.setattr(
        format_utils,
        "ngettext",
        lambda singular, plural, count: (
            "{count} élément" if count == 1 else "{count} éléments"
        ),
    )

    assert properties._folder_label(None) == "Dossier"
    assert properties._folder_label(0) == "0 éléments"
    assert properties._folder_label(1) == "1 élément"
    assert properties._folder_label(3) == "3 éléments"
    assert properties._folder_calculating_text(None) == "…"
    assert properties._folder_calculating_text(3) == "3 éléments"
    assert properties._folder_size_text(3, 2048) == "3 éléments, total 2.0 KB"
    assert properties._folder_size_text(0, 0) == "Dossier vide"
    assert properties._folder_size_text(3, -1) == "taille inconnue pour 3 éléments"
    assert properties._folder_size_text(None, -1) == "Taille indisponible"
    assert properties._free_space_text(2048) == "2.0 KB libres"


def test_selection_title_translates_when_count_exceeds_max(monkeypatch):
    from types import SimpleNamespace

    translations = {
        "{count} Selected Item": "{count} élément sélectionné",
        "{count} Selected Items": "{count} éléments sélectionnés",
    }
    monkeypatch.setattr(
        properties,
        "ngettext",
        lambda singular, plural, count: translations[singular if count == 1 else plural],
    )
    entries_few = [SimpleNamespace(name=f"file{i}.txt") for i in range(3)]
    assert properties._selection_title(entries_few) == "file0.txt, file1.txt, file2.txt"

    entries_many = [SimpleNamespace(name=f"file{i}.txt") for i in range(6)]
    assert properties._selection_title(entries_many) == "6 éléments sélectionnés"

