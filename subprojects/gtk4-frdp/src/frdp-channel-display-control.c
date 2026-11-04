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

#include "frdp-channel-display-control.h"

#include <freerdp/freerdp.h>
#include <freerdp/client/disp.h>

typedef struct
{
  DispClientContext *display_client_context;

  guint32            max_num_monitors;
  guint32            max_monitor_area_factor_a;
  guint32            max_monitor_area_factor_b;

  gboolean           caps_set;
} FrdpChannelDisplayControlPrivate;

G_DEFINE_TYPE_WITH_PRIVATE (FrdpChannelDisplayControl, frdp_channel_display_control, FRDP_TYPE_CHANNEL)

enum
{
  PROP_0 = 0,
  PROP_DISPLAY_CLIENT_CONTEXT,
  PROP_MAX_NUM_MONITORS,
  PROP_MAX_MONITOR_AREA_FACTOR_A,
  PROP_MAX_MONITOR_AREA_FACTOR_B,
  LAST_PROP
};

enum
{
  CAPS_SET,
  LAST_SIGNAL
};

static guint signals[LAST_SIGNAL];

static void frdp_channel_display_control_set_client_context (FrdpChannelDisplayControl *self,
                                                             DispClientContext         *context);

static void
frdp_channel_display_control_get_property (GObject    *object,
                                           guint       property_id,
                                           GValue     *value,
                                           GParamSpec *pspec)
{
  FrdpChannelDisplayControl        *self = FRDP_CHANNEL_DISPLAY_CONTROL (object);
  FrdpChannelDisplayControlPrivate *priv = frdp_channel_display_control_get_instance_private (self);

  switch (property_id)
    {
      case PROP_DISPLAY_CLIENT_CONTEXT:
        g_value_set_pointer (value, priv->display_client_context);
        break;
      case PROP_MAX_NUM_MONITORS:
        g_value_set_uint (value, priv->max_num_monitors);
        break;
      case PROP_MAX_MONITOR_AREA_FACTOR_A:
        g_value_set_uint (value, priv->max_monitor_area_factor_a);
        break;
      case PROP_MAX_MONITOR_AREA_FACTOR_B:
        g_value_set_uint (value, priv->max_monitor_area_factor_b);
        break;
      default:
        G_OBJECT_WARN_INVALID_PROPERTY_ID (object, property_id, pspec);
        break;
    }
}

static void
frdp_channel_display_control_set_property (GObject      *object,
                                           guint         property_id,
                                           const GValue *value,
                                           GParamSpec   *pspec)
{
  FrdpChannelDisplayControl        *self = FRDP_CHANNEL_DISPLAY_CONTROL (object);
  FrdpChannelDisplayControlPrivate *priv = frdp_channel_display_control_get_instance_private (self);

  switch (property_id)
    {
      case PROP_DISPLAY_CLIENT_CONTEXT:
        frdp_channel_display_control_set_client_context (self, g_value_get_pointer (value));
        break;
      case PROP_MAX_NUM_MONITORS:
        priv->max_num_monitors = g_value_get_uint (value);
        break;
      case PROP_MAX_MONITOR_AREA_FACTOR_A:
        priv->max_monitor_area_factor_a = g_value_get_uint (value);
        break;
      case PROP_MAX_MONITOR_AREA_FACTOR_B:
        priv->max_monitor_area_factor_b = g_value_get_uint (value);
        break;
      default:
        G_OBJECT_WARN_INVALID_PROPERTY_ID (object, property_id, pspec);
        break;
    }
}

static void
frdp_channel_display_control_class_init (FrdpChannelDisplayControlClass *klass)
{
  GObjectClass *gobject_class = G_OBJECT_CLASS (klass);

  gobject_class->get_property = frdp_channel_display_control_get_property;
  gobject_class->set_property = frdp_channel_display_control_set_property;

  g_object_class_install_property (gobject_class,
                                   PROP_DISPLAY_CLIENT_CONTEXT,
                                   g_param_spec_pointer ("display-client-context",
                                                         "display-client-context",
                                                         "Context for display client",
                                                         G_PARAM_READWRITE));

  g_object_class_install_property (gobject_class,
                                   PROP_MAX_NUM_MONITORS,
                                   g_param_spec_uint ("max-num-monitors",
                                                      "max-num-monitors",
                                                      "Maximum number of monitors supported by the server",
                                                      0,
                                                      UINT_MAX,
                                                      16,
                                                      G_PARAM_READWRITE));

  g_object_class_install_property (gobject_class,
                                   PROP_MAX_MONITOR_AREA_FACTOR_A,
                                   g_param_spec_uint ("max-monitor-area-factor-a",
                                                      "max-monitor-area-factor-a",
                                                      "Maximum monitor area factor A",
                                                      0,
                                                      UINT_MAX,
                                                      8192,
                                                      G_PARAM_READWRITE));

  g_object_class_install_property (gobject_class,
                                   PROP_MAX_MONITOR_AREA_FACTOR_B,
                                   g_param_spec_uint ("max-monitor-area-factor-b",
                                                      "max-monitor-area-factor-b",
                                                      "Maximum monitor area factor B",
                                                      0,
                                                      UINT_MAX,
                                                      8192,
                                                      G_PARAM_READWRITE));

  signals[CAPS_SET] = g_signal_new ("caps-set",
                                    G_TYPE_FROM_CLASS (klass),
                                    G_SIGNAL_RUN_LAST,
                                    0, NULL, NULL, NULL,
                                    G_TYPE_NONE, 0);
}

static void
frdp_channel_display_control_init (FrdpChannelDisplayControl *self)
{
}

static guint
display_control_caps (DispClientContext *context,
                      guint32            max_num_monitors,
                      guint32            max_monitor_area_factor_a,
                      guint32            max_monitor_area_factor_b)
{
  FrdpChannelDisplayControlPrivate *priv;
  FrdpChannelDisplayControl        *channel;

  if (context != NULL) {
    channel = (FrdpChannelDisplayControl *) context->custom;
    priv = frdp_channel_display_control_get_instance_private (channel);

    g_object_set (G_OBJECT (channel),
                  "max-num-monitors", max_num_monitors,
                  "max-monitor-area-factor-a", max_monitor_area_factor_a,
                  "max-monitor-area-factor-b", max_monitor_area_factor_b,
                  NULL);

    priv->caps_set = TRUE;
    g_signal_emit (channel, signals[CAPS_SET], 0);
  }

  return CHANNEL_RC_OK;
}

static void
frdp_channel_display_control_set_client_context (FrdpChannelDisplayControl *self,
                                                 DispClientContext         *context)
{
  FrdpChannelDisplayControlPrivate *priv = frdp_channel_display_control_get_instance_private (self);

  priv->display_client_context = context;
  context->custom = self;
  context->DisplayControlCaps = display_control_caps;
}

void
frdp_channel_display_control_resize_display (FrdpChannelDisplayControl *self,
                                             guint                      width,
                                             guint                      height)
{
  FrdpChannelDisplayControlPrivate *priv = frdp_channel_display_control_get_instance_private (self);
  DISPLAY_CONTROL_MONITOR_LAYOUT    monitor_layout = {};
  guint32                           request_width, request_height;
  guint                             ret_value;

  request_width = CLAMP (width,
                         DISPLAY_CONTROL_MIN_MONITOR_WIDTH,
                         DISPLAY_CONTROL_MAX_MONITOR_WIDTH);

  request_height = CLAMP (height,
                          DISPLAY_CONTROL_MIN_MONITOR_WIDTH,
                          DISPLAY_CONTROL_MAX_MONITOR_WIDTH);

  if (request_width % 2 == 1)
    request_width--;

  if (priv->display_client_context != NULL &&
      priv->caps_set &&
      (request_width * request_height) <= (priv->max_num_monitors * priv->max_monitor_area_factor_a * priv->max_monitor_area_factor_b)) {

    monitor_layout.Flags = DISPLAY_CONTROL_MONITOR_PRIMARY;
    monitor_layout.Width = request_width;
    monitor_layout.Height = request_height;
    monitor_layout.Orientation = ORIENTATION_LANDSCAPE;
    monitor_layout.DesktopScaleFactor = 100;
    monitor_layout.DeviceScaleFactor = 100;

    ret_value = priv->display_client_context->SendMonitorLayout (priv->display_client_context, 1, &monitor_layout);
    if (ret_value != CHANNEL_RC_OK)
      g_warning ("Changing of monitor layout failed with Win32 error code 0x%X", ret_value);
  } else {
    if (priv->display_client_context == NULL)
      g_warning ("DispClientContext has not been set yet!");

    if (!priv->caps_set)
      g_warning ("DisplayControlCaps() has not been called yet!");

    if ((request_width * request_height) > (priv->max_num_monitors * priv->max_monitor_area_factor_a * priv->max_monitor_area_factor_b))
      g_warning ("Requested display area is larger than allowed maximum area!");
  }
}
