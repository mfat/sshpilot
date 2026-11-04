/* SPDX-License-Identifier: GPL-3.0-or-later */
/* frdp-channel-clipboard.c — RDP clipboard redirection (cliprdr) for GTK 4
 *
 * Copyright (C) 2026 mFat
 *
 * Text, images and HTML in both directions. Text and HTML are converted by
 * WinPR's wClipboard (UTF-16 text <-> "text/plain" UTF-8, "HTML Format" <->
 * "text/html"), as in FreeRDP's own SDL client. Images go through
 * frdp-dib.c (CF_DIB <-> GdkTexture -> PNG/BMP): WinPR's image support
 * depends on its build and may lack PNG. File transfer is not supported.
 *
 * Threading. cliprdr callbacks arrive on FreeRDP's side (not the GTK
 * thread); each copies what it needs and queues the work to the main
 * thread. All GTK clipboard work and every message sent to the server
 * happen on the main thread. `lock` guards `ctx`: the session thread clears
 * it in frdp_channel_clipboard_detach() before FreeRDP frees the context,
 * and the main thread only uses it while holding the lock.
 *
 * Server -> local. When the server announces formats, a GdkContentProvider
 * offering the matching MIME types becomes the local clipboard content.
 * Data is fetched from the server only when something pastes: requests are
 * sent one at a time (responses carry no request id) and time out.
 *
 * Local -> server. When the local clipboard changes (and the content is not
 * our own provider), the available formats are announced. When the server
 * asks for data, the local clipboard is read asynchronously and the answer
 * sent; server requests are also handled one at a time.
 *
 * This program is free software: you can redistribute it and/or modify
 * it under the terms of the GNU General Public License as published by
 * the Free Software Foundation, either version 3 of the License, or
 * (at your option) any later version.
 */

#include "frdp-channel-clipboard.h"
#include "frdp-dib.h"
#include "frdp-session.h"

#include <freerdp/channels/cliprdr.h>
#include <freerdp/freerdp.h>
#include <gtk/gtk.h>
#include <string.h>
#include <winpr/clipboard.h>
#include <winpr/user.h>

#define FORMAT_HTML           "HTML Format"   /* Windows' registered HTML format name */
#define WINPR_TEXT            "text/plain"    /* WinPR's UTF-8 text format */
#define MIME_TEXT_UTF8        "text/plain;charset=utf-8"
#define MIME_HTML             "text/html"
#define MIME_PNG              "image/png"
#define MIME_BMP              "image/bmp"
#define REQUEST_TIMEOUT_S     10

static const char *text_mimes[] = { MIME_TEXT_UTF8, "text/plain", "UTF8_STRING", "STRING", "TEXT" };
static const char *image_mimes[] = { MIME_PNG, MIME_BMP };

typedef enum
{
  KIND_NONE,
  KIND_TEXT,
  KIND_IMAGE,
  KIND_HTML,
} ContentKind;

/* A local paste waiting for server data. */
typedef struct
{
  GTask         *task;
  ContentKind    kind;
  guint32        server_format_id;  /* id to request from the server */
  guint32        source_format_id;  /* wClipboard id the response is stored as */
  char          *mime;              /* MIME type requested locally */
  GOutputStream *stream;
} PendingRead;

/* A server format list, handed to the main thread. */
typedef struct
{
  GArray  *ids;       /* guint32 */
  guint32  html_id;   /* server's id for "HTML Format", 0 if absent */
} ServerFormats;

static void
server_formats_free (gpointer p)
{
  ServerFormats *formats = p;

  g_clear_pointer (&formats->ids, g_array_unref);
  g_free (formats);
}

/* A server request for local data. */
typedef struct
{
  guint32 format_id;
} ServerRequest;

struct _FrdpChannelClipboard
{
  FrdpChannel           parent_instance;

  GMutex                lock;            /* guards ctx */
  CliprdrClientContext *ctx;             /* NULL once detached */
  gpointer              session;         /* FrdpSession, weak */
  wClipboard           *system;          /* conversions (WinPR locks internally) */
  guint32               html_format_id;  /* local wClipboard id of "HTML Format" */

  /* ---- main thread ---- */
  GdkClipboard         *gdk_clipboard;
  gulong                changed_id;
  GdkContentProvider   *own_provider;    /* currently offered server data */
  GArray               *server_formats;  /* guint32 ids, from the last format list */
  guint32               server_html_id;  /* server's id for "HTML Format", 0 if none */
  GQueue                reads;           /* PendingRead*, head in flight */
  gboolean              read_in_flight;
  guint                 read_timeout_id;
  GQueue                server_requests; /* ServerRequest*, head being served */
  gboolean              serving;
  gboolean              ready;           /* MonitorReady received */
  gboolean              attached;
};

G_DEFINE_TYPE (FrdpChannelClipboard, frdp_channel_clipboard, FRDP_TYPE_CHANNEL)

static void announce_local_formats (FrdpChannelClipboard *self);
static void pump_reads             (FrdpChannelClipboard *self);
static void serve_next_request     (FrdpChannelClipboard *self);

/* ---- helpers -------------------------------------------------------------- */

/* Always queue to the GTK main context (see frdp-session.c for why not
 * g_main_context_invoke()). */
static void
queue_main (GSourceFunc func, gpointer data, GDestroyNotify notify)
{
  GSource *source = g_idle_source_new ();

  g_source_set_priority (source, G_PRIORITY_DEFAULT);
  g_source_set_callback (source, func, data, notify);
  g_source_set_static_name (source, "[gtk4-frdp] clipboard");
  g_source_attach (source, g_main_context_default ());
  g_source_unref (source);
}

typedef struct
{
  FrdpChannelClipboard *self;
  gpointer              data;
  GDestroyNotify        data_free;
} MainCall;

static void
main_call_free (gpointer p)
{
  MainCall *call = p;

  if (call->data_free != NULL && call->data != NULL)
    call->data_free (call->data);
  g_object_unref (call->self);
  g_free (call);
}

static void
run_on_main (FrdpChannelClipboard *self, GSourceFunc func, gpointer data, GDestroyNotify data_free)
{
  MainCall *call = g_new0 (MainCall, 1);

  call->self = g_object_ref (self);
  call->data = data;
  call->data_free = data_free;
  queue_main (func, call, main_call_free);
}

static ContentKind
kind_for_server_format (FrdpChannelClipboard *self, guint32 id)
{
  switch (id) {
  case CF_TEXT:
  case CF_OEMTEXT:
  case CF_UNICODETEXT:
    return KIND_TEXT;
  case CF_DIB:
  case CF_DIBV5:
    return KIND_IMAGE;
  default:
    return id != 0 && id == self->server_html_id ? KIND_HTML : KIND_NONE;
  }
}

static gboolean
server_has (FrdpChannelClipboard *self, guint32 id)
{
  guint i;

  for (i = 0; self->server_formats != NULL && i < self->server_formats->len; i++)
    if (g_array_index (self->server_formats, guint32, i) == id)
      return TRUE;

  return FALSE;
}

static ContentKind
kind_for_mime (const char *mime)
{
  gsize i;

  for (i = 0; i < G_N_ELEMENTS (text_mimes); i++)
    if (g_str_equal (mime, text_mimes[i]))
      return KIND_TEXT;
  for (i = 0; i < G_N_ELEMENTS (image_mimes); i++)
    if (g_str_equal (mime, image_mimes[i]))
      return KIND_IMAGE;
  if (g_str_equal (mime, MIME_HTML))
    return KIND_HTML;

  return KIND_NONE;
}

/* wClipboard conversion: put @data in as @from, get it out as @to. */
static GBytes *
convert (FrdpChannelClipboard *self, guint32 from, gconstpointer data, gsize size, guint32 to)
{
  UINT32  out_size = 0;
  void   *out;
  GBytes *result = NULL;

  if (size > G_MAXUINT32)
    return NULL;

  ClipboardLock (self->system);
  if (ClipboardSetData (self->system, from, data, (UINT32) size)) {
    out = ClipboardGetData (self->system, to, &out_size);
    if (out != NULL)
      result = g_bytes_new_with_free_func (out, out_size, free, out);
  }
  ClipboardUnlock (self->system);

  return result;
}

static guint32
winpr_id (FrdpChannelClipboard *self, const char *name)
{
  return ClipboardRegisterFormat (self->system, name);
}

/* ---- sending (main thread, under lock) ----------------------------------- */

static void
send_format_list (FrdpChannelClipboard *self, const CLIPRDR_FORMAT *formats, guint n)
{
  CLIPRDR_FORMAT_LIST list = { 0 };

  list.common.msgType = CB_FORMAT_LIST;
  list.numFormats = n;
  list.formats = (CLIPRDR_FORMAT *) formats;

  g_mutex_lock (&self->lock);
  if (self->ctx != NULL)
    self->ctx->ClientFormatList (self->ctx, &list);
  g_mutex_unlock (&self->lock);
}

static gboolean
send_data_request (FrdpChannelClipboard *self, guint32 format_id)
{
  CLIPRDR_FORMAT_DATA_REQUEST request = { 0 };
  gboolean                    sent = FALSE;

  request.common.msgType = CB_FORMAT_DATA_REQUEST;
  request.requestedFormatId = format_id;

  g_mutex_lock (&self->lock);
  if (self->ctx != NULL)
    sent = self->ctx->ClientFormatDataRequest (self->ctx, &request) == CHANNEL_RC_OK;
  g_mutex_unlock (&self->lock);

  return sent;
}

static void
send_data_response (FrdpChannelClipboard *self, GBytes *data)
{
  CLIPRDR_FORMAT_DATA_RESPONSE response = { 0 };
  gsize                        size = 0;
  gconstpointer                bytes = data != NULL ? g_bytes_get_data (data, &size) : NULL;

  response.common.msgType = CB_FORMAT_DATA_RESPONSE;
  response.common.msgFlags = bytes != NULL && size <= G_MAXUINT32 ? CB_RESPONSE_OK : CB_RESPONSE_FAIL;
  response.common.dataLen = response.common.msgFlags == CB_RESPONSE_OK ? (UINT32) size : 0;
  response.requestedFormatData = response.common.msgFlags == CB_RESPONSE_OK ? bytes : NULL;

  g_mutex_lock (&self->lock);
  if (self->ctx != NULL)
    self->ctx->ClientFormatDataResponse (self->ctx, &response);
  g_mutex_unlock (&self->lock);
}

/* ---- the content provider offering server data --------------------------- */

#define FRDP_TYPE_CLIPBOARD_PROVIDER (frdp_clipboard_provider_get_type ())
G_DECLARE_FINAL_TYPE (FrdpClipboardProvider, frdp_clipboard_provider, FRDP, CLIPBOARD_PROVIDER, GdkContentProvider)

struct _FrdpClipboardProvider
{
  GdkContentProvider    parent_instance;
  FrdpChannelClipboard *channel;   /* weak; cleared on detach */
  GdkContentFormats    *formats;
};

G_DEFINE_TYPE (FrdpClipboardProvider, frdp_clipboard_provider, GDK_TYPE_CONTENT_PROVIDER)

static GdkContentFormats *
frdp_clipboard_provider_ref_formats (GdkContentProvider *provider)
{
  return gdk_content_formats_ref (FRDP_CLIPBOARD_PROVIDER (provider)->formats);
}

static void
pending_read_free (PendingRead *read)
{
  g_clear_object (&read->task);
  g_clear_object (&read->stream);
  g_free (read->mime);
  g_free (read);
}

static void
frdp_clipboard_provider_write_mime_type_async (GdkContentProvider  *provider,
                                               const char          *mime_type,
                                               GOutputStream       *stream,
                                               int                  io_priority,
                                               GCancellable        *cancellable,
                                               GAsyncReadyCallback  callback,
                                               gpointer             user_data)
{
  FrdpClipboardProvider *self = FRDP_CLIPBOARD_PROVIDER (provider);
  FrdpChannelClipboard  *channel = self->channel;
  GTask                 *task = g_task_new (provider, cancellable, callback, user_data);
  PendingRead           *read;
  ContentKind            kind = kind_for_mime (mime_type);

  g_task_set_source_tag (task, frdp_clipboard_provider_write_mime_type_async);
  g_task_set_priority (task, io_priority);

  if (channel == NULL || !channel->attached) {
    g_task_return_new_error (task, G_IO_ERROR, G_IO_ERROR_CLOSED, "Remote session closed");
    g_object_unref (task);
    return;
  }

  read = g_new0 (PendingRead, 1);
  read->task = task;
  read->kind = kind;
  read->mime = g_strdup (mime_type);
  read->stream = g_object_ref (stream);

  switch (kind) {
  case KIND_TEXT:
    read->server_format_id = server_has (channel, CF_UNICODETEXT) ? CF_UNICODETEXT : CF_TEXT;
    read->source_format_id = read->server_format_id;
    break;
  case KIND_IMAGE:
    read->server_format_id = server_has (channel, CF_DIB) ? CF_DIB : CF_DIBV5;
    read->source_format_id = read->server_format_id;
    break;
  case KIND_HTML:
    read->server_format_id = channel->server_html_id;
    read->source_format_id = channel->html_format_id;
    break;
  case KIND_NONE:
  default:
    g_task_return_new_error (task, G_IO_ERROR, G_IO_ERROR_NOT_SUPPORTED,
                             "Unsupported clipboard type %s", mime_type);
    pending_read_free (read);
    return;
  }

  g_queue_push_tail (&channel->reads, read);
  pump_reads (channel);
}

static gboolean
frdp_clipboard_provider_write_mime_type_finish (GdkContentProvider  *provider,
                                                GAsyncResult        *result,
                                                GError             **error)
{
  return g_task_propagate_boolean (G_TASK (result), error);
}

static void
frdp_clipboard_provider_finalize (GObject *object)
{
  FrdpClipboardProvider *self = FRDP_CLIPBOARD_PROVIDER (object);

  gdk_content_formats_unref (self->formats);

  G_OBJECT_CLASS (frdp_clipboard_provider_parent_class)->finalize (object);
}

static void
frdp_clipboard_provider_class_init (FrdpClipboardProviderClass *klass)
{
  GdkContentProviderClass *provider_class = GDK_CONTENT_PROVIDER_CLASS (klass);

  G_OBJECT_CLASS (klass)->finalize = frdp_clipboard_provider_finalize;
  provider_class->ref_formats = frdp_clipboard_provider_ref_formats;
  provider_class->write_mime_type_async = frdp_clipboard_provider_write_mime_type_async;
  provider_class->write_mime_type_finish = frdp_clipboard_provider_write_mime_type_finish;
}

static void
frdp_clipboard_provider_init (FrdpClipboardProvider *self)
{
}

/* ---- server -> local -------------------------------------------------------- */

static void
fail_read (PendingRead *read, const char *message)
{
  g_task_return_new_error (read->task, G_IO_ERROR, G_IO_ERROR_FAILED, "%s", message);
  pending_read_free (read);
}

static gboolean
read_timeout_cb (gpointer user_data)
{
  FrdpChannelClipboard *self = user_data;
  PendingRead          *read = g_queue_pop_head (&self->reads);

  self->read_timeout_id = 0;
  self->read_in_flight = FALSE;
  if (read != NULL)
    fail_read (read, "The remote clipboard did not answer");
  pump_reads (self);

  return G_SOURCE_REMOVE;
}

static void
pump_reads (FrdpChannelClipboard *self)
{
  PendingRead *read;

  if (self->read_in_flight || (read = g_queue_peek_head (&self->reads)) == NULL)
    return;

  if (!send_data_request (self, read->server_format_id)) {
    g_queue_pop_head (&self->reads);
    fail_read (read, "Remote clipboard unavailable");
    pump_reads (self);
    return;
  }

  self->read_in_flight = TRUE;
  self->read_timeout_id = g_timeout_add_seconds (REQUEST_TIMEOUT_S, read_timeout_cb, self);
}

static void
write_done_cb (GObject *source, GAsyncResult *result, gpointer user_data)
{
  GTask  *task = user_data;
  GError *error = NULL;

  if (g_output_stream_write_all_finish (G_OUTPUT_STREAM (source), result, NULL, &error))
    g_task_return_boolean (task, TRUE);
  else
    g_task_return_error (task, error);
  g_object_unref (task);
}

typedef struct
{
  gboolean ok;
  GBytes  *data;
} DataResponse;

static void
data_response_free (gpointer p)
{
  DataResponse *response = p;

  g_clear_pointer (&response->data, g_bytes_unref);
  g_free (response);
}

static gboolean
data_response_main (gpointer user_data)
{
  MainCall             *call = user_data;
  FrdpChannelClipboard *self = call->self;
  DataResponse         *response = call->data;
  PendingRead          *read;
  GBytes               *converted = NULL;
  gconstpointer         bytes;
  gsize                 size;

  if (!self->read_in_flight)
    return G_SOURCE_REMOVE;   /* late answer to a request that already timed out */

  g_clear_handle_id (&self->read_timeout_id, g_source_remove);
  self->read_in_flight = FALSE;
  read = g_queue_pop_head (&self->reads);

  if (read == NULL) {
    pump_reads (self);
    return G_SOURCE_REMOVE;
  }

  if (!response->ok || response->data == NULL) {
    fail_read (read, "The remote clipboard refused the request");
    pump_reads (self);
    return G_SOURCE_REMOVE;
  }

  bytes = g_bytes_get_data (response->data, &size);
  switch (read->kind) {
  case KIND_TEXT:
    converted = convert (self, read->source_format_id, bytes, size, winpr_id (self, WINPR_TEXT));
    break;
  case KIND_IMAGE:
    if (g_str_equal (read->mime, MIME_BMP)) {
      converted = frdp_dib_to_bmp_file (response->data);
    } else {
      GdkTexture *texture = frdp_dib_to_texture (response->data);

      if (texture != NULL) {
        converted = gdk_texture_save_to_png_bytes (texture);
        g_object_unref (texture);
      }
    }
    break;
  case KIND_HTML:
    converted = convert (self, read->source_format_id, bytes, size, winpr_id (self, MIME_HTML));
    break;
  default:
    break;
  }

  if (converted == NULL) {
    fail_read (read, "Could not convert the remote clipboard data");
  } else {
    gsize n;
    const char *data = g_bytes_get_data (converted, &n);

    /* Text formats come back NUL-terminated; a paste must not include it. */
    if (read->kind != KIND_IMAGE)
      while (n > 0 && data[n - 1] == '\0')
        n--;

    /* Take the task out first: C does not order argument evaluation, so
     * stealing it inside the call below could run before the getters. */
    GTask *task = g_steal_pointer (&read->task);

    /* Keep the bytes alive until the write completes. */
    g_object_set_data_full (G_OBJECT (task), "frdp-data", converted,
                            (GDestroyNotify) g_bytes_unref);
    g_output_stream_write_all_async (read->stream, data, n,
                                     g_task_get_priority (task),
                                     g_task_get_cancellable (task),
                                     write_done_cb, task);
    pending_read_free (read);
  }

  pump_reads (self);

  return G_SOURCE_REMOVE;
}

static gboolean
server_format_list_main (gpointer user_data)
{
  MainCall              *call = user_data;
  FrdpChannelClipboard  *self = call->self;
  ServerFormats         *list = call->data;
  GArray                *formats = list->ids;
  GdkContentFormatsBuilder *builder;
  FrdpClipboardProvider *provider;
  gboolean               text = FALSE, image = FALSE, html = FALSE;
  guint                  i;

  g_clear_pointer (&self->server_formats, g_array_unref);
  self->server_formats = g_steal_pointer (&list->ids);
  self->server_html_id = list->html_id;

  if (!self->attached || self->gdk_clipboard == NULL)
    return G_SOURCE_REMOVE;

  g_debug ("clipboard: server offers %u format(s)", formats->len);
  for (i = 0; i < formats->len; i++) {
    switch (kind_for_server_format (self, g_array_index (formats, guint32, i))) {
    case KIND_TEXT: text = TRUE; break;
    case KIND_IMAGE: image = TRUE; break;
    case KIND_HTML: html = TRUE; break;
    default: break;
    }
  }

  builder = gdk_content_formats_builder_new ();
  if (text)
    for (i = 0; i < G_N_ELEMENTS (text_mimes); i++)
      gdk_content_formats_builder_add_mime_type (builder, text_mimes[i]);
  if (image)
    for (i = 0; i < G_N_ELEMENTS (image_mimes); i++)
      gdk_content_formats_builder_add_mime_type (builder, image_mimes[i]);
  if (html)
    gdk_content_formats_builder_add_mime_type (builder, MIME_HTML);

  provider = g_object_new (FRDP_TYPE_CLIPBOARD_PROVIDER, NULL);
  provider->channel = self;
  provider->formats = gdk_content_formats_builder_free_to_formats (builder);

  /* Becoming the clipboard owner fires "changed"; own_provider makes
   * announce_local_formats() ignore our own content. */
  g_set_object (&self->own_provider, GDK_CONTENT_PROVIDER (provider));
  if (text || image || html)
    gdk_clipboard_set_content (self->gdk_clipboard, GDK_CONTENT_PROVIDER (provider));
  g_object_unref (provider);

  return G_SOURCE_REMOVE;
}

/* ---- local -> server ------------------------------------------------------- */

/* Exact GType matching is not enough: content set in-process advertises
 * the concrete class (an image is a GdkMemoryTexture, not GdkTexture). */
static gboolean
formats_contain_gtype_a (GdkContentFormats *formats, GType type)
{
  gsize        n, i;
  const GType *types = gdk_content_formats_get_gtypes (formats, &n);

  for (i = 0; types != NULL && i < n; i++)
    if (g_type_is_a (types[i], type))
      return TRUE;

  return FALSE;
}

static void
announce_local_formats (FrdpChannelClipboard *self)
{
  GdkContentFormats *formats;
  CLIPRDR_FORMAT     list[3];
  guint              n = 0;

  if (!self->ready || !self->attached || self->gdk_clipboard == NULL)
    return;

  /* Our own provider means the server owns the data: nothing to announce. */
  if (self->own_provider != NULL &&
      gdk_clipboard_get_content (self->gdk_clipboard) == self->own_provider)
    return;

  formats = gdk_clipboard_get_formats (self->gdk_clipboard);

  if (formats_contain_gtype_a (formats, G_TYPE_STRING) ||
      gdk_content_formats_contain_mime_type (formats, MIME_TEXT_UTF8) ||
      gdk_content_formats_contain_mime_type (formats, "text/plain") ||
      gdk_content_formats_contain_mime_type (formats, "UTF8_STRING"))
    list[n++] = (CLIPRDR_FORMAT) { CF_UNICODETEXT, NULL };

  if (formats_contain_gtype_a (formats, GDK_TYPE_TEXTURE) ||
      gdk_content_formats_contain_mime_type (formats, MIME_PNG) ||
      gdk_content_formats_contain_mime_type (formats, MIME_BMP) ||
      gdk_content_formats_contain_mime_type (formats, "image/jpeg"))
    list[n++] = (CLIPRDR_FORMAT) { CF_DIB, NULL };

  if (gdk_content_formats_contain_mime_type (formats, MIME_HTML))
    list[n++] = (CLIPRDR_FORMAT) { self->html_format_id, (char *) FORMAT_HTML };

  /* Also sent when empty: the server must stop offering stale data. */
  g_debug ("clipboard: announcing %u local format(s)%s%s%s", n,
           n > 0 && list[0].formatId == CF_UNICODETEXT ? " text" : "",
           (n > 0 && list[0].formatId == CF_DIB) || (n > 1 && list[1].formatId == CF_DIB) ? " image" : "",
           n > 0 && list[n - 1].formatName != NULL ? " html" : "");
  send_format_list (self, list, n);
}

/* notify::formats fires whenever the available formats change, whether
 * another application or this process set the clipboard. */
static void
clipboard_changed_cb (GdkClipboard *clipboard, GParamSpec *pspec, FrdpChannelClipboard *self)
{
  announce_local_formats (self);
}

static void
finish_server_request (FrdpChannelClipboard *self, GBytes *answer)
{
  g_debug ("clipboard: answering server request with %s",
           answer != NULL ? "data" : "failure");
  send_data_response (self, answer);
  g_free (g_queue_pop_head (&self->server_requests));
  self->serving = FALSE;
  serve_next_request (self);
}

static void
local_text_cb (GObject *source, GAsyncResult *result, gpointer user_data)
{
  FrdpChannelClipboard *self = user_data;
  g_autofree char      *text = gdk_clipboard_read_text_finish (GDK_CLIPBOARD (source), result, NULL);
  ServerRequest        *request = g_queue_peek_head (&self->server_requests);
  GBytes               *answer = NULL;

  if (text != NULL && request != NULL)
    answer = convert (self, winpr_id (self, WINPR_TEXT), text, strlen (text) + 1, request->format_id);
  finish_server_request (self, answer);
  g_clear_pointer (&answer, g_bytes_unref);
  g_object_unref (self);
}

static void
local_texture_cb (GObject *source, GAsyncResult *result, gpointer user_data)
{
  FrdpChannelClipboard *self = user_data;
  GdkTexture           *texture = gdk_clipboard_read_texture_finish (GDK_CLIPBOARD (source), result, NULL);
  ServerRequest        *request = g_queue_peek_head (&self->server_requests);
  GBytes               *answer = NULL;

  /* Only CF_DIB is announced, so that is what the server asks for. */
  if (texture != NULL && request != NULL)
    answer = frdp_dib_from_texture (texture);
  finish_server_request (self, answer);
  g_clear_pointer (&answer, g_bytes_unref);
  g_clear_object (&texture);
  g_object_unref (self);
}

static void
local_html_read_cb (GObject *source, GAsyncResult *result, gpointer user_data)
{
  FrdpChannelClipboard *self = user_data;
  GBytes               *bytes = g_input_stream_read_bytes_finish (G_INPUT_STREAM (source), result, NULL);
  ServerRequest        *request = g_queue_peek_head (&self->server_requests);
  GBytes               *answer = NULL;

  if (bytes != NULL && request != NULL) {
    /* WinPR's text/html -> "HTML Format" expects a NUL-terminated string. */
    GByteArray *html = g_bytes_unref_to_array (bytes);

    g_byte_array_append (html, (const guint8 *) "", 1);
    answer = convert (self, winpr_id (self, MIME_HTML), html->data, html->len, request->format_id);
    g_byte_array_unref (html);
  }
  finish_server_request (self, answer);
  g_clear_pointer (&answer, g_bytes_unref);
  g_object_unref (self);
}

static void
local_html_stream_cb (GObject *source, GAsyncResult *result, gpointer user_data)
{
  FrdpChannelClipboard *self = user_data;
  GInputStream         *stream = gdk_clipboard_read_finish (GDK_CLIPBOARD (source), result, NULL, NULL);

  if (stream == NULL) {
    finish_server_request (self, NULL);
    g_object_unref (self);
    return;
  }

  /* HTML clips are small; read up to 16 MiB in one go. */
  g_input_stream_read_bytes_async (stream, 16 * 1024 * 1024, G_PRIORITY_DEFAULT, NULL,
                                   local_html_read_cb, self);
  g_object_unref (stream);
}

static void
serve_next_request (FrdpChannelClipboard *self)
{
  ServerRequest *request;
  const char    *html_mimes[] = { MIME_HTML, NULL };

  if (self->serving || (request = g_queue_peek_head (&self->server_requests)) == NULL)
    return;

  if (!self->attached || self->gdk_clipboard == NULL) {
    self->serving = TRUE;
    finish_server_request (self, NULL);
    return;
  }

  self->serving = TRUE;
  g_debug ("clipboard: server requests format %u (%s)", request->format_id,
           ClipboardGetFormatIdString (request->format_id));
  switch (request->format_id) {
  case CF_TEXT:
  case CF_OEMTEXT:
  case CF_UNICODETEXT:
    gdk_clipboard_read_text_async (self->gdk_clipboard, NULL, local_text_cb, g_object_ref (self));
    return;
  case CF_DIB:
  case CF_DIBV5:
    gdk_clipboard_read_texture_async (self->gdk_clipboard, NULL, local_texture_cb, g_object_ref (self));
    return;
  default:
    if (request->format_id == self->html_format_id) {
      gdk_clipboard_read_async (self->gdk_clipboard, html_mimes, G_PRIORITY_DEFAULT, NULL,
                                local_html_stream_cb, g_object_ref (self));
      return;
    }
    finish_server_request (self, NULL);
    return;
  }
}

static gboolean
server_request_main (gpointer user_data)
{
  MainCall *call = user_data;

  g_queue_push_tail (&call->self->server_requests, g_steal_pointer (&call->data));
  serve_next_request (call->self);

  return G_SOURCE_REMOVE;
}

static gboolean
monitor_ready_main (gpointer user_data)
{
  MainCall *call = user_data;

  call->self->ready = TRUE;
  announce_local_formats (call->self);

  return G_SOURCE_REMOVE;
}

/* ---- cliprdr callbacks (FreeRDP thread) ------------------------------------- */

static UINT
on_monitor_ready (CliprdrClientContext *context, const CLIPRDR_MONITOR_READY *ready)
{
  FrdpChannelClipboard  *self = context->custom;
  CLIPRDR_CAPABILITIES   caps = { 0 };
  CLIPRDR_GENERAL_CAPABILITY_SET general = { 0 };

  if (self == NULL)
    return CHANNEL_RC_OK;

  general.capabilitySetType = CB_CAPSTYPE_GENERAL;
  general.capabilitySetLength = 12;
  general.version = CB_CAPS_VERSION_2;
  general.generalFlags = CB_USE_LONG_FORMAT_NAMES;
  caps.cCapabilitiesSets = 1;
  caps.capabilitySets = (CLIPRDR_CAPABILITY_SET *) &general;
  context->ClientCapabilities (context, &caps);

  run_on_main (self, monitor_ready_main, NULL, NULL);

  return CHANNEL_RC_OK;
}

static UINT
on_server_capabilities (CliprdrClientContext *context, const CLIPRDR_CAPABILITIES *caps)
{
  return CHANNEL_RC_OK;
}

static UINT
on_server_format_list (CliprdrClientContext *context, const CLIPRDR_FORMAT_LIST *list)
{
  FrdpChannelClipboard           *self = context->custom;
  CLIPRDR_FORMAT_LIST_RESPONSE    response = { 0 };
  ServerFormats                  *formats;
  UINT32                          i;

  if (self == NULL)
    return CHANNEL_RC_OK;

  formats = g_new0 (ServerFormats, 1);
  formats->ids = g_array_sized_new (FALSE, FALSE, sizeof (guint32), list->numFormats);
  for (i = 0; i < list->numFormats; i++) {
    const CLIPRDR_FORMAT *format = &list->formats[i];

    if (format->formatName != NULL && g_str_equal (format->formatName, FORMAT_HTML))
      formats->html_id = format->formatId;
    g_array_append_val (formats->ids, format->formatId);
  }

  response.common.msgType = CB_FORMAT_LIST_RESPONSE;
  response.common.msgFlags = CB_RESPONSE_OK;
  context->ClientFormatListResponse (context, &response);

  run_on_main (self, server_format_list_main, formats, server_formats_free);

  return CHANNEL_RC_OK;
}

static UINT
on_server_format_list_response (CliprdrClientContext *context, const CLIPRDR_FORMAT_LIST_RESPONSE *response)
{
  return CHANNEL_RC_OK;
}

static UINT
on_server_format_data_request (CliprdrClientContext *context, const CLIPRDR_FORMAT_DATA_REQUEST *request)
{
  FrdpChannelClipboard *self = context->custom;
  ServerRequest        *copy;

  if (self == NULL)
    return CHANNEL_RC_OK;

  copy = g_new0 (ServerRequest, 1);
  copy->format_id = request->requestedFormatId;
  run_on_main (self, server_request_main, copy, g_free);

  return CHANNEL_RC_OK;
}

static UINT
on_server_format_data_response (CliprdrClientContext *context, const CLIPRDR_FORMAT_DATA_RESPONSE *response)
{
  FrdpChannelClipboard *self = context->custom;
  DataResponse         *copy;

  if (self == NULL)
    return CHANNEL_RC_OK;

  copy = g_new0 (DataResponse, 1);
  copy->ok = (response->common.msgFlags & CB_RESPONSE_OK) &&
             !(response->common.msgFlags & CB_RESPONSE_FAIL);
  if (copy->ok && response->requestedFormatData != NULL)
    copy->data = g_bytes_new (response->requestedFormatData, response->common.dataLen);
  run_on_main (self, data_response_main, copy, data_response_free);

  /* A late reply is dropped on the main thread; never error out here, that
   * would tear down the channel (FreeRDP's SDL client notes the same). */
  return CHANNEL_RC_OK;
}

/* ---- attach / detach -------------------------------------------------------- */

static gboolean
attach_main (gpointer user_data)
{
  MainCall             *call = user_data;
  FrdpChannelClipboard *self = call->self;
  GtkWidget            *display;

  g_mutex_lock (&self->lock);
  if (self->ctx == NULL || self->session == NULL ||
      (display = frdp_session_get_display_widget (self->session)) == NULL) {
    g_mutex_unlock (&self->lock);
    return G_SOURCE_REMOVE;
  }
  g_mutex_unlock (&self->lock);

  self->gdk_clipboard = g_object_ref (gtk_widget_get_clipboard (display));
  self->changed_id = g_signal_connect (self->gdk_clipboard, "notify::formats",
                                       G_CALLBACK (clipboard_changed_cb), self);
  self->attached = TRUE;
  announce_local_formats (self);

  return G_SOURCE_REMOVE;
}

static gboolean
detach_main (gpointer user_data)
{
  MainCall             *call = user_data;
  FrdpChannelClipboard *self = call->self;
  PendingRead          *read;

  self->attached = FALSE;
  self->ready = FALSE;
  g_clear_handle_id (&self->read_timeout_id, g_source_remove);
  self->read_in_flight = FALSE;
  while ((read = g_queue_pop_head (&self->reads)) != NULL)
    fail_read (read, "Remote session closed");
  g_queue_clear_full (&self->server_requests, g_free);
  self->serving = FALSE;

  if (self->own_provider != NULL) {
    FRDP_CLIPBOARD_PROVIDER (self->own_provider)->channel = NULL;
    /* Leave the clipboard empty rather than holding a dead promise. */
    if (self->gdk_clipboard != NULL &&
        gdk_clipboard_get_content (self->gdk_clipboard) == self->own_provider)
      gdk_clipboard_set_content (self->gdk_clipboard, NULL);
    g_clear_object (&self->own_provider);
  }

  if (self->gdk_clipboard != NULL) {
    g_clear_signal_handler (&self->changed_id, self->gdk_clipboard);
    g_clear_object (&self->gdk_clipboard);
  }

  return G_SOURCE_REMOVE;
}

FrdpChannelClipboard *
frdp_channel_clipboard_new (gpointer session, CliprdrClientContext *context)
{
  FrdpChannelClipboard *self = g_object_new (FRDP_TYPE_CHANNEL_CLIPBOARD, "session", session, NULL);

  self->session = session;
  self->ctx = context;
  context->custom = self;
  context->MonitorReady = on_monitor_ready;
  context->ServerCapabilities = on_server_capabilities;
  context->ServerFormatList = on_server_format_list;
  context->ServerFormatListResponse = on_server_format_list_response;
  context->ServerFormatDataRequest = on_server_format_data_request;
  context->ServerFormatDataResponse = on_server_format_data_response;

  run_on_main (self, attach_main, NULL, NULL);

  return self;
}

void
frdp_channel_clipboard_detach (FrdpChannelClipboard *self)
{
  g_mutex_lock (&self->lock);
  if (self->ctx != NULL) {
    self->ctx->custom = NULL;
    self->ctx = NULL;
  }
  self->session = NULL;
  g_mutex_unlock (&self->lock);

  run_on_main (self, detach_main, NULL, NULL);
}

/* ---- GObject ------------------------------------------------------------------ */

static void
frdp_channel_clipboard_finalize (GObject *object)
{
  FrdpChannelClipboard *self = FRDP_CHANNEL_CLIPBOARD (object);

  /* Only plain memory here: finalize may run on either thread. GTK state
   * was released in detach_main() on the main thread. */
  g_clear_pointer (&self->server_formats, g_array_unref);
  ClipboardDestroy (self->system);
  g_mutex_clear (&self->lock);

  G_OBJECT_CLASS (frdp_channel_clipboard_parent_class)->finalize (object);
}

static void
frdp_channel_clipboard_class_init (FrdpChannelClipboardClass *klass)
{
  G_OBJECT_CLASS (klass)->finalize = frdp_channel_clipboard_finalize;
}

static void
frdp_channel_clipboard_init (FrdpChannelClipboard *self)
{
  g_mutex_init (&self->lock);
  g_queue_init (&self->reads);
  g_queue_init (&self->server_requests);
  self->system = ClipboardCreate ();
  self->html_format_id = ClipboardRegisterFormat (self->system, FORMAT_HTML);
}
