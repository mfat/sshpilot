/* SPDX-License-Identifier: GPL-3.0-or-later */
/* frdp-channel-clipboard.h — RDP clipboard redirection (cliprdr) for GTK 4
 *
 * Copyright (C) 2026 mFat
 */
#pragma once

#include <freerdp/client/cliprdr.h>

#include "frdp-channel.h"

G_BEGIN_DECLS

#define FRDP_TYPE_CHANNEL_CLIPBOARD (frdp_channel_clipboard_get_type())

G_DECLARE_FINAL_TYPE (FrdpChannelClipboard, frdp_channel_clipboard, FRDP, CHANNEL_CLIPBOARD, FrdpChannel)

/* Session thread: creates the channel for a connected cliprdr context and
 * installs its callbacks. The GTK side is attached on the main thread. */
FrdpChannelClipboard *frdp_channel_clipboard_new    (gpointer              session,
                                                     CliprdrClientContext *context);

/* Session thread: the cliprdr channel is going away. After this returns the
 * context is never touched again; the GTK side is released on the main
 * thread. */
void                  frdp_channel_clipboard_detach (FrdpChannelClipboard *self);

G_END_DECLS
