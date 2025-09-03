# PyInstaller hook for GObject Introspection (gi)
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

# Collect all gi modules
hiddenimports = collect_submodules('gi')

# Add specific gi.repository modules
hiddenimports += [
    'gi.repository.Gtk',
    'gi.repository.Adw',
    'gi.repository.Gio',
    'gi.repository.GLib',
    'gi.repository.GObject',
    'gi.repository.Gdk',
    'gi.repository.Pango',
    'gi.repository.PangoFT2',
    'gi.repository.Vte',
]

# Collect gi data files
datas = collect_data_files('gi')
