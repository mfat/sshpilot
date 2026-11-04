/* SPDX-License-Identifier: GPL-3.0-or-later */
/* frdp-channel.c
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

#include "frdp-channel.h"

#include "frdp-session.h"

typedef struct
{
  FrdpSession *session;
} FrdpChannelPrivate;

G_DEFINE_TYPE_WITH_PRIVATE (FrdpChannel, frdp_channel, G_TYPE_OBJECT)

enum
{
  PROP_0 = 0,
  PROP_SESSION,
};

static void
frdp_channel_get_property (GObject      *object,
                           guint         property_id,
                           GValue       *value,
                           GParamSpec   *pspec)
{
  FrdpChannel        *self = FRDP_CHANNEL (object);
  FrdpChannelPrivate *priv = frdp_channel_get_instance_private (self);

  switch (property_id)
    {
      case PROP_SESSION:
        g_value_set_pointer (value, priv->session);
        break;
      default:
        G_OBJECT_WARN_INVALID_PROPERTY_ID (object, property_id, pspec);
        break;
    }
}

static void
frdp_channel_set_property (GObject      *object,
                           guint         property_id,
                           const GValue *value,
                           GParamSpec   *pspec)
{
  FrdpChannel        *self = FRDP_CHANNEL (object);
  FrdpChannelPrivate *priv = frdp_channel_get_instance_private (self);

  switch (property_id)
    {
      case PROP_SESSION:
        priv->session = g_value_get_pointer (value);
        break;
      default:
        G_OBJECT_WARN_INVALID_PROPERTY_ID (object, property_id, pspec);
        break;
    }
}

static void
frdp_channel_class_init (FrdpChannelClass *klass)
{
  GObjectClass *gobject_class = G_OBJECT_CLASS (klass);

  gobject_class->get_property = frdp_channel_get_property;
  gobject_class->set_property = frdp_channel_set_property;

  g_object_class_install_property (gobject_class,
                                   PROP_SESSION,
                                   g_param_spec_pointer ("session",
                                                         "session",
                                                         "Current RDP session",
                                                         G_PARAM_READWRITE));
}

static void
frdp_channel_init (FrdpChannel *self)
{
}
