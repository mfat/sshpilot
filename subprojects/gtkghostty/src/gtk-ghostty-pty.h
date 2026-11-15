/* SPDX-License-Identifier: LGPL-2.1-or-later
 * Copyright 2026 mFat <newmfat@gmail.com> and the gtkghostty contributors
 */
/* GtkGhosttyPty: an optional program-on-a-pseudo-terminal adapter.
 *
 * GtkGhosttyTerminal never starts a process. This object does, on a POSIX
 * pseudo terminal or a Windows pseudo console (ConPTY), and
 * gtk_ghostty_pty_attach() wires it to a terminal. Applications with their
 * own transport (an SSH channel, a serial port) do not need it.
 */
#pragma once

#include <gio/gio.h>

#include "gtk-ghostty-terminal.h"
#include "gtk-ghostty-visibility.h"

G_BEGIN_DECLS

#define GTK_GHOSTTY_TYPE_PTY (gtk_ghostty_pty_get_type ())
GTK_GHOSTTY_API
G_DECLARE_FINAL_TYPE (GtkGhosttyPty, gtk_ghostty_pty, GTK_GHOSTTY, PTY, GObject)

GTK_GHOSTTY_API
GtkGhosttyPty *gtk_ghostty_pty_spawn       (const char * const *argv,
                                            const char * const *envp,
                                            const char         *cwd,
                                            guint               columns,
                                            guint               rows,
                                            GError            **error);

GTK_GHOSTTY_API
void           gtk_ghostty_pty_write       (GtkGhosttyPty      *self,
                                            GBytes             *data);
GTK_GHOSTTY_API
void           gtk_ghostty_pty_write_data  (GtkGhosttyPty      *self,
                                            const guint8       *data,
                                            gsize               length);
GTK_GHOSTTY_API
void           gtk_ghostty_pty_resize      (GtkGhosttyPty      *self,
                                            guint               columns,
                                            guint               rows);
GTK_GHOSTTY_API
void           gtk_ghostty_pty_terminate   (GtkGhosttyPty      *self);
GTK_GHOSTTY_API
int            gtk_ghostty_pty_get_pid     (GtkGhosttyPty      *self);
GTK_GHOSTTY_API
gboolean       gtk_ghostty_pty_is_running  (GtkGhosttyPty      *self);

GTK_GHOSTTY_API
void           gtk_ghostty_pty_attach      (GtkGhosttyPty      *self,
                                            GtkGhosttyTerminal *terminal);

G_END_DECLS
