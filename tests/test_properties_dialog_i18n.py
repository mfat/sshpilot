"""Localized FileManager properties text."""

from sshpilot.file_manager import format_utils
from sshpilot.file_manager import properties_dialog as properties


def test_folder_states_translate_before_formatting(monkeypatch):
    translations = {
        "Folder": "Dossier",
        "{folder} (calculating size...)": "calcul de {folder}",
        "{items} ({size})": "{items}, taille {size}",
        "{items} (size unavailable)": "taille inconnue pour {items}",
        "Size unavailable": "Taille indisponible",
        "{size} Free": "{size} libres",
    }
    monkeypatch.setattr(properties, "_", translations.__getitem__)
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
    assert properties._folder_calculating_text(3) == "calcul de 3 éléments"
    assert properties._folder_size_text(3, 2048) == "3 éléments, taille 2.0 KB"
    assert properties._folder_size_text(3, -1) == "taille inconnue pour 3 éléments"
    assert properties._folder_size_text(None, -1) == "Taille indisponible"
    assert properties._free_space_text(2048) == "2.0 KB libres"
