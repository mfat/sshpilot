/* SPDX-License-Identifier: GPL-3.0-or-later */
/* frdp-context.h
 *
 * Copyright (C) 2019 Armin Novak <akallabeth@posteo.net>
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

#include <freerdp/freerdp.h>
#include <freerdp/client/disp.h>
#include <gtk/gtk.h>

G_BEGIN_DECLS

typedef struct _FrdpSession FrdpSession;

struct frdp_context
{
  rdpContext context;
  FrdpSession *self;
};
typedef struct frdp_context frdpContext;

G_END_DECLS
