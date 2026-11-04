/* SPDX-License-Identifier: GPL-3.0-or-later */
/* frdp-channel.h
 *
 * Copyright (C) 2023 Marek Kasik <mkasik@redhat.com>
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

#include <gtk/gtk.h>

G_BEGIN_DECLS

#define FRDP_TYPE_CHANNEL (frdp_channel_get_type())

G_DECLARE_DERIVABLE_TYPE (FrdpChannel, frdp_channel, FRDP, CHANNEL, GObject)

typedef struct _FrdpChannelClass FrdpChannelClass;

struct _FrdpChannelClass
{
  GObjectClass parent_class;
};

G_END_DECLS
