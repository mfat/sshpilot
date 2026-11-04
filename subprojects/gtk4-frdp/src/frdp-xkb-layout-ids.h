/* SPDX-License-Identifier: Apache-2.0 */
/* Imported from FreeRDP 3.32.1 (client/X11/xkb_layout_ids.h), Apache-2.0. */
#pragma once

#include <freerdp/types.h>

/* XKB layout + variant ("" for none) -> RDP keyboard layout ID, 0 if unknown. */
UINT32 frdp_xkb_layout_lookup (const char *layout, const char *variant);
