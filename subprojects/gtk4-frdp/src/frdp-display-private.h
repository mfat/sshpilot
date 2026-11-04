/* SPDX-License-Identifier: GPL-3.0-or-later */
/* frdp-display-private.h
 *
 * Copyright (C) 2026 mFat
 *
 * Prompt plumbing between FrdpSession (whose FreeRDP callbacks run on the
 * session thread) and FrdpDisplay (main thread). These replace upstream's
 * public frdp_display_authenticate()/..._verify_ex() functions, which spun a
 * nested main loop until the application answered.
 *
 * The session marshals each prompt to the main thread and calls one of the
 * functions below there. They only emit the matching signal; the
 * application answers later through the public *_finish() functions, which
 * forward the answer with frdp_session_answer_prompt().
 *
 * This program is free software: you can redistribute it and/or modify
 * it under the terms of the GNU General Public License as published by
 * the Free Software Foundation, either version 3 of the License, or
 * (at your option) any later version.
 */

#pragma once

#include "frdp-display.h"

G_BEGIN_DECLS

void _frdp_display_request_authentication (FrdpDisplay *self);

void _frdp_display_request_certificate_verification (FrdpDisplay *self,
                                                      const gchar *host,
                                                      guint16      port,
                                                      const gchar *common_name,
                                                      const gchar *subject,
                                                      const gchar *issuer,
                                                      const gchar *fingerprint,
                                                      guint32      flags);

void _frdp_display_request_certificate_change_verification (FrdpDisplay *self,
                                                             const gchar *host,
                                                             guint16      port,
                                                             const gchar *common_name,
                                                             const gchar *subject,
                                                             const gchar *issuer,
                                                             const gchar *fingerprint,
                                                             const gchar *old_subject,
                                                             const gchar *old_issuer,
                                                             const gchar *old_fingerprint,
                                                             guint32      flags);

G_END_DECLS
