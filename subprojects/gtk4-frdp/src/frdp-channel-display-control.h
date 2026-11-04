/* SPDX-License-Identifier: GPL-3.0-or-later */
/* frdp-channel-display-control.h
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

#include "frdp-channel.h"

G_BEGIN_DECLS

#define FRDP_TYPE_CHANNEL_DISPLAY_CONTROL (frdp_channel_display_control_get_type())

G_DECLARE_FINAL_TYPE (FrdpChannelDisplayControl, frdp_channel_display_control, FRDP, CHANNEL_DISPLAY_CONTROL, GObject)

typedef struct _FrdpChannelDisplayControl FrdpChannelDisplayControl;

struct _FrdpChannelDisplayControl
{
  GObject parent_instance;
};

struct _FrdpChannelDisplayControlClass
{
  FrdpChannelClass parent_class;

  void (*caps_set) (FrdpChannelDisplayControl *self);
};

void frdp_channel_display_control_resize_display (FrdpChannelDisplayControl *self,
                                                  guint                      width,
                                                  guint                      height);

G_END_DECLS
