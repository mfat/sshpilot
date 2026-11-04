/* SPDX-License-Identifier: GPL-3.0-or-later */
/* frdp-keyboard-layout.h — detect the local keyboard layout for RDP
 *
 * Copyright (C) 2026 mFat
 */
#pragma once

#include <glib.h>

G_BEGIN_DECLS

/* RDP keyboard layout ID (a Windows KLID such as 0x00000409) for an XKB
 * layout and optional variant, or 0 if unknown. Also accepts GNOME's
 * "layout+variant" form in @layout when @variant is NULL. */
guint32      frdp_keyboard_layout_from_xkb          (const char *layout,
                                                     const char *variant);

/* XKB layout/variant for a human-readable layout name as shown by desktops
 * and reported by GTK ("German", "English (US, Dvorak)"), using the
 * system's xkeyboard-config catalogue. Returns FALSE if not found. */
gboolean     frdp_keyboard_layout_xkb_from_description (const char  *description,
                                                        char       **layout,
                                                        char       **variant);

/* Detects the layout to announce to the server; never returns 0.
 * Must be called on the GTK main thread. If @source is not NULL it is set
 * to a static string naming where the result came from. */
guint32      frdp_keyboard_layout_detect            (const char **source);

G_END_DECLS
