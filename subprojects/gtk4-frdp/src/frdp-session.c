/* SPDX-License-Identifier: GPL-3.0-or-later */
/* frdp-session.c
 *
 * Copyright (C) 2018 Felipe Borges <felipeborges@gnome.org>
 * Copyright (C) 2019 Armin Novak <akallabeth@posteo.net>
 * Copyright (C) 2023 Marek Kasik <mkasik@redhat.com>
 * Copyright (C) 2026 mFat
 *
 * GTK4 port with a session thread (2026). Threading model, following
 * Remmina's RDP plugin and FreeRDP's own SDL client:
 *
 *  - Each session has one worker thread that owns FreeRDP: it runs
 *    freerdp_connect(), the event loop and all decoding. The GTK main thread
 *    never waits on the network. (Upstream connected on the main thread and
 *    froze the UI for the whole connect: 15 s for an unreachable host.)
 *  - FreeRDP callbacks therefore run on the worker. Anything that touches
 *    GTK is marshalled to the main thread by queueing idle sources on the
 *    default main context (never g_main_context_invoke(), see below).
 *  - The framebuffer is guarded by frame_lock, held from BeginPaint to
 *    EndPaint (and around gdi_init/resize/free); snapshot() copies it into an
 *    immutable GdkTexture under the same lock.
 *  - Input is sent from the main thread directly, as FreeRDP's SDL client
 *    does; FreeRDP serialises transport writes with its own WriteLock.
 *  - Certificate/authentication prompts block only the worker, on a
 *    condition variable, until the application answers (Remmina's
 *    ui_sync pattern). Upstream spun a nested main loop instead.
 *  - Closing aborts the worker with freerdp_abort_connect_context(); the
 *    context is freed on the main thread after the worker is joined.
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

#include <errno.h>
#include <freerdp/input.h>
#include <freerdp/locale/keyboard.h>
#include <freerdp/freerdp.h>
#include <freerdp/version.h>
#include <freerdp/gdi/gdi.h>
#include <freerdp/gdi/video.h>
#include <freerdp/gdi/gfx.h>
#include <freerdp/client/channels.h>
#include <freerdp/client/cmdline.h>
#include <freerdp/client/rdpei.h>
#include <freerdp/client/tsmf.h>
#include <freerdp/client/rail.h>
#include <freerdp/client/cliprdr.h>
#include <freerdp/client/rdpgfx.h>
#include <freerdp/client/encomsp.h>
#include <freerdp/client/disp.h>
#include <freerdp/client/geometry.h>
#include <freerdp/client/video.h>
#include <freerdp/channels/disp.h>
#include <gdk/gdk.h>
#include <gio/gio.h>
#include <gtk/gtk.h>
#include <math.h>
#include <string.h>
#ifdef __GLIBC__
#include <malloc.h>
#endif

#include "frdp-session.h"
#include "frdp-display-private.h"
#include "frdp-context.h"
#include "frdp-channel-display-control.h"
#include "frdp-keyboard-layout.h"
#include "frdp-channel-clipboard.h"

/* The worker re-checks shall-disconnect at least this often. */
#define FRDP_WORKER_WAIT_MS 100

#define CONST_QUALIFIER const

struct frdp_pointer
{
	rdpPointer pointer;
	GdkTexture *texture;
};
typedef struct frdp_pointer frdpPointer;

/* Connection settings copied on the main thread in frdp_session_connect():
 * the session thread builds the FreeRDP instance from these, so properties
 * the application changes meanwhile cannot race with it. */
typedef struct
{
  gchar    *hostname;
  gchar    *username;
  gchar    *password;
  gchar    *domain;
  guint     port;
  gboolean  clipboard;
  gboolean  audio_playback;
  guint32   keyboard_layout;   /* resolved on the main thread (GDK) */
} FrdpConnectParams;

/* Overwrite a secret before freeing it; the volatile writes cannot be
 * optimised away as a dead store. */
static void
frdp_wipe_string (gchar *str)
{
  volatile gchar *p = str;

  while (*p != '\0')
    *p++ = '\0';
}

static void
frdp_connect_params_free (FrdpConnectParams *params)
{
  if (params == NULL)
    return;
  g_free (params->hostname);
  g_free (params->username);
  if (params->password != NULL) {
    frdp_wipe_string (params->password);
    g_free (params->password);
  }
  g_free (params->domain);
  g_free (params);
}

typedef enum
{
  FRDP_PROMPT_AUTHENTICATION,
  FRDP_PROMPT_CERTIFICATE,
  FRDP_PROMPT_CERTIFICATE_CHANGE,
} FrdpPromptKind;

typedef struct
{
  FrdpSession    *self;
  FrdpPromptKind  kind;
  gchar          *host;
  guint16         port;
  gchar          *common_name;
  gchar          *subject;
  gchar          *issuer;
  gchar          *fingerprint;
  gchar          *old_subject;
  gchar          *old_issuer;
  gchar          *old_fingerprint;
  guint32         flags;
} FrdpPromptRequest;

struct _FrdpSessionPrivate
{
  /* Created on the main thread, owned by the worker while it runs, freed on
   * the main thread after the worker has been joined. */
  /* Created on the session thread and published under instance_lock, so
   * close() can abort a connect that is still being set up. */
  freerdp      *freerdp_session;
  GMutex        instance_lock;
  FrdpConnectParams *connect_params; /* main -> worker; freed by the worker */
  gboolean      init_failed;        /* written by worker before connect_done */
  GThread      *worker;
  GTask        *connect_task;
  gint          closing;            /* atomic */
  gboolean      connect_ok;         /* written by worker before connect_done */
  guint32       connect_error;      /* idem */
  guint32       disconnect_error;   /* written by worker before worker_done */

  /* ---- main thread only ---- */
  GtkWidget    *display;            /* weak; cleared by FrdpDisplay on dispose */
  gboolean      is_connected;
  gboolean      scaling;
  double        scale;
  double        offset_x;
  double        offset_y;
  int           alloc_width;
  int           alloc_height;
  guint         resize_timeout_id;
  double        cursor_scale_applied; /* scale the current cursor was made for */
  gboolean      monitor_layout_supported;
  GdkTexture   *frame;
  gchar        *hostname;
  gchar        *username;
  gchar        *password;
  gchar        *domain;
  guint         port;
  guint         keyboard_layout;    /* 0 = detect */
  gboolean      clipboard;
  gboolean      audio_playback;
  guint32       last_error_code;    /* 0 = no failure */

  /* ---- framebuffer: frame_lock ---- */
  GMutex        frame_lock;
  gboolean      paint_locked;       /* worker only: frame_lock held since BeginPaint */
  gboolean      gdi_valid;
  gboolean      frame_dirty;
  guint         desktop_width;
  guint         desktop_height;
  gint          draw_scheduled;     /* atomic */

  /* ---- cursor + channels: state_lock ---- */
  GMutex        state_lock;
  GdkTexture   *cursor_texture;
  gint          cursor_x;
  gint          cursor_y;
  gboolean      cursor_null;
  gint          cursor_scheduled;   /* atomic */
  FrdpChannelDisplayControl *display_control_channel;
  FrdpChannelClipboard      *clipboard_channel;

  /* ---- prompts: prompt_lock ---- */
  GMutex        prompt_lock;
  GCond         prompt_cond;
  gboolean      prompt_pending;
  gboolean      prompt_answered;
  guint         prompt_value;

  /* spike instrumentation (FRDP_SPIKE_STATS=1) */
  guint         stats_uploads;
  guint         stats_draws;
  gint64        stats_since;
};

G_DEFINE_TYPE_WITH_PRIVATE (FrdpSession, frdp_session, G_TYPE_OBJECT)

enum
{
  PROP_0 = 0,
  PROP_HOSTNAME,
  PROP_PORT,
  PROP_USERNAME,
  PROP_PASSWORD,
  PROP_DISPLAY,
  PROP_SCALING,
  PROP_MONITOR_LAYOUT_SUPPORTED,
  PROP_DOMAIN,
  PROP_KEYBOARD_LAYOUT,
  PROP_CLIPBOARD,
  PROP_AUDIO_PLAYBACK,
  PROP_LAST_ERROR_CODE
};

enum
{
  RDP_ERROR,
  RDP_CONNECTED,
  RDP_DISCONNECTED,
  RDP_AUTH_FAILURE,
  RDP_CHANNEL_CONNECTED,
  LAST_SIGNAL
};

static guint signals[LAST_SIGNAL];

static void frdp_session_update_mouse_pointer (FrdpSession *self);

/* ---- main-thread marshalling ------------------------------------------- */

/* Queues @func(data) on the default main context (the GTK thread).
 *
 * Deliberately not g_main_context_invoke(): when no thread owns the context
 * at that moment (an application iterating the loop by hand, or before its
 * loop starts), it runs the function right away on the *calling* thread.
 * That ran the clean-up on the session thread, which then tried to join
 * itself ("Resource deadlock avoided" abort, found by tests/test-widget.py). */
static void
frdp_main_context_queue (GSourceFunc    func,
                         gpointer       data,
                         GDestroyNotify notify)
{
  GSource *source = g_idle_source_new ();

  g_source_set_priority (source, G_PRIORITY_DEFAULT);
  g_source_set_callback (source, func, data, notify);
  g_source_set_static_name (source, "[gtk4-frdp] main-thread call");
  g_source_attach (source, g_main_context_default ());
  g_source_unref (source);
}

/* Runs @func(self) on the main thread, always asynchronously. */
static void
frdp_session_run_on_main (FrdpSession *self,
                          GSourceFunc  func)
{
  frdp_main_context_queue (func, g_object_ref (self), g_object_unref);
}

static gboolean
frdp_session_draw_cb (gpointer user_data)
{
  FrdpSession        *self = user_data;
  FrdpSessionPrivate *priv = self->priv;

  g_atomic_int_set (&priv->draw_scheduled, 0);
  priv->stats_draws++;

  if (priv->display != NULL && priv->is_connected)
    gtk_widget_queue_draw (priv->display);

  return G_SOURCE_REMOVE;
}

/* Coalesces redraw requests from the worker: at most one pending. */
static void
frdp_session_schedule_draw (FrdpSession *self)
{
  if (g_atomic_int_compare_and_exchange (&self->priv->draw_scheduled, 0, 1))
    frdp_session_run_on_main (self, frdp_session_draw_cb);
}

static gboolean
frdp_session_cursor_cb (gpointer user_data)
{
  FrdpSession *self = user_data;

  g_atomic_int_set (&self->priv->cursor_scheduled, 0);
  frdp_session_update_mouse_pointer (self);

  return G_SOURCE_REMOVE;
}

static void
frdp_session_schedule_cursor_update (FrdpSession *self)
{
  if (g_atomic_int_compare_and_exchange (&self->priv->cursor_scheduled, 0, 1))
    frdp_session_run_on_main (self, frdp_session_cursor_cb);
}

/* ---- pointer ------------------------------------------------------------ */

/* Resizes a cursor image (CPU; cursors are tiny). GDK_MEMORY_DEFAULT is
 * the format that matches cairo's ARGB32 on every platform. */
static GdkTexture *
frdp_scale_cursor_texture (GdkTexture *texture,
                           double      scale)
{
  int                   w = gdk_texture_get_width (texture);
  int                   h = gdk_texture_get_height (texture);
  int                   sw = MAX (1, (int) ceil (w * scale));
  int                   sh = MAX (1, (int) ceil (h * scale));
  GdkTextureDownloader *downloader;
  GBytes               *src_bytes, *dst_bytes;
  gsize                 src_stride;
  cairo_surface_t      *src, *dst;
  cairo_t              *cr;
  GdkTexture           *result;

  downloader = gdk_texture_downloader_new (texture);
  gdk_texture_downloader_set_format (downloader, GDK_MEMORY_DEFAULT);
  src_bytes = gdk_texture_downloader_download_bytes (downloader, &src_stride);
  gdk_texture_downloader_free (downloader);

  src = cairo_image_surface_create_for_data ((guchar *) g_bytes_get_data (src_bytes, NULL),
                                             CAIRO_FORMAT_ARGB32, w, h, src_stride);
  dst = cairo_image_surface_create (CAIRO_FORMAT_ARGB32, sw, sh);
  cr = cairo_create (dst);
  cairo_scale (cr, (double) sw / w, (double) sh / h);
  cairo_set_source_surface (cr, src, 0, 0);
  cairo_pattern_set_filter (cairo_get_source (cr), CAIRO_FILTER_GOOD);
  cairo_paint (cr);
  cairo_destroy (cr);
  cairo_surface_flush (dst);

  dst_bytes = g_bytes_new (cairo_image_surface_get_data (dst),
                           (gsize) cairo_image_surface_get_stride (dst) * sh);
  result = gdk_memory_texture_new (sw, sh, GDK_MEMORY_DEFAULT, dst_bytes,
                                   cairo_image_surface_get_stride (dst));

  g_bytes_unref (dst_bytes);
  cairo_surface_destroy (dst);
  cairo_surface_destroy (src);
  g_bytes_unref (src_bytes);

  return result;
}

static void
frdp_session_update_mouse_pointer (FrdpSession  *self)
{
  FrdpSessionPrivate *priv = self->priv;
  GdkTexture *texture = NULL;
  GdkCursor  *cursor;
  gboolean    cursor_null;
  gint        hot_x, hot_y;

  if (priv->display == NULL)
    return;

  g_mutex_lock (&priv->state_lock);
  if (priv->cursor_texture != NULL)
    texture = g_object_ref (priv->cursor_texture);
  cursor_null = priv->cursor_null;
  hot_x = priv->cursor_x;
  hot_y = priv->cursor_y;
  g_mutex_unlock (&priv->state_lock);

  /* GTK 4 cursors are per widget and only shown while the pointer is over
   * it, so no enter/leave bookkeeping is needed (GTK 3 gtk-frdp set the
   * cursor on the whole window and had to swap it on enter/leave). */
  if (cursor_null) {
    /* Server hides the pointer. */
    cursor = gdk_cursor_new_from_name ("none", NULL);
  } else if (texture == NULL) {
    cursor = gdk_cursor_new_from_name ("default", NULL);
  } else {
    double scale = priv->scaling ? priv->scale : 1.0;

    if (fabs (scale - 1.0) > 0.01) {
      /* Match the scaled desktop: a 32 px remote cursor over a desktop
       * drawn at 50 % becomes 16 px, with its hotspot moved accordingly. */
      GdkTexture *scaled = frdp_scale_cursor_texture (texture, scale);

      hot_x = (gint) floor (hot_x * scale);
      hot_y = (gint) floor (hot_y * scale);
      g_set_object (&texture, scaled);
      g_object_unref (scaled);
    }
    cursor = gdk_cursor_new_from_texture (texture, hot_x, hot_y, NULL);
  }
  priv->cursor_scale_applied = priv->scaling ? priv->scale : 1.0;

  gtk_widget_set_cursor (priv->display, cursor);
  g_clear_object (&cursor);
  g_clear_object (&texture);
}

static BOOL
frdp_pointer_new (rdpContext *context,
                  rdpPointer *pointer)
{
  frdpContext *fcontext = (frdpContext *) context;
  frdpPointer *fpointer = (frdpPointer *) pointer;
  gsize        stride;
  guint8      *data;
  GBytes      *bytes;

  if (fcontext == NULL || fpointer == NULL || pointer->width == 0 || pointer->height == 0)
    return FALSE;

  stride = (gsize) pointer->width * 4;
  /* Zeroed: freerdp_image_copy_from_pointer_data does not initialise the
   * buffer (FreeRDP issue 5061). */
  data = g_malloc0 (stride * pointer->height);

  if (!freerdp_image_copy_from_pointer_data (data, PIXEL_FORMAT_BGRA32,
                                             stride, 0, 0, pointer->width,
                                             pointer->height,
                                             pointer->xorMaskData,
                                             pointer->lengthXorMask,
                                             pointer->andMaskData,
                                             pointer->lengthAndMask,
                                             pointer->xorBpp,
                                             &context->gdi->palette)) {
    g_free (data);
    return FALSE;
  }

  bytes = g_bytes_new_take (data, stride * pointer->height);
  fpointer->texture = gdk_memory_texture_new (pointer->width, pointer->height,
                                              GDK_MEMORY_B8G8R8A8,
                                              bytes, stride);
  g_bytes_unref (bytes);

  return TRUE;
}

static void
frdp_pointer_free (rdpContext *context,
                   rdpPointer *pointer)
{
  frdpPointer *fpointer = (frdpPointer *) pointer;

  if (fpointer != NULL)
    g_clear_object (&fpointer->texture);
}

/* Pointer callbacks run on the worker: record the state, update on main. */
static BOOL
frdp_pointer_set (rdpContext *context,
                  rdpPointer *pointer)
{
  FrdpSession *self = ((frdpContext *) context)->self;
  frdpPointer *fpointer = (frdpPointer *) pointer;
  FrdpSessionPrivate *priv = self->priv;

  g_mutex_lock (&priv->state_lock);
  g_set_object (&priv->cursor_texture, fpointer->texture);
  priv->cursor_x = pointer->xPos;
  priv->cursor_y = pointer->yPos;
  priv->cursor_null = FALSE;
  g_mutex_unlock (&priv->state_lock);

  frdp_session_schedule_cursor_update (self);

  return TRUE;
}

static BOOL
frdp_pointer_set_null (rdpContext *context)
{
  FrdpSession *self = ((frdpContext *) context)->self;
  FrdpSessionPrivate *priv = self->priv;

  g_mutex_lock (&priv->state_lock);
  g_clear_object (&priv->cursor_texture);
  priv->cursor_null = TRUE;
  g_mutex_unlock (&priv->state_lock);

  frdp_session_schedule_cursor_update (self);

  return TRUE;
}

static BOOL
frdp_pointer_set_default (rdpContext *context)
{
  FrdpSession *self = ((frdpContext *) context)->self;
  FrdpSessionPrivate *priv = self->priv;

  g_mutex_lock (&priv->state_lock);
  g_clear_object (&priv->cursor_texture);
  priv->cursor_null = FALSE;
  g_mutex_unlock (&priv->state_lock);

  frdp_session_schedule_cursor_update (self);

  return TRUE;
}

static BOOL
frdp_pointer_set_position (rdpContext *context,
                           UINT32      x,
                           UINT32      y)
{
  return TRUE;
}

static void
frdp_register_pointer (rdpGraphics *graphics)
{
  rdpPointer pointer = { 0 };

  pointer.size = sizeof (frdpPointer);
  pointer.New = frdp_pointer_new;
  pointer.Free = frdp_pointer_free;
  pointer.Set = frdp_pointer_set;
  pointer.SetNull = frdp_pointer_set_null;
  pointer.SetDefault = frdp_pointer_set_default;
  pointer.SetPosition = frdp_pointer_set_position;
  graphics_register_pointer (graphics, &pointer);
}

/* ---- geometry (main thread) -------------------------------------------- */

static void
frdp_session_get_desktop_size (FrdpSession *self,
                               guint       *width,
                               guint       *height)
{
  g_mutex_lock (&self->priv->frame_lock);
  *width = self->priv->gdi_valid ? self->priv->desktop_width : 0;
  *height = self->priv->gdi_valid ? self->priv->desktop_height : 0;
  g_mutex_unlock (&self->priv->frame_lock);
}

/* Scale/offset used both for drawing and for mapping pointer coordinates. */
static void
frdp_session_update_scale (FrdpSession *self)
{
  FrdpSessionPrivate *priv = self->priv;
  double              width = priv->alloc_width, height = priv->alloc_height;
  guint               dw, dh;

  frdp_session_get_desktop_size (self, &dw, &dh);

  if (dw == 0 || dh == 0 || !priv->scaling || width <= 0 || height <= 0) {
    priv->scale = 1.0;
    priv->offset_x = priv->offset_y = 0.0;
  } else {
    if (width / height > (double) dw / dh)
      priv->scale = height / dh;
    else
      priv->scale = width / dw;

    priv->offset_x = (width - dw * priv->scale) / 2.0;
    priv->offset_y = (height - dh * priv->scale) / 2.0;
  }

  /* The cursor is scaled with the desktop; rebuild it when the scale moved
   * noticeably (not on every pixel of a window drag). */
  if (priv->is_connected &&
      fabs ((priv->scaling ? priv->scale : 1.0) - priv->cursor_scale_applied) > 0.02)
    frdp_session_update_mouse_pointer (self);
}

static void
frdp_session_update_size_request (FrdpSession *self)
{
  FrdpSessionPrivate *priv = self->priv;
  gboolean            allow_resize = FALSE;
  guint               dw, dh;

  if (priv->display == NULL)
    return;

  g_object_get (G_OBJECT (priv->display), "allow-resize", &allow_resize, NULL);
  frdp_session_get_desktop_size (self, &dw, &dh);

  if (priv->scaling || allow_resize || dw == 0)
    gtk_widget_set_size_request (priv->display, -1, -1);
  else
    gtk_widget_set_size_request (priv->display, dw, dh);
}

static gboolean
frdp_session_geometry_changed_cb (gpointer user_data)
{
  FrdpSession *self = user_data;

  frdp_session_update_size_request (self);
  frdp_session_update_scale (self);
  if (self->priv->display != NULL)
    gtk_widget_queue_draw (self->priv->display);

  return G_SOURCE_REMOVE;
}

static void
frdp_session_request_remote_resize (FrdpSession *self)
{
  FrdpSessionPrivate *priv = self->priv;
  guint32             request_width, request_height;
  guint               dw, dh;

  if (!priv->is_connected || priv->alloc_width <= 0 || priv->alloc_height <= 0)
    return;

  request_width = priv->alloc_width;
  request_height = priv->alloc_height;

  /* Round width to even number */
  if (request_width % 2 == 1)
    request_width--;

  frdp_session_get_desktop_size (self, &dw, &dh);
  if (dw == request_width && dh == request_height)
    return;

  /* Held across the call: the worker tears the channel down under the same
   * lock when the dynamic channel disconnects. */
  g_mutex_lock (&priv->state_lock);
  if (priv->display_control_channel != NULL)
    frdp_channel_display_control_resize_display (priv->display_control_channel,
                                                 request_width,
                                                 request_height);
  g_mutex_unlock (&priv->state_lock);
}

static gboolean
frdp_session_resize_timeout (gpointer user_data)
{
  FrdpSession *self = user_data;

  self->priv->resize_timeout_id = 0;
  frdp_session_request_remote_resize (self);

  return G_SOURCE_REMOVE;
}

/* Replaces the GTK3 "configure-event" handler. Uses the widget's own
 * allocation (upstream assumed a GtkScrolledWindow ancestor) and debounces
 * remote resizes so a window drag does not renegotiate on every allocation. */
void
frdp_session_size_allocated (FrdpSession *self,
                             int          width,
                             int          height)
{
  FrdpSessionPrivate *priv = self->priv;
  gboolean            allow_resize = FALSE;

  priv->alloc_width = width;
  priv->alloc_height = height;

  frdp_session_update_scale (self);

  if (priv->display == NULL || !priv->is_connected)
    return;

  g_object_get (G_OBJECT (priv->display), "allow-resize", &allow_resize, NULL);
  if (!allow_resize)
    return;

  if (priv->resize_timeout_id != 0)
    g_source_remove (priv->resize_timeout_id);
  priv->resize_timeout_id = g_timeout_add (200, frdp_session_resize_timeout, self);
}

static void
frdp_session_resize_supported_changed (FrdpDisplay *display,
                                       GParamSpec  *pspec,
                                       gpointer     user_data)
{
  FrdpSession *self = user_data;
  gboolean     resize_supported, allow_resize;

  g_object_get (G_OBJECT (display),
                "resize-supported", &resize_supported,
                "allow-resize", &allow_resize,
                NULL);

  if (resize_supported && allow_resize)
    frdp_session_request_remote_resize (self);
}

static void
frdp_session_set_scaling (FrdpSession *self,
                          gboolean     scaling)
{
  self->priv->scaling = scaling;
  frdp_session_update_scale (self);
  frdp_session_update_size_request (self);
}

/* ---- framebuffer ---------------------------------------------------------- */

static void
frdp_session_stats (FrdpSession *self)
{
  FrdpSessionPrivate *priv = self->priv;
  gint64              now = g_get_monotonic_time ();

  if (priv->stats_since == 0)
    priv->stats_since = now;
  if (now - priv->stats_since < 5 * G_USEC_PER_SEC)
    return;
  if (g_getenv ("FRDP_SPIKE_STATS") != NULL)
    g_printerr ("frdp-stats: per 5s main-thread draws=%u texture_uploads=%u\n",
                priv->stats_draws, priv->stats_uploads);
  priv->stats_draws = priv->stats_uploads = 0;
  priv->stats_since = now;
}

/* Main thread. Copies the framebuffer into an immutable GdkMemoryTexture
 * (under frame_lock, only when FreeRDP painted since the last copy). */
void
frdp_session_snapshot (FrdpSession *self,
                       GtkSnapshot *snapshot,
                       int          width,
                       int          height)
{
  FrdpSessionPrivate *priv = self->priv;
  rdpGdi             *gdi;
  guint               dw = 0, dh = 0;

  if (!priv->is_connected || priv->freerdp_session == NULL)
    return;

  g_mutex_lock (&priv->frame_lock);
  gdi = priv->freerdp_session->context->gdi;
  if (priv->gdi_valid && gdi != NULL && gdi->primary_buffer != NULL) {
    dw = gdi->width;
    dh = gdi->height;
    if (priv->frame == NULL || priv->frame_dirty ||
        gdk_texture_get_width (priv->frame) != (int) dw ||
        gdk_texture_get_height (priv->frame) != (int) dh) {
      GBytes *bytes = g_bytes_new (gdi->primary_buffer, (gsize) gdi->stride * dh);

      g_clear_object (&priv->frame);
      priv->frame = gdk_memory_texture_new (dw, dh, GDK_MEMORY_B8G8R8X8, bytes, gdi->stride);
      g_bytes_unref (bytes);
      priv->frame_dirty = FALSE;
      priv->stats_uploads++;
    }
  }
  g_mutex_unlock (&priv->frame_lock);

  frdp_session_stats (self);

  if (priv->frame == NULL)
    return;

  dw = gdk_texture_get_width (priv->frame);
  dh = gdk_texture_get_height (priv->frame);

  if (priv->scaling) {
    graphene_rect_t rect = GRAPHENE_RECT_INIT (priv->offset_x, priv->offset_y,
                                               dw * priv->scale, dh * priv->scale);
    gtk_snapshot_append_scaled_texture (snapshot, priv->frame,
                                        GSK_SCALING_FILTER_LINEAR, &rect);
  } else {
    graphene_rect_t rect = GRAPHENE_RECT_INIT (0, 0, dw, dh);
    gtk_snapshot_append_texture (snapshot, priv->frame, &rect);
  }
}

/* Worker. frame_lock is held from BeginPaint to EndPaint so the main thread
 * never copies a half-painted frame. */
static BOOL
frdp_begin_paint (rdpContext *context)
{
  FrdpSessionPrivate *priv = ((frdpContext *) context)->self->priv;
  rdpGdi *gdi = context->gdi;

  if (!priv->paint_locked) {
    g_mutex_lock (&priv->frame_lock);
    priv->paint_locked = TRUE;
  }

  gdi->primary->hdc->hwnd->invalid->null = 1;
  gdi->primary->hdc->hwnd->ninvalid = 0;

  return TRUE;
}

static BOOL
frdp_end_paint (rdpContext *context)
{
  FrdpSession        *self = ((frdpContext *) context)->self;
  FrdpSessionPrivate *priv = self->priv;
  rdpGdi             *gdi = context->gdi;
  gboolean            painted = !gdi->primary->hdc->hwnd->invalid->null;

  if (painted)
    priv->frame_dirty = TRUE;

  if (priv->paint_locked) {
    priv->paint_locked = FALSE;
    g_mutex_unlock (&priv->frame_lock);
  }

  if (painted)
    frdp_session_schedule_draw (self);

  return TRUE;
}

/* Worker. */
static BOOL
frdp_desktop_resize (rdpContext *context)
{
  FrdpSession        *self = ((frdpContext *) context)->self;
  FrdpSessionPrivate *priv = self->priv;
  rdpGdi             *gdi = context->gdi;
  gboolean            took_lock = !priv->paint_locked;
  gboolean            ok;

  if (took_lock)
    g_mutex_lock (&priv->frame_lock);

  ok = gdi_resize (gdi,
                   freerdp_settings_get_uint32 (context->settings, FreeRDP_DesktopWidth),
                   freerdp_settings_get_uint32 (context->settings, FreeRDP_DesktopHeight));
  if (ok) {
    priv->desktop_width = gdi->width;
    priv->desktop_height = gdi->height;
    priv->frame_dirty = TRUE;
  }

  if (took_lock)
    g_mutex_unlock (&priv->frame_lock);

  if (ok) {
    frdp_session_run_on_main (self, frdp_session_geometry_changed_cb);
    frdp_session_schedule_draw (self);
  }

  return ok;
}

/* ---- prompts (worker blocks, main answers) ------------------------------ */

static void
frdp_prompt_request_free (gpointer data)
{
  FrdpPromptRequest *req = data;

  g_object_unref (req->self);
  g_free (req->host);
  g_free (req->common_name);
  g_free (req->subject);
  g_free (req->issuer);
  g_free (req->fingerprint);
  g_free (req->old_subject);
  g_free (req->old_issuer);
  g_free (req->old_fingerprint);
  g_free (req);
}

static gboolean
frdp_prompt_on_main (gpointer data)
{
  FrdpPromptRequest  *req = data;
  FrdpSession        *self = req->self;
  FrdpSessionPrivate *priv = self->priv;

  if (g_atomic_int_get (&priv->closing) || priv->display == NULL) {
    frdp_session_answer_prompt (self, 0);
    return G_SOURCE_REMOVE;
  }

  switch (req->kind) {
  case FRDP_PROMPT_AUTHENTICATION:
    _frdp_display_request_authentication (FRDP_DISPLAY (priv->display));
    break;
  case FRDP_PROMPT_CERTIFICATE:
    _frdp_display_request_certificate_verification (FRDP_DISPLAY (priv->display),
                                                    req->host, req->port,
                                                    req->common_name, req->subject,
                                                    req->issuer, req->fingerprint,
                                                    req->flags);
    break;
  case FRDP_PROMPT_CERTIFICATE_CHANGE:
    _frdp_display_request_certificate_change_verification (FRDP_DISPLAY (priv->display),
                                                           req->host, req->port,
                                                           req->common_name, req->subject,
                                                           req->issuer, req->fingerprint,
                                                           req->old_subject, req->old_issuer,
                                                           req->old_fingerprint, req->flags);
    break;
  }

  return G_SOURCE_REMOVE;
}

/* Worker: hands @req to the main thread and sleeps until it is answered or
 * the session is closed (then 0 = refuse). Takes ownership of @req. */
static guint
frdp_session_prompt (FrdpSession       *self,
                     FrdpPromptRequest *req)
{
  FrdpSessionPrivate *priv = self->priv;
  guint               value;

  g_mutex_lock (&priv->prompt_lock);
  priv->prompt_pending = TRUE;
  priv->prompt_answered = FALSE;
  priv->prompt_value = 0;
  g_mutex_unlock (&priv->prompt_lock);

  req->self = g_object_ref (self);
  frdp_main_context_queue (frdp_prompt_on_main, req, frdp_prompt_request_free);

  g_mutex_lock (&priv->prompt_lock);
  while (!priv->prompt_answered && !g_atomic_int_get (&priv->closing))
    g_cond_wait (&priv->prompt_cond, &priv->prompt_lock);
  value = priv->prompt_answered ? priv->prompt_value : 0;
  priv->prompt_pending = FALSE;
  g_mutex_unlock (&priv->prompt_lock);

  return value;
}

void
frdp_session_answer_prompt (FrdpSession *self,
                            guint        value)
{
  FrdpSessionPrivate *priv = self->priv;

  g_mutex_lock (&priv->prompt_lock);
  if (priv->prompt_pending && !priv->prompt_answered) {
    priv->prompt_value = value;
    priv->prompt_answered = TRUE;
    g_cond_broadcast (&priv->prompt_cond);
  }
  g_mutex_unlock (&priv->prompt_lock);
}

/*
 * Return 1 to accept and store a certificate, 2 to accept
 * a certificate only for this session, 0 otherwise.
 */
static DWORD
frdp_certificate_verify_ex (freerdp     *freerdp_session,
                            const char  *host,
                            UINT16       port,
                            const char  *common_name,
                            const char  *subject,
                            const char  *issuer,
                            const char  *fingerprint,
                            DWORD        flags)
{
  FrdpSession       *self = ((frdpContext *) freerdp_session->context)->self;
  FrdpPromptRequest *req = g_new0 (FrdpPromptRequest, 1);

  req->kind = FRDP_PROMPT_CERTIFICATE;
  req->host = g_strdup (host);
  req->port = port;
  req->common_name = g_strdup (common_name);
  req->subject = g_strdup (subject);
  req->issuer = g_strdup (issuer);
  req->fingerprint = g_strdup (fingerprint);
  req->flags = flags;

  return frdp_session_prompt (self, req);
}

static DWORD
frdp_changed_certificate_verify_ex (freerdp     *freerdp_session,
                                    const char  *host,
                                    UINT16       port,
                                    const char  *common_name,
                                    const char  *subject,
                                    const char  *issuer,
                                    const char  *fingerprint,
                                    const char  *old_subject,
                                    const char  *old_issuer,
                                    const char  *old_fingerprint,
                                    DWORD        flags)
{
  FrdpSession       *self = ((frdpContext *) freerdp_session->context)->self;
  FrdpPromptRequest *req = g_new0 (FrdpPromptRequest, 1);

  req->kind = FRDP_PROMPT_CERTIFICATE_CHANGE;
  req->host = g_strdup (host);
  req->port = port;
  req->common_name = g_strdup (common_name);
  req->subject = g_strdup (subject);
  req->issuer = g_strdup (issuer);
  req->fingerprint = g_strdup (fingerprint);
  req->old_subject = g_strdup (old_subject);
  req->old_issuer = g_strdup (old_issuer);
  req->old_fingerprint = g_strdup (old_fingerprint);
  req->flags = flags;

  return frdp_session_prompt (self, req);
}

static BOOL
frdp_authenticate_ex (freerdp          *freerdp_session,
                      char            **username,
                      char            **password,
                      char            **domain,
                      rdp_auth_reason   reason)
{
  FrdpSession       *self = ((frdpContext *) freerdp_session->context)->self;
  FrdpPromptRequest *req = g_new0 (FrdpPromptRequest, 1);

  req->kind = FRDP_PROMPT_AUTHENTICATION;
  if (frdp_session_prompt (self, req) == 0)
    return FALSE;

  /* The main thread stored the answer before waking us (prompt_lock gives
   * the ordering). GLib allocates with malloc, so FreeRDP may free() these. */
  *username = *password = *domain = NULL;
  g_object_get (self,
                "username", username,
                "password", password,
                "domain", domain,
                NULL);

  /* This runs during NLA, before the client-info PDU. A password typed into
   * the prompt is enough for network authentication, but Windows still draws
   * its own logon screen — black, in the framebuffer — unless INFO_AUTOLOGON
   * is set. A password known before connect sets the flag in
   * frdp_session_create_instance(); one that arrives here has to as well. */
  if (password != NULL && *password != NULL && **password != '\0')
    freerdp_settings_set_bool (freerdp_session->context->settings,
                               FreeRDP_AutoLogonEnabled, TRUE);

  return TRUE;
}

/* ---- channels (worker) ----------------------------------------------------- */

static gboolean
frdp_session_caps_set_cb (gpointer user_data)
{
  g_object_set (G_OBJECT (user_data), "monitor-layout-supported", TRUE, NULL);

  return G_SOURCE_REMOVE;
}

static void
caps_set (FrdpChannelDisplayControl *channel,
          gpointer                   user_data)
{
  /* Can fire on the worker; property notifications reach GTK. */
  frdp_session_run_on_main (FRDP_SESSION (user_data), frdp_session_caps_set_cb);
}

static void
frdp_on_channel_connected_event_handler (void                                      *context,
                                         CONST_QUALIFIER ChannelConnectedEventArgs *e)
{
  frdpContext        *ctx = (frdpContext *) context;
  FrdpSession        *session = ctx->self;
  FrdpSessionPrivate *priv = frdp_session_get_instance_private (session);

  if (strcmp (e->name, RDPEI_DVC_CHANNEL_NAME) == 0) {
    // TODO Touch input redirection
  } else if (strcmp (e->name, DISP_DVC_CHANNEL_NAME) == 0) {
    g_mutex_lock (&priv->state_lock);
    g_clear_object (&priv->display_control_channel);
    priv->display_control_channel = g_object_new (FRDP_TYPE_CHANNEL_DISPLAY_CONTROL,
                                                  "session", session,
                                                  "display-client-context", (DispClientContext *) e->pInterface,
                                                  NULL);
    g_signal_connect (priv->display_control_channel, "caps-set", G_CALLBACK (caps_set), session);
    g_mutex_unlock (&priv->state_lock);
  } else if (strcmp (e->name, TSMF_DVC_CHANNEL_NAME) == 0) {
    // TODO Old windows 7 multimedia redirection
  } else if (strcmp (e->name, RDPGFX_DVC_CHANNEL_NAME) == 0) {
    gdi_graphics_pipeline_init (ctx->context.gdi, (RdpgfxClientContext *) e->pInterface);
  } else if (strcmp (e->name, RAIL_SVC_CHANNEL_NAME) == 0) {
    // TODO Remote application
  } else if (strcmp (e->name, CLIPRDR_SVC_CHANNEL_NAME) == 0) {
    g_mutex_lock (&priv->state_lock);
    if (priv->clipboard_channel != NULL)
      frdp_channel_clipboard_detach (priv->clipboard_channel);
    g_clear_object (&priv->clipboard_channel);
    priv->clipboard_channel = frdp_channel_clipboard_new (session, (CliprdrClientContext *) e->pInterface);
    g_mutex_unlock (&priv->state_lock);
  } else if (strcmp (e->name, ENCOMSP_SVC_CHANNEL_NAME) == 0) {
    // TODO Multiparty channel
  } else if (strcmp (e->name, GEOMETRY_DVC_CHANNEL_NAME) == 0) {
    gdi_video_geometry_init (ctx->context.gdi, (GeometryClientContext *) e->pInterface);
  } else if (strcmp (e->name, VIDEO_CONTROL_DVC_CHANNEL_NAME) == 0) {
    gdi_video_control_init (ctx->context.gdi, (VideoClientContext *) e->pInterface);
  } else if (strcmp (e->name, VIDEO_DATA_DVC_CHANNEL_NAME) == 0) {
    gdi_video_data_init (ctx->context.gdi, (VideoClientContext *) e->pInterface);
  }
}

static void
frdp_on_channel_disconnected_event_handler (void                                         *context,
                                            CONST_QUALIFIER ChannelDisconnectedEventArgs *e)
{
  frdpContext        *ctx = (frdpContext *) context;
  FrdpSession        *session = ctx->self;
  FrdpSessionPrivate *priv = frdp_session_get_instance_private (session);

  if (strcmp (e->name, DISP_DVC_CHANNEL_NAME) == 0) {
    g_mutex_lock (&priv->state_lock);
    g_clear_object (&priv->display_control_channel);
    g_mutex_unlock (&priv->state_lock);
  } else if (strcmp (e->name, RDPGFX_DVC_CHANNEL_NAME) == 0) {
    gdi_graphics_pipeline_uninit (ctx->context.gdi, (RdpgfxClientContext *) e->pInterface);
  } else if (strcmp (e->name, CLIPRDR_SVC_CHANNEL_NAME) == 0) {
    g_mutex_lock (&priv->state_lock);
    if (priv->clipboard_channel != NULL)
      frdp_channel_clipboard_detach (priv->clipboard_channel);
    g_clear_object (&priv->clipboard_channel);
    g_mutex_unlock (&priv->state_lock);
  } else if (strcmp (e->name, GEOMETRY_DVC_CHANNEL_NAME) == 0) {
    gdi_video_geometry_uninit (ctx->context.gdi, (GeometryClientContext *) e->pInterface);
  } else if (strcmp (e->name, VIDEO_CONTROL_DVC_CHANNEL_NAME) == 0) {
    gdi_video_control_uninit (ctx->context.gdi, (VideoClientContext *) e->pInterface);
  } else if (strcmp (e->name, VIDEO_DATA_DVC_CHANNEL_NAME) == 0) {
    gdi_video_data_uninit (ctx->context.gdi, (VideoClientContext *) e->pInterface);
  }
}

static BOOL
frdp_load_channels (freerdp *instance)
{
  if (!freerdp_client_load_addins (instance->context->channels, instance->context->settings))
    return FALSE;

  return TRUE;
}

static BOOL
frdp_pre_connect (freerdp *freerdp_session)
{
  rdpSettings *settings = freerdp_session->context->settings;
  rdpContext *context = freerdp_session->context;

  /* freerdp_connect() resets the abort event before calling us, so a
   * close() that came just before it would otherwise be lost. */
  if (g_atomic_int_get (&((frdpContext *) context)->self->priv->closing))
    return FALSE;
  BYTE *OrderSupport = freerdp_settings_get_pointer_writable(settings, FreeRDP_OrderSupport);

  if (OrderSupport != NULL) {
    OrderSupport[NEG_DSTBLT_INDEX] = TRUE;
    OrderSupport[NEG_PATBLT_INDEX] = TRUE;
    OrderSupport[NEG_SCRBLT_INDEX] = TRUE;
    OrderSupport[NEG_OPAQUE_RECT_INDEX] = TRUE;
    OrderSupport[NEG_DRAWNINEGRID_INDEX] = FALSE;
    OrderSupport[NEG_MULTIDSTBLT_INDEX] = FALSE;
    OrderSupport[NEG_MULTIPATBLT_INDEX] = FALSE;
    OrderSupport[NEG_MULTISCRBLT_INDEX] = FALSE;
    OrderSupport[NEG_MULTIOPAQUERECT_INDEX] = TRUE;
    OrderSupport[NEG_MULTI_DRAWNINEGRID_INDEX] = FALSE;
    OrderSupport[NEG_LINETO_INDEX] = TRUE;
    OrderSupport[NEG_POLYLINE_INDEX] = TRUE;
    OrderSupport[NEG_MEMBLT_INDEX] = TRUE;
    OrderSupport[NEG_MEM3BLT_INDEX] = FALSE;
    OrderSupport[NEG_MEMBLT_V2_INDEX] = TRUE;
    OrderSupport[NEG_MEM3BLT_V2_INDEX] = FALSE;
    OrderSupport[NEG_SAVEBITMAP_INDEX] = FALSE;
    OrderSupport[NEG_GLYPH_INDEX_INDEX] = TRUE;
    OrderSupport[NEG_FAST_INDEX_INDEX] = TRUE;
    OrderSupport[NEG_FAST_GLYPH_INDEX] = FALSE;
    OrderSupport[NEG_POLYGON_SC_INDEX] = FALSE;
    OrderSupport[NEG_POLYGON_CB_INDEX] = FALSE;
    OrderSupport[NEG_ELLIPSE_SC_INDEX] = FALSE;
    OrderSupport[NEG_ELLIPSE_CB_INDEX] = FALSE;
  }

  PubSub_SubscribeChannelConnected (context->pubSub,
                                    frdp_on_channel_connected_event_handler);
  PubSub_SubscribeChannelDisconnected (context->pubSub,
                                       frdp_on_channel_disconnected_event_handler);

  return TRUE;
}

/* Worker. */
static BOOL
frdp_post_connect (freerdp *freerdp_session)
{
  rdpContext         *context = freerdp_session->context;
  rdpSettings        *settings = context->settings;
  FrdpSession        *self = ((frdpContext *) context)->self;
  FrdpSessionPrivate *priv = self->priv;
  ResizeWindowEventArgs e;

  g_mutex_lock (&priv->frame_lock);
  /* GTK4 has no visuals to probe; GdkMemoryTexture takes 32-bit BGRX. */
  if (!gdi_init (freerdp_session, PIXEL_FORMAT_BGRX32)) {
    g_mutex_unlock (&priv->frame_lock);
    return FALSE;
  }
  priv->gdi_valid = TRUE;
  priv->desktop_width = context->gdi->width;
  priv->desktop_height = context->gdi->height;
  priv->frame_dirty = TRUE;
  g_mutex_unlock (&priv->frame_lock);

  frdp_register_pointer (context->graphics);
  context->update->BeginPaint = frdp_begin_paint;
  context->update->EndPaint = frdp_end_paint;
  context->update->DesktopResize = frdp_desktop_resize;

  EventArgsInit (&e, "frdp");
  e.width = freerdp_settings_get_uint32 (settings, FreeRDP_DesktopWidth);
  e.height = freerdp_settings_get_uint32 (settings, FreeRDP_DesktopHeight);
  PubSub_OnResizeWindow (context->pubSub, context, &e);

  return TRUE;
}

/* Worker (inside freerdp_disconnect). */
static void
frdp_post_disconnect (freerdp *instance)
{
  FrdpSession        *self;
  FrdpSessionPrivate *priv;
  rdpContext         *context;

  if (!instance || !instance->context)
    return;

  context = instance->context;
  self = ((frdpContext *) context)->self;
  priv = self->priv;

  PubSub_UnsubscribeChannelConnected (context->pubSub,
                                      frdp_on_channel_connected_event_handler);
  PubSub_UnsubscribeChannelDisconnected (context->pubSub,
                                         frdp_on_channel_disconnected_event_handler);

  frdp_pointer_set_null (context);

  g_mutex_lock (&priv->frame_lock);
  priv->gdi_valid = FALSE;
  gdi_free (instance);
  g_mutex_unlock (&priv->frame_lock);
}

/* Main thread: layout detection may ask GDK for the keyboard device. */
static guint32
frdp_session_resolve_keyboard_layout (FrdpSession *self)
{
  const char *source = "property";
  guint32     layout = self->priv->keyboard_layout;
  gint64      t_start = g_get_monotonic_time ();

  if (layout == 0)
    layout = frdp_keyboard_layout_detect (&source);

  g_debug ("Keyboard layout 0x%08x (%s, from %s) in %.1f ms on the main thread", layout,
           freerdp_keyboard_get_layout_name_from_id (layout), source,
           (g_get_monotonic_time () - t_start) / 1000.0);
  return layout;
}

/* Session thread: build the FreeRDP instance. Its first creation in a
 * process initialises FreeRDP's codecs and channels, which took 0.4-0.6 s
 * on macOS when this ran on the main thread. Returns NULL on failure. */
static freerdp *
frdp_session_create_instance (FrdpSession       *self,
                              FrdpConnectParams *params)
{
  CONST_QUALIFIER gchar *collections[] = { "disp" };
  freerdp               *instance;
  rdpSettings           *settings;
  gchar                 *build_options;
  int                    count = 1;
  gint64                 t_start = g_get_monotonic_time ();
  gint64                 t_context;

  instance = freerdp_new ();
  if (instance == NULL)
    return NULL;

  instance->PreConnect = frdp_pre_connect;
  instance->PostConnect = frdp_post_connect;
  instance->PostDisconnect = frdp_post_disconnect;
  instance->AuthenticateEx = frdp_authenticate_ex;
  instance->VerifyCertificateEx = frdp_certificate_verify_ex;
  instance->VerifyChangedCertificateEx = frdp_changed_certificate_verify_ex;
  instance->LoadChannels = frdp_load_channels;

  instance->ContextSize = sizeof (frdpContext);

  if (!freerdp_context_new (instance)) {
    freerdp_free (instance);
    return NULL;
  }

  ((frdpContext *) instance->context)->self = self;
  t_context = g_get_monotonic_time ();

  settings = instance->context->settings;

  freerdp_settings_set_string (settings, FreeRDP_ServerHostname, params->hostname);
  freerdp_settings_set_uint32 (settings, FreeRDP_ServerPort, params->port);
  freerdp_settings_set_string (settings, FreeRDP_Username, params->username);
  freerdp_settings_set_string (settings, FreeRDP_Password, params->password);
  freerdp_settings_set_string (settings, FreeRDP_Domain, params->domain);

  /* Without INFO_AUTOLOGON Windows authenticates the connection (NLA) and
   * then stops at its own logon screen; some hosts draw that black and drop
   * the session after ~55 s (ERRINFO_RPC_INITIATED_DISCONNECT). A password
   * entered later, in the authentication prompt, sets the same flag from
   * frdp_authenticate_ex(). */
  if (params->password != NULL && *params->password != '\0')
    freerdp_settings_set_bool (settings, FreeRDP_AutoLogonEnabled, TRUE);

  freerdp_settings_set_bool (settings, FreeRDP_AllowFontSmoothing, TRUE);
  freerdp_settings_set_bool (settings, FreeRDP_AllowUnanouncedOrdersFromServer, TRUE);

  /* Security settings */
  freerdp_settings_set_bool (settings, FreeRDP_RdpSecurity, TRUE);
  freerdp_settings_set_bool (settings, FreeRDP_TlsSecurity, TRUE);
  freerdp_settings_set_bool (settings, FreeRDP_NlaSecurity, TRUE);
  freerdp_settings_set_uint32 (settings, FreeRDP_EncryptionMethods, ENCRYPTION_METHOD_40BIT | ENCRYPTION_METHOD_128BIT | ENCRYPTION_METHOD_FIPS);
  freerdp_settings_set_uint32 (settings, FreeRDP_EncryptionLevel, ENCRYPTION_LEVEL_CLIENT_COMPATIBLE);
  freerdp_settings_set_bool (settings, FreeRDP_UseRdpSecurityLayer, FALSE);

  freerdp_settings_set_bool (settings, FreeRDP_NegotiateSecurityLayer, TRUE);

  freerdp_settings_set_bool (settings, FreeRDP_DesktopResize, TRUE);
  freerdp_settings_set_bool (settings, FreeRDP_DynamicResolutionUpdate, TRUE);
  freerdp_settings_set_bool (settings, FreeRDP_SupportDisplayControl, TRUE);
  freerdp_settings_set_bool (settings, FreeRDP_RemoteFxCodec, TRUE);
  freerdp_settings_set_uint32 (settings, FreeRDP_ColorDepth, 32);
  /* FreeRDP's add-in loader turns these into the cliprdr / rdpsnd channels;
   * rdpsnd plays the audio itself (PulseAudio, ALSA, ...). */
  freerdp_settings_set_bool (settings, FreeRDP_RedirectClipboard, params->clipboard);
  freerdp_settings_set_bool (settings, FreeRDP_AudioPlayback, params->audio_playback);
  freerdp_settings_set_bool (settings, FreeRDP_SupportGraphicsPipeline, TRUE);

  freerdp_client_add_dynamic_channel (settings, count, collections);

  build_options = g_ascii_strup (freerdp_get_build_config (), -1);
  if (g_strrstr (build_options, "WITH_GFX_H264=ON") != NULL) {
    freerdp_settings_set_bool (settings, FreeRDP_GfxH264, TRUE);
    freerdp_settings_set_bool (settings, FreeRDP_GfxAVC444, TRUE);
  } else {
    freerdp_settings_set_bool (settings, FreeRDP_GfxH264, FALSE);
    freerdp_settings_set_bool (settings, FreeRDP_GfxAVC444, FALSE);
  }
  g_free (build_options);

  freerdp_settings_set_uint32 (settings, FreeRDP_KeyboardLayout, params->keyboard_layout);

  freerdp_register_addin_provider(freerdp_channels_load_static_addin_entry, 0);

  g_debug ("FreeRDP instance ready on the session thread: context %.1f ms, settings %.1f ms",
           (t_context - t_start) / 1000.0, (g_get_monotonic_time () - t_context) / 1000.0);

  return instance;
}

static void
frdp_instance_free (freerdp *instance)
{
  /* freerdp_free() frees only the instance struct (FreeRDP 3.32.1
   * libfreerdp/core/freerdp.c); upstream never freed the context. */
  if (instance->context != NULL)
    freerdp_context_free (instance);
  freerdp_free (instance);
}

/* ---- session thread --------------------------------------------------------- */

static gboolean frdp_session_connect_done_cb (gpointer user_data);
static gboolean frdp_session_worker_done_cb (gpointer user_data);

static gpointer
frdp_session_worker (gpointer data)
{
  FrdpSession        *self = data;
  FrdpSessionPrivate *priv = self->priv;
  freerdp            *instance;
  rdpContext         *context;

  instance = frdp_session_create_instance (self, priv->connect_params);
  g_clear_pointer (&priv->connect_params, frdp_connect_params_free);

  g_mutex_lock (&priv->instance_lock);
  if (instance != NULL && !g_atomic_int_get (&priv->closing))
    priv->freerdp_session = instance;
  g_mutex_unlock (&priv->instance_lock);

  if (priv->freerdp_session == NULL) {
    /* Setup failed, or close() came first. */
    priv->init_failed = (instance == NULL);
    g_clear_pointer (&instance, frdp_instance_free);
    priv->connect_ok = FALSE;
    priv->connect_error = FREERDP_ERROR_CONNECT_FAILED;
    priv->disconnect_error = FREERDP_ERROR_SUCCESS;
    frdp_session_run_on_main (self, frdp_session_connect_done_cb);
    frdp_session_run_on_main (self, frdp_session_worker_done_cb);
    return NULL;
  }
  context = instance->context;

  priv->connect_ok = freerdp_connect (instance);
  priv->connect_error = priv->connect_ok ? FREERDP_ERROR_SUCCESS
                                         : freerdp_get_last_error (context);
  frdp_session_run_on_main (self, frdp_session_connect_done_cb);

  while (priv->connect_ok && !g_atomic_int_get (&priv->closing) &&
         !freerdp_shall_disconnect_context (context)) {
    HANDLE handles[MAXIMUM_WAIT_OBJECTS];
    DWORD  n, status;

    n = freerdp_get_event_handles (context, handles, ARRAYSIZE (handles));
    if (n == 0) {
      g_warning ("Failed to get FreeRDP event handles");
      break;
    }

    status = WaitForMultipleObjects (n, handles, FALSE, FRDP_WORKER_WAIT_MS);
    if (status == WAIT_FAILED)
      break;

    if (!freerdp_check_event_handles (context)) {
      if (freerdp_get_last_error (context) == FREERDP_ERROR_SUCCESS)
        g_warning ("Failed to check FreeRDP event handles");
      break;
    }
  }

  priv->disconnect_error = freerdp_get_last_error (context);
  freerdp_disconnect (instance);

  frdp_session_run_on_main (self, frdp_session_worker_done_cb);

  return NULL;
}

/* FreeRDP 3.26 added the setting that records an automatic retry (see
 * frdp_session_first_connect_error()). Older builds keep the code they got. */
#if FREERDP_VERSION_MAJOR > 3 || \
    (FREERDP_VERSION_MAJOR == 3 && FREERDP_VERSION_MINOR >= 26)
#define FRDP_HAVE_RECONNECT_SETTING 1
#endif

static void
frdp_session_set_last_error_code (FrdpSession *self,
                                  guint32      error_code)
{
  if (self->priv->last_error_code == error_code)
    return;

  self->priv->last_error_code = error_code;
  g_object_notify (G_OBJECT (self), "last-error-code");
}

/* freerdp_connect() retries a failed connect once when the first attempt
 * ended in FREERDP_ERROR_CONNECT_TRANSPORT_FAILED (freerdp.c). The retry
 * clears the recorded error (rdp_client_disconnect_and_clear()), so a retry
 * that then fails at the TCP layer leaves only the generic
 * FREERDP_ERROR_CONNECT_FAILED: a server that refused the logon by closing
 * the connection looks like a host that cannot be reached. The retry itself
 * proves what the first error was, so report that one. */
static guint32
frdp_session_first_connect_error (FrdpSession *self,
                                  guint32      error_code)
{
#ifdef FRDP_HAVE_RECONNECT_SETTING
  FrdpSessionPrivate *priv = self->priv;

  if (error_code == FREERDP_ERROR_CONNECT_FAILED &&
      priv->freerdp_session != NULL &&
      freerdp_settings_get_bool (priv->freerdp_session->context->settings,
                                 FreeRDP_SessionHasBeenReconnected))
    return FREERDP_ERROR_CONNECT_TRANSPORT_FAILED;
#else
  (void) self;
#endif
  return error_code;
}

/* Only the server refusing the credentials is an authentication failure.
 * The rest of FreeRDP's connect errors (unreachable host, DNS, TLS, security
 * negotiation, the transport breaking) say nothing about the logon and are
 * reported on rdp-error. */
static gboolean
frdp_error_is_authentication (guint32 error_code)
{
  switch (error_code) {
  case FREERDP_ERROR_AUTHENTICATION_FAILED:
  case FREERDP_ERROR_CONNECT_NO_OR_MISSING_CREDENTIALS:
  case FREERDP_ERROR_CONNECT_LOGON_FAILURE:
  case FREERDP_ERROR_CONNECT_WRONG_PASSWORD:
  case FREERDP_ERROR_CONNECT_ACCESS_DENIED:
  case FREERDP_ERROR_CONNECT_ACCOUNT_DISABLED:
  case FREERDP_ERROR_CONNECT_ACCOUNT_RESTRICTION:
  case FREERDP_ERROR_CONNECT_ACCOUNT_LOCKED_OUT:
  case FREERDP_ERROR_CONNECT_ACCOUNT_EXPIRED:
  case FREERDP_ERROR_CONNECT_PASSWORD_EXPIRED:
  case FREERDP_ERROR_CONNECT_PASSWORD_CERTAINLY_EXPIRED:
  case FREERDP_ERROR_CONNECT_PASSWORD_MUST_CHANGE:
  case FREERDP_ERROR_CONNECT_LOGON_TYPE_NOT_GRANTED:
    return TRUE;
  default:
    return FALSE;
  }
}

static void
frdp_session_report_connect_failure (FrdpSession *self,
                                     guint32      error_code)
{
  const gchar *message;

  error_code = frdp_session_first_connect_error (self, error_code);
  frdp_session_set_last_error_code (self, error_code);
  message = freerdp_get_last_error_string (error_code);

  if (frdp_error_is_authentication (error_code)) {
    g_debug ("RDP host refused the logon: '%s'", message);
    g_signal_emit (self, signals[RDP_AUTH_FAILURE], 0, message);
  } else {
    g_debug ("RDP connection failed: '%s'", message);
    g_signal_emit (self, signals[RDP_ERROR], 0, message);
  }
}

/* Main thread: freerdp_connect() returned on the worker. */
static gboolean
frdp_session_connect_done_cb (gpointer user_data)
{
  FrdpSession        *self = user_data;
  FrdpSessionPrivate *priv = self->priv;
  GTask              *task = g_steal_pointer (&priv->connect_task);
  gboolean            cancelled = g_atomic_int_get (&priv->closing);

  if (priv->connect_ok && !cancelled) {
    priv->is_connected = TRUE;
    if (priv->display != NULL)
      g_signal_connect (priv->display, "notify::resize-supported",
                        G_CALLBACK (frdp_session_resize_supported_changed), self);
    frdp_session_update_size_request (self);
    frdp_session_update_scale (self);
    frdp_session_update_mouse_pointer (self);
    frdp_session_schedule_draw (self);
    frdp_session_request_remote_resize (self);
    g_signal_emit (self, signals[RDP_CONNECTED], 0);
  } else if (priv->init_failed && !cancelled) {
    g_signal_emit (self, signals[RDP_ERROR], 0, "Failed to initialize RDP!");
  } else if (!priv->connect_ok && !cancelled) {
    frdp_session_report_connect_failure (self, priv->connect_error);
  }

  if (task != NULL) {
    if (priv->connect_ok && !cancelled)
      g_task_return_boolean (task, TRUE);
    else if (cancelled)
      g_task_return_new_error (task, G_IO_ERROR, G_IO_ERROR_CANCELLED, "Connection cancelled");
    else
      g_task_return_new_error (task, G_IO_ERROR, G_IO_ERROR_FAILED, "%s",
                               freerdp_get_last_error_string (priv->connect_error));
    g_object_unref (task);
  }

  return G_SOURCE_REMOVE;
}

static void
frdp_session_free_instance (FrdpSession *self)
{
  FrdpSessionPrivate *priv = self->priv;

  /* Main thread, after the worker has been joined. */
  g_clear_pointer (&priv->freerdp_session, frdp_instance_free);
  priv->init_failed = FALSE;
}

/* Main thread: the worker has finished (disconnect, error or close). */
static gboolean
frdp_session_worker_done_cb (gpointer user_data)
{
  FrdpSession        *self = user_data;
  FrdpSessionPrivate *priv = self->priv;
  gboolean            cancelled = g_atomic_int_get (&priv->closing);
  gboolean            was_connected = priv->is_connected;
  guint32             error = priv->disconnect_error;

  g_thread_join (g_steal_pointer (&priv->worker));

  priv->is_connected = FALSE;

  if (priv->resize_timeout_id != 0) {
    g_source_remove (priv->resize_timeout_id);
    priv->resize_timeout_id = 0;
  }

  if (priv->display != NULL)
    g_signal_handlers_disconnect_by_func (priv->display,
                                          G_CALLBACK (frdp_session_resize_supported_changed),
                                          self);

  frdp_session_free_instance (self);

  g_clear_object (&priv->frame);
  g_mutex_lock (&priv->state_lock);
  g_clear_object (&priv->cursor_texture);
  g_mutex_unlock (&priv->state_lock);
  priv->monitor_layout_supported = FALSE;

  if (priv->display != NULL) {
    frdp_session_update_mouse_pointer (self);
    gtk_widget_queue_draw (priv->display);
  }

  g_atomic_int_set (&priv->closing, 0);

  if (was_connected && !cancelled && error != FREERDP_ERROR_SUCCESS) {
    frdp_session_set_last_error_code (self, error);
    g_signal_emit (self, signals[RDP_ERROR], 0, freerdp_get_last_error_string (error));
  }

  g_signal_emit (self, signals[RDP_DISCONNECTED], 0);
  g_debug ("RDP client disconnected");

#ifdef __GLIBC__
  /* Each session thread gets its own glibc malloc arena, and freed arena
   * memory is not returned to the OS by default: without this RSS grew
   * ~20 MB per session (251 -> 440 MB over 10) although nothing leaked
   * (flat at 246 MB with MALLOC_ARENA_MAX=2). */
  malloc_trim (0);
#endif

  /* Balances the reference taken when the worker started. */
  g_object_unref (self);

  return G_SOURCE_REMOVE;
}

static void
frdp_session_get_property (GObject    *object,
                           guint       property_id,
                           GValue     *value,
                           GParamSpec *pspec)
{
  FrdpSession *self = (FrdpSession*) object;

  switch (property_id)
    {
      case PROP_HOSTNAME:
        g_value_set_string (value, self->priv->hostname);
        break;
      case PROP_PORT:
        g_value_set_uint (value, self->priv->port);
        break;
      case PROP_USERNAME:
        g_value_set_string (value, self->priv->username);
        break;
      case PROP_PASSWORD:
        g_value_set_string (value, self->priv->password);
        break;
      case PROP_DOMAIN:
        g_value_set_string (value, self->priv->domain);
        break;
      case PROP_KEYBOARD_LAYOUT:
        g_value_set_uint (value, self->priv->keyboard_layout);
        break;
      case PROP_CLIPBOARD:
        g_value_set_boolean (value, self->priv->clipboard);
        break;
      case PROP_AUDIO_PLAYBACK:
        g_value_set_boolean (value, self->priv->audio_playback);
        break;
      case PROP_DISPLAY:
        g_value_set_object (value, self->priv->display);
        break;
      case PROP_SCALING:
        g_value_set_boolean (value, self->priv->scaling);
        break;
      case PROP_MONITOR_LAYOUT_SUPPORTED:
        g_value_set_boolean (value, self->priv->monitor_layout_supported);
        break;
      case PROP_LAST_ERROR_CODE:
        g_value_set_uint (value, self->priv->last_error_code);
        break;
      default:
        G_OBJECT_WARN_INVALID_PROPERTY_ID (object, property_id, pspec);
        break;
    }
}

static void
frdp_session_set_property (GObject      *object,
                           guint         property_id,
                           const GValue *value,
                           GParamSpec   *pspec)
{
  FrdpSession *self = (FrdpSession*) object;

  switch (property_id)
    {
      case PROP_HOSTNAME:
        g_free (self->priv->hostname);
        self->priv->hostname = g_value_dup_string (value);
        break;
      case PROP_PORT:
        self->priv->port = g_value_get_uint (value);
        break;
      case PROP_USERNAME:
        g_free (self->priv->username);
        self->priv->username = g_value_dup_string (value);
        break;
      case PROP_PASSWORD:
        g_free (self->priv->password);
        self->priv->password = g_value_dup_string (value);
        break;
      case PROP_DOMAIN:
        g_free (self->priv->domain);
        self->priv->domain = g_value_dup_string (value);
        break;
      case PROP_KEYBOARD_LAYOUT:
        self->priv->keyboard_layout = g_value_get_uint (value);
        break;
      case PROP_CLIPBOARD:
        self->priv->clipboard = g_value_get_boolean (value);
        break;
      case PROP_AUDIO_PLAYBACK:
        self->priv->audio_playback = g_value_get_boolean (value);
        break;
      case PROP_DISPLAY:
        self->priv->display = g_value_get_object (value);
        break;
      case PROP_SCALING:
        frdp_session_set_scaling (self, g_value_get_boolean (value));
        break;
      case PROP_MONITOR_LAYOUT_SUPPORTED:
        self->priv->monitor_layout_supported = g_value_get_boolean (value);
        g_object_notify (G_OBJECT (self), "monitor-layout-supported");
        break;
      default:
        G_OBJECT_WARN_INVALID_PROPERTY_ID (object, property_id, pspec);
        break;
    }
}

static void
frdp_session_finalize (GObject *object)
{
  FrdpSession        *self = (FrdpSession*) object;
  FrdpSessionPrivate *priv = self->priv;

  /* The worker holds a reference, so it cannot be running here; only an
   * instance whose connect never started can remain. */
  g_warn_if_fail (priv->worker == NULL);
  frdp_session_free_instance (self);

  g_clear_object (&priv->frame);
  g_clear_object (&priv->cursor_texture);
  g_clear_object (&priv->display_control_channel);
  g_clear_object (&priv->clipboard_channel);

  g_mutex_clear (&priv->frame_lock);
  g_mutex_clear (&priv->state_lock);
  g_mutex_clear (&priv->prompt_lock);
  g_mutex_clear (&priv->instance_lock);
  g_cond_clear (&priv->prompt_cond);

  g_free (priv->hostname);
  g_free (priv->username);
  if (priv->password != NULL)
    memset (priv->password, 0, strlen (priv->password));
  g_free (priv->password);
  g_free (priv->domain);

  G_OBJECT_CLASS (frdp_session_parent_class)->finalize (object);
}

static void
frdp_session_class_init (FrdpSessionClass *klass)
{
  GObjectClass *gobject_class = G_OBJECT_CLASS (klass);

  gobject_class->finalize = frdp_session_finalize;
  gobject_class->get_property = frdp_session_get_property;
  gobject_class->set_property = frdp_session_set_property;

  g_object_class_install_property (gobject_class,
                                   PROP_HOSTNAME,
                                   g_param_spec_string ("hostname",
                                                        "hostname",
                                                        "hostname",
                                                        NULL,
                                                        G_PARAM_STATIC_STRINGS | G_PARAM_READWRITE));

  g_object_class_install_property (gobject_class,
                                   PROP_PORT,
                                   g_param_spec_uint ("port",
                                                      "port",
                                                      "port",
                                                       0, G_MAXUINT16, 3389,
                                                       G_PARAM_STATIC_STRINGS | G_PARAM_READWRITE));

  g_object_class_install_property (gobject_class,
                                   PROP_USERNAME,
                                   g_param_spec_string ("username",
                                                        "username",
                                                        "username",
                                                        NULL,
                                                        G_PARAM_STATIC_STRINGS | G_PARAM_READWRITE));

  g_object_class_install_property (gobject_class,
                                   PROP_PASSWORD,
                                   g_param_spec_string ("password",
                                                        "password",
                                                        "password",
                                                        NULL,
                                                        G_PARAM_STATIC_STRINGS | G_PARAM_READWRITE));

  g_object_class_install_property (gobject_class,
                                   PROP_DOMAIN,
                                   g_param_spec_string ("domain",
                                                        "domain",
                                                        "domain",
                                                        NULL,
                                                        G_PARAM_STATIC_STRINGS | G_PARAM_READWRITE));

  g_object_class_install_property (gobject_class,
                                   PROP_KEYBOARD_LAYOUT,
                                   g_param_spec_uint ("keyboard-layout", NULL, NULL,
                                                      0, G_MAXUINT32, 0,
                                                      G_PARAM_STATIC_STRINGS | G_PARAM_READWRITE));

  g_object_class_install_property (gobject_class, PROP_CLIPBOARD,
                                   g_param_spec_boolean ("clipboard", NULL, NULL, TRUE,
                                                         G_PARAM_STATIC_STRINGS | G_PARAM_READWRITE));

  g_object_class_install_property (gobject_class, PROP_AUDIO_PLAYBACK,
                                   g_param_spec_boolean ("audio-playback", NULL, NULL, FALSE,
                                                         G_PARAM_STATIC_STRINGS | G_PARAM_READWRITE));

  g_object_class_install_property (gobject_class,
                                   PROP_DISPLAY,
                                   g_param_spec_object ("display",
                                                        "display",
                                                        "display",
                                                        GTK_TYPE_WIDGET,
                                                        G_PARAM_READWRITE));

  g_object_class_install_property (gobject_class,
                                   PROP_SCALING,
                                   g_param_spec_boolean ("scaling",
                                                         "scaling",
                                                         "scaling",
                                                         TRUE,
                                                         G_PARAM_READWRITE));

  g_object_class_install_property (gobject_class,
                                   PROP_MONITOR_LAYOUT_SUPPORTED,
                                   g_param_spec_boolean ("monitor-layout-supported",
                                                         "monitor-layout-supported",
                                                         "monitor-layout-supported",
                                                         FALSE,
                                                         G_PARAM_READWRITE));

  /**
   * FrdpSession:last-error-code:
   *
   * FreeRDP's code for the error that ended the last connection attempt, or
   * 0 when none did. It is set before `rdp-error` and `rdp-auth-failure` are
   * emitted, so a handler can name the failure exactly instead of parsing
   * the message.
   */
  g_object_class_install_property (gobject_class,
                                   PROP_LAST_ERROR_CODE,
                                   g_param_spec_uint ("last-error-code",
                                                      "last-error-code",
                                                      "FreeRDP error code of the last failure",
                                                      0, G_MAXUINT32, 0,
                                                      G_PARAM_STATIC_STRINGS | G_PARAM_READABLE));

  signals[RDP_ERROR] = g_signal_new ("rdp-error",
                                     FRDP_TYPE_SESSION,
                                     G_SIGNAL_RUN_FIRST,
                                     0, NULL, NULL, NULL,
                                     G_TYPE_NONE, 1,
                                     G_TYPE_STRING);

  signals[RDP_CONNECTED] = g_signal_new ("rdp-connected",
                                         FRDP_TYPE_SESSION,
                                         G_SIGNAL_RUN_FIRST,
                                         0, NULL, NULL, NULL,
                                         G_TYPE_NONE, 0);

  signals[RDP_DISCONNECTED] = g_signal_new ("rdp-disconnected",
                                            FRDP_TYPE_SESSION,
                                            G_SIGNAL_RUN_FIRST,
                                            0, NULL, NULL, NULL,
                                            G_TYPE_NONE, 0);

  signals[RDP_AUTH_FAILURE] = g_signal_new ("rdp-auth-failure",
                                            FRDP_TYPE_SESSION,
                                            G_SIGNAL_RUN_FIRST,
                                            0, NULL, NULL, NULL,
                                            G_TYPE_NONE, 1,
                                            G_TYPE_STRING);
}

static void
frdp_session_init (FrdpSession *self)
{
  self->priv = frdp_session_get_instance_private (self);

  g_mutex_init (&self->priv->frame_lock);
  g_mutex_init (&self->priv->state_lock);
  g_mutex_init (&self->priv->prompt_lock);
  g_mutex_init (&self->priv->instance_lock);
  g_cond_init (&self->priv->prompt_cond);

  self->priv->is_connected = FALSE;
  self->priv->scale = 1.0;
  self->priv->cursor_scale_applied = 1.0;
  self->priv->clipboard = TRUE;
}

FrdpSession*
frdp_session_new (FrdpDisplay *display)
{
  return g_object_new (FRDP_TYPE_SESSION,
                       "display", display,
                       NULL);
}

/**
 * frdp_session_connect:
 *
 * Starts connecting on the session thread and returns immediately; the
 * callback runs on the main thread once freerdp_connect() has finished.
 */
void
frdp_session_connect (FrdpSession         *self,
                      const gchar         *hostname,
                      guint                port,
                      GCancellable        *cancellable,
                      GAsyncReadyCallback  callback,
                      gpointer             user_data)
{
  FrdpSessionPrivate *priv = self->priv;
  GTask              *task;

  task = g_task_new (self, cancellable, callback, user_data);

  if (priv->worker != NULL) {
    g_task_return_new_error (task, G_IO_ERROR, G_IO_ERROR_BUSY,
                             "A connection is already active");
    g_object_unref (task);
    return;
  }

  frdp_session_set_last_error_code (self, FREERDP_ERROR_SUCCESS);

  g_free (priv->hostname);
  priv->hostname = g_strdup (hostname);
  priv->port = port;

  priv->connect_params = g_new0 (FrdpConnectParams, 1);
  priv->connect_params->hostname = g_strdup (priv->hostname);
  priv->connect_params->username = g_strdup (priv->username);
  priv->connect_params->password = g_strdup (priv->password);
  priv->connect_params->domain = g_strdup (priv->domain);
  priv->connect_params->port = priv->port;
  priv->connect_params->clipboard = priv->clipboard;
  priv->connect_params->audio_playback = priv->audio_playback;
  priv->connect_params->keyboard_layout = frdp_session_resolve_keyboard_layout (self);
  priv->init_failed = FALSE;

  g_atomic_int_set (&priv->closing, 0);
  priv->connect_task = task;
  /* Released in frdp_session_worker_done_cb() on the main thread, so the
   * last reference can never be dropped on the worker. */
  g_object_ref (self);
  priv->worker = g_thread_new ("frdp-session", frdp_session_worker, self);
}

gboolean
frdp_session_connect_finish (FrdpSession   *self,
                             GAsyncResult  *result,
                             GError       **error)
{
  g_return_val_if_fail (g_task_is_valid (result, self), FALSE);

  return g_task_propagate_boolean (G_TASK (result), error);
}

/* Main thread. The display widget, or NULL once it has been disposed. */
GtkWidget *
frdp_session_get_display_widget (FrdpSession *self)
{
  return self->priv->display;
}

gboolean
frdp_session_is_open (FrdpSession *self)
{
  return self->priv->is_connected;
}

/**
 * frdp_session_close:
 *
 * Requests the session to end. Non-blocking: the worker is aborted (also
 * while still connecting or waiting for a prompt answer) and
 * #FrdpSession::rdp-disconnected is emitted on the main thread once it has
 * finished and the FreeRDP context has been freed.
 */
void
frdp_session_close (FrdpSession *self)
{
  FrdpSessionPrivate *priv = self->priv;

  if (priv->worker == NULL)
    return;

  if (!g_atomic_int_compare_and_exchange (&priv->closing, 0, 1))
    return;

  /* Stop sending input immediately. */
  priv->is_connected = FALSE;

  /* Wake a worker that is waiting for a prompt answer. */
  g_mutex_lock (&priv->prompt_lock);
  g_cond_broadcast (&priv->prompt_cond);
  g_mutex_unlock (&priv->prompt_lock);

  /* The worker may still be creating the instance: it checks closing
   * before publishing it, and frdp_pre_connect() checks again after
   * freerdp_connect() has reset FreeRDP's abort event. */
  g_mutex_lock (&priv->instance_lock);
  if (priv->freerdp_session != NULL && priv->freerdp_session->context != NULL)
    freerdp_abort_connect_context (priv->freerdp_session->context);
  g_mutex_unlock (&priv->instance_lock);

  g_debug ("Closing RDP session");
}

void
frdp_session_mouse_event (FrdpSession          *self,
                          FrdpMouseEvent        event,
                          guint16               x,
                          guint16               y)
{
  FrdpSessionPrivate *priv = self->priv;
  rdpInput *input;
  guint16 flags = 0;
  guint16 xflags = 0;

  /* Main thread only; dropped while connecting or closing. */
  if (!priv->is_connected || priv->freerdp_session == NULL)
    return;

  if (event & FRDP_MOUSE_EVENT_MOVE)
    flags |= PTR_FLAGS_MOVE;
  if (event & FRDP_MOUSE_EVENT_DOWN)
    flags |= PTR_FLAGS_DOWN;
  if (event & FRDP_MOUSE_EVENT_WHEEL) {
    flags |= PTR_FLAGS_WHEEL;
    if (event & FRDP_MOUSE_EVENT_WHEEL_NEGATIVE)
      flags |= PTR_FLAGS_WHEEL_NEGATIVE | 0x0088;
    else
      flags |= 0x0078;
  }
  if (event & FRDP_MOUSE_EVENT_HWHEEL) {
    flags |= PTR_FLAGS_HWHEEL;
    if (event & FRDP_MOUSE_EVENT_WHEEL_NEGATIVE)
      flags |= PTR_FLAGS_WHEEL_NEGATIVE | 0x0088;
    else
      flags |= 0x0078;
  }

  if (event & FRDP_MOUSE_EVENT_BUTTON1)
    flags |= PTR_FLAGS_BUTTON1;
  if (event & FRDP_MOUSE_EVENT_BUTTON2)
    flags |= PTR_FLAGS_BUTTON2;
  if (event & FRDP_MOUSE_EVENT_BUTTON3)
    flags |= PTR_FLAGS_BUTTON3;
  if (event & FRDP_MOUSE_EVENT_BUTTON4)
    xflags |=  PTR_XFLAGS_BUTTON1;
  if (event & FRDP_MOUSE_EVENT_BUTTON5)
    xflags |=  PTR_XFLAGS_BUTTON2;

  input = priv->freerdp_session->context->input;

  if (priv->scaling) {
    x = (x - priv->offset_x) / priv->scale;
    y = (y - priv->offset_y) / priv->scale;
  }

  x = x < 0.0 ? 0.0 : x;
  y = y < 0.0 ? 0.0 : y;
  if (xflags != 0) {
    if (event & FRDP_MOUSE_EVENT_DOWN)
        xflags |=  PTR_XFLAGS_DOWN;
    freerdp_input_send_extended_mouse_event(input, xflags, x, y);
  } else if (flags != 0) {
    freerdp_input_send_mouse_event (input, flags, x, y);
  }
}

void
frdp_session_mouse_smooth_scroll_event (FrdpSession          *self,
                                        guint16               x,
                                        guint16               y,
                                        double                delta_x,
                                        double                delta_y)
{
  FrdpSessionPrivate *priv = self->priv;
  rdpInput           *input;
  guint16             flags = 0, value;

  /* Main thread only; dropped while connecting or closing. */
  if (!priv->is_connected || priv->freerdp_session == NULL)
    return;

  if (fabs (delta_y) >= fabs (delta_x)) {
    flags |= PTR_FLAGS_WHEEL;
    value = (guint16) round (fabs (delta_y) * 0x78);
    if (value > 0) {
      /* Reversing direction here to reflect the behaviour on local side. */
      if (delta_y < 0.0) {
        if (value > 255)
          value = 255;
        flags |= value & WheelRotationMask;
      } else {
        if (value > 256)
          value = 256;
        flags |= PTR_FLAGS_WHEEL_NEGATIVE;
        flags |= (~value + 1) & WheelRotationMask;
      }
    }
  } else {
    flags |= PTR_FLAGS_HWHEEL;
    value = (guint16) round (fabs (delta_x) * 0x78);
    if (value > 0) {
      if (delta_x < 0.0) {
        if (value > 256)
          value = 256;
        flags |= PTR_FLAGS_WHEEL_NEGATIVE;
        flags |= (~value + 1) & WheelRotationMask;
      } else {
        if (value > 255)
          value = 255;
        flags |= value & WheelRotationMask;
      }
    }
  }

  input = priv->freerdp_session->context->input;

  freerdp_input_send_mouse_event (input, flags, 0, 0);
}

void
frdp_session_mouse_pointer  (FrdpSession          *self,
                             gboolean              enter)
{
  /* Kept for the widget's enter/leave handlers; GTK 4 shows the widget
   * cursor only while the pointer is over it, so nothing to switch. */
  if (enter)
    frdp_session_update_mouse_pointer (self);
}

void
frdp_session_send_key (FrdpSession  *self,
                       guint         keycode,
                       gboolean      pressed)
{
  rdpInput *input;
  guint32   virtual_code, virtual_scan_code;

  if (!self->priv->is_connected || self->priv->freerdp_session == NULL)
    return;
  input = self->priv->freerdp_session->context->input;

#if defined(G_OS_WIN32)
  virtual_code = keycode;
  /* GDK's Win32 hardware keycode is a virtual key without WinPR's
   * KBDEXT marker. Navigation keys live in WinPR's extended table;
   * looking them up without it returns RDP_SCANCODE_UNKNOWN and drops
   * the event. Keypad digits have separate VK_NUMPAD* codes. */
  switch (virtual_code) {
  case VK_LEFT:
  case VK_UP:
  case VK_RIGHT:
  case VK_DOWN:
  case VK_HOME:
  case VK_END:
  case VK_PRIOR:
  case VK_NEXT:
  case VK_INSERT:
  case VK_DELETE:
    virtual_code |= KBDEXT;
    break;
  default:
    break;
  }
#elif defined(__APPLE__)
  virtual_code = GetVirtualKeyCodeFromKeycode (keycode, WINPR_KEYCODE_TYPE_APPLE);
#else
  virtual_code = GetVirtualKeyCodeFromKeycode (keycode, WINPR_KEYCODE_TYPE_XKB);
#endif
  virtual_scan_code = GetVirtualScanCodeFromVirtualKeyCode (virtual_code, WINPR_KBD_TYPE_IBM_ENHANCED);

  if (virtual_scan_code != RDP_SCANCODE_UNKNOWN &&
      (frdp_display_is_keyboard_grabbed (FRDP_DISPLAY (self->priv->display)) ||
       (virtual_scan_code != RDP_SCANCODE_LWIN && virtual_scan_code != RDP_SCANCODE_RWIN)))
    freerdp_input_send_keyboard_event_ex (input, pressed, FALSE, virtual_scan_code);
}
