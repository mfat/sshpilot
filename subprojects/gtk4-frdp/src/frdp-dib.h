/* SPDX-License-Identifier: GPL-3.0-or-later */
/* frdp-dib.h — Windows clipboard bitmaps (CF_DIB) <-> GdkTexture
 *
 * Copyright (C) 2026 mFat
 */
#pragma once

#include <gdk/gdk.h>

G_BEGIN_DECLS

/* A CF_DIB (BITMAPINFOHEADER + 32 bpp bottom-up BGRA pixels). */
GBytes     *frdp_dib_from_texture (GdkTexture *texture);

/* Parses a CF_DIB or CF_DIBV5 with 24 or 32 bpp, BI_RGB or BI_BITFIELDS
 * (standard BGR(A) masks), bottom-up or top-down. NULL if unsupported. */
GdkTexture *frdp_dib_to_texture   (GBytes     *dib);

/* A .bmp file (BITMAPFILEHEADER + @dib), for local "image/bmp" requests. */
GBytes     *frdp_dib_to_bmp_file  (GBytes     *dib);

G_END_DECLS
