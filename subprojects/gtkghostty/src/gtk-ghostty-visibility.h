/* SPDX-License-Identifier: LGPL-2.1-or-later
 * Copyright 2026 mFat <newmfat@gmail.com> and the gtkghostty contributors
 */
/* Symbol visibility for the gtkghostty library. */
#pragma once

#if defined(_WIN32) || defined(__CYGWIN__)
#  ifdef GTK_GHOSTTY_COMPILATION
#    define GTK_GHOSTTY_API __declspec(dllexport) extern
#  else
#    define GTK_GHOSTTY_API __declspec(dllimport) extern
#  endif
#else
#  define GTK_GHOSTTY_API __attribute__((visibility("default"))) extern
#endif
