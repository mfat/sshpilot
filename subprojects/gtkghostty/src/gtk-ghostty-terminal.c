/* SPDX-License-Identifier: LGPL-2.1-or-later
 * Copyright 2026 mFat <newmfat@gmail.com> and the gtkghostty contributors
 */
/* GtkGhosttyTerminal: a GTK4 terminal view over libghostty-vt 1.0.
 *
 * Terminal behaviour (parsing, screens, encoders, paste, formatter, effects)
 * is libghostty-vt's; this file is GTK glue. The drawing scheme -- one Pango
 * layout per run of same-styled cells, rebuilt only for dirty rows, glyphs
 * held to the cell grid by letter spacing, wide cells scaled to two cells,
 * box drawing on the grid -- follows tilix2 (https://github.com/markuman/tilix2,
 * WTFPL v2), moved from Cairo to GtkSnapshot.
 */

#include "gtk-ghostty-terminal.h"
#include "gtk-ghostty-tables.h"

#include <ghostty/vt.h>
#include <stdint.h>
#include <string.h>

#ifdef G_OS_WIN32
# define DEFAULT_FONT "Cascadia Mono, Consolas, Courier New 11"
/* Pango on Windows falls back only along its built-in alias lists: "Monospace"
 * has no Korean font, and a named family (Consolas) has no fallback at all,
 * so Hangul, CJK and emoji showed as hex boxes. Every font gets these
 * standard Windows families appended, in order. */
# define FALLBACK_FAMILIES "Malgun Gothic,Microsoft YaHei,Yu Gothic,Segoe UI Emoji,Segoe UI Symbol"
#else
# define DEFAULT_FONT "Monospace 11"
#endif
#define DEFAULT_COLUMNS 80
#define DEFAULT_ROWS 24
#define DEFAULT_SCROLLBACK_LINES 10000

typedef struct
{
  int x, n;
  GdkRGBA color;
} Span;

typedef struct
{
  int x, n;
  GdkRGBA fg;
  PangoLayout *layout;
  float scale;            /* wide runs: horizontal scale to exactly two cells */
  guint underline : 1;
  guint strike : 1;
  guint overline : 1;
} Run;

typedef struct
{
  int x;
  gunichar c;
  GdkRGBA fg;
} BoxCell;

typedef struct
{
  GArray *bgs;            /* Span */
  GArray *runs;           /* Run */
  GArray *boxes;          /* BoxCell */
  GPtrArray *chars;       /* char * per column, for the block cursor */
} Row;

struct _GtkGhosttyTerminal
{
  GtkWidget parent_instance;

  GhosttyTerminal terminal;
  GhosttyRenderState render_state;
  GhosttyRenderStateRowIterator rows;
  GhosttyRenderStateRowCells cells;
  GhosttyKeyEncoder key_encoder;

  GtkIMContext *im;

  PangoFontDescription *font;
  PangoFontDescription *fonts[2][2];  /* [bold][italic] */
  PangoAttrList *grid_attrs;
  int cell_width, cell_height, ascent;
  gboolean metrics_valid;

  guint columns, n_rows;
  GPtrArray *row_cache;   /* Row *, per viewport row */

  GByteArray *pending_reply;
  gboolean pending_bell, pending_title;
  char *title;

  gboolean legacy_keyboard;
  gboolean allow_clipboard_write;
  gboolean pull_history_on_grow;
  gboolean focused;

  /* Input-method composition (preedit), drawn at the cursor. */
  char *preedit;
  PangoAttrList *preedit_attrs;
  int preedit_cursor;           /* in characters */
  GdkRectangle im_location;     /* last caret rectangle told to the IM */

  double font_scale;
  GhosttyColorRgb builtin_fg, builtin_bg;  /* what the renderer falls back to */
  GtkGhosttyCursorShape cursor_shape;
  gboolean cursor_blink;
  gboolean cursor_reset_pending;  /* waiting for the parser to reach ground */
  GString *key_commit;          /* IM text committed while filtering a key */
  GArray *held_keys;            /* keycodes pressed and not yet released */
  GhosttySearch plain_search;   /* libghostty search for plain, caseless text */
  GRegex *search;               /* regex or case-sensitive search, if any */
  int search_row, search_column;  /* last hit (screen row), or -1 */

  /* GtkScrollable: the vertical adjustment mirrors libghostty's scrollbar. */
  GtkAdjustment *hadjustment, *vadjustment;
  GtkScrollablePolicy hscroll_policy, vscroll_policy;
  gboolean syncing_adjustment;

  /* Pointer: mouse reports and libghostty's selection gesture. */
  GhosttyMouseEncoder mouse_encoder;
  GhosttyMouseEvent mouse_event;
  GhosttySelectionGesture gesture;
  GhosttySelectionGestureEvent gesture_press, gesture_drag, gesture_release, gesture_tick;
  gboolean selecting;
  guint buttons_down;           /* bit per GDK button reported to the program */
  double pointer_x, pointer_y;
  double scroll_remainder;
  guint autoscroll_id;
};

enum
{
  PROP_0,
  PROP_COLUMNS,
  PROP_ROWS,
  PROP_TITLE,
  PROP_LEGACY_KEYBOARD,
  PROP_PULL_HISTORY_ON_GROW,
  PROP_FONT_DESC,
  PROP_ALLOW_CLIPBOARD_WRITE,
  N_PROPS,
  /* GtkScrollable, overridden */
  PROP_HADJUSTMENT = N_PROPS,
  PROP_VADJUSTMENT,
  PROP_HSCROLL_POLICY,
  PROP_VSCROLL_POLICY,
};

enum
{
  SIGNAL_INPUT,
  SIGNAL_REPLY,
  SIGNAL_SIZE_CHANGED,
  SIGNAL_BELL,
  SIGNAL_CONTENTS_CHANGED,
  SIGNAL_SELECTION_CHANGED,
  N_SIGNALS
};

static GParamSpec *properties[N_PROPS];
static guint signals[N_SIGNALS];

G_DEFINE_ENUM_TYPE (GtkGhosttyCursorShape, gtk_ghostty_cursor_shape,
  G_DEFINE_ENUM_VALUE (GTK_GHOSTTY_CURSOR_SHAPE_BLOCK, "block"),
  G_DEFINE_ENUM_VALUE (GTK_GHOSTTY_CURSOR_SHAPE_IBEAM, "ibeam"),
  G_DEFINE_ENUM_VALUE (GTK_GHOSTTY_CURSOR_SHAPE_UNDERLINE, "underline"))

G_DEFINE_FINAL_TYPE_WITH_CODE (GtkGhosttyTerminal, gtk_ghostty_terminal, GTK_TYPE_WIDGET,
                               G_IMPLEMENT_INTERFACE (GTK_TYPE_SCROLLABLE, NULL))

static void update_adjustment (GtkGhosttyTerminal *self);
static void scroll_to_bottom (GtkGhosttyTerminal *self);
static gboolean is_grid_symbol (gunichar c);
static void apply_pending_cursor_reset (GtkGhosttyTerminal *self);

/* ------------------------------------------------------------------ rows */

static void
row_free (gpointer data)
{
  Row *row = data;
  for (guint i = 0; i < row->runs->len; i++)
    g_clear_object (&g_array_index (row->runs, Run, i).layout);
  g_array_unref (row->bgs);
  g_array_unref (row->runs);
  g_array_unref (row->boxes);
  g_ptr_array_unref (row->chars);
  g_free (row);
}

static Row *
row_new (void)
{
  Row *row = g_new0 (Row, 1);
  row->bgs = g_array_new (FALSE, FALSE, sizeof (Span));
  row->runs = g_array_new (FALSE, FALSE, sizeof (Run));
  row->boxes = g_array_new (FALSE, FALSE, sizeof (BoxCell));
  row->chars = g_ptr_array_new_with_free_func (g_free);
  return row;
}

static GdkRGBA
rgba (GhosttyColorRgb c)
{
  return (GdkRGBA) { c.r / 255.f, c.g / 255.f, c.b / 255.f, 1.f };
}

static gboolean
rgba_equal (const GdkRGBA *a, const GdkRGBA *b)
{
  return a->red == b->red && a->green == b->green && a->blue == b->blue && a->alpha == b->alpha;
}

/* ------------------------------------------------------------- effects */

static void
on_write_pty (GhosttyTerminal terminal, void *userdata, const uint8_t *data, size_t len)
{
  GtkGhosttyTerminal *self = userdata;
  /* Never re-enter the terminal from a callback: queue, emit after the write. */
  g_byte_array_append (self->pending_reply, data, (guint) len);
}

static void
on_bell (GhosttyTerminal terminal, void *userdata)
{
  GTK_GHOSTTY_TERMINAL (userdata)->pending_bell = TRUE;
}

static void
on_title_changed (GhosttyTerminal terminal, void *userdata)
{
  GTK_GHOSTTY_TERMINAL (userdata)->pending_title = TRUE;
}

static bool
on_device_attributes (GhosttyTerminal terminal, void *userdata, GhosttyDeviceAttributes *out)
{
  return false;  /* the library's default attributes */
}

/* XTWINOPS size reports (CSI 14/16/18 t) and mode 2048: image tools size
 * their pictures from the cell size. */
static bool
on_size (GhosttyTerminal terminal, void *userdata, GhosttySizeReportSize *out)
{
  GtkGhosttyTerminal *self = userdata;
  out->rows = (uint16_t) self->n_rows;
  out->columns = (uint16_t) self->columns;
  out->cell_width = (uint32_t) MAX (1, self->cell_width);
  out->cell_height = (uint32_t) MAX (1, self->cell_height);
  return true;
}

/* XTVERSION (CSI > q). */
static GhosttyString
on_xtversion (GhosttyTerminal terminal, void *userdata)
{
  static const char version[] = "gtkghostty " GTK_GHOSTTY_VERSION;
  return (GhosttyString) { (const uint8_t *) version, sizeof version - 1 };
}

/* Colour scheme query (CSI ? 996 n): what the program draws on is the
 * terminal's background, so its brightness decides. */
/* OSC 52 (and OSC 1337 Copy, kitty OSC 5522) writes: a program sets the
 * clipboard, as tmux and editors do to copy across SSH. Plain text only.
 * Reads are not registered, so programs cannot read the clipboard: they
 * would have to be answered before this returns, and GTK reads the system
 * clipboard asynchronously; they would also let a remote host read what
 * the user copied locally. */
static void
on_clipboard_write (GhosttyTerminal terminal, void *userdata, const GhosttyClipboardWrite *write)
{
  GtkGhosttyTerminal *self = userdata;
  GhosttyClipboardWriteReply reply = GHOSTTY_INIT_SIZED (GhosttyClipboardWriteReply);
  GdkClipboard *clipboard;
  const GhosttyClipboardContent *text = NULL;
  (void) terminal;

  reply.result = GHOSTTY_CLIPBOARD_WRITE_RESULT_DENIED;
  if (!self->allow_clipboard_write)
    goto done;
  clipboard = write->location == GHOSTTY_CLIPBOARD_LOCATION_STANDARD
                ? gtk_widget_get_clipboard (GTK_WIDGET (self))
                : gtk_widget_get_primary_clipboard (GTK_WIDGET (self));
  if (write->contents_len == 0)
    {
      gdk_clipboard_set_content (clipboard, NULL);
      reply.result = GHOSTTY_CLIPBOARD_WRITE_RESULT_SUCCESS;
      goto done;
    }
  for (size_t i = 0; i < write->contents_len && text == NULL; i++)
    {
      const GhosttyString *mime = &write->contents[i].mime;
      if (mime->len >= 10 && memcmp (mime->ptr, "text/plain", 10) == 0)
        text = &write->contents[i];
    }
  reply.result = GHOSTTY_CLIPBOARD_WRITE_RESULT_UNSUPPORTED;
  if (text == NULL)
    goto done;
  reply.result = GHOSTTY_CLIPBOARD_WRITE_RESULT_INVALID_DATA;
  if (g_utf8_validate_len ((const char *) text->data.ptr, text->data.len, NULL))
    {
      char *value = g_strndup ((const char *) text->data.ptr, text->data.len);
      gdk_clipboard_set_text (clipboard, value);
      g_free (value);
      reply.result = GHOSTTY_CLIPBOARD_WRITE_RESULT_SUCCESS;
    }
done:
  write->reply (write, &reply);
}

static bool
on_color_scheme (GhosttyTerminal terminal, void *userdata, GhosttyColorScheme *out)
{
  GhosttyColorRgb background = { 0, 0, 0 };  /* libghostty's default background */
  ghostty_terminal_get (terminal, GHOSTTY_TERMINAL_DATA_COLOR_BACKGROUND, &background);
  *out = ghostty_color_perceived_luminance (&background) < 0.5 ? GHOSTTY_COLOR_SCHEME_DARK
                                                               : GHOSTTY_COLOR_SCHEME_LIGHT;
  return true;
}

static void
flush_effects (GtkGhosttyTerminal *self)
{
  if (self->pending_reply->len > 0)
    {
      GBytes *reply = g_bytes_new (self->pending_reply->data, self->pending_reply->len);
      g_byte_array_set_size (self->pending_reply, 0);
      g_signal_emit (self, signals[SIGNAL_REPLY], 0, reply);
      g_bytes_unref (reply);
    }
  if (self->pending_title)
    {
      GhosttyString title = { 0 };
      self->pending_title = FALSE;
      g_free (self->title);
      self->title = NULL;
      if (ghostty_terminal_get (self->terminal, GHOSTTY_TERMINAL_DATA_TITLE, &title) == GHOSTTY_SUCCESS
          && title.ptr != NULL)
        self->title = g_utf8_make_valid ((const char *) title.ptr, title.len);
      g_object_notify_by_pspec (G_OBJECT (self), properties[PROP_TITLE]);
    }
  if (self->pending_bell)
    {
      self->pending_bell = FALSE;
      g_signal_emit (self, signals[SIGNAL_BELL], 0);
    }
}

static void
emit_input (GtkGhosttyTerminal *self, const guint8 *data, gsize len)
{
  GBytes *bytes;
  if (len == 0)
    return;
  bytes = g_bytes_new (data, len);
  g_signal_emit (self, signals[SIGNAL_INPUT], 0, bytes);
  g_bytes_unref (bytes);
}

/* ------------------------------------------------------------- metrics */

static void
ensure_metrics (GtkGhosttyTerminal *self)
{
  PangoContext *context;
  PangoLayout *probe;
  PangoRectangle logical;
  int baseline;

  if (self->metrics_valid)
    return;

  for (int bold = 0; bold < 2; bold++)
    for (int italic = 0; italic < 2; italic++)
      {
        PangoFontDescription *desc = pango_font_description_copy (self->font);
        if (self->font_scale != 1.0)
          {
            int size = pango_font_description_get_size (desc);
            int scaled = MAX (PANGO_SCALE, (int) (size * self->font_scale + 0.5));
            if (pango_font_description_get_size_is_absolute (desc))
              pango_font_description_set_absolute_size (desc, scaled);
            else
              pango_font_description_set_size (desc, scaled);
          }
#ifdef FALLBACK_FAMILIES
        const char *family = pango_font_description_get_family (desc);
        if (family != NULL)
          {
            char *families = g_strconcat (family, ",", FALLBACK_FAMILIES, NULL);
            pango_font_description_set_family (desc, families);
            g_free (families);
          }
#endif
        if (bold)
          pango_font_description_set_weight (desc, PANGO_WEIGHT_BOLD);
        if (italic)
          pango_font_description_set_style (desc, PANGO_STYLE_ITALIC);
        g_clear_pointer (&self->fonts[bold][italic], pango_font_description_free);
        self->fonts[bold][italic] = desc;
      }

  context = gtk_widget_get_pango_context (GTK_WIDGET (self));
  probe = pango_layout_new (context);
  pango_layout_set_font_description (probe, self->fonts[0][0]);
  pango_layout_set_text (probe, "M", -1);
  pango_layout_get_extents (probe, NULL, &logical);
  baseline = pango_layout_get_baseline (probe);
  g_object_unref (probe);

  self->cell_width = MAX (1, PANGO_PIXELS_CEIL (logical.width));
  self->cell_height = MAX (1, PANGO_PIXELS_CEIL (logical.height));
  self->ascent = PANGO_PIXELS (baseline);

  /* Stretch every glyph's advance to the cell (tilix2): long runs then stay
   * on the grid instead of drifting by the font's sub-pixel advance. */
  g_clear_pointer (&self->grid_attrs, pango_attr_list_unref);
  self->grid_attrs = pango_attr_list_new ();
  pango_attr_list_insert (self->grid_attrs,
                          pango_attr_letter_spacing_new (self->cell_width * PANGO_SCALE - logical.width));

  g_ptr_array_set_size (self->row_cache, 0);
  self->metrics_valid = TRUE;
}

/* ------------------------------------------------------------ geometry */

static void
apply_grid_size (GtkGhosttyTerminal *self, guint columns, guint n_rows)
{
  columns = CLAMP (columns, 1, G_MAXUINT16);
  n_rows = CLAMP (n_rows, 1, G_MAXUINT16);
  if (columns == self->columns && n_rows == self->n_rows)
    return;
  self->columns = columns;
  self->n_rows = n_rows;
  ghostty_terminal_resize (self->terminal, columns, n_rows,
                           MAX (1, self->cell_width), MAX (1, self->cell_height));
  g_ptr_array_set_size (self->row_cache, 0);
  flush_effects (self);
  g_object_notify_by_pspec (G_OBJECT (self), properties[PROP_COLUMNS]);
  g_object_notify_by_pspec (G_OBJECT (self), properties[PROP_ROWS]);
  g_signal_emit (self, signals[SIGNAL_SIZE_CHANGED], 0,
                 columns, n_rows, (guint) self->cell_width, (guint) self->cell_height);
  update_adjustment (self);
  gtk_widget_queue_draw (GTK_WIDGET (self));
}

static void
gtk_ghostty_terminal_measure (GtkWidget *widget, GtkOrientation orientation, int for_size,
                              int *minimum, int *natural, int *minimum_baseline, int *natural_baseline)
{
  GtkGhosttyTerminal *self = GTK_GHOSTTY_TERMINAL (widget);
  ensure_metrics (self);
  if (orientation == GTK_ORIENTATION_HORIZONTAL)
    {
      *minimum = self->cell_width;
      *natural = self->cell_width * DEFAULT_COLUMNS;
    }
  else
    {
      *minimum = self->cell_height;
      *natural = self->cell_height * DEFAULT_ROWS;
    }
}

static void
gtk_ghostty_terminal_size_allocate (GtkWidget *widget, int width, int height, int baseline)
{
  GtkGhosttyTerminal *self = GTK_GHOSTTY_TERMINAL (widget);
  ensure_metrics (self);
  apply_grid_size (self, MAX (1, width / self->cell_width), MAX (1, height / self->cell_height));
}

/* ------------------------------------------------------------- drawing */

static void
add_bg (Row *row, int x, const GdkRGBA *color)
{
  if (row->bgs->len > 0)
    {
      Span *last = &g_array_index (row->bgs, Span, row->bgs->len - 1);
      if (last->x + last->n == x && rgba_equal (&last->color, color))
        {
          last->n++;
          return;
        }
    }
  g_array_append_val (row->bgs, ((Span) { x, 1, *color }));
}

typedef struct
{
  int x, n;
  GdkRGBA fg;
  gboolean bold, italic, wide, underline, strike, overline;
  GString *text;
} PendingRun;

static void
close_run (GtkGhosttyTerminal *self, Row *row, PendingRun *run)
{
  Run out = { 0 };
  PangoLayout *layout;
  if (run->text == NULL)
    return;
  layout = pango_layout_new (gtk_widget_get_pango_context (GTK_WIDGET (self)));
  pango_layout_set_font_description (layout, self->fonts[run->bold][run->italic]);
  pango_layout_set_text (layout, run->text->str, (int) run->text->len);
  if (!run->wide)
    pango_layout_set_attributes (layout, self->grid_attrs);
  out.x = run->x;
  out.n = run->n;
  out.fg = run->fg;
  out.layout = layout;
  out.underline = run->underline;
  out.strike = run->strike;
  out.overline = run->overline;
  if (run->wide)
    {
      int width = 0;
      pango_layout_get_pixel_size (layout, &width, NULL);
      out.scale = (float) (2 * self->cell_width) / MAX (1, width);
    }
  g_array_append_val (row->runs, out);
  g_string_free (run->text, TRUE);
  run->text = NULL;
}

/* The current iterator row -> backgrounds, text runs and grid-drawn cells. */
static Row *
build_row (GtkGhosttyTerminal *self, const GdkRGBA *fg_default, const GdkRGBA *bg_default)
{
  Row *row = row_new ();
  GhosttyRenderStateRowSelection selection = GHOSTTY_INIT_SIZED (GhosttyRenderStateRowSelection);
  int sel_start = -1, sel_end = -1;
  uint8_t text_buf[256];
  PendingRun run = { 0 };
  int x = 0;

  ghostty_render_state_row_get (self->rows, GHOSTTY_RENDER_STATE_ROW_DATA_CELLS, &self->cells);
  if (ghostty_render_state_row_get (self->rows, GHOSTTY_RENDER_STATE_ROW_DATA_SELECTION, &selection)
      == GHOSTTY_SUCCESS)
    {
      sel_start = selection.start_x;
      sel_end = selection.end_x;
    }

  while (ghostty_render_state_row_cells_next (self->cells))
    {
      GhosttyBuffer text = { text_buf, sizeof text_buf, 0 };
      GhosttyColorRgb color;
      GdkRGBA fg = *fg_default, bg;
      gboolean has_bg = FALSE, bold = FALSE, italic = FALSE;
      gboolean underline = FALSE, strike = FALSE, overline = FALSE, invisible = FALSE;
      bool styled = false;
      const char *str = "";
      gsize len = 0;
      char *owned = NULL;

      if (ghostty_render_state_row_cells_get (self->cells, GHOSTTY_RENDER_STATE_ROW_CELLS_DATA_GRAPHEMES_UTF8,
                                              &text) == GHOSTTY_SUCCESS)
        {
          str = (const char *) text_buf;
          len = text.len;
        }
      else
        {
          str = "\xEF\xBF\xBD";  /* cluster over 256 bytes: replacement character */
          len = 3;
        }

      if (ghostty_render_state_row_cells_get (self->cells, GHOSTTY_RENDER_STATE_ROW_CELLS_DATA_BG_COLOR,
                                              &color) == GHOSTTY_SUCCESS)
        {
          bg = rgba (color);
          has_bg = TRUE;
        }
      ghostty_render_state_row_cells_get (self->cells, GHOSTTY_RENDER_STATE_ROW_CELLS_DATA_HAS_STYLING, &styled);
      if (styled)
        {
          GhosttyStyle style;
          ghostty_render_state_row_cells_get (self->cells, GHOSTTY_RENDER_STATE_ROW_CELLS_DATA_STYLE, &style);
          if (ghostty_render_state_row_cells_get (self->cells, GHOSTTY_RENDER_STATE_ROW_CELLS_DATA_FG_COLOR,
                                                  &color) == GHOSTTY_SUCCESS)
            fg = rgba (color);
          bold = (style.flags & GHOSTTY_STYLE_FLAG_BOLD) != 0;
          italic = (style.flags & GHOSTTY_STYLE_FLAG_ITALIC) != 0;
          underline = GHOSTTY_STYLE_UNDERLINE (style) != GHOSTTY_SGR_UNDERLINE_NONE;
          strike = (style.flags & GHOSTTY_STYLE_FLAG_STRIKETHROUGH) != 0;
          overline = (style.flags & GHOSTTY_STYLE_FLAG_OVERLINE) != 0;
          invisible = (style.flags & GHOSTTY_STYLE_FLAG_INVISIBLE) != 0;
          if (style.flags & GHOSTTY_STYLE_FLAG_INVERSE)
            {
              GdkRGBA old_bg = has_bg ? bg : *bg_default;
              bg = fg;
              has_bg = TRUE;
              fg = old_bg;
            }
          if (style.flags & GHOSTTY_STYLE_FLAG_FAINT)
            {
              fg.red *= 0.6f;
              fg.green *= 0.6f;
              fg.blue *= 0.6f;
            }
        }
      if (x >= sel_start && x <= sel_end)
        {
          GdkRGBA old_bg = has_bg ? bg : *bg_default;
          bg = fg;
          has_bg = TRUE;
          fg = old_bg;
        }
      if (invisible)
        len = 0;
      if (has_bg && !rgba_equal (&bg, bg_default))
        add_bg (row, x, &bg);

      owned = g_strndup (str, len);
      g_ptr_array_add (row->chars, owned);

      if (len == 0)
        close_run (self, row, &run);
      else
        {
          gunichar c = g_utf8_get_char_validated (owned, len);
          if (g_utf8_strlen (owned, len) == 1 && is_grid_symbol (c))
            {
              close_run (self, row, &run);
              g_array_append_val (row->boxes, ((BoxCell) { x, c, fg }));
            }
          else
            {
              GhosttyCell raw = 0;
              GhosttyCellWide wide = GHOSTTY_CELL_WIDE_NARROW;
              gboolean is_wide;
              ghostty_render_state_row_cells_get (self->cells, GHOSTTY_RENDER_STATE_ROW_CELLS_DATA_RAW, &raw);
              ghostty_cell_get (raw, GHOSTTY_CELL_DATA_WIDE, &wide);
              is_wide = wide == GHOSTTY_CELL_WIDE_WIDE;
              /* Wide cells (CJK, emoji) get a run of their own, never merged. */
              if (run.text != NULL && !is_wide && !run.wide && run.x + run.n == x
                  && rgba_equal (&run.fg, &fg) && run.bold == bold && run.italic == italic
                  && run.underline == underline && run.strike == strike && run.overline == overline)
                {
                  g_string_append_len (run.text, owned, len);
                  run.n++;
                }
              else
                {
                  close_run (self, row, &run);
                  run = (PendingRun) { x, is_wide ? 2 : 1, fg, bold, italic, is_wide,
                                       underline, strike, overline, g_string_new_len (owned, len) };
                }
            }
        }
      x++;
    }
  close_run (self, row, &run);
  return row;
}

static void
append_rect (GtkSnapshot *snapshot, const GdkRGBA *color, double x, double y, double w, double h)
{
  if (w <= 0 || h <= 0)
    return;
  gtk_snapshot_append_color (snapshot, color, &GRAPHENE_RECT_INIT ((float) x, (float) y, (float) w, (float) h));
}

static void
draw_box (GtkSnapshot *snapshot, const GtkGhosttyBoxGlyph *glyph, const GdkRGBA *color,
          int x, int y, int w, int h)
{
  if (glyph->kind == GTK_GHOSTTY_BOX_SHADE)
    {
      GdkRGBA shaded = *color;
      shaded.alpha *= glyph->shade;
      append_rect (snapshot, &shaded, x, y, w, h);
    }
  else if (glyph->kind == GTK_GHOSTTY_BOX_RECTS)
    {
      for (int i = 0; i < glyph->n_rects; i++)
        {
          const float *r = glyph->rects[i];
          int x1 = x + (int) (r[0] * (float) w + 0.5f), y1 = y + (int) (r[1] * (float) h + 0.5f);
          int x2 = x + (int) ((r[0] + r[2]) * (float) w + 0.5f), y2 = y + (int) ((r[1] + r[3]) * (float) h + 0.5f);
          append_rect (snapshot, color, x1, y1, x2 - x1, y2 - y1);
        }
    }
  else
    {
      int light = MAX (1, h / 16);
      int cx = x + w / 2, cy = y + h / 2;  /* lines run through these pixels */
      if (glyph->arc)
        {
          /* ╭╮╯╰: a quarter curve from the vertical edge to the horizontal one. */
          float px = (float) cx + (float) (light % 2) / 2.f, py = (float) cy + (float) (light % 2) / 2.f;
          GskPathBuilder *builder = gsk_path_builder_new ();
          GskStroke *stroke = gsk_stroke_new ((float) light);
          GskPath *path;
          gsk_path_builder_move_to (builder, px, (float) (glyph->arms[3] ? y + h : y));
          gsk_path_builder_cubic_to (builder, px, py, px, py, (float) (glyph->arms[1] ? x + w : x), py);
          path = gsk_path_builder_free_to_path (builder);
          gtk_snapshot_append_stroke (snapshot, path, stroke, color);
          gsk_path_unref (path);
          gsk_stroke_free (stroke);
          return;
        }
      for (int arm = 0; arm < 4; arm++)
        {
          int weight = glyph->arms[arm], t, a;
          if (weight == 0)
            continue;
          t = weight == 1 ? light : 2 * light + 1;
          a = t / 2;
          switch (arm)
            {
            case 0: append_rect (snapshot, color, x, cy - a, cx - x + t - a, t); break;
            case 1: append_rect (snapshot, color, cx - a, cy - a, x + w - cx + a, t); break;
            case 2: append_rect (snapshot, color, cx - a, y, t, cy - y + t - a); break;
            default: append_rect (snapshot, color, cx - a, cy - a, t, y + h - cy + a); break;
            }
        }
    }
}

/* ----------------------------------------------------- grid symbols */

/* Characters drawn on the cell grid instead of by the font, so neighbours
 * join without gaps whatever font is installed: box drawing and block
 * elements (the generated tables), Powerline separators, braille and
 * sextants. */

static gboolean
is_braille (gunichar c)
{
  return c >= 0x2800 && c <= 0x28FF;
}

static gboolean
is_sextant (gunichar c)
{
  return c >= 0x1FB00 && c <= 0x1FB3B;
}

static gboolean
is_powerline (gunichar c)
{
  return c >= 0xE0B0 && c <= 0xE0BF;
}

static gboolean
is_grid_symbol (gunichar c)
{
  return gtk_ghostty_box_glyph (c) != NULL || is_braille (c) || is_sextant (c) || is_powerline (c);
}

static void
fill_path (GtkSnapshot *snapshot, GskPathBuilder *builder, const GdkRGBA *color)
{
  GskPath *path = gsk_path_builder_free_to_path (builder);
  gtk_snapshot_append_fill (snapshot, path, GSK_FILL_RULE_WINDING, color);
  gsk_path_unref (path);
}

static void
stroke_path (GtkSnapshot *snapshot, GskPathBuilder *builder, const GdkRGBA *color, float width)
{
  GskPath *path = gsk_path_builder_free_to_path (builder);
  GskStroke *stroke = gsk_stroke_new (width);
  gtk_snapshot_append_stroke (snapshot, path, stroke, color);
  gsk_stroke_free (stroke);
  gsk_path_unref (path);
}

/* Braille: dots 1-3 and 7 down the left column, 4-6 and 8 down the right. */
static void
draw_braille (GtkSnapshot *snapshot, gunichar c, const GdkRGBA *color, int x, int y, int w, int h)
{
  static const int column[8] = { 0, 0, 0, 1, 1, 1, 0, 1 };
  static const int row[8] = { 0, 1, 2, 0, 1, 2, 3, 3 };
  guint bits = c - 0x2800;
  float dot = MAX (1.f, MIN (w / 4.f, h / 8.f));
  for (int i = 0; i < 8; i++)
    if (bits & (1u << i))
      {
        float cx = x + w * (column[i] ? 0.72f : 0.28f);
        float cy = y + h * (0.125f + row[i] * 0.25f);
        GskPathBuilder *builder = gsk_path_builder_new ();
        gsk_path_builder_add_circle (builder, &GRAPHENE_POINT_INIT (cx, cy), dot / 2 + 0.25f);
        fill_path (snapshot, builder, color);
      }
}

/* Sextants: 2x3 blocks. U+1FB00.. enumerate the patterns 1..62 without the
 * two that are the left and right half blocks (21 and 42). */
static void
draw_sextant (GtkSnapshot *snapshot, gunichar c, const GdkRGBA *color, int x, int y, int w, int h)
{
  guint pattern = c - 0x1FB00 + 1;
  if (pattern >= 21)
    pattern++;
  if (pattern >= 42)
    pattern++;
  for (int i = 0; i < 6; i++)
    if (pattern & (1u << i))
      {
        int col = i % 2, rw = i / 2;
        int x1 = x + (col ? w / 2 : 0), x2 = x + (col ? w : w / 2);
        int y1 = y + (rw * h + 1) / 3, y2 = y + ((rw + 1) * h + 1) / 3;
        append_rect (snapshot, color, x1, y1, x2 - x1, y2 - y1);
      }
}

/* Powerline separators: solid and thin triangles, half circles and slants. */
static void
draw_powerline (GtkSnapshot *snapshot, gunichar c, const GdkRGBA *color, int x, int y, int w, int h)
{
  GskPathBuilder *b = gsk_path_builder_new ();
  float l = (float) x, r = (float) (x + w), t = (float) y, bo = (float) (y + h), m = y + h / 2.f;
  float thin = MAX (1.f, h / 16.f);
  gboolean solid = (c - 0xE0B0) % 2 == 0;
  switch (c)
    {
    case 0xE0B0: case 0xE0B1:  /* right-pointing triangle */
      gsk_path_builder_move_to (b, l, t);
      gsk_path_builder_line_to (b, r, m);
      gsk_path_builder_line_to (b, l, bo);
      break;
    case 0xE0B2: case 0xE0B3:  /* left-pointing triangle */
      gsk_path_builder_move_to (b, r, t);
      gsk_path_builder_line_to (b, l, m);
      gsk_path_builder_line_to (b, r, bo);
      break;
    case 0xE0B4: case 0xE0B5:  /* right half circle */
      gsk_path_builder_move_to (b, l, t);
      gsk_path_builder_cubic_to (b, r, t, r, bo, l, bo);
      break;
    case 0xE0B6: case 0xE0B7:  /* left half circle */
      gsk_path_builder_move_to (b, r, t);
      gsk_path_builder_cubic_to (b, l, t, l, bo, r, bo);
      break;
    case 0xE0B8: case 0xE0B9:  /* lower-left triangle / backslash */
      gsk_path_builder_move_to (b, l, t);
      gsk_path_builder_line_to (b, r, bo);
      if (solid)
        gsk_path_builder_line_to (b, l, bo);
      break;
    case 0xE0BA: case 0xE0BB:  /* lower-right triangle / slash */
      gsk_path_builder_move_to (b, r, t);
      gsk_path_builder_line_to (b, l, bo);
      if (solid)
        gsk_path_builder_line_to (b, r, bo);
      break;
    case 0xE0BC: case 0xE0BD:  /* upper-left triangle / slash */
      gsk_path_builder_move_to (b, l, bo);
      gsk_path_builder_line_to (b, r, t);
      if (solid)
        gsk_path_builder_line_to (b, l, t);
      break;
    default:  /* 0xE0BE, 0xE0BF: upper-right triangle / backslash */
      gsk_path_builder_move_to (b, l, t);
      gsk_path_builder_line_to (b, r, bo);
      if (solid)
        gsk_path_builder_line_to (b, r, t);
      break;
    }
  if (solid)
    {
      gsk_path_builder_close (b);
      fill_path (snapshot, b, color);
    }
  else
    stroke_path (snapshot, b, color, thin);
}

static void
draw_symbol (GtkSnapshot *snapshot, gunichar c, const GdkRGBA *color, int x, int y, int w, int h)
{
  const GtkGhosttyBoxGlyph *glyph = gtk_ghostty_box_glyph (c);
  if (glyph != NULL)
    draw_box (snapshot, glyph, color, x, y, w, h);
  else if (is_braille (c))
    draw_braille (snapshot, c, color, x, y, w, h);
  else if (is_sextant (c))
    draw_sextant (snapshot, c, color, x, y, w, h);
  else if (is_powerline (c))
    draw_powerline (snapshot, c, color, x, y, w, h);
}

static void
draw_layout (GtkSnapshot *snapshot, PangoLayout *layout, const GdkRGBA *color, double x, double y, float scale)
{
  gtk_snapshot_save (snapshot);
  gtk_snapshot_translate (snapshot, &GRAPHENE_POINT_INIT ((float) x, (float) y));
  if (scale > 0)
    gtk_snapshot_scale (snapshot, scale, 1);
  gtk_snapshot_append_layout (snapshot, layout, color);
  gtk_snapshot_restore (snapshot);
}

/* ------------------------------------------------------------- preedit */

/* The composition an input method is showing, laid out as cells from the
 * terminal cursor: grapheme widths from libghostty (the same rules the
 * terminal applies when the text is committed), wrapped at the right edge,
 * shifted up when it would leave the bottom, and hidden while the cursor
 * is scrolled out of view. The layout follows Kwi's PreeditLayout design
 * (see NOTICE). */

typedef struct
{
  int x, y, width;
  int byte_start, byte_end;     /* into self->preedit */
} PreeditCell;

typedef struct
{
  GdkRGBA fg, bg;
  gboolean bold;
  PangoUnderline underline;
} PreeditStyle;

static PreeditStyle
preedit_style (GtkGhosttyTerminal *self, int start, int end, const GdkRGBA *fg, const GdkRGBA *bg)
{
  PreeditStyle style = { *fg, *bg, FALSE, PANGO_UNDERLINE_SINGLE };
  GSList *attributes;
  if (self->preedit_attrs == NULL)
    return style;
  attributes = pango_attr_list_get_attributes (self->preedit_attrs);
  for (GSList *l = attributes; l != NULL; l = l->next)
    {
      PangoAttribute *attr = l->data;
      if ((int) attr->start_index >= end || (int) attr->end_index <= start)
        continue;
      switch (attr->klass->type)
        {
        case PANGO_ATTR_UNDERLINE:
          style.underline = (PangoUnderline) ((PangoAttrInt *) attr)->value;
          break;
        case PANGO_ATTR_FOREGROUND:
          {
            PangoColor c = ((PangoAttrColor *) attr)->color;
            style.fg = (GdkRGBA) { c.red / 65535.f, c.green / 65535.f, c.blue / 65535.f, 1.f };
          }
          break;
        case PANGO_ATTR_BACKGROUND:
          {
            PangoColor c = ((PangoAttrColor *) attr)->color;
            style.bg = (GdkRGBA) { c.red / 65535.f, c.green / 65535.f, c.blue / 65535.f, 1.f };
          }
          break;
        case PANGO_ATTR_WEIGHT:
          style.bold = ((PangoAttrInt *) attr)->value >= PANGO_WEIGHT_BOLD;
          break;
        default:
          break;
        }
    }
  g_slist_free_full (attributes, (GDestroyNotify) pango_attribute_destroy);
  return style;
}

/* Lays the preedit out from cursor (x, y); returns the cells and sets the
 * caret. NULL when there is nothing to draw. */
static GArray *
layout_preedit (GtkGhosttyTerminal *self, int cursor_x, int cursor_y, int *caret_x, int *caret_y)
{
  GArray *cells;
  gunichar *cps;
  glong n = 0;
  const char *p;
  int x = cursor_x, y = cursor_y, last_row, shift, columns = (int) self->columns, rows = (int) self->n_rows;

  *caret_x = cursor_x;
  *caret_y = cursor_y;
  if (self->preedit == NULL || self->preedit[0] == '\0' || columns <= 0)
    return NULL;
  cps = g_utf8_to_ucs4_fast (self->preedit, -1, &n);
  cells = g_array_new (FALSE, FALSE, sizeof (PreeditCell));
  p = self->preedit;
  for (glong i = 0; i < n;)
    {
      uint8_t ghostty_width = 1;
      size_t count = ghostty_unicode_grapheme_width ((const uint32_t *) cps + i, (size_t) (n - i), &ghostty_width);
      PreeditCell cell;
      const char *end = p;
      if (count == 0)
        count = 1;
      for (size_t k = 0; k < count; k++)
        end = g_utf8_next_char (end);
      /* An isolated combining mark still gets a cell; one column clips wide clusters. */
      cell.width = CLAMP ((int) ghostty_width, 1, MIN (2, columns));
      if (x + cell.width > columns)
        {
          x = 0;
          y++;
        }
      /* An IM caret inside a cluster snaps to the cluster. */
      if (self->preedit_cursor >= i && self->preedit_cursor < i + (glong) count)
        {
          *caret_x = x;
          *caret_y = y;
        }
      cell.x = x;
      cell.y = y;
      cell.byte_start = (int) (p - self->preedit);
      cell.byte_end = (int) (end - self->preedit);
      g_array_append_val (cells, cell);
      x += cell.width;
      p = end;
      i += (glong) count;
    }
  if (self->preedit_cursor >= n)
    {
      *caret_x = MIN (x, columns - 1);
      *caret_y = y;
    }
  g_free (cps);

  last_row = MAX (*caret_y, cells->len > 0 ? g_array_index (cells, PreeditCell, cells->len - 1).y : 0);
  shift = MIN (*caret_y, MAX (0, last_row - rows + 1));
  *caret_y -= shift;
  for (guint i = 0; i < cells->len;)
    {
      PreeditCell *cell = &g_array_index (cells, PreeditCell, i);
      cell->y -= shift;
      if (cell->y < 0 || cell->y >= rows)
        g_array_remove_index (cells, i);
      else
        i++;
    }
  return cells;
}

static void
tell_im_location (GtkGhosttyTerminal *self, int x, int y)
{
  GdkRectangle rect = { x * self->cell_width, y * self->cell_height, self->cell_width, self->cell_height };
  if (rect.x == self->im_location.x && rect.y == self->im_location.y
      && rect.width == self->im_location.width && rect.height == self->im_location.height)
    return;
  self->im_location = rect;
  gtk_im_context_set_cursor_location (self->im, &rect);
}

/* Draws the composition; FALSE when there is none (the normal cursor shows). */
static gboolean
draw_preedit (GtkGhosttyTerminal *self, GtkSnapshot *snapshot, const GdkRGBA *fg, const GdkRGBA *bg)
{
  GhosttyRenderStateCursor cursor = GHOSTTY_INIT_SIZED (GhosttyRenderStateCursor);
  GArray *cells;
  int caret_x, caret_y, cw = self->cell_width, ch = self->cell_height;
  GdkRGBA caret_color = *fg;

  if (ghostty_render_state_get (self->render_state, GHOSTTY_RENDER_STATE_DATA_CURSOR, &cursor) != GHOSTTY_SUCCESS
      || !cursor.viewport_has_value)
    return self->preedit != NULL && self->preedit[0] != '\0';  /* no anchor while scrolled back */
  tell_im_location (self, cursor.viewport_x, cursor.viewport_y);
  cells = layout_preedit (self, cursor.viewport_x, cursor.viewport_y, &caret_x, &caret_y);
  if (cells == NULL)
    return FALSE;
  for (guint i = 0; i < cells->len; i++)
    {
      PreeditCell *cell = &g_array_index (cells, PreeditCell, i);
      PreeditStyle style = preedit_style (self, cell->byte_start, cell->byte_end, fg, bg);
      PangoLayout *layout = pango_layout_new (gtk_widget_get_pango_context (GTK_WIDGET (self)));
      double left = (double) cell->x * cw, top = (double) cell->y * ch;
      float scale = 0;
      append_rect (snapshot, &style.bg, left, top, cell->width * cw, ch);
      pango_layout_set_font_description (layout, self->fonts[style.bold][0]);
      pango_layout_set_text (layout, self->preedit + cell->byte_start, cell->byte_end - cell->byte_start);
      if (cell->width == 2)
        {
          int width = 0;
          pango_layout_get_pixel_size (layout, &width, NULL);
          scale = (float) (2 * cw) / MAX (1, width);
        }
      draw_layout (snapshot, layout, &style.fg, left, top, scale);
      g_object_unref (layout);
      if (style.underline != PANGO_UNDERLINE_NONE)
        append_rect (snapshot, &style.fg, left, top + ch - 2, cell->width * cw, 1);
      if (cell->x == caret_x && cell->y == caret_y)
        caret_color = style.fg;
    }
  g_array_unref (cells);
  append_rect (snapshot, &caret_color, (double) caret_x * cw, (double) caret_y * ch, 2, ch);
  tell_im_location (self, caret_x, caret_y);
  return TRUE;
}

static void
on_preedit_changed (GtkIMContext *im, GtkGhosttyTerminal *self)
{
  g_clear_pointer (&self->preedit, g_free);
  g_clear_pointer (&self->preedit_attrs, pango_attr_list_unref);
  gtk_im_context_get_preedit_string (im, &self->preedit, &self->preedit_attrs, &self->preedit_cursor);
  gtk_widget_queue_draw (GTK_WIDGET (self));
}

static void
draw_cursor (GtkGhosttyTerminal *self, GtkSnapshot *snapshot, const GhosttyRenderStateColors *colors,
             const GdkRGBA *fg_default, const GdkRGBA *bg_default)
{
  GhosttyRenderStateCursor cursor = GHOSTTY_INIT_SIZED (GhosttyRenderStateCursor);
  GdkRGBA color;
  int cw = self->cell_width, ch = self->cell_height, x, y;

  if (ghostty_render_state_get (self->render_state, GHOSTTY_RENDER_STATE_DATA_CURSOR, &cursor) != GHOSTTY_SUCCESS
      || !cursor.visible || !cursor.viewport_has_value)
    return;
  x = cursor.viewport_x - (cursor.wide_tail ? 1 : 0);
  y = cursor.viewport_y;
  if ((guint) y >= self->row_cache->len)
    return;
  color = colors->cursor_has_value ? rgba (colors->cursor) : *fg_default;

  if (cursor.visual_style == GHOSTTY_CURSOR_STYLE_BAR)
    append_rect (snapshot, &color, x * cw, y * ch, 2, ch);
  else if (cursor.visual_style == GHOSTTY_CURSOR_STYLE_UNDERLINE)
    append_rect (snapshot, &color, x * cw, (y + 1) * ch - 2, cw, 2);
  else if (cursor.visual_style == GHOSTTY_CURSOR_STYLE_BLOCK_HOLLOW || !self->focused)
    {
      append_rect (snapshot, &color, x * cw, y * ch, cw, 1);
      append_rect (snapshot, &color, x * cw, (y + 1) * ch - 1, cw, 1);
      append_rect (snapshot, &color, x * cw, y * ch, 1, ch);
      append_rect (snapshot, &color, (x + 1) * cw - 1, y * ch, 1, ch);
    }
  else
    {
      /* Block: fill the cell, then the character on top in the background colour. */
      Row *row = g_ptr_array_index (self->row_cache, y);
      append_rect (snapshot, &color, x * cw, y * ch, cw, ch);
      if ((guint) x < row->chars->len)
        {
          const char *text = g_ptr_array_index (row->chars, x);
          gunichar symbol = text[0] ? g_utf8_get_char (text) : 0;
          if (symbol != 0 && is_grid_symbol (symbol))
            draw_symbol (snapshot, symbol, bg_default, x * cw, y * ch, cw, ch);
          else if (text[0])
            {
              PangoLayout *layout = pango_layout_new (gtk_widget_get_pango_context (GTK_WIDGET (self)));
              pango_layout_set_font_description (layout, self->fonts[0][0]);
              pango_layout_set_text (layout, text, -1);
              draw_layout (snapshot, layout, bg_default, x * cw, y * ch, 0);
              g_object_unref (layout);
            }
        }
    }
}

static void
gtk_ghostty_terminal_snapshot (GtkWidget *widget, GtkSnapshot *snapshot)
{
  GtkGhosttyTerminal *self = GTK_GHOSTTY_TERMINAL (widget);
  GhosttyRenderStateColors colors = GHOSTTY_INIT_SIZED (GhosttyRenderStateColors);
  GhosttyRenderStateDirty dirty = GHOSTTY_RENDER_STATE_DIRTY_FULL;
  GdkRGBA fg, bg;
  int cw, ch;
  guint y = 0;

  ensure_metrics (self);
  cw = self->cell_width;
  ch = self->cell_height;

  ghostty_render_state_update (self->render_state, self->terminal);
  ghostty_render_state_get (self->render_state, GHOSTTY_RENDER_STATE_DATA_COLORS, &colors);
  ghostty_render_state_get (self->render_state, GHOSTTY_RENDER_STATE_DATA_DIRTY, &dirty);
  fg = rgba (colors.foreground);
  bg = rgba (colors.background);

  /* Rebuild only dirty rows; Pango shaping is the expensive part, and on
   * Windows font fallback makes it far more so. */
  ghostty_render_state_get (self->render_state, GHOSTTY_RENDER_STATE_DATA_ROW_ITERATOR, &self->rows);
  while (ghostty_render_state_row_iterator_next (self->rows))
    {
      bool row_dirty = true;
      ghostty_render_state_row_get (self->rows, GHOSTTY_RENDER_STATE_ROW_DATA_DIRTY, &row_dirty);
      if (y >= self->row_cache->len)
        g_ptr_array_add (self->row_cache, build_row (self, &fg, &bg));
      else if (dirty == GHOSTTY_RENDER_STATE_DIRTY_FULL || row_dirty)
        {
          row_free (g_ptr_array_index (self->row_cache, y));
          g_ptr_array_index (self->row_cache, y) = build_row (self, &fg, &bg);
        }
      y++;
    }
  if (y < self->row_cache->len)
    g_ptr_array_set_size (self->row_cache, y);
  ghostty_render_state_clean (self->render_state);

  append_rect (snapshot, &bg, 0, 0, (float) gtk_widget_get_width (widget), (float) gtk_widget_get_height (widget));

  for (y = 0; y < self->row_cache->len; y++)
    {
      Row *row = g_ptr_array_index (self->row_cache, y);
      float top = (float) y * ch;
      for (guint i = 0; i < row->bgs->len; i++)
        {
          Span *span = &g_array_index (row->bgs, Span, i);
          append_rect (snapshot, &span->color, (float) (span->x * cw), top, (float) (span->n * cw), (float) ch);
        }
    }
  for (y = 0; y < self->row_cache->len; y++)
    {
      Row *row = g_ptr_array_index (self->row_cache, y);
      float top = (float) y * ch;
      for (guint i = 0; i < row->boxes->len; i++)
        {
          BoxCell *box = &g_array_index (row->boxes, BoxCell, i);
          draw_symbol (snapshot, box->c, &box->fg, box->x * cw, (int) y * ch, cw, ch);
        }
      for (guint i = 0; i < row->runs->len; i++)
        {
          Run *run = &g_array_index (row->runs, Run, i);
          float left = (float) (run->x * cw), width = (float) (run->n * cw);
          draw_layout (snapshot, run->layout, &run->fg, left, top, run->scale);
          if (run->underline)
            append_rect (snapshot, &run->fg, left, top + (float) (self->ascent + 2), width, 1);
          if (run->strike)
            append_rect (snapshot, &run->fg, left, top + (float) (ch / 2), width, 1);
          if (run->overline)
            append_rect (snapshot, &run->fg, left, top + 1, width, 1);
        }
    }
  if (!draw_preedit (self, snapshot, &fg, &bg))
    draw_cursor (self, snapshot, &colors, &fg, &bg);
}

/* ---------------------------------------------------------------- input */

static GhosttyMods
mods_from_state (GdkModifierType state)
{
  GhosttyMods mods = 0;
  if (state & GDK_SHIFT_MASK)
    mods |= GHOSTTY_MODS_SHIFT;
  if (state & GDK_CONTROL_MASK)
    mods |= GHOSTTY_MODS_CTRL;
  if (state & GDK_ALT_MASK)
    mods |= GHOSTTY_MODS_ALT;
  if (state & GDK_SUPER_MASK)
    mods |= GHOSTTY_MODS_SUPER;
  if (state & GDK_LOCK_MASK)
    mods |= GHOSTTY_MODS_CAPS_LOCK;
  return mods;
}

static gboolean
is_modifier_key (guint keyval)
{
  switch (keyval)
    {
    case GDK_KEY_Shift_L: case GDK_KEY_Shift_R: case GDK_KEY_Control_L: case GDK_KEY_Control_R:
    case GDK_KEY_Alt_L: case GDK_KEY_Alt_R: case GDK_KEY_Meta_L: case GDK_KEY_Meta_R:
    case GDK_KEY_Super_L: case GDK_KEY_Super_R: case GDK_KEY_Hyper_L: case GDK_KEY_Hyper_R:
    case GDK_KEY_ISO_Level3_Shift: case GDK_KEY_Caps_Lock: case GDK_KEY_Num_Lock: case GDK_KEY_Scroll_Lock:
      return TRUE;
    default:
      return FALSE;
    }
}

/* The keyval of the physical key without modifiers (kitty's unshifted codepoint). */
static guint
base_keyval (GtkEventController *controller, guint keycode, guint keyval)
{
  GdkEvent *event = gtk_event_controller_get_current_event (controller);
  GdkDisplay *display = gtk_widget_get_display (gtk_event_controller_get_widget (controller));
  guint base = 0;
  int group = event != NULL ? (int) gdk_key_event_get_layout (event) : 0;
  if (keycode != 0 && gdk_display_translate_key (display, keycode, 0, group, &base, NULL, NULL, NULL))
    return base;
  return gdk_keyval_to_lower (keyval);
}

/* Encodes one key event with libghostty's encoder, which follows the
 * protocol the program asked for (legacy, or kitty with its flags). @utf8
 * is the text the key produced, from the input method when it committed
 * one; releases carry none. FALSE for a key it has nothing for, so the
 * event can go on to the application. */
static gboolean
encode_key (GtkGhosttyTerminal *self, GtkEventController *controller, guint keyval, guint keycode,
            GdkModifierType state, GhosttyKeyAction action, const char *utf8)
{
  GhosttyKeyEvent event = NULL;
  GhosttyKey key;
  GhosttyMods mods;
  gunichar text = 0, unshifted;
  char own_utf8[8];
  size_t utf8_len = 0;
  uint8_t buf[128];
  size_t written = 0;
  GhosttyResult result;

  key = gtk_ghostty_key_from_name (gdk_keyval_name (keyval));
  mods = mods_from_state (state);
  if (utf8 != NULL && utf8[0] != '\0')
    text = g_utf8_get_char (utf8);
  else if (action != GHOSTTY_KEY_ACTION_RELEASE)
    text = gdk_keyval_to_unicode (keyval);
  if (text < 0x20 || text == 0x7f)
    text = 0;  /* control characters are the encoder's job */
  /* Windows reports AltGr as Ctrl+Alt: AltGr+Q on a German layout is "@",
   * which must reach the program as text, not as Ctrl+Alt+@. */
  if ((mods & GHOSTTY_MODS_CTRL) && (mods & GHOSTTY_MODS_ALT) && text != 0
      && !g_unichar_isalpha (text) && g_unichar_isprint (text))
    mods &= ~(GHOSTTY_MODS_CTRL | GHOSTTY_MODS_ALT);
  if (key == GHOSTTY_KEY_UNIDENTIFIED && text == 0)
    return FALSE;
  if (text != 0)
    {
      if (utf8 == NULL || utf8[0] == '\0')
        {
          own_utf8[g_unichar_to_utf8 (text, own_utf8)] = '\0';
          utf8 = own_utf8;
        }
      utf8_len = strlen (utf8);
    }
  unshifted = gdk_keyval_to_unicode (base_keyval (controller, keycode, keyval));

  if (ghostty_key_event_new (NULL, &event) != GHOSTTY_SUCCESS)
    return FALSE;
  ghostty_key_event_set_action (event, action);
  ghostty_key_event_set_key (event, key);
  ghostty_key_event_set_mods (event, mods);
  /* Shift produced the text ('A', '!'): consumed, or kitty sends CSI 97;2u. */
  ghostty_key_event_set_consumed_mods (event, text != 0 ? (mods & GHOSTTY_MODS_SHIFT) : 0);
  if (unshifted >= 0x20)
    ghostty_key_event_set_unshifted_codepoint (event, unshifted);
  if (utf8_len > 0)
    ghostty_key_event_set_utf8 (event, (GhosttyString) { (const uint8_t *) utf8, utf8_len });

  ghostty_key_encoder_set_from_terminal (self->key_encoder, self->terminal);
  if (self->legacy_keyboard)
    {
      /* ConPTY forwards a program's kitty request but cannot parse kitty keys. */
      GhosttyKittyKeyFlags flags = GHOSTTY_KITTY_KEY_DISABLED;
      ghostty_key_encoder_set (self->key_encoder, GHOSTTY_KEY_ENCODER_OPT_KITTY_FLAGS, &flags);
    }
  result = ghostty_key_encoder_encode (self->key_encoder, event, buf, sizeof buf, &written);
  if (written > 0 && action != GHOSTTY_KEY_ACTION_RELEASE)
    scroll_to_bottom (self);  /* typing jumps back to the prompt */
  if (result == GHOSTTY_SUCCESS)
    emit_input (self, buf, written);
  else if (result == GHOSTTY_OUT_OF_SPACE)
    {
      uint8_t *big = g_malloc (written);
      if (ghostty_key_encoder_encode (self->key_encoder, event, big, written, &written) == GHOSTTY_SUCCESS)
        emit_input (self, big, written);
      g_free (big);
    }
  ghostty_key_event_free (event);
  return TRUE;
}

static gboolean
preedit_active (GtkGhosttyTerminal *self)
{
  return self->preedit != NULL && self->preedit[0] != '\0';
}

/* The input method sees every key first. A key it composes with (dead keys,
 * CJK) stays with it and the result goes out as text; a plain character it
 * merely passes through is encoded as a key, so a program that asked for the
 * kitty protocol gets kitty sequences and not raw text (issue #1). */
static gboolean
filter_through_im (GtkGhosttyTerminal *self, GtkEventController *controller, gboolean *raw_sent, char **text)
{
  GdkEvent *event = gtk_event_controller_get_current_event (controller);
  gboolean composing = preedit_active (self), handled;
  *raw_sent = FALSE;
  *text = NULL;
  if (event == NULL)
    return FALSE;
  self->key_commit = g_string_new (NULL);
  handled = gtk_im_context_filter_keypress (self->im, event);
  if (self->key_commit->len > 0)
    {
      /* Passed through means the input method committed exactly what the
       * key types by itself. Anything else (an e key committing é after a
       * dead key; a whole word) it composed. Not every input method shows a
       * preedit while composing, so that alone cannot tell. */
      gunichar own = gdk_keyval_to_unicode (gdk_key_event_get_keyval (event));
      gboolean passed_through = g_utf8_strlen (self->key_commit->str, -1) == 1
                                && g_utf8_get_char (self->key_commit->str) == own;
      if (composing || !passed_through)
        {
          /* A finished composition: text, as the input method meant it. */
          scroll_to_bottom (self);
          emit_input (self, (const guint8 *) self->key_commit->str, self->key_commit->len);
          *raw_sent = TRUE;
        }
      else
        *text = g_strdup (self->key_commit->str);
    }
  g_string_free (self->key_commit, TRUE);
  self->key_commit = NULL;
  return handled;
}

static gboolean
key_is_held (GtkGhosttyTerminal *self, guint keycode)
{
  for (guint i = 0; i < self->held_keys->len; i++)
    if (g_array_index (self->held_keys, guint, i) == keycode)
      return TRUE;
  return FALSE;
}

static void
key_released (GtkGhosttyTerminal *self, guint keycode)
{
  for (guint i = 0; i < self->held_keys->len; i++)
    if (g_array_index (self->held_keys, guint, i) == keycode)
      {
        g_array_remove_index_fast (self->held_keys, i);
        return;
      }
}

static gboolean
on_key_pressed (GtkEventControllerKey *controller, guint keyval, guint keycode,
                GdkModifierType state, GtkGhosttyTerminal *self)
{
  gboolean raw_sent, repeat, handled;
  char *text = NULL;

  if (filter_through_im (self, GTK_EVENT_CONTROLLER (controller), &raw_sent, &text) && text == NULL)
    return TRUE;  /* composing, or the composition already went out */
  if (raw_sent)
    {
      g_free (text);
      return TRUE;
    }
  if (is_modifier_key (keyval))
    {
      g_free (text);
      return FALSE;
    }
  repeat = key_is_held (self, keycode);
  handled = encode_key (self, GTK_EVENT_CONTROLLER (controller), keyval, keycode, state,
                        repeat ? GHOSTTY_KEY_ACTION_REPEAT : GHOSTTY_KEY_ACTION_PRESS, text);
  if (handled && !repeat)
    g_array_append_val (self->held_keys, keycode);
  g_free (text);
  return handled;
}

static void
on_key_released (GtkEventControllerKey *controller, guint keyval, guint keycode,
                 GdkModifierType state, GtkGhosttyTerminal *self)
{
  gboolean raw_sent, was_held = key_is_held (self, keycode);
  char *text = NULL;

  filter_through_im (self, GTK_EVENT_CONTROLLER (controller), &raw_sent, &text);
  g_free (text);
  key_released (self, keycode);
  /* Only keys whose press went out get a release (the encoder sends it only
   * when the program asked for event types). */
  if (was_held && !raw_sent && !is_modifier_key (keyval))
    encode_key (self, GTK_EVENT_CONTROLLER (controller), keyval, keycode, state,
                GHOSTTY_KEY_ACTION_RELEASE, NULL);
}

static void
on_im_commit (GtkIMContext *im, const char *text, GtkGhosttyTerminal *self)
{
  if (self->key_commit != NULL)
    {
      /* Committed while filtering a key: the key handler decides. */
      g_string_append (self->key_commit, text);
      return;
    }
  scroll_to_bottom (self);
  emit_input (self, (const guint8 *) text, strlen (text));
}

static void
send_focus (GtkGhosttyTerminal *self, GhosttyFocusEvent event)
{
  GhosttyTerminalModeConfig mode = { ghostty_mode_new (1004, false), false };
  uint8_t buf[8];
  size_t written = 0;
  if (ghostty_terminal_get (self->terminal, GHOSTTY_TERMINAL_DATA_MODE, &mode) != GHOSTTY_SUCCESS || !mode.value)
    return;
  if (ghostty_focus_encode (event, buf, sizeof buf, &written) == GHOSTTY_SUCCESS)
    emit_input (self, buf, written);
}

static void
on_focus_enter (GtkEventControllerFocus *controller, GtkGhosttyTerminal *self)
{
  self->focused = TRUE;
  gtk_im_context_focus_in (self->im);
  send_focus (self, GHOSTTY_FOCUS_GAINED);
  gtk_widget_queue_draw (GTK_WIDGET (self));
}

static void
on_focus_leave (GtkEventControllerFocus *controller, GtkGhosttyTerminal *self)
{
  self->focused = FALSE;
  gtk_im_context_focus_out (self->im);
  send_focus (self, GHOSTTY_FOCUS_LOST);
  gtk_widget_queue_draw (GTK_WIDGET (self));
}

/* -------------------------------------------------------------- scrolling */

static void
update_adjustment (GtkGhosttyTerminal *self)
{
  GhosttyTerminalScrollbar bar = { 0 };
  if (self->vadjustment == NULL
      || ghostty_terminal_get (self->terminal, GHOSTTY_TERMINAL_DATA_SCROLLBAR, &bar) != GHOSTTY_SUCCESS)
    return;
  self->syncing_adjustment = TRUE;
  gtk_adjustment_configure (self->vadjustment, (double) bar.offset, 0, (double) bar.total, 1,
                            (double) bar.len, (double) bar.len);
  self->syncing_adjustment = FALSE;
}

static void
scroll_viewport (GtkGhosttyTerminal *self, GhosttyTerminalScrollViewport behavior)
{
  ghostty_terminal_scroll_viewport (self->terminal, behavior);
  update_adjustment (self);
  gtk_widget_queue_draw (GTK_WIDGET (self));
}

static void
on_vadjustment_changed (GtkAdjustment *adjustment, GtkGhosttyTerminal *self)
{
  GhosttyTerminalScrollViewport behavior = { GHOSTTY_TERMINAL_SCROLL_VIEWPORT_ROW };
  if (self->syncing_adjustment)
    return;
  behavior.value.row = (size_t) MAX (0.0, gtk_adjustment_get_value (adjustment) + 0.5);
  scroll_viewport (self, behavior);
}

static void
set_adjustment (GtkGhosttyTerminal *self, GtkAdjustment **slot, GtkAdjustment *adjustment, gboolean vertical)
{
  if (adjustment == NULL)
    adjustment = gtk_adjustment_new (0, 0, 0, 0, 0, 0);
  if (*slot == adjustment)
    return;
  if (*slot != NULL)
    {
      g_signal_handlers_disconnect_by_data (*slot, self);
      g_object_unref (*slot);
    }
  *slot = g_object_ref_sink (adjustment);
  if (vertical)
    {
      g_signal_connect (adjustment, "value-changed", G_CALLBACK (on_vadjustment_changed), self);
      update_adjustment (self);
    }
}

static gboolean
viewport_active (GtkGhosttyTerminal *self)
{
  bool active = true;
  ghostty_terminal_get (self->terminal, GHOSTTY_TERMINAL_DATA_VIEWPORT_ACTIVE, &active);
  return active;
}

static void
scroll_to_bottom (GtkGhosttyTerminal *self)
{
  GhosttyTerminalScrollViewport behavior = { GHOSTTY_TERMINAL_SCROLL_VIEWPORT_BOTTOM };
  if (!viewport_active (self))
    scroll_viewport (self, behavior);
}

/* -------------------------------------------------------------- pointer */

static gboolean
mode_enabled (GtkGhosttyTerminal *self, GhosttyMode mode)
{
  GhosttyTerminalModeConfig config = { mode, false };
  return ghostty_terminal_get (self->terminal, GHOSTTY_TERMINAL_DATA_MODE, &config) == GHOSTTY_SUCCESS
         && config.value;
}

static gboolean
reporting_mouse (GtkGhosttyTerminal *self, GdkModifierType state)
{
  bool tracking = false;
  ghostty_terminal_get (self->terminal, GHOSTTY_TERMINAL_DATA_MOUSE_TRACKING, &tracking);
  /* Shift bypasses reporting so text can still be selected (xterm convention). */
  return tracking && !(state & GDK_SHIFT_MASK);
}

static void
viewport_cell (GtkGhosttyTerminal *self, double x, double y, guint *column, guint *row)
{
  *column = (guint) CLAMP (x / MAX (1, self->cell_width), 0, (double) self->columns - 1);
  *row = (guint) CLAMP (y / MAX (1, self->cell_height), 0, (double) self->n_rows - 1);
}

static gboolean
viewport_ref (GtkGhosttyTerminal *self, double x, double y, GhosttyGridRef *ref)
{
  GhosttyPoint point = { GHOSTTY_POINT_TAG_VIEWPORT };
  guint column, row;
  viewport_cell (self, x, y, &column, &row);
  point.value.coordinate.x = (uint16_t) column;
  point.value.coordinate.y = row;
  memset (ref, 0, sizeof *ref);
  return ghostty_terminal_grid_ref (self->terminal, point, ref) == GHOSTTY_SUCCESS;
}

static GhosttyMouseButton
mouse_button (guint gdk_button)
{
  switch (gdk_button)
    {
    case GDK_BUTTON_PRIMARY: return GHOSTTY_MOUSE_BUTTON_LEFT;
    case GDK_BUTTON_MIDDLE: return GHOSTTY_MOUSE_BUTTON_MIDDLE;
    case GDK_BUTTON_SECONDARY: return GHOSTTY_MOUSE_BUTTON_RIGHT;
    case 8: return GHOSTTY_MOUSE_BUTTON_EIGHT;
    case 9: return GHOSTTY_MOUSE_BUTTON_NINE;
    default: return GHOSTTY_MOUSE_BUTTON_UNKNOWN;
    }
}

static void
report_mouse (GtkGhosttyTerminal *self, GhosttyMouseAction action, GhosttyMouseButton button,
              double x, double y, GdkModifierType state)
{
  GhosttyMouseEncoderSize size = GHOSTTY_INIT_SIZED (GhosttyMouseEncoderSize);
  bool pressed = self->buttons_down != 0;
  uint8_t buf[64];
  size_t written = 0;

  size.screen_width = self->columns * (uint32_t) self->cell_width;
  size.screen_height = self->n_rows * (uint32_t) self->cell_height;
  size.cell_width = (uint32_t) self->cell_width;
  size.cell_height = (uint32_t) self->cell_height;
  ghostty_mouse_encoder_set_from_terminal (self->mouse_encoder, self->terminal);
  ghostty_mouse_encoder_set (self->mouse_encoder, GHOSTTY_MOUSE_ENCODER_OPT_SIZE, &size);
  ghostty_mouse_encoder_set (self->mouse_encoder, GHOSTTY_MOUSE_ENCODER_OPT_ANY_BUTTON_PRESSED, &pressed);

  ghostty_mouse_event_set_action (self->mouse_event, action);
  if (button != GHOSTTY_MOUSE_BUTTON_UNKNOWN)
    ghostty_mouse_event_set_button (self->mouse_event, button);
  else
    ghostty_mouse_event_clear_button (self->mouse_event);
  ghostty_mouse_event_set_mods (self->mouse_event, mods_from_state (state));
  ghostty_mouse_event_set_position (self->mouse_event, (GhosttyMousePosition) { (float) x, (float) y });
  if (ghostty_mouse_encoder_encode (self->mouse_encoder, self->mouse_event, buf, sizeof buf, &written)
      == GHOSTTY_SUCCESS)
    emit_input (self, buf, written);
}

static void
set_selection (GtkGhosttyTerminal *self, const GhosttySelection *selection)
{
  ghostty_terminal_set (self->terminal, GHOSTTY_TERMINAL_OPT_SELECTION, selection);
  gtk_widget_queue_draw (GTK_WIDGET (self));
  g_signal_emit (self, signals[SIGNAL_SELECTION_CHANGED], 0);
}

/* The selected text as Ghostty copies it: soft wraps joined, trailing blanks trimmed. */
static char *
selected_text (GtkGhosttyTerminal *self)
{
  GhosttySelection selection = { 0 };
  GhosttyFormatter formatter = NULL;
  GhosttyFormatterFormat emit = GHOSTTY_FORMATTER_FORMAT_PLAIN;
  bool yes = true;
  uint8_t *ptr = NULL;
  size_t len = 0;
  char *text = NULL;

  if (ghostty_terminal_get (self->terminal, GHOSTTY_TERMINAL_DATA_SELECTION, &selection) != GHOSTTY_SUCCESS)
    return NULL;
  if (ghostty_formatter_terminal_new (NULL, &formatter, self->terminal) != GHOSTTY_SUCCESS)
    return NULL;
  ghostty_formatter_set (formatter, GHOSTTY_FORMATTER_OPT_EMIT, &emit);
  ghostty_formatter_set (formatter, GHOSTTY_FORMATTER_OPT_UNWRAP, &yes);
  ghostty_formatter_set (formatter, GHOSTTY_FORMATTER_OPT_TRIM, &yes);
  ghostty_formatter_set (formatter, GHOSTTY_FORMATTER_OPT_SELECTION, &selection);
  if (ghostty_formatter_format_alloc (formatter, NULL, &ptr, &len) == GHOSTTY_SUCCESS)
    {
      text = ptr != NULL ? g_strndup ((const char *) ptr, len) : g_strdup ("");
      ghostty_free (NULL, ptr, len);
    }
  ghostty_formatter_free (formatter);
  return text;
}

static void
copy_to (GtkGhosttyTerminal *self, GdkClipboard *clipboard)
{
  char *text = selected_text (self);
  if (text != NULL && text[0] != '\0')
    gdk_clipboard_set_text (clipboard, text);
  g_free (text);
}

static void
on_clipboard_text (GObject *source, GAsyncResult *result, gpointer data)
{
  GtkGhosttyTerminal *self = data;
  char *text = gdk_clipboard_read_text_finish (GDK_CLIPBOARD (source), result, NULL);
  if (text != NULL)
    {
      gtk_ghostty_terminal_paste_text (self, text);
      g_free (text);
    }
  g_object_unref (self);
}

static void
paste_from (GtkGhosttyTerminal *self, GdkClipboard *clipboard)
{
  gdk_clipboard_read_text_async (clipboard, NULL, on_clipboard_text, g_object_ref (self));
}

static GhosttySelectionGestureGeometry
gesture_geometry (GtkGhosttyTerminal *self)
{
  GhosttySelectionGestureGeometry geometry = { 0 };
  geometry.columns = self->columns;
  geometry.cell_width = (uint32_t) self->cell_width;
  geometry.padding_left = 0;
  geometry.screen_height = self->n_rows * (uint32_t) self->cell_height;
  return geometry;
}

static void stop_autoscroll (GtkGhosttyTerminal *self);

static gboolean
autoscroll_tick (gpointer data)
{
  GtkGhosttyTerminal *self = data;
  GhosttySelectionGestureGeometry geometry = gesture_geometry (self);
  GhosttySurfacePosition position = { self->pointer_x, self->pointer_y };
  GhosttyPointCoordinate cell = { 0 };
  GhosttySelection selection = { 0 };
  guint column, row;

  viewport_cell (self, self->pointer_x, self->pointer_y, &column, &row);
  cell.x = (uint16_t) column;
  cell.y = row;
  ghostty_selection_gesture_event_set (self->gesture_tick, GHOSTTY_SELECTION_GESTURE_EVENT_OPT_VIEWPORT, &cell);
  ghostty_selection_gesture_event_set (self->gesture_tick, GHOSTTY_SELECTION_GESTURE_EVENT_OPT_GEOMETRY, &geometry);
  ghostty_selection_gesture_event_set (self->gesture_tick, GHOSTTY_SELECTION_GESTURE_EVENT_OPT_POSITION, &position);
  if (ghostty_selection_gesture_event (self->gesture, self->terminal, self->gesture_tick, &selection)
      != GHOSTTY_SUCCESS)
    {
      self->autoscroll_id = 0;  /* the gesture cancelled itself */
      return G_SOURCE_REMOVE;
    }
  set_selection (self, &selection);
  update_adjustment (self);
  return G_SOURCE_CONTINUE;
}

static void
stop_autoscroll (GtkGhosttyTerminal *self)
{
  g_clear_handle_id (&self->autoscroll_id, g_source_remove);
}

static void
on_pressed (GtkGestureClick *click, int n_press, double x, double y, GtkGhosttyTerminal *self)
{
  GdkEvent *event = gtk_event_controller_get_current_event (GTK_EVENT_CONTROLLER (click));
  GdkModifierType state = gtk_event_controller_get_current_event_state (GTK_EVENT_CONTROLLER (click));
  guint button = gtk_gesture_single_get_current_button (GTK_GESTURE_SINGLE (click));

  gtk_widget_grab_focus (GTK_WIDGET (self));
  self->pointer_x = x;
  self->pointer_y = y;

  if (reporting_mouse (self, state))
    {
      GhosttyMouseButton ghostty_button = mouse_button (button);
      if (ghostty_button != GHOSTTY_MOUSE_BUTTON_UNKNOWN)
        {
          self->buttons_down |= 1u << button;
          report_mouse (self, GHOSTTY_MOUSE_ACTION_PRESS, ghostty_button, x, y, state);
        }
      return;
    }

  if (button == GDK_BUTTON_PRIMARY)
    {
      GhosttyGridRef ref;
      GhosttySurfacePosition position = { x, y };
      uint64_t time_ns = event != NULL ? (uint64_t) gdk_event_get_time (event) * 1000000u : 0;
      GhosttySelection selection = { 0 };
      if (!viewport_ref (self, x, y, &ref))
        return;
      ghostty_selection_gesture_event_set (self->gesture_press, GHOSTTY_SELECTION_GESTURE_EVENT_OPT_REF, &ref);
      ghostty_selection_gesture_event_set (self->gesture_press, GHOSTTY_SELECTION_GESTURE_EVENT_OPT_POSITION, &position);
      if (time_ns != 0)
        {
          /* Repeat clicks (word, line) follow the desktop's double-click
           * settings; libghostty has no defaults for them. */
          GtkSettings *settings = gtk_widget_get_settings (GTK_WIDGET (self));
          int interval_ms = 400, distance = 5;
          uint64_t interval_ns;
          double max_distance;
          g_object_get (settings, "gtk-double-click-time", &interval_ms,
                        "gtk-double-click-distance", &distance, NULL);
          interval_ns = (uint64_t) MAX (1, interval_ms) * 1000000u;
          max_distance = MAX (1, distance);
          ghostty_selection_gesture_event_set (self->gesture_press, GHOSTTY_SELECTION_GESTURE_EVENT_OPT_TIME_NS, &time_ns);
          ghostty_selection_gesture_event_set (self->gesture_press,
                                               GHOSTTY_SELECTION_GESTURE_EVENT_OPT_REPEAT_INTERVAL_NS, &interval_ns);
          ghostty_selection_gesture_event_set (self->gesture_press,
                                               GHOSTTY_SELECTION_GESTURE_EVENT_OPT_REPEAT_DISTANCE, &max_distance);
        }
      self->selecting = TRUE;
      /* A single press only anchors; double and triple clicks select word and line. */
      if (ghostty_selection_gesture_event (self->gesture, self->terminal, self->gesture_press, &selection)
          == GHOSTTY_SUCCESS)
        set_selection (self, &selection);
      else
        set_selection (self, NULL);
    }
  else if (button == GDK_BUTTON_MIDDLE)
    paste_from (self, gtk_widget_get_primary_clipboard (GTK_WIDGET (self)));
}

static void
on_released (GtkGestureClick *click, int n_press, double x, double y, GtkGhosttyTerminal *self)
{
  GdkModifierType state = gtk_event_controller_get_current_event_state (GTK_EVENT_CONTROLLER (click));
  guint button = gtk_gesture_single_get_current_button (GTK_GESTURE_SINGLE (click));

  if (self->buttons_down & (1u << button))
    {
      self->buttons_down &= ~(1u << button);
      report_mouse (self, GHOSTTY_MOUSE_ACTION_RELEASE, mouse_button (button), x, y, state);
      return;
    }
  if (button == GDK_BUTTON_PRIMARY && self->selecting)
    {
      GhosttyGridRef ref;
      self->selecting = FALSE;
      stop_autoscroll (self);
      if (viewport_ref (self, x, y, &ref))
        ghostty_selection_gesture_event_set (self->gesture_release, GHOSTTY_SELECTION_GESTURE_EVENT_OPT_REF, &ref);
      ghostty_selection_gesture_event (self->gesture, self->terminal, self->gesture_release, NULL);
      copy_to (self, gtk_widget_get_primary_clipboard (GTK_WIDGET (self)));
    }
}

static void
on_motion (GtkEventControllerMotion *motion, double x, double y, GtkGhosttyTerminal *self)
{
  GdkModifierType state = gtk_event_controller_get_current_event_state (GTK_EVENT_CONTROLLER (motion));
  self->pointer_x = x;
  self->pointer_y = y;

  if (self->selecting)
    {
      GhosttyGridRef ref;
      GhosttySurfacePosition position = { x, y };
      GhosttySelectionGestureGeometry geometry = gesture_geometry (self);
      GhosttySelection selection = { 0 };
      GhosttySelectionGestureAutoscroll autoscroll = GHOSTTY_SELECTION_GESTURE_AUTOSCROLL_NONE;
      if (!viewport_ref (self, x, y, &ref))
        return;
      ghostty_selection_gesture_event_set (self->gesture_drag, GHOSTTY_SELECTION_GESTURE_EVENT_OPT_REF, &ref);
      ghostty_selection_gesture_event_set (self->gesture_drag, GHOSTTY_SELECTION_GESTURE_EVENT_OPT_POSITION, &position);
      ghostty_selection_gesture_event_set (self->gesture_drag, GHOSTTY_SELECTION_GESTURE_EVENT_OPT_GEOMETRY, &geometry);
      if (ghostty_selection_gesture_event (self->gesture, self->terminal, self->gesture_drag, &selection)
          == GHOSTTY_SUCCESS)
        set_selection (self, &selection);
      ghostty_selection_gesture_get (self->gesture, self->terminal, GHOSTTY_SELECTION_GESTURE_DATA_AUTOSCROLL,
                                     &autoscroll);
      if (autoscroll != GHOSTTY_SELECTION_GESTURE_AUTOSCROLL_NONE && self->autoscroll_id == 0)
        self->autoscroll_id = g_timeout_add (15, autoscroll_tick, self);
      else if (autoscroll == GHOSTTY_SELECTION_GESTURE_AUTOSCROLL_NONE)
        stop_autoscroll (self);
      return;
    }
  /* Motion reports: the encoder drops what the program's tracking mode does not want. */
  if (reporting_mouse (self, state))
    {
      GhosttyMouseButton held = GHOSTTY_MOUSE_BUTTON_UNKNOWN;
      for (guint b = 1; b <= 9 && held == GHOSTTY_MOUSE_BUTTON_UNKNOWN; b++)
        if (self->buttons_down & (1u << b))
          held = mouse_button (b);
      report_mouse (self, GHOSTTY_MOUSE_ACTION_MOTION, held, x, y, state);
    }
}

static void
send_key (GtkGhosttyTerminal *self, GhosttyKey key)
{
  GhosttyKeyEvent event = NULL;
  uint8_t buf[16];
  size_t written = 0;
  if (ghostty_key_event_new (NULL, &event) != GHOSTTY_SUCCESS)
    return;
  ghostty_key_event_set_action (event, GHOSTTY_KEY_ACTION_PRESS);
  ghostty_key_event_set_key (event, key);
  ghostty_key_encoder_set_from_terminal (self->key_encoder, self->terminal);
  if (self->legacy_keyboard)
    {
      GhosttyKittyKeyFlags flags = GHOSTTY_KITTY_KEY_DISABLED;
      ghostty_key_encoder_set (self->key_encoder, GHOSTTY_KEY_ENCODER_OPT_KITTY_FLAGS, &flags);
    }
  if (ghostty_key_encoder_encode (self->key_encoder, event, buf, sizeof buf, &written) == GHOSTTY_SUCCESS)
    emit_input (self, buf, written);
  ghostty_key_event_free (event);
}

static gboolean
on_scroll (GtkEventControllerScroll *controller, double dx, double dy, GtkGhosttyTerminal *self)
{
  GdkModifierType state = gtk_event_controller_get_current_event_state (GTK_EVENT_CONTROLLER (controller));
  GdkScrollUnit unit = gtk_event_controller_scroll_get_unit (controller);
  double rows = unit == GDK_SCROLL_UNIT_WHEEL ? dy * 3 : dy / MAX (1, self->cell_height);
  int steps;

  self->scroll_remainder += rows;
  steps = (int) self->scroll_remainder;
  if (steps == 0)
    return GDK_EVENT_STOP;
  self->scroll_remainder -= steps;

  if (reporting_mouse (self, state))
    {
      GhosttyMouseButton wheel = steps < 0 ? GHOSTTY_MOUSE_BUTTON_FOUR : GHOSTTY_MOUSE_BUTTON_FIVE;
      for (int i = 0; i < ABS (steps); i++)
        report_mouse (self, GHOSTTY_MOUSE_ACTION_PRESS, wheel, self->pointer_x, self->pointer_y, state);
      return GDK_EVENT_STOP;
    }
  if (mode_enabled (self, GHOSTTY_MODE_ALT_SCREEN_SAVE_CURSOR_CLEAR_ENTER)
      || mode_enabled (self, GHOSTTY_MODE_ALT_SCREEN) || mode_enabled (self, GHOSTTY_MODE_ALT_SCREEN_LEGACY))
    {
      /* No history on the alternate screen: with mode 1007 the wheel becomes
       * arrow keys for the pager or editor. */
      if (mode_enabled (self, GHOSTTY_MODE_MOUSE_ALTERNATE_SCROLL))
        for (int i = 0; i < ABS (steps); i++)
          send_key (self, steps < 0 ? GHOSTTY_KEY_ARROW_UP : GHOSTTY_KEY_ARROW_DOWN);
      return GDK_EVENT_STOP;
    }
  {
    GhosttyTerminalScrollViewport behavior = { GHOSTTY_TERMINAL_SCROLL_VIEWPORT_DELTA };
    behavior.value.delta = steps;
    scroll_viewport (self, behavior);
  }
  return GDK_EVENT_STOP;
}

/* ------------------------------------------------------------ public API */

/**
 * gtk_ghostty_terminal_new:
 *
 * Returns: (transfer floating): a new terminal view
 */
GtkWidget *
gtk_ghostty_terminal_new (void)
{
  return g_object_new (GTK_GHOSTTY_TYPE_TERMINAL, NULL);
}

/**
 * gtk_ghostty_terminal_feed_data:
 * @self: the terminal
 * @data: (array length=length): bytes the program wrote
 * @length: number of bytes
 *
 * Feeds program output into the terminal. Replies the terminal generates
 * are emitted as #GtkGhosttyTerminal::reply after the bytes are processed.
 */
void
gtk_ghostty_terminal_feed_data (GtkGhosttyTerminal *self, const guint8 *data, gsize length)
{
  g_return_if_fail (GTK_GHOSTTY_IS_TERMINAL (self));
  if (length == 0)
    return;
  ghostty_terminal_vt_write (self->terminal, data, length);
  apply_pending_cursor_reset (self);
  flush_effects (self);
  update_adjustment (self);
  gtk_widget_queue_draw (GTK_WIDGET (self));
  g_signal_emit (self, signals[SIGNAL_CONTENTS_CHANGED], 0);
}

/**
 * gtk_ghostty_terminal_feed:
 * @self: the terminal
 * @data: bytes the program wrote
 */
void
gtk_ghostty_terminal_feed (GtkGhosttyTerminal *self, GBytes *data)
{
  gsize length = 0;
  const guint8 *bytes;
  g_return_if_fail (data != NULL);
  bytes = g_bytes_get_data (data, &length);
  gtk_ghostty_terminal_feed_data (self, bytes, length);
}

/**
 * gtk_ghostty_terminal_paste_text:
 * @self: the terminal
 * @text: UTF-8 text to paste
 *
 * Emits @text as input, with control bytes replaced and framed for
 * bracketed paste when the program asked for it. Whether to confirm an
 * unsafe paste first (see gtk_ghostty_terminal_paste_is_safe()) is up to
 * the caller.
 */
void
gtk_ghostty_terminal_paste_text (GtkGhosttyTerminal *self, const char *text)
{
  GhosttyTerminalModeConfig mode = { ghostty_mode_new (2004, false), false };
  char *copy;
  uint8_t *buf;
  size_t length, needed = 0;

  g_return_if_fail (GTK_GHOSTTY_IS_TERMINAL (self));
  g_return_if_fail (text != NULL);
  length = strlen (text);
  if (length == 0)
    return;
  ghostty_terminal_get (self->terminal, GHOSTTY_TERMINAL_DATA_MODE, &mode);
  copy = g_memdup2 (text, length);  /* encoded in place */
  if (ghostty_paste_encode (copy, length, mode.value, NULL, 0, &needed) != GHOSTTY_OUT_OF_SPACE)
    {
      g_free (copy);
      return;
    }
  buf = g_malloc (needed);
  if (ghostty_paste_encode (copy, length, mode.value, buf, needed, &needed) == GHOSTTY_SUCCESS)
    {
      scroll_to_bottom (self);
      emit_input (self, buf, needed);
    }
  g_free (buf);
  g_free (copy);
}

/**
 * gtk_ghostty_terminal_paste_is_safe:
 * @text: UTF-8 text
 *
 * Returns: %FALSE when @text could run commands on its own (newlines,
 *   or an attempt to end bracketed paste early)
 */
gboolean
gtk_ghostty_terminal_paste_is_safe (const char *text)
{
  g_return_val_if_fail (text != NULL, TRUE);
  return ghostty_paste_is_safe (text, strlen (text));
}

/* --------------------------------------------------------- appearance */

static GhosttyColorRgb
to_rgb (const GdkRGBA *color)
{
  return (GhosttyColorRgb) { (uint8_t) CLAMP (color->red * 255 + 0.5, 0, 255),
                             (uint8_t) CLAMP (color->green * 255 + 0.5, 0, 255),
                             (uint8_t) CLAMP (color->blue * 255 + 0.5, 0, 255) };
}

/**
 * gtk_ghostty_terminal_set_colors:
 * @self: the terminal
 * @foreground: (nullable): default text colour, or %NULL for the built-in one
 * @background: (nullable): default background, or %NULL for the built-in one
 * @cursor: (nullable): cursor colour, or %NULL to follow the text
 * @palette: (array length=palette_size) (nullable): the first palette
 *   entries (16 for a usual theme); the rest keep their defaults
 * @palette_size: number of palette entries, at most 256
 *
 * Sets the theme. These are defaults: programs can still change colours
 * with OSC sequences, as in other terminals.
 */
void
gtk_ghostty_terminal_set_colors (GtkGhosttyTerminal *self, const GdkRGBA *foreground, const GdkRGBA *background,
                                 const GdkRGBA *cursor, const GdkRGBA *palette, gsize palette_size)
{
  GhosttyColorRgb fg, bg, cur, colors[256];
  g_return_if_fail (GTK_GHOSTTY_IS_TERMINAL (self));
  /* Never leave the defaults unset: colour queries (OSC 10/11), which vim
   * and others use to pick a light or dark look, go unanswered without. */
  fg = foreground != NULL ? to_rgb (foreground) : self->builtin_fg;
  bg = background != NULL ? to_rgb (background) : self->builtin_bg;
  if (cursor != NULL)
    cur = to_rgb (cursor);
  ghostty_terminal_set (self->terminal, GHOSTTY_TERMINAL_OPT_COLOR_FOREGROUND, &fg);
  ghostty_terminal_set (self->terminal, GHOSTTY_TERMINAL_OPT_COLOR_BACKGROUND, &bg);
  ghostty_terminal_set (self->terminal, GHOSTTY_TERMINAL_OPT_COLOR_CURSOR, cursor != NULL ? &cur : NULL);
  if (palette != NULL && palette_size > 0)
    {
      /* Start from the built-in palette, so a 16-colour theme keeps the
       * 256-colour cube and greys. */
      ghostty_terminal_set (self->terminal, GHOSTTY_TERMINAL_OPT_COLOR_PALETTE, NULL);
      ghostty_terminal_get (self->terminal, GHOSTTY_TERMINAL_DATA_COLOR_PALETTE_DEFAULT, colors);
      for (gsize i = 0; i < MIN (palette_size, 256); i++)
        colors[i] = to_rgb (&palette[i]);
      ghostty_terminal_set (self->terminal, GHOSTTY_TERMINAL_OPT_COLOR_PALETTE, colors);
    }
  else
    ghostty_terminal_set (self->terminal, GHOSTTY_TERMINAL_OPT_COLOR_PALETTE, NULL);
  g_ptr_array_set_size (self->row_cache, 0);  /* resolved colours are cached per row */
  gtk_widget_queue_draw (GTK_WIDGET (self));
}

/* Whether the parser is between sequences (and not inside a UTF-8
 * character), so bytes of our own cannot split one of the program's. */
static gboolean
parser_at_ground (GtkGhosttyTerminal *self)
{
  GhosttyTerminalWriteStop stop = GHOSTTY_INIT_SIZED (GhosttyTerminalWriteStop);
  return ghostty_terminal_vt_write_until (self->terminal, NULL, 0, GHOSTTY_TERMINAL_BOUNDARY_GROUND, &stop)
         == GHOSTTY_SUCCESS;
}

static void
apply_pending_cursor_reset (GtkGhosttyTerminal *self)
{
  if (!self->cursor_reset_pending || !parser_at_ground (self))
    return;
  self->cursor_reset_pending = FALSE;
  ghostty_terminal_vt_write (self->terminal, (const uint8_t *) "\x1b[0 q", 5);
  flush_effects (self);
  gtk_widget_queue_draw (GTK_WIDGET (self));
}

static void
apply_cursor_style (GtkGhosttyTerminal *self)
{
  GhosttyCursorStyle style = self->cursor_shape == GTK_GHOSTTY_CURSOR_SHAPE_IBEAM ? GHOSTTY_CURSOR_STYLE_BAR
                             : self->cursor_shape == GTK_GHOSTTY_CURSOR_SHAPE_UNDERLINE ? GHOSTTY_CURSOR_STYLE_UNDERLINE
                             : GHOSTTY_CURSOR_STYLE_BLOCK;
  bool blink = self->cursor_blink;
  ghostty_terminal_set (self->terminal, GHOSTTY_TERMINAL_OPT_DEFAULT_CURSOR_STYLE, &style);
  ghostty_terminal_set (self->terminal, GHOSTTY_TERMINAL_OPT_DEFAULT_CURSOR_BLINK, &blink);
  /* The defaults apply on a cursor-style reset (DECSCUSR 0). Doing one now
   * would land inside a sequence the program has only partly sent, so it
   * waits for the parser to be between sequences. */
  self->cursor_reset_pending = TRUE;
  apply_pending_cursor_reset (self);
}

/**
 * gtk_ghostty_terminal_set_cursor_shape:
 * @self: the terminal
 * @shape: the default cursor shape
 *
 * Sets the shape the cursor has unless the program chooses another
 * (DECSCUSR). Applied as soon as the program is between escape sequences.
 */
void
gtk_ghostty_terminal_set_cursor_shape (GtkGhosttyTerminal *self, GtkGhosttyCursorShape shape)
{
  g_return_if_fail (GTK_GHOSTTY_IS_TERMINAL (self));
  self->cursor_shape = shape;
  apply_cursor_style (self);
}

/**
 * gtk_ghostty_terminal_get_cursor_shape:
 * @self: the terminal
 *
 * Returns: the default cursor shape set with
 *   gtk_ghostty_terminal_set_cursor_shape()
 */
GtkGhosttyCursorShape
gtk_ghostty_terminal_get_cursor_shape (GtkGhosttyTerminal *self)
{
  g_return_val_if_fail (GTK_GHOSTTY_IS_TERMINAL (self), GTK_GHOSTTY_CURSOR_SHAPE_BLOCK);
  return self->cursor_shape;
}

/**
 * gtk_ghostty_terminal_set_cursor_blink:
 * @self: the terminal
 * @blink: whether the cursor blinks by default
 *
 * Recorded as the terminal's default; the widget does not animate the
 * cursor yet.
 */
void
gtk_ghostty_terminal_set_cursor_blink (GtkGhosttyTerminal *self, gboolean blink)
{
  g_return_if_fail (GTK_GHOSTTY_IS_TERMINAL (self));
  self->cursor_blink = !!blink;
  apply_cursor_style (self);
}

/**
 * gtk_ghostty_terminal_set_scrollback_lines:
 * @self: the terminal
 * @lines: rows of history to keep (approximately: whole pages are pruned)
 */
void
gtk_ghostty_terminal_set_scrollback_lines (GtkGhosttyTerminal *self, guint lines)
{
  size_t max_lines = lines, max_bytes = lines > 0 ? SIZE_MAX : 0;
  g_return_if_fail (GTK_GHOSTTY_IS_TERMINAL (self));
  ghostty_terminal_set (self->terminal, GHOSTTY_TERMINAL_OPT_SCROLLBACK_MAX_BYTES, &max_bytes);
  if (lines > 0)
    ghostty_terminal_set (self->terminal, GHOSTTY_TERMINAL_OPT_SCROLLBACK_MAX_LINES, &max_lines);
  update_adjustment (self);
}

/**
 * gtk_ghostty_terminal_set_font_scale:
 * @self: the terminal
 * @scale: zoom factor applied to the font size (1.0 is the font's own)
 */
void
gtk_ghostty_terminal_set_font_scale (GtkGhosttyTerminal *self, double scale)
{
  g_return_if_fail (GTK_GHOSTTY_IS_TERMINAL (self));
  scale = CLAMP (scale, 0.25, 4.0);
  if (scale == self->font_scale)
    return;
  self->font_scale = scale;
  self->metrics_valid = FALSE;
  gtk_widget_queue_resize (GTK_WIDGET (self));
}

/**
 * gtk_ghostty_terminal_get_font_scale:
 * @self: the terminal
 *
 * Returns: the zoom factor set with gtk_ghostty_terminal_set_font_scale()
 */
double
gtk_ghostty_terminal_get_font_scale (GtkGhosttyTerminal *self)
{
  g_return_val_if_fail (GTK_GHOSTTY_IS_TERMINAL (self), 1.0);
  return self->font_scale;
}

/* ------------------------------------------------------------- search */

/* The whole screen (history included) as plain text, one line per row. */
static char **
screen_lines (GtkGhosttyTerminal *self)
{
  GhosttyFormatter formatter = NULL;
  bool no = false;
  uint8_t *ptr = NULL;
  size_t len = 0;
  char *text, **lines;
  if (ghostty_formatter_terminal_new (NULL, &formatter, self->terminal) != GHOSTTY_SUCCESS)
    return g_new0 (char *, 1);
  ghostty_formatter_set (formatter, GHOSTTY_FORMATTER_OPT_UNWRAP, &no);
  ghostty_formatter_set (formatter, GHOSTTY_FORMATTER_OPT_TRIM, &no);
  ghostty_formatter_format_alloc (formatter, NULL, &ptr, &len);
  text = ptr != NULL ? g_strndup ((const char *) ptr, len) : g_strdup ("");
  ghostty_free (NULL, ptr, len);
  ghostty_formatter_free (formatter);
  lines = g_strsplit (text, "\n", -1);
  g_free (text);
  return lines;
}

/* Cell column of byte offset @offset in @line, by the terminal's widths. */
static int
column_of (const char *line, int offset)
{
  glong n = 0;
  gunichar *cps = g_utf8_to_ucs4_fast (line, offset, &n);
  int column = 0;
  for (glong i = 0; i < n;)
    {
      uint8_t width = 1;
      size_t count = ghostty_unicode_grapheme_width ((const uint32_t *) cps + i, (size_t) (n - i), &width);
      column += width;
      i += count > 0 ? (glong) count : 1;
    }
  g_free (cps);
  return column;
}

/**
 * gtk_ghostty_terminal_search_set_pattern:
 * @self: the terminal
 * @pattern: (nullable): text to find, or %NULL to end the search
 * @regex: whether @pattern is a regular expression (PCRE)
 * @case_sensitive: whether case must match
 *
 * Returns: %FALSE when @pattern is not a valid regular expression (it is
 *   then searched as plain text)
 */
gboolean
gtk_ghostty_terminal_search_set_pattern (GtkGhosttyTerminal *self, const char *pattern, gboolean regex,
                                         gboolean case_sensitive)
{
  GRegexCompileFlags flags = G_REGEX_OPTIMIZE | (case_sensitive ? 0 : G_REGEX_CASELESS);
  gboolean valid = TRUE;
  g_return_val_if_fail (GTK_GHOSTTY_IS_TERMINAL (self), FALSE);
  g_clear_pointer (&self->search, g_regex_unref);
  self->search_row = self->search_column = -1;
  if (self->plain_search != NULL)
    ghostty_search_set (self->plain_search, GHOSTTY_SEARCH_OPT_NEEDLE, NULL);
  if (pattern == NULL || pattern[0] == '\0')
    return TRUE;
  if (!regex && !case_sensitive)
    {
      /* The common find bar: libghostty's own search, which follows
       * history, the alternate screen and new output incrementally. Its
       * matching ignores ASCII case only. */
      GhosttyString needle = { (const uint8_t *) pattern, strlen (pattern) };
      if (self->plain_search != NULL
          || ghostty_search_new (NULL, &self->plain_search, self->terminal) == GHOSTTY_SUCCESS)
        {
          ghostty_search_set (self->plain_search, GHOSTTY_SEARCH_OPT_NEEDLE, &needle);
          return TRUE;
        }
    }
  if (regex)
    self->search = g_regex_new (pattern, flags, 0, NULL);
  if (self->search == NULL)
    {
      char *escaped = g_regex_escape_string (pattern, -1);
      valid = !regex;
      self->search = g_regex_new (escaped, flags, 0, NULL);
      g_free (escaped);
    }
  return valid;
}

static gboolean
select_hit (GtkGhosttyTerminal *self, int row, int start_column, int end_column)
{
  GhosttyPoint start = { GHOSTTY_POINT_TAG_SCREEN }, end = { GHOSTTY_POINT_TAG_SCREEN };
  GhosttySelection selection = { 0 };
  GhosttyTerminalScrollbar bar = { 0 };
  start.value.coordinate.x = (uint16_t) MIN (start_column, (int) self->columns - 1);
  start.value.coordinate.y = (uint32_t) row;
  end.value.coordinate.x = (uint16_t) MIN (MAX (start_column, end_column - 1), (int) self->columns - 1);
  end.value.coordinate.y = (uint32_t) row;
  if (ghostty_terminal_grid_ref (self->terminal, start, &selection.start) != GHOSTTY_SUCCESS
      || ghostty_terminal_grid_ref (self->terminal, end, &selection.end) != GHOSTTY_SUCCESS)
    return FALSE;
  set_selection (self, &selection);
  /* Bring the row into view, centred, unless it already is. */
  ghostty_terminal_get (self->terminal, GHOSTTY_TERMINAL_DATA_SCROLLBAR, &bar);
  if ((uint64_t) row < bar.offset || (uint64_t) row >= bar.offset + bar.len)
    {
      GhosttyTerminalScrollViewport behavior = { GHOSTTY_TERMINAL_SCROLL_VIEWPORT_ROW };
      behavior.value.row = (size_t) MAX (0, row - (int) self->n_rows / 2);
      scroll_viewport (self, behavior);
    }
  return TRUE;
}

/**
 * gtk_ghostty_terminal_search_find:
 * @self: the terminal
 * @backwards: search towards older output
 *
 * Finds the next hit after (or before) the previous one, wrapping around,
 * selects it and scrolls it into view.
 *
 * Returns: whether something was found
 */
gboolean
gtk_ghostty_terminal_search_find (GtkGhosttyTerminal *self, gboolean backwards)
{
  char **lines;
  int n, start_row, found_row = -1, found_start = 0, found_end = 0;
  g_return_val_if_fail (GTK_GHOSTTY_IS_TERMINAL (self), FALSE);
  if (self->search == NULL && self->plain_search != NULL)
    {
      GhosttySelection match = { 0 };
      /* libghostty's "next" walks towards older output. */
      if ((backwards ? ghostty_search_select_next (self->plain_search)
                     : ghostty_search_select_prev (self->plain_search)) != GHOSTTY_SUCCESS
          || ghostty_search_get (self->plain_search, GHOSTTY_SEARCH_DATA_SELECTED_MATCH, &match) != GHOSTTY_SUCCESS)
        return FALSE;
      set_selection (self, &match);
      update_adjustment (self);  /* the search scrolled the viewport if needed */
      return TRUE;
    }
  if (self->search == NULL)
    return FALSE;
  lines = screen_lines (self);
  n = (int) g_strv_length (lines);
  if (n == 0)
    {
      g_strfreev (lines);
      return FALSE;
    }
  start_row = self->search_row >= 0 ? MIN (self->search_row, n - 1) : (backwards ? n - 1 : 0);
  for (int step = 0; step <= n && found_row < 0; step++)
    {
      int row = backwards ? (start_row - step + n) % n : (start_row + step) % n;
      GMatchInfo *match = NULL;
      int best_start = -1, best_end = -1;
      g_regex_match (self->search, lines[row], 0, &match);
      while (g_match_info_matches (match))
        {
          int s, e, column;
          g_match_info_fetch_pos (match, 0, &s, &e);
          if (e > s)
            {
              column = column_of (lines[row], s);
              gboolean after = step > 0 || self->search_row < 0 || column > self->search_column;
              gboolean before = step > 0 || self->search_row < 0 || column < self->search_column;
              if (!backwards && after)
                {
                  best_start = s;
                  best_end = e;
                  break;
                }
              if (backwards && before)
                {
                  best_start = s;
                  best_end = e;  /* keep going: the last one before wins */
                }
            }
          g_match_info_next (match, NULL);
        }
      g_match_info_free (match);
      if (best_start >= 0)
        {
          found_row = row;
          found_start = column_of (lines[row], best_start);
          found_end = column_of (lines[row], best_end);
        }
    }
  g_strfreev (lines);
  if (found_row < 0)
    return FALSE;
  self->search_row = found_row;
  self->search_column = found_start;
  return select_hit (self, found_row, found_start, found_end);
}

/* -------------------------------------------------------------- links */

/**
 * gtk_ghostty_terminal_hyperlink_at:
 * @self: the terminal
 * @x: widget x
 * @y: widget y
 *
 * Returns: (transfer full) (nullable): the OSC 8 link of the cell under
 *   (@x, @y), or %NULL
 */
char *
gtk_ghostty_terminal_hyperlink_at (GtkGhosttyTerminal *self, double x, double y)
{
  GhosttyGridRef ref;
  char buf[512];
  size_t length = 0;
  GhosttyResult result;
  g_return_val_if_fail (GTK_GHOSTTY_IS_TERMINAL (self), NULL);
  if (!viewport_ref (self, x, y, &ref))
    return NULL;
  result = ghostty_grid_ref_hyperlink_uri (&ref, (uint8_t *) buf, sizeof buf, &length);
  if (result == GHOSTTY_OUT_OF_SPACE && length <= 65536)
    {
      char *big = g_malloc (length);
      char *uri = NULL;
      if (ghostty_grid_ref_hyperlink_uri (&ref, (uint8_t *) big, length, &length) == GHOSTTY_SUCCESS && length > 0)
        uri = g_strndup (big, length);
      g_free (big);
      return uri;
    }
  return result == GHOSTTY_SUCCESS && length > 0 ? g_strndup (buf, length) : NULL;
}

/**
 * gtk_ghostty_terminal_get_line_at:
 * @self: the terminal
 * @x: widget x
 * @y: widget y
 * @column: (out) (optional): the cell column under @x
 *
 * For finding plain-text links: the text of the row under (@x, @y).
 *
 * Returns: (transfer full): the row's text
 */
char *
gtk_ghostty_terminal_get_line_at (GtkGhosttyTerminal *self, double x, double y, guint *column)
{
  GhosttyTerminalScrollbar bar = { 0 };
  char **lines, *text;
  guint cell_column, viewport_row;
  guint64 row;
  g_return_val_if_fail (GTK_GHOSTTY_IS_TERMINAL (self), NULL);
  viewport_cell (self, x, y, &cell_column, &viewport_row);
  if (column != NULL)
    *column = cell_column;
  ghostty_terminal_get (self->terminal, GHOSTTY_TERMINAL_DATA_SCROLLBAR, &bar);
  row = bar.offset + viewport_row;
  lines = screen_lines (self);
  text = row < g_strv_length (lines) ? g_strdup (lines[row]) : g_strdup ("");
  g_strfreev (lines);
  return text;
}

/**
 * gtk_ghostty_terminal_get_selected_text:
 * @self: the terminal
 *
 * Returns: (transfer full) (nullable): the selection as Ghostty copies it
 *   (soft-wrapped lines joined, trailing blanks trimmed), or %NULL
 */
char *
gtk_ghostty_terminal_get_selected_text (GtkGhosttyTerminal *self)
{
  g_return_val_if_fail (GTK_GHOSTTY_IS_TERMINAL (self), NULL);
  return selected_text (self);
}

/**
 * gtk_ghostty_terminal_has_selection:
 * @self: the terminal
 *
 * Returns: whether text is selected
 */
gboolean
gtk_ghostty_terminal_has_selection (GtkGhosttyTerminal *self)
{
  GhosttySelection selection = { 0 };
  g_return_val_if_fail (GTK_GHOSTTY_IS_TERMINAL (self), FALSE);
  return ghostty_terminal_get (self->terminal, GHOSTTY_TERMINAL_DATA_SELECTION, &selection) == GHOSTTY_SUCCESS;
}

/**
 * gtk_ghostty_terminal_select_all:
 * @self: the terminal
 *
 * Selects the whole screen, history included.
 */
void
gtk_ghostty_terminal_select_all (GtkGhosttyTerminal *self)
{
  GhosttySelection selection = { 0 };
  g_return_if_fail (GTK_GHOSTTY_IS_TERMINAL (self));
  if (ghostty_terminal_select_all (self->terminal, &selection) == GHOSTTY_SUCCESS)
    set_selection (self, &selection);
}

/**
 * gtk_ghostty_terminal_unselect_all:
 * @self: the terminal
 *
 * Clears the selection.
 */
void
gtk_ghostty_terminal_unselect_all (GtkGhosttyTerminal *self)
{
  g_return_if_fail (GTK_GHOSTTY_IS_TERMINAL (self));
  set_selection (self, NULL);
}

/**
 * gtk_ghostty_terminal_copy_clipboard:
 * @self: the terminal
 *
 * Copies the selection to the clipboard. Bind it to a shortcut of your
 * choice (Ctrl+Shift+C is common); the widget binds none.
 */
void
gtk_ghostty_terminal_copy_clipboard (GtkGhosttyTerminal *self)
{
  g_return_if_fail (GTK_GHOSTTY_IS_TERMINAL (self));
  copy_to (self, gtk_widget_get_clipboard (GTK_WIDGET (self)));
}

/**
 * gtk_ghostty_terminal_paste_clipboard:
 * @self: the terminal
 *
 * Reads the clipboard and pastes it as gtk_ghostty_terminal_paste_text()
 * does, without asking about unsafe text: check
 * gtk_ghostty_terminal_paste_is_safe() first if you want to confirm.
 */
void
gtk_ghostty_terminal_paste_clipboard (GtkGhosttyTerminal *self)
{
  g_return_if_fail (GTK_GHOSTTY_IS_TERMINAL (self));
  paste_from (self, gtk_widget_get_clipboard (GTK_WIDGET (self)));
}

/**
 * gtk_ghostty_terminal_scroll_lines:
 * @self: the terminal
 * @lines: rows to scroll, negative towards older output
 */
void
gtk_ghostty_terminal_scroll_lines (GtkGhosttyTerminal *self, int lines)
{
  GhosttyTerminalScrollViewport behavior = { GHOSTTY_TERMINAL_SCROLL_VIEWPORT_DELTA };
  g_return_if_fail (GTK_GHOSTTY_IS_TERMINAL (self));
  behavior.value.delta = lines;
  scroll_viewport (self, behavior);
}

/**
 * gtk_ghostty_terminal_scroll_to_bottom:
 * @self: the terminal
 *
 * Scrolls the view back to the newest output (the program's screen).
 */
void
gtk_ghostty_terminal_scroll_to_bottom (GtkGhosttyTerminal *self)
{
  g_return_if_fail (GTK_GHOSTTY_IS_TERMINAL (self));
  scroll_to_bottom (self);
}

/**
 * gtk_ghostty_terminal_get_text:
 * @self: the terminal
 *
 * Returns: (transfer full): the screen and its history as plain text
 */
char *
gtk_ghostty_terminal_get_text (GtkGhosttyTerminal *self)
{
  GhosttyFormatter formatter = NULL;
  uint8_t *ptr = NULL;
  size_t len = 0;
  char *text;

  g_return_val_if_fail (GTK_GHOSTTY_IS_TERMINAL (self), NULL);
  if (ghostty_formatter_terminal_new (NULL, &formatter, self->terminal) != GHOSTTY_SUCCESS)
    return g_strdup ("");
  if (ghostty_formatter_format_alloc (formatter, NULL, &ptr, &len) != GHOSTTY_SUCCESS)
    {
      ghostty_formatter_free (formatter);
      return g_strdup ("");
    }
  /* Empty output comes back as a NULL buffer. */
  text = ptr != NULL ? g_strndup ((const char *) ptr, len) : g_strdup ("");
  ghostty_free (NULL, ptr, len);
  ghostty_formatter_free (formatter);
  return text;
}

/**
 * gtk_ghostty_terminal_get_columns:
 * @self: the terminal
 *
 * Returns: the number of columns in the grid
 */
guint
gtk_ghostty_terminal_get_columns (GtkGhosttyTerminal *self)
{
  g_return_val_if_fail (GTK_GHOSTTY_IS_TERMINAL (self), 0);
  return self->columns;
}

/**
 * gtk_ghostty_terminal_get_rows:
 * @self: the terminal
 *
 * Returns: the number of visible rows in the grid
 */
guint
gtk_ghostty_terminal_get_rows (GtkGhosttyTerminal *self)
{
  g_return_val_if_fail (GTK_GHOSTTY_IS_TERMINAL (self), 0);
  return self->n_rows;
}

/**
 * gtk_ghostty_terminal_set_grid_size:
 * @self: the terminal
 * @columns: columns
 * @rows: rows
 *
 * Resizes the terminal grid. A widget that gets allocated sets its own size
 * from the allocation; this is for terminals that are not shown (tests,
 * headless use) or before the first allocation.
 */
void
gtk_ghostty_terminal_set_grid_size (GtkGhosttyTerminal *self, guint columns, guint rows)
{
  g_return_if_fail (GTK_GHOSTTY_IS_TERMINAL (self));
  ensure_metrics (self);
  apply_grid_size (self, columns, rows);
}

/**
 * gtk_ghostty_terminal_get_title:
 * @self: the terminal
 *
 * The window title the program set (OSC 0 or 2).
 *
 * Returns: (nullable): the title, or %NULL before the program sets one
 */
const char *
gtk_ghostty_terminal_get_title (GtkGhosttyTerminal *self)
{
  g_return_val_if_fail (GTK_GHOSTTY_IS_TERMINAL (self), NULL);
  return self->title;
}

/**
 * gtk_ghostty_terminal_set_font_desc:
 * @self: the terminal
 * @font_desc: (nullable): a monospace font, or %NULL for the default one
 *
 * Sets the font. A description without a size gets 11 points. The grid is
 * recomputed, so #GtkGhosttyTerminal::size-changed follows once allocated.
 */
void
gtk_ghostty_terminal_set_font_desc (GtkGhosttyTerminal *self, const PangoFontDescription *font_desc)
{
  g_return_if_fail (GTK_GHOSTTY_IS_TERMINAL (self));
  g_clear_pointer (&self->font, pango_font_description_free);
  self->font = font_desc != NULL ? pango_font_description_copy (font_desc)
                                 : pango_font_description_from_string (DEFAULT_FONT);
  if (pango_font_description_get_size (self->font) == 0)
    pango_font_description_set_size (self->font, 11 * PANGO_SCALE);
  self->metrics_valid = FALSE;
  g_object_notify_by_pspec (G_OBJECT (self), properties[PROP_FONT_DESC]);
  gtk_widget_queue_resize (GTK_WIDGET (self));
}

/**
 * gtk_ghostty_terminal_get_font_desc:
 * @self: the terminal
 *
 * Returns: (transfer full): the font
 */
PangoFontDescription *
gtk_ghostty_terminal_get_font_desc (GtkGhosttyTerminal *self)
{
  g_return_val_if_fail (GTK_GHOSTTY_IS_TERMINAL (self), NULL);
  return pango_font_description_copy (self->font);
}

/**
 * gtk_ghostty_terminal_get_legacy_keyboard:
 * @self: the terminal
 *
 * Returns: the #GtkGhosttyTerminal:legacy-keyboard property
 */
gboolean
gtk_ghostty_terminal_get_legacy_keyboard (GtkGhosttyTerminal *self)
{
  g_return_val_if_fail (GTK_GHOSTTY_IS_TERMINAL (self), FALSE);
  return self->legacy_keyboard;
}

/**
 * gtk_ghostty_terminal_set_legacy_keyboard:
 * @self: the terminal
 * @legacy: whether to ignore the kitty keyboard protocol
 *
 * Set this when the byte stream goes through Windows ConPTY: it forwards a
 * program's kitty keyboard request but cannot parse kitty-encoded keys.
 */
void
gtk_ghostty_terminal_set_legacy_keyboard (GtkGhosttyTerminal *self, gboolean legacy)
{
  g_return_if_fail (GTK_GHOSTTY_IS_TERMINAL (self));
  legacy = !!legacy;
  if (self->legacy_keyboard == legacy)
    return;
  self->legacy_keyboard = legacy;
  g_object_notify_by_pspec (G_OBJECT (self), properties[PROP_LEGACY_KEYBOARD]);
}

/**
 * gtk_ghostty_terminal_get_pull_history_on_grow:
 * @self: the terminal
 *
 * Returns: the #GtkGhosttyTerminal:pull-history-on-grow property
 */
gboolean
gtk_ghostty_terminal_get_pull_history_on_grow (GtkGhosttyTerminal *self)
{
  g_return_val_if_fail (GTK_GHOSTTY_IS_TERMINAL (self), TRUE);
  return self->pull_history_on_grow;
}

/**
 * gtk_ghostty_terminal_set_pull_history_on_grow:
 * @self: the terminal
 * @pull: whether growing the terminal brings scrollback rows back
 *
 * Turn this off when the program side keeps its own screen without
 * scrollback and repaints it after a resize (Windows ConPTY).
 */
void
gtk_ghostty_terminal_set_pull_history_on_grow (GtkGhosttyTerminal *self, gboolean pull)
{
  bool value;
  g_return_if_fail (GTK_GHOSTTY_IS_TERMINAL (self));
  pull = !!pull;
  if (self->pull_history_on_grow == pull)
    return;
  self->pull_history_on_grow = pull;
  value = pull;
  ghostty_terminal_set (self->terminal, GHOSTTY_TERMINAL_OPT_RESIZE_PULL_SCROLLBACK, &value);
  g_object_notify_by_pspec (G_OBJECT (self), properties[PROP_PULL_HISTORY_ON_GROW]);
}

/* -------------------------------------------------------------- GObject */

static void
gtk_ghostty_terminal_get_property (GObject *object, guint prop_id, GValue *value, GParamSpec *pspec)
{
  GtkGhosttyTerminal *self = GTK_GHOSTTY_TERMINAL (object);
  switch (prop_id)
    {
    case PROP_COLUMNS: g_value_set_uint (value, self->columns); break;
    case PROP_ROWS: g_value_set_uint (value, self->n_rows); break;
    case PROP_TITLE: g_value_set_string (value, self->title); break;
    case PROP_LEGACY_KEYBOARD: g_value_set_boolean (value, self->legacy_keyboard); break;
    case PROP_PULL_HISTORY_ON_GROW: g_value_set_boolean (value, self->pull_history_on_grow); break;
    case PROP_FONT_DESC: g_value_take_boxed (value, pango_font_description_copy (self->font)); break;
    case PROP_ALLOW_CLIPBOARD_WRITE: g_value_set_boolean (value, self->allow_clipboard_write); break;
    case PROP_HADJUSTMENT: g_value_set_object (value, self->hadjustment); break;
    case PROP_VADJUSTMENT: g_value_set_object (value, self->vadjustment); break;
    case PROP_HSCROLL_POLICY: g_value_set_enum (value, self->hscroll_policy); break;
    case PROP_VSCROLL_POLICY: g_value_set_enum (value, self->vscroll_policy); break;
    default: G_OBJECT_WARN_INVALID_PROPERTY_ID (object, prop_id, pspec);
    }
}

static void
gtk_ghostty_terminal_set_property (GObject *object, guint prop_id, const GValue *value, GParamSpec *pspec)
{
  GtkGhosttyTerminal *self = GTK_GHOSTTY_TERMINAL (object);
  switch (prop_id)
    {
    case PROP_LEGACY_KEYBOARD: gtk_ghostty_terminal_set_legacy_keyboard (self, g_value_get_boolean (value)); break;
    case PROP_PULL_HISTORY_ON_GROW:
      gtk_ghostty_terminal_set_pull_history_on_grow (self, g_value_get_boolean (value));
      break;
    case PROP_FONT_DESC: gtk_ghostty_terminal_set_font_desc (self, g_value_get_boxed (value)); break;
    case PROP_ALLOW_CLIPBOARD_WRITE:
      if (self->allow_clipboard_write != g_value_get_boolean (value))
        {
          self->allow_clipboard_write = g_value_get_boolean (value);
          g_object_notify_by_pspec (object, pspec);
        }
      break;
    case PROP_HADJUSTMENT: set_adjustment (self, &self->hadjustment, g_value_get_object (value), FALSE); break;
    case PROP_VADJUSTMENT: set_adjustment (self, &self->vadjustment, g_value_get_object (value), TRUE); break;
    case PROP_HSCROLL_POLICY: self->hscroll_policy = g_value_get_enum (value); break;
    case PROP_VSCROLL_POLICY: self->vscroll_policy = g_value_get_enum (value); break;
    default: G_OBJECT_WARN_INVALID_PROPERTY_ID (object, prop_id, pspec);
    }
}

static void
gtk_ghostty_terminal_realize (GtkWidget *widget)
{
  GtkGhosttyTerminal *self = GTK_GHOSTTY_TERMINAL (widget);
  GTK_WIDGET_CLASS (gtk_ghostty_terminal_parent_class)->realize (widget);
  gtk_im_context_set_client_widget (self->im, widget);
}

static void
gtk_ghostty_terminal_unrealize (GtkWidget *widget)
{
  GtkGhosttyTerminal *self = GTK_GHOSTTY_TERMINAL (widget);
  gtk_im_context_set_client_widget (self->im, NULL);
  GTK_WIDGET_CLASS (gtk_ghostty_terminal_parent_class)->unrealize (widget);
}

static void
gtk_ghostty_terminal_dispose (GObject *object)
{
  GtkGhosttyTerminal *self = GTK_GHOSTTY_TERMINAL (object);
  if (self->im != NULL)
    gtk_im_context_set_client_widget (self->im, NULL);
  g_clear_object (&self->im);
  stop_autoscroll (self);
  if (self->vadjustment != NULL)
    g_signal_handlers_disconnect_by_data (self->vadjustment, self);
  g_clear_object (&self->vadjustment);
  g_clear_object (&self->hadjustment);
  G_OBJECT_CLASS (gtk_ghostty_terminal_parent_class)->dispose (object);
}

static void
gtk_ghostty_terminal_finalize (GObject *object)
{
  GtkGhosttyTerminal *self = GTK_GHOSTTY_TERMINAL (object);
  g_clear_pointer (&self->row_cache, g_ptr_array_unref);
  ghostty_render_state_row_cells_free (self->cells);
  ghostty_render_state_row_iterator_free (self->rows);
  ghostty_render_state_free (self->render_state);
  ghostty_key_encoder_free (self->key_encoder);
  ghostty_mouse_event_free (self->mouse_event);
  ghostty_mouse_encoder_free (self->mouse_encoder);
  ghostty_selection_gesture_event_free (self->gesture_press);
  ghostty_selection_gesture_event_free (self->gesture_drag);
  ghostty_selection_gesture_event_free (self->gesture_release);
  ghostty_selection_gesture_event_free (self->gesture_tick);
  ghostty_selection_gesture_free (self->gesture, self->terminal);
  ghostty_terminal_free (self->terminal);
  g_byte_array_unref (self->pending_reply);
  g_free (self->title);
  g_free (self->preedit);
  g_clear_pointer (&self->preedit_attrs, pango_attr_list_unref);
  g_clear_pointer (&self->search, g_regex_unref);
  g_clear_pointer (&self->plain_search, ghostty_search_free);
  g_array_unref (self->held_keys);
  g_clear_pointer (&self->font, pango_font_description_free);
  for (int bold = 0; bold < 2; bold++)
    for (int italic = 0; italic < 2; italic++)
      g_clear_pointer (&self->fonts[bold][italic], pango_font_description_free);
  g_clear_pointer (&self->grid_attrs, pango_attr_list_unref);
  G_OBJECT_CLASS (gtk_ghostty_terminal_parent_class)->finalize (object);
}

static void
gtk_ghostty_terminal_class_init (GtkGhosttyTerminalClass *klass)
{
  GObjectClass *object_class = G_OBJECT_CLASS (klass);
  GtkWidgetClass *widget_class = GTK_WIDGET_CLASS (klass);

  object_class->get_property = gtk_ghostty_terminal_get_property;
  object_class->set_property = gtk_ghostty_terminal_set_property;
  object_class->dispose = gtk_ghostty_terminal_dispose;
  object_class->finalize = gtk_ghostty_terminal_finalize;
  widget_class->measure = gtk_ghostty_terminal_measure;
  widget_class->size_allocate = gtk_ghostty_terminal_size_allocate;
  widget_class->snapshot = gtk_ghostty_terminal_snapshot;
  widget_class->realize = gtk_ghostty_terminal_realize;
  widget_class->unrealize = gtk_ghostty_terminal_unrealize;

  /**
   * GtkGhosttyTerminal:columns:
   *
   * Columns in the grid; set by the allocation, or by
   * gtk_ghostty_terminal_set_grid_size() before there is one.
   */
  properties[PROP_COLUMNS] = g_param_spec_uint ("columns", NULL, NULL, 0, G_MAXUINT16, DEFAULT_COLUMNS,
                                                G_PARAM_READABLE | G_PARAM_STATIC_STRINGS | G_PARAM_EXPLICIT_NOTIFY);
  /**
   * GtkGhosttyTerminal:rows:
   *
   * Visible rows in the grid.
   */
  properties[PROP_ROWS] = g_param_spec_uint ("rows", NULL, NULL, 0, G_MAXUINT16, DEFAULT_ROWS,
                                             G_PARAM_READABLE | G_PARAM_STATIC_STRINGS | G_PARAM_EXPLICIT_NOTIFY);
  /**
   * GtkGhosttyTerminal:title:
   *
   * The window title the program set (OSC 0 or 2), %NULL until it sets one.
   */
  properties[PROP_TITLE] = g_param_spec_string ("title", NULL, NULL, NULL,
                                                G_PARAM_READABLE | G_PARAM_STATIC_STRINGS | G_PARAM_EXPLICIT_NOTIFY);
  /**
   * GtkGhosttyTerminal:legacy-keyboard:
   *
   * Encode keys with the legacy protocol even when the program asks for the
   * kitty keyboard protocol. For a channel that forwards the request but
   * cannot carry kitty keys: Windows ConPTY turns them into literal text.
   */
  properties[PROP_LEGACY_KEYBOARD] =
    g_param_spec_boolean ("legacy-keyboard", NULL, NULL, FALSE,
                          G_PARAM_READWRITE | G_PARAM_STATIC_STRINGS | G_PARAM_EXPLICIT_NOTIFY);
  /**
   * GtkGhosttyTerminal:pull-history-on-grow:
   *
   * When the grid gets taller, pull rows back from history (as xterm does).
   * Turn it off for a stream that repaints its own screen after a resize,
   * such as Windows ConPTY, where pulled rows would be painted over.
   */
  properties[PROP_PULL_HISTORY_ON_GROW] =
    g_param_spec_boolean ("pull-history-on-grow", NULL, NULL, TRUE,
                          G_PARAM_READWRITE | G_PARAM_STATIC_STRINGS | G_PARAM_EXPLICIT_NOTIFY);
  /**
   * GtkGhosttyTerminal:font-desc:
   *
   * The font (see gtk_ghostty_terminal_set_font_desc()).
   */
  properties[PROP_FONT_DESC] =
    g_param_spec_boxed ("font-desc", NULL, NULL, PANGO_TYPE_FONT_DESCRIPTION,
                        G_PARAM_READWRITE | G_PARAM_STATIC_STRINGS | G_PARAM_EXPLICIT_NOTIFY);
  /**
   * GtkGhosttyTerminal:allow-clipboard-write:
   *
   * Whether programs may set the clipboard (OSC 52), as tmux and editors do
   * to copy over SSH. On by default, as in Ghostty and kitty. Programs can
   * never read the clipboard.
   */
  properties[PROP_ALLOW_CLIPBOARD_WRITE] =
    g_param_spec_boolean ("allow-clipboard-write", NULL, NULL, TRUE,
                          G_PARAM_READWRITE | G_PARAM_STATIC_STRINGS | G_PARAM_EXPLICIT_NOTIFY);
  g_object_class_install_properties (object_class, N_PROPS, properties);
  g_object_class_override_property (object_class, PROP_HADJUSTMENT, "hadjustment");
  g_object_class_override_property (object_class, PROP_VADJUSTMENT, "vadjustment");
  g_object_class_override_property (object_class, PROP_HSCROLL_POLICY, "hscroll-policy");
  g_object_class_override_property (object_class, PROP_VSCROLL_POLICY, "vscroll-policy");

  /**
   * GtkGhosttyTerminal::input:
   * @self: the terminal
   * @data: bytes to write to the program (keys, committed text, paste, focus)
   *
   * Emitted for what the user does: write @data to the program (its pty, or
   * the remote end of a connection). See also #GtkGhosttyTerminal::reply.
   */
  signals[SIGNAL_INPUT] = g_signal_new ("input", G_TYPE_FROM_CLASS (klass), G_SIGNAL_RUN_LAST,
                                        0, NULL, NULL, NULL, G_TYPE_NONE, 1, G_TYPE_BYTES);
  /**
   * GtkGhosttyTerminal::reply:
   * @self: the terminal
   * @data: the terminal's answers to the program's queries (DA, DSR, ...)
   *
   * Write these to the program like input. Emitted after the fed bytes are
   * processed, never from inside libghostty-vt.
   */
  signals[SIGNAL_REPLY] = g_signal_new ("reply", G_TYPE_FROM_CLASS (klass), G_SIGNAL_RUN_LAST,
                                        0, NULL, NULL, NULL, G_TYPE_NONE, 1, G_TYPE_BYTES);
  /**
   * GtkGhosttyTerminal::size-changed:
   * @self: the terminal
   * @columns: new number of columns
   * @rows: new number of rows
   * @cell_width: cell width in pixels
   * @cell_height: cell height in pixels
   *
   * Tell the program's PTY (TIOCSWINSZ, ResizePseudoConsole, ssh window-change).
   */
  signals[SIGNAL_SIZE_CHANGED] = g_signal_new ("size-changed", G_TYPE_FROM_CLASS (klass), G_SIGNAL_RUN_LAST,
                                               0, NULL, NULL, NULL, G_TYPE_NONE, 4,
                                               G_TYPE_UINT, G_TYPE_UINT, G_TYPE_UINT, G_TYPE_UINT);
  /**
   * GtkGhosttyTerminal::bell:
   * @self: the terminal
   *
   * The program rang the bell (BEL).
   */
  signals[SIGNAL_BELL] = g_signal_new ("bell", G_TYPE_FROM_CLASS (klass), G_SIGNAL_RUN_LAST,
                                       0, NULL, NULL, NULL, G_TYPE_NONE, 0);
  /**
   * GtkGhosttyTerminal::contents-changed:
   * @self: the terminal
   *
   * Emitted after fed output was processed.
   */
  signals[SIGNAL_CONTENTS_CHANGED] = g_signal_new ("contents-changed", G_TYPE_FROM_CLASS (klass),
                                                   G_SIGNAL_RUN_LAST, 0, NULL, NULL, NULL, G_TYPE_NONE, 0);
  /**
   * GtkGhosttyTerminal::selection-changed:
   * @self: the terminal
   *
   * The selection changed, including while it is dragged out and when a
   * search selects a hit.
   */
  signals[SIGNAL_SELECTION_CHANGED] = g_signal_new ("selection-changed", G_TYPE_FROM_CLASS (klass),
                                                    G_SIGNAL_RUN_LAST, 0, NULL, NULL, NULL, G_TYPE_NONE, 0);

  gtk_widget_class_set_css_name (widget_class, "ghostty-terminal");
  gtk_widget_class_set_accessible_role (widget_class, GTK_ACCESSIBLE_ROLE_TERMINAL);
}

static void
gtk_ghostty_terminal_init (GtkGhosttyTerminal *self)
{
  GtkEventController *keys, *focus, *motion, *scroll;
  GtkGesture *click;
  size_t max_bytes = SIZE_MAX, max_lines = DEFAULT_SCROLLBACK_LINES;

  self->columns = DEFAULT_COLUMNS;
  self->n_rows = DEFAULT_ROWS;
  self->pull_history_on_grow = TRUE;
  self->font_scale = 1.0;
  self->allow_clipboard_write = TRUE;
  self->held_keys = g_array_new (FALSE, FALSE, sizeof (guint));
  self->search_row = self->search_column = -1;
  self->pending_reply = g_byte_array_new ();
  self->row_cache = g_ptr_array_new_with_free_func (row_free);
  self->font = pango_font_description_from_string (DEFAULT_FONT);

  if (ghostty_terminal_new (NULL, &self->terminal, self->columns, self->n_rows) != GHOSTTY_SUCCESS)
    g_error ("ghostty_terminal_new failed");
  ghostty_render_state_new (NULL, &self->render_state);
  ghostty_render_state_row_iterator_new (NULL, &self->rows);
  {
    /* Register the renderer's fallback colours as the terminal's defaults,
     * so colour queries are answered from the start. */
    GhosttyRenderStateColors colors = GHOSTTY_INIT_SIZED (GhosttyRenderStateColors);
    ghostty_render_state_update (self->render_state, self->terminal);
    ghostty_render_state_get (self->render_state, GHOSTTY_RENDER_STATE_DATA_COLORS, &colors);
    self->builtin_fg = colors.foreground;
    self->builtin_bg = colors.background;
    ghostty_terminal_set (self->terminal, GHOSTTY_TERMINAL_OPT_COLOR_FOREGROUND, &self->builtin_fg);
    ghostty_terminal_set (self->terminal, GHOSTTY_TERMINAL_OPT_COLOR_BACKGROUND, &self->builtin_bg);
  }
  ghostty_render_state_row_cells_new (NULL, &self->cells);
  ghostty_key_encoder_new (NULL, &self->key_encoder);
  ghostty_mouse_encoder_new (NULL, &self->mouse_encoder);
  ghostty_mouse_event_new (NULL, &self->mouse_event);
  ghostty_selection_gesture_new (NULL, &self->gesture);
  ghostty_selection_gesture_event_new (NULL, &self->gesture_press, GHOSTTY_SELECTION_GESTURE_EVENT_TYPE_PRESS);
  ghostty_selection_gesture_event_new (NULL, &self->gesture_drag, GHOSTTY_SELECTION_GESTURE_EVENT_TYPE_DRAG);
  ghostty_selection_gesture_event_new (NULL, &self->gesture_release, GHOSTTY_SELECTION_GESTURE_EVENT_TYPE_RELEASE);
  ghostty_selection_gesture_event_new (NULL, &self->gesture_tick, GHOSTTY_SELECTION_GESTURE_EVENT_TYPE_AUTOSCROLL_TICK);

  ghostty_terminal_set (self->terminal, GHOSTTY_TERMINAL_OPT_USERDATA, self);
  ghostty_terminal_set (self->terminal, GHOSTTY_TERMINAL_OPT_WRITE_PTY, (const void *) on_write_pty);
  ghostty_terminal_set (self->terminal, GHOSTTY_TERMINAL_OPT_BELL, (const void *) on_bell);
  ghostty_terminal_set (self->terminal, GHOSTTY_TERMINAL_OPT_TITLE_CHANGED, (const void *) on_title_changed);
  ghostty_terminal_set (self->terminal, GHOSTTY_TERMINAL_OPT_DEVICE_ATTRIBUTES, (const void *) on_device_attributes);
  ghostty_terminal_set (self->terminal, GHOSTTY_TERMINAL_OPT_SIZE, (const void *) on_size);
  ghostty_terminal_set (self->terminal, GHOSTTY_TERMINAL_OPT_XTVERSION, (const void *) on_xtversion);
  ghostty_terminal_set (self->terminal, GHOSTTY_TERMINAL_OPT_COLOR_SCHEME, (const void *) on_color_scheme);
  ghostty_terminal_set (self->terminal, GHOSTTY_TERMINAL_OPT_CLIPBOARD_WRITE, (const void *) on_clipboard_write);
  /* The byte limit defaults to one page (~10 KB): let the line limit decide. */
  ghostty_terminal_set (self->terminal, GHOSTTY_TERMINAL_OPT_SCROLLBACK_MAX_BYTES, &max_bytes);
  ghostty_terminal_set (self->terminal, GHOSTTY_TERMINAL_OPT_SCROLLBACK_MAX_LINES, &max_lines);

  gtk_widget_set_focusable (GTK_WIDGET (self), TRUE);

  /* The client widget is set on realize: the Windows IME module asks for the
   * widget's native surface, which does not exist before then. */
  self->im = gtk_im_multicontext_new ();
  g_signal_connect (self->im, "commit", G_CALLBACK (on_im_commit), self);
  g_signal_connect (self->im, "preedit-changed", G_CALLBACK (on_preedit_changed), self);
  g_signal_connect (self->im, "preedit-end", G_CALLBACK (on_preedit_changed), self);

  keys = gtk_event_controller_key_new ();
  /* The input method is fed from the handlers (filter_through_im), not by
   * the controller, which would let it swallow plain characters. */
  g_signal_connect (keys, "key-pressed", G_CALLBACK (on_key_pressed), self);
  g_signal_connect (keys, "key-released", G_CALLBACK (on_key_released), self);
  gtk_widget_add_controller (GTK_WIDGET (self), keys);

  focus = gtk_event_controller_focus_new ();
  g_signal_connect (focus, "enter", G_CALLBACK (on_focus_enter), self);
  g_signal_connect (focus, "leave", G_CALLBACK (on_focus_leave), self);
  gtk_widget_add_controller (GTK_WIDGET (self), focus);

  click = gtk_gesture_click_new ();
  gtk_gesture_single_set_button (GTK_GESTURE_SINGLE (click), 0);  /* every button */
  g_signal_connect (click, "pressed", G_CALLBACK (on_pressed), self);
  g_signal_connect (click, "released", G_CALLBACK (on_released), self);
  gtk_widget_add_controller (GTK_WIDGET (self), GTK_EVENT_CONTROLLER (click));

  motion = gtk_event_controller_motion_new ();
  g_signal_connect (motion, "motion", G_CALLBACK (on_motion), self);
  gtk_widget_add_controller (GTK_WIDGET (self), motion);

  scroll = gtk_event_controller_scroll_new (GTK_EVENT_CONTROLLER_SCROLL_VERTICAL);
  g_signal_connect (scroll, "scroll", G_CALLBACK (on_scroll), self);
  gtk_widget_add_controller (GTK_WIDGET (self), scroll);

  gtk_widget_set_cursor_from_name (GTK_WIDGET (self), "text");
}
