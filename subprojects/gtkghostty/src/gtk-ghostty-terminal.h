/* SPDX-License-Identifier: LGPL-2.1-or-later
 * Copyright 2026 mFat <newmfat@gmail.com> and the gtkghostty contributors
 */
/* GtkGhosttyTerminal: a GTK4 terminal view over libghostty-vt.
 *
 * The widget renders a terminal byte stream the caller feeds it and reports
 * what should go back to the program. It never starts a process or opens a
 * PTY: connect GtkGhosttyTerminal::input and ::reply to your transport.
 */
#pragma once

#include <gtk/gtk.h>

#include "gtk-ghostty-visibility.h"

G_BEGIN_DECLS

/**
 * GtkGhosttyCursorShape:
 * @GTK_GHOSTTY_CURSOR_SHAPE_BLOCK: a filled cell
 * @GTK_GHOSTTY_CURSOR_SHAPE_IBEAM: a vertical bar
 * @GTK_GHOSTTY_CURSOR_SHAPE_UNDERLINE: a line under the cell
 *
 * The default cursor shape; programs can change it (DECSCUSR).
 */
typedef enum
{
  GTK_GHOSTTY_CURSOR_SHAPE_BLOCK,
  GTK_GHOSTTY_CURSOR_SHAPE_IBEAM,
  GTK_GHOSTTY_CURSOR_SHAPE_UNDERLINE,
} GtkGhosttyCursorShape;

#define GTK_GHOSTTY_TYPE_CURSOR_SHAPE (gtk_ghostty_cursor_shape_get_type ())
GTK_GHOSTTY_API
GType gtk_ghostty_cursor_shape_get_type (void);

#define GTK_GHOSTTY_TYPE_TERMINAL (gtk_ghostty_terminal_get_type ())
GTK_GHOSTTY_API
G_DECLARE_FINAL_TYPE (GtkGhosttyTerminal, gtk_ghostty_terminal, GTK_GHOSTTY, TERMINAL, GtkWidget)

GTK_GHOSTTY_API
GtkWidget  *gtk_ghostty_terminal_new                (void);

GTK_GHOSTTY_API
void        gtk_ghostty_terminal_feed               (GtkGhosttyTerminal         *self,
                                                     GBytes                     *data);
GTK_GHOSTTY_API
void        gtk_ghostty_terminal_feed_data          (GtkGhosttyTerminal         *self,
                                                     const guint8               *data,
                                                     gsize                       length);

GTK_GHOSTTY_API
void        gtk_ghostty_terminal_paste_text         (GtkGhosttyTerminal         *self,
                                                     const char                 *text);
GTK_GHOSTTY_API
gboolean    gtk_ghostty_terminal_paste_is_safe      (const char                 *text);

GTK_GHOSTTY_API
char       *gtk_ghostty_terminal_get_text           (GtkGhosttyTerminal         *self);

GTK_GHOSTTY_API
char       *gtk_ghostty_terminal_get_selected_text  (GtkGhosttyTerminal         *self);
GTK_GHOSTTY_API
gboolean    gtk_ghostty_terminal_has_selection      (GtkGhosttyTerminal         *self);
GTK_GHOSTTY_API
void        gtk_ghostty_terminal_select_all         (GtkGhosttyTerminal         *self);
GTK_GHOSTTY_API
void        gtk_ghostty_terminal_unselect_all       (GtkGhosttyTerminal         *self);
GTK_GHOSTTY_API
void        gtk_ghostty_terminal_copy_clipboard     (GtkGhosttyTerminal         *self);
GTK_GHOSTTY_API
void        gtk_ghostty_terminal_paste_clipboard    (GtkGhosttyTerminal         *self);

GTK_GHOSTTY_API
void        gtk_ghostty_terminal_scroll_lines       (GtkGhosttyTerminal         *self,
                                                     int                         lines);
GTK_GHOSTTY_API
void        gtk_ghostty_terminal_scroll_to_bottom   (GtkGhosttyTerminal         *self);

GTK_GHOSTTY_API
void        gtk_ghostty_terminal_set_colors         (GtkGhosttyTerminal         *self,
                                                     const GdkRGBA              *foreground,
                                                     const GdkRGBA              *background,
                                                     const GdkRGBA              *cursor,
                                                     const GdkRGBA              *palette,
                                                     gsize                       palette_size);
GTK_GHOSTTY_API
void        gtk_ghostty_terminal_set_cursor_shape   (GtkGhosttyTerminal         *self,
                                                     GtkGhosttyCursorShape       shape);
GTK_GHOSTTY_API
GtkGhosttyCursorShape
            gtk_ghostty_terminal_get_cursor_shape   (GtkGhosttyTerminal         *self);
GTK_GHOSTTY_API
void        gtk_ghostty_terminal_set_cursor_blink   (GtkGhosttyTerminal         *self,
                                                     gboolean                    blink);
GTK_GHOSTTY_API
void        gtk_ghostty_terminal_set_scrollback_lines (GtkGhosttyTerminal       *self,
                                                       guint                     lines);
GTK_GHOSTTY_API
void        gtk_ghostty_terminal_set_font_scale     (GtkGhosttyTerminal         *self,
                                                     double                      scale);
GTK_GHOSTTY_API
double      gtk_ghostty_terminal_get_font_scale     (GtkGhosttyTerminal         *self);

GTK_GHOSTTY_API
char       *gtk_ghostty_terminal_get_selected_html  (GtkGhosttyTerminal         *self);
GTK_GHOSTTY_API
gboolean    gtk_ghostty_terminal_set_encoding       (GtkGhosttyTerminal         *self,
                                                     const char                 *encoding);
GTK_GHOSTTY_API
const char *gtk_ghostty_terminal_get_encoding       (GtkGhosttyTerminal         *self);
GTK_GHOSTTY_API
void        gtk_ghostty_terminal_set_wheel_scroll_lines (GtkGhosttyTerminal     *self,
                                                         guint                   lines);
GTK_GHOSTTY_API
guint       gtk_ghostty_terminal_get_wheel_scroll_lines (GtkGhosttyTerminal     *self);
GTK_GHOSTTY_API
const char *gtk_ghostty_terminal_get_current_directory_uri (GtkGhosttyTerminal  *self);

GTK_GHOSTTY_API
gboolean    gtk_ghostty_terminal_search_set_pattern (GtkGhosttyTerminal         *self,
                                                     const char                 *pattern,
                                                     gboolean                    regex,
                                                     gboolean                    case_sensitive);
GTK_GHOSTTY_API
gboolean    gtk_ghostty_terminal_search_find        (GtkGhosttyTerminal         *self,
                                                     gboolean                    backwards);

GTK_GHOSTTY_API
char       *gtk_ghostty_terminal_hyperlink_at       (GtkGhosttyTerminal         *self,
                                                     double                      x,
                                                     double                      y);
GTK_GHOSTTY_API
char       *gtk_ghostty_terminal_get_line_at        (GtkGhosttyTerminal         *self,
                                                     double                      x,
                                                     double                      y,
                                                     guint                      *column);

GTK_GHOSTTY_API
guint       gtk_ghostty_terminal_get_columns        (GtkGhosttyTerminal         *self);
GTK_GHOSTTY_API
guint       gtk_ghostty_terminal_get_rows           (GtkGhosttyTerminal         *self);
GTK_GHOSTTY_API
void        gtk_ghostty_terminal_set_grid_size      (GtkGhosttyTerminal         *self,
                                                     guint                       columns,
                                                     guint                       rows);

GTK_GHOSTTY_API
const char *gtk_ghostty_terminal_get_title          (GtkGhosttyTerminal         *self);

GTK_GHOSTTY_API
void        gtk_ghostty_terminal_set_font_desc      (GtkGhosttyTerminal         *self,
                                                     const PangoFontDescription *font_desc);
GTK_GHOSTTY_API
PangoFontDescription *
            gtk_ghostty_terminal_get_font_desc      (GtkGhosttyTerminal         *self);

GTK_GHOSTTY_API
gboolean    gtk_ghostty_terminal_get_legacy_keyboard (GtkGhosttyTerminal        *self);
GTK_GHOSTTY_API
void        gtk_ghostty_terminal_set_legacy_keyboard (GtkGhosttyTerminal        *self,
                                                      gboolean                   legacy);

GTK_GHOSTTY_API
gboolean    gtk_ghostty_terminal_get_pull_history_on_grow (GtkGhosttyTerminal   *self);
GTK_GHOSTTY_API
void        gtk_ghostty_terminal_set_pull_history_on_grow (GtkGhosttyTerminal   *self,
                                                           gboolean              pull);

G_END_DECLS
