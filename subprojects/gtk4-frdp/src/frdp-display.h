/* SPDX-License-Identifier: GPL-3.0-or-later */
/* frdp-display.h
 *
 * Copyright (C) 2018 Felipe Borges <felipeborges@gnome.org>
 * Copyright (C) 2026 mFat
 *
 * GTK4 port for the sshPilot embedded-RDP spike (2026).
 *
 * This program is free software: you can redistribute it and/or modify
 * it under the terms of the GNU General Public License as published by
 * the Free Software Foundation, either version 3 of the License, or
 * (at your option) any later version.
 *
 * This program is distributed in the hope that it will be useful,
 * but WITHOUT ANY WARRANTY; without even the implied warranty of
 * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
 * GNU General Public License for more details.
 *
 * You should have received a copy of the GNU General Public License
 * along with this program.  If not, see <http://www.gnu.org/licenses/>.
 */

#pragma once

#include <gtk/gtk.h>

G_BEGIN_DECLS

/* Windows DLLs only export what is marked: MinGW exports everything by
 * default, MSVC and clang-cl export nothing. Elsewhere the library's
 * default visibility applies. */
#if defined(_WIN32) || defined(__CYGWIN__)
# ifdef GTK4_FRDP_COMPILATION
#  define GTK4_FRDP_EXPORT __declspec(dllexport) extern
# else
#  define GTK4_FRDP_EXPORT __declspec(dllimport) extern
# endif
#else
# define GTK4_FRDP_EXPORT extern
#endif

#define FRDP_TYPE_DISPLAY (frdp_display_get_type())

GTK4_FRDP_EXPORT
G_DECLARE_DERIVABLE_TYPE (FrdpDisplay, frdp_display, FRDP, DISPLAY, GtkWidget)

typedef struct _FrdpDisplayPrivate FrdpDisplayPrivate;

struct _FrdpDisplayClass
{
  GtkWidgetClass parent_class;

  gboolean (*authenticate) (FrdpDisplay *self, gchar **username, gchar **password, gchar **domain);
};

GTK4_FRDP_EXPORT
GtkWidget *frdp_display_new       (void);

GTK4_FRDP_EXPORT
void       frdp_display_open_host (FrdpDisplay *display,
                                   const gchar *host,
                                   guint        port);

GTK4_FRDP_EXPORT
gboolean   frdp_display_is_open   (FrdpDisplay *display);

GTK4_FRDP_EXPORT
void       frdp_display_close     (FrdpDisplay *display);

GTK4_FRDP_EXPORT
void       frdp_display_set_scaling (FrdpDisplay *display,
                                     gboolean     scaling);

GTK4_FRDP_EXPORT
void       frdp_display_certificate_verify_ex_finish (FrdpDisplay *self,
                                                      guint        verification);

GTK4_FRDP_EXPORT
void       frdp_display_certificate_change_verify_ex_finish (FrdpDisplay *self,
                                                             guint        verification);

GTK4_FRDP_EXPORT
void       frdp_display_authenticate_finish (FrdpDisplay *self,
                                             gchar       *username,
                                             gchar       *password,
                                             gchar       *domain);

GTK4_FRDP_EXPORT
gboolean   frdp_display_is_keyboard_grabbed (FrdpDisplay *self);

G_END_DECLS
