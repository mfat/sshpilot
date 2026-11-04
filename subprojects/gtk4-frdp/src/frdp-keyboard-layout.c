/* SPDX-License-Identifier: GPL-3.0-or-later */
/* frdp-keyboard-layout.c — detect the local keyboard layout for RDP
 *
 * Copyright (C) 2026 mFat
 *
 * The server needs to know which layout the user types with: input is sent
 * as physical scancodes, and the remote side turns them into characters.
 * FreeRDP leaves detection to clients since 3.11; this follows the order of
 * FreeRDP's own X11 client (explicit setting, current layout, system
 * language, US) and adds desktop-specific sources:
 *
 *   1. Windows: the active input layout (its KLID is the RDP layout ID)
 *   2. GNOME: org.gnome.desktop.input-sources mru-sources (current first)
 *   3. GTK >= 4.18: the keyboard device's active layout name, mapped back to
 *      XKB via xkeyboard-config's evdev.xml (KDE, Sway, Xfce, ... on
 *      Wayland and X11)
 *   4. KDE: ~/.config/kxkbrc LayoutList (first entry)
 *   5. XKB_DEFAULT_LAYOUT / XKB_DEFAULT_VARIANT
 *   6. the system locale (FreeRDP), then US English
 *
 * An explicit FrdpDisplay:keyboard-layout overrides all of this.
 *
 * This program is free software: you can redistribute it and/or modify
 * it under the terms of the GNU General Public License as published by
 * the Free Software Foundation, either version 3 of the License, or
 * (at your option) any later version.
 */

#include "frdp-keyboard-layout.h"
#include "frdp-xkb-layout-ids.h"

#include <freerdp/locale/keyboard.h>
#include <freerdp/locale/locale.h>
#include <gio/gio.h>
#include <gtk/gtk.h>
#include <stdlib.h>
#include <string.h>

#ifdef G_OS_WIN32
#include <windows.h>
#endif

/* Layouts FreeRDP's table maps to 0 but that have a standard Windows
 * layout. Checked before the table. */
static const struct
{
  const char *layout;
  const char *variant;
  guint32     id;
} overrides[] = {
  /* XKB "ir" is the standard Persian layout; Windows' default Persian
   * keyboard is 0x00000429 (KBD_FARSI). FreeRDP's table has 0 for it. */
  { "ir", "",    KBD_FARSI },
  { "ir", "pro", KBD_FARSI },
};

guint32
frdp_keyboard_layout_from_xkb (const char *layout,
                               const char *variant)
{
  g_autofree char *base = NULL;
  const char      *plus;
  guint32          id;
  gsize            i;

  if (layout == NULL || *layout == '\0')
    return 0;

  /* GNOME writes "layout+variant". */
  if (variant == NULL && (plus = strchr (layout, '+')) != NULL) {
    base = g_strndup (layout, plus - layout);
    variant = plus + 1;
    layout = base;
  }
  if (variant == NULL)
    variant = "";

  for (i = 0; i < G_N_ELEMENTS (overrides); i++)
    if (g_str_equal (layout, overrides[i].layout) && g_str_equal (variant, overrides[i].variant))
      return overrides[i].id;

  id = frdp_xkb_layout_lookup (layout, variant);
  if (id == 0 && *variant != '\0')
    id = frdp_xkb_layout_lookup (layout, "");

  return id;
}

/* ---- xkeyboard-config catalogue (description -> layout/variant) -------- */

typedef struct
{
  const char *wanted;
  gboolean    in_layout;
  gboolean    in_variant;
  gboolean    in_config;
  const char *element;        /* current text element */
  GString    *text;
  char       *layout_name;    /* name of the enclosing <layout> */
  char       *variant_name;   /* name of the enclosing <variant> */
  char       *found_layout;
  char       *found_variant;
} CatalogueParse;

static void
replace_str (char **dest, const char *value)
{
  g_free (*dest);
  *dest = g_strdup (value);
}

static void
catalogue_start (GMarkupParseContext *ctx, const gchar *name, const gchar **attr_names,
                 const gchar **attr_values, gpointer data, GError **error)
{
  CatalogueParse *p = data;

  if (g_str_equal (name, "layout")) {
    p->in_layout = TRUE;
    g_clear_pointer (&p->layout_name, g_free);
  } else if (g_str_equal (name, "variant")) {
    p->in_variant = TRUE;
    g_clear_pointer (&p->variant_name, g_free);
  } else if (g_str_equal (name, "configItem")) {
    p->in_config = TRUE;
  } else if (p->in_config && (g_str_equal (name, "name") || g_str_equal (name, "description"))) {
    p->element = g_str_equal (name, "name") ? "name" : "description";
    g_string_truncate (p->text, 0);
  }
}

static void
catalogue_text (GMarkupParseContext *ctx, const gchar *text, gsize len,
                gpointer data, GError **error)
{
  CatalogueParse *p = data;

  if (p->element != NULL)
    g_string_append_len (p->text, text, len);
}

static void
catalogue_end (GMarkupParseContext *ctx, const gchar *name, gpointer data, GError **error)
{
  CatalogueParse *p = data;

  if (p->element != NULL && g_str_equal (name, p->element)) {
    if (g_str_equal (p->element, "name")) {
      if (p->in_variant)
        replace_str (&p->variant_name, p->text->str);
      else if (p->in_layout)
        replace_str (&p->layout_name, p->text->str);
    } else if (p->found_layout == NULL && g_str_equal (p->text->str, p->wanted) &&
               p->layout_name != NULL) {
      p->found_layout = g_strdup (p->layout_name);
      p->found_variant = g_strdup (p->in_variant && p->variant_name ? p->variant_name : "");
    }
    p->element = NULL;
  } else if (g_str_equal (name, "configItem")) {
    p->in_config = FALSE;
  } else if (g_str_equal (name, "variant")) {
    p->in_variant = FALSE;
  } else if (g_str_equal (name, "layout")) {
    p->in_layout = FALSE;
  }
}

static char *
find_evdev_xml (void)
{
  const char *roots[] = { g_getenv ("XKB_CONFIG_ROOT"), "/usr/share/X11/xkb",
                          "/usr/local/share/X11/xkb", "/opt/homebrew/share/X11/xkb", NULL };
  gsize       i;

  for (i = 0; i < G_N_ELEMENTS (roots); i++) {
    char *path;

    if (roots[i] == NULL)
      continue;
    path = g_build_filename (roots[i], "rules", "evdev.xml", NULL);
    if (g_file_test (path, G_FILE_TEST_IS_REGULAR))
      return path;
    g_free (path);
  }

  return NULL;
}

gboolean
frdp_keyboard_layout_xkb_from_description (const char  *description,
                                           char       **layout,
                                           char       **variant)
{
  static const GMarkupParser parser = { catalogue_start, catalogue_end, catalogue_text, NULL, NULL };
  g_autofree char     *path = find_evdev_xml ();
  g_autofree char     *contents = NULL;
  gsize                length;
  CatalogueParse       p = { 0 };
  GMarkupParseContext *ctx;
  gboolean             found;

  if (description == NULL || path == NULL ||
      !g_file_get_contents (path, &contents, &length, NULL))
    return FALSE;

  p.wanted = description;
  p.text = g_string_new (NULL);
  ctx = g_markup_parse_context_new (&parser, 0, &p, NULL);
  g_markup_parse_context_parse (ctx, contents, length, NULL);
  g_markup_parse_context_free (ctx);

  found = p.found_layout != NULL;
  if (found) {
    *layout = g_steal_pointer (&p.found_layout);
    *variant = g_steal_pointer (&p.found_variant);
  }

  g_free (p.found_layout);
  g_free (p.found_variant);
  g_free (p.layout_name);
  g_free (p.variant_name);
  g_string_free (p.text, TRUE);

  return found;
}

/* ---- sources ------------------------------------------------------------- */

#ifdef G_OS_WIN32
static guint32
detect_windows (void)
{
  char klid[KL_NAMELENGTH] = { 0 };

  /* The KLID ("00000409", "00000429", ...) is the RDP layout ID. */
  if (!GetKeyboardLayoutNameA (klid))
    return 0;

  return (guint32) strtoul (klid, NULL, 16);
}
#endif

static guint32
detect_gnome (void)
{
  GSettingsSchemaSource *source = g_settings_schema_source_get_default ();
  GSettingsSchema       *schema;
  GSettings             *settings;
  GVariant              *sources;
  const char            *type = NULL, *layout = NULL;
  guint32                id = 0;

  if (source == NULL)
    return 0;
  schema = g_settings_schema_source_lookup (source, "org.gnome.desktop.input-sources", TRUE);
  if (schema == NULL)
    return 0;
  if (!g_settings_schema_has_key (schema, "mru-sources")) {
    g_settings_schema_unref (schema);
    return 0;
  }

  settings = g_settings_new_full (schema, NULL, NULL);
  sources = g_settings_get_value (settings, "mru-sources");
  if (g_variant_n_children (sources) >= 1) {
    g_variant_get_child (sources, 0, "(&s&s)", &type, &layout);
    /* "ibus" engines (input methods) have no XKB layout name. */
    if (g_strcmp0 (type, "xkb") == 0)
      id = frdp_keyboard_layout_from_xkb (layout, NULL);
  }

  g_variant_unref (sources);
  g_object_unref (settings);
  g_settings_schema_unref (schema);

  return id;
}

static guint32
detect_gdk_device (void)
{
#if GTK_CHECK_VERSION (4, 18, 0)
  GdkDisplay       *display = gdk_display_get_default ();
  GdkSeat          *seat;
  GdkDevice        *keyboard;
  g_auto(GStrv)     names = NULL;
  int               active;
  g_autofree char  *layout = NULL, *variant = NULL;

  if (display == NULL || (seat = gdk_display_get_default_seat (display)) == NULL ||
      (keyboard = gdk_seat_get_keyboard (seat)) == NULL)
    return 0;

  names = gdk_device_get_layout_names (keyboard);
  active = gdk_device_get_active_layout_index (keyboard);
  if (names == NULL || active < 0 || (guint) active >= g_strv_length (names))
    return 0;

  if (!frdp_keyboard_layout_xkb_from_description (names[active], &layout, &variant))
    return 0;

  return frdp_keyboard_layout_from_xkb (layout, variant);
#else
  return 0;
#endif
}

static guint32
detect_kde (void)
{
  g_autofree char *path = g_build_filename (g_get_user_config_dir (), "kxkbrc", NULL);
  g_autoptr(GKeyFile) file = g_key_file_new ();
  g_auto(GStrv)    layouts = NULL, variants = NULL;
  guint32          id = 0;

  if (!g_key_file_load_from_file (file, path, G_KEY_FILE_NONE, NULL))
    return 0;

  {
    /* KDE separates the lists with commas, not GKeyFile's ';'. */
    g_autofree char *l = g_key_file_get_string (file, "Layout", "LayoutList", NULL);
    g_autofree char *v = g_key_file_get_string (file, "Layout", "VariantList", NULL);

    layouts = l != NULL ? g_strsplit (l, ",", -1) : NULL;
    variants = v != NULL ? g_strsplit (v, ",", -1) : NULL;
  }
  if (layouts != NULL && layouts[0] != NULL)
    id = frdp_keyboard_layout_from_xkb (layouts[0], variants != NULL && variants[0] != NULL ? variants[0] : "");

  return id;
}

static guint32
detect_environment (void)
{
  const char       *layouts = g_getenv ("XKB_DEFAULT_LAYOUT");
  const char       *variants = g_getenv ("XKB_DEFAULT_VARIANT");
  g_auto(GStrv)     l = NULL, v = NULL;

  if (layouts == NULL || *layouts == '\0')
    return 0;

  l = g_strsplit (layouts, ",", 2);
  v = g_strsplit (variants != NULL ? variants : "", ",", 2);

  return frdp_keyboard_layout_from_xkb (l[0], v[0] != NULL ? v[0] : "");
}

guint32
frdp_keyboard_layout_detect (const char **source)
{
  static const struct
  {
    const char *name;
    guint32   (*detect) (void);
  } sources[] = {
#ifdef G_OS_WIN32
    { "windows", detect_windows },
#endif
    { "gnome", detect_gnome },
    { "gdk-device", detect_gdk_device },
    { "kde", detect_kde },
    { "environment", detect_environment },
  };
  DWORD id = 0;
  gsize i;

  for (i = 0; i < G_N_ELEMENTS (sources); i++) {
    id = sources[i].detect ();
    if (id != 0) {
      if (source != NULL)
        *source = sources[i].name;
      return id;
    }
  }

  if (freerdp_detect_keyboard_layout_from_system_locale (&id) == 0 && id != 0) {
    if (source != NULL)
      *source = "locale";
    return id;
  }

  if (source != NULL)
    *source = "default";

  return KBD_US;
}
