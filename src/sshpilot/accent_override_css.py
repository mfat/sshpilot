"""Shared CSS for accent-color overrides and navigation-sidebar selection.

``main`` and ``preferences`` both install an accent override provider at
``STYLE_PROVIDER_PRIORITY_APPLICATION``. At equal priority the provider added
later wins for every property it sets, regardless of selector specificity, so
the override must carry the full sidebar selection ladder (selected / hover /
active) rather than only the idle selected state. Keeping the rule text here
stops the two call sites from drifting apart.
"""

from __future__ import annotations

# Neutral .navigation-sidebar selection (Adwaita model). Stronger than
# libadwaita's default ~0.10 shade so selection still reads on dark cards.
# Include list- and row-scoped class forms; either may carry the class.
NAVIGATION_SIDEBAR_SELECTION_CSS = """
/* :focus belongs at rest: a clicked row is selected *and* focused, so
   grouping it with :hover would leave hover with no visible step. */
.navigation-sidebar row:selected,
.navigation-sidebar row:selected:focus,
row.navigation-sidebar:selected,
row.navigation-sidebar:selected:focus {
  background-color: alpha(@window_fg_color, 0.18);
  color: @window_fg_color;
}

.navigation-sidebar row:selected:hover,
row.navigation-sidebar:selected:hover {
  background-color: alpha(@window_fg_color, 0.22);
  color: @window_fg_color;
}

.navigation-sidebar row:selected:active,
row.navigation-sidebar:selected:active {
  background-color: alpha(@window_fg_color, 0.26);
  color: @window_fg_color;
}

/* Section headers stay chrome, not selected pills, even when an accent
   override provider is installed after the sidebar stylesheet. */
.navigation-sidebar row.sidebar-section-header:hover,
.navigation-sidebar row.sidebar-section-header:selected,
.navigation-sidebar row.sidebar-section-header:selected:hover,
row.navigation-sidebar.sidebar-section-header:hover,
row.navigation-sidebar.sidebar-section-header:selected,
row.navigation-sidebar.sidebar-section-header:selected:hover {
  background: transparent;
  box-shadow: none;
  color: inherit;
}
"""


def build_accent_override_css(accent_color: str) -> str:
    """Return CSS that remaps accent/selection named colours to ``accent_color``.

    Non-sidebar ``row:selected`` uses the custom accent. Navigation sidebars
    keep the shared neutral selection ladder so standalone semantic status
    colours (``.success`` / ``.error``) stay readable.
    """
    color = str(accent_color).strip()
    return "\n".join(
        [
            f"@define-color accent_color {color};",
            f"@define-color accent_bg_color {color};",
            "@define-color accent_fg_color white;",
            f"@define-color theme_selected_bg_color {color};",
            "@define-color theme_selected_fg_color white;",
            f"@define-color theme_unfocused_selected_bg_color {color};",
            "@define-color theme_unfocused_selected_fg_color white;",
            "",
            "/* Force row selection to use custom colors */",
            "row:selected {",
            "  background-color: @theme_selected_bg_color;",
            "  color: @theme_selected_fg_color;",
            "}",
            "",
            "row:selected:focus {",
            "  background-color: @theme_selected_bg_color;",
            "  color: @theme_selected_fg_color;",
            "}",
            "",
            "list row:selected {",
            "  background-color: @theme_selected_bg_color;",
            "  color: @theme_selected_fg_color;",
            "}",
            "",
            NAVIGATION_SIDEBAR_SELECTION_CSS.strip(),
            "",
        ]
    )
