/* SPDX-License-Identifier: GPL-3.0-or-later */
/* frdp-dib.c — Windows clipboard bitmaps (CF_DIB) <-> GdkTexture
 *
 * Copyright (C) 2026 mFat
 *
 * Done here rather than through WinPR's wClipboard because WinPR's image
 * conversions depend on how it was built: Ubuntu's FreeRDP 3.32 reports no
 * PNG support, so "image/png" <-> CF_DIB silently did nothing.
 *
 * This program is free software: you can redistribute it and/or modify
 * it under the terms of the GNU General Public License as published by
 * the Free Software Foundation, either version 3 of the License, or
 * (at your option) any later version.
 */

#include "frdp-dib.h"

#include <string.h>

#define BI_RGB        0
#define BI_BITFIELDS  3
#define INFO_HEADER   40   /* BITMAPINFOHEADER */
#define V4_HEADER     108  /* BITMAPV4HEADER */
#define V5_HEADER     124  /* BITMAPV5HEADER */
#define FILE_HEADER   14   /* BITMAPFILEHEADER */

static void
put_u16 (guint8 *p, guint16 v)
{
  p[0] = v & 0xff;
  p[1] = v >> 8;
}

static void
put_u32 (guint8 *p, guint32 v)
{
  p[0] = v & 0xff;
  p[1] = (v >> 8) & 0xff;
  p[2] = (v >> 16) & 0xff;
  p[3] = v >> 24;
}

static guint32
get_u32 (const guint8 *p)
{
  return p[0] | (p[1] << 8) | (p[2] << 16) | ((guint32) p[3] << 24);
}

static guint16
get_u16 (const guint8 *p)
{
  return p[0] | (p[1] << 8);
}

GBytes *
frdp_dib_from_texture (GdkTexture *texture)
{
  int                   width = gdk_texture_get_width (texture);
  int                   height = gdk_texture_get_height (texture);
  gsize                 row = (gsize) width * 4;
  gsize                 size = INFO_HEADER + row * height;
  GdkTextureDownloader *downloader;
  GBytes               *pixels;
  gsize                 stride;
  const guint8         *src;
  guint8               *dib;
  int                   y;

  if (width <= 0 || height <= 0 || row > G_MAXUINT32 / (gsize) height)
    return NULL;

  downloader = gdk_texture_downloader_new (texture);
  gdk_texture_downloader_set_format (downloader, GDK_MEMORY_B8G8R8A8);   /* straight alpha */
  pixels = gdk_texture_downloader_download_bytes (downloader, &stride);
  gdk_texture_downloader_free (downloader);
  src = g_bytes_get_data (pixels, NULL);

  dib = g_malloc0 (size);
  put_u32 (dib + 0, INFO_HEADER);
  put_u32 (dib + 4, width);
  put_u32 (dib + 8, height);              /* positive: bottom-up rows */
  put_u16 (dib + 12, 1);                  /* planes */
  put_u16 (dib + 14, 32);                 /* bits per pixel */
  put_u32 (dib + 16, BI_RGB);
  put_u32 (dib + 20, (guint32) (row * height));
  put_u32 (dib + 24, 2835);               /* 72 dpi */
  put_u32 (dib + 28, 2835);

  for (y = 0; y < height; y++)
    memcpy (dib + INFO_HEADER + (gsize) (height - 1 - y) * row, src + (gsize) y * stride, row);

  g_bytes_unref (pixels);

  return g_bytes_new_take (dib, size);
}

GdkTexture *
frdp_dib_to_texture (GBytes *bytes)
{
  gsize            size;
  const guint8    *dib = g_bytes_get_data (bytes, &size);
  guint32          header, compression, colors;
  gint32           width, height;
  guint16          bpp;
  gboolean         top_down, has_alpha = FALSE;
  gsize            offset, src_stride, dst_stride, abs_height, y, x;
  guint8          *out;
  GBytes          *out_bytes;
  GdkTexture      *texture;

  if (dib == NULL || size < INFO_HEADER)
    return NULL;

  header = get_u32 (dib);
  if (header != INFO_HEADER && header != V4_HEADER && header != V5_HEADER)
    return NULL;
  if (size < header)
    return NULL;

  width = (gint32) get_u32 (dib + 4);
  height = (gint32) get_u32 (dib + 8);
  bpp = get_u16 (dib + 14);
  compression = get_u32 (dib + 16);
  colors = get_u32 (dib + 32);

  if (width <= 0 || height == 0 || height == G_MININT32 || (bpp != 24 && bpp != 32))
    return NULL;
  if (compression != BI_RGB && !(compression == BI_BITFIELDS && bpp == 32))
    return NULL;

  top_down = height < 0;
  abs_height = (gsize) ABS (height);

  offset = header + (gsize) colors * 4;
  if (compression == BI_BITFIELDS) {
    const guint8 *masks = dib + INFO_HEADER;    /* in V4/V5 headers, or right after an info header */

    if (header == INFO_HEADER)
      offset += 12;
    if (size < INFO_HEADER + 12 ||
        get_u32 (masks) != 0x00ff0000 || get_u32 (masks + 4) != 0x0000ff00 || get_u32 (masks + 8) != 0x000000ff)
      return NULL;   /* only standard BGR(A) layouts */
    if (header >= V4_HEADER)
      has_alpha = get_u32 (dib + 52) == 0xff000000;
  }

  src_stride = bpp == 32 ? (gsize) width * 4 : (((gsize) width * 3 + 3) & ~(gsize) 3);
  if (abs_height > G_MAXSIZE / src_stride || offset > size || size - offset < src_stride * abs_height)
    return NULL;

  dst_stride = (gsize) width * 4;
  out = g_malloc (dst_stride * abs_height);
  for (y = 0; y < abs_height; y++) {
    const guint8 *src = dib + offset + (top_down ? y : abs_height - 1 - y) * src_stride;
    guint8       *dst = out + y * dst_stride;

    if (bpp == 32) {
      memcpy (dst, src, dst_stride);
    } else {
      for (x = 0; x < (gsize) width; x++) {
        dst[x * 4 + 0] = src[x * 3 + 0];
        dst[x * 4 + 1] = src[x * 3 + 1];
        dst[x * 4 + 2] = src[x * 3 + 2];
        dst[x * 4 + 3] = 0xff;
      }
    }
  }

  out_bytes = g_bytes_new_take (out, dst_stride * abs_height);
  /* BI_RGB has no alpha (Windows usually leaves it 0): treat as opaque. */
  texture = gdk_memory_texture_new (width, abs_height,
                                    has_alpha ? GDK_MEMORY_B8G8R8A8 : GDK_MEMORY_B8G8R8X8,
                                    out_bytes, dst_stride);
  g_bytes_unref (out_bytes);

  return texture;
}

GBytes *
frdp_dib_to_bmp_file (GBytes *dib)
{
  gsize         size;
  const guint8 *data = g_bytes_get_data (dib, &size);
  guint32       header, colors, offset;
  guint8       *file;

  if (data == NULL || size < INFO_HEADER || size > G_MAXUINT32 - FILE_HEADER)
    return NULL;

  header = get_u32 (data);
  colors = get_u32 (data + 32);
  offset = FILE_HEADER + header + colors * 4;
  if (header == INFO_HEADER && get_u32 (data + 16) == BI_BITFIELDS)
    offset += 12;

  file = g_malloc (FILE_HEADER + size);
  file[0] = 'B';
  file[1] = 'M';
  put_u32 (file + 2, (guint32) (FILE_HEADER + size));
  put_u32 (file + 6, 0);
  put_u32 (file + 10, offset);
  memcpy (file + FILE_HEADER, data, size);

  return g_bytes_new_take (file, FILE_HEADER + size);
}
