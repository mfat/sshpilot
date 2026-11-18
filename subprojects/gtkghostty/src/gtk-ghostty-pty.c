/* SPDX-License-Identifier: LGPL-2.1-or-later
 * Copyright 2026 mFat <newmfat@gmail.com> and the gtkghostty contributors
 */
/* GtkGhosttyPty: a program on a pseudo terminal, for GtkGhosttyTerminal.
 *
 * Ported from SSH Pilot's sshpilot/local_shell_pty.py (process model) and
 * sshpilot/platform/windows/conpty.py (ConPTY), relicensed by their author
 * for gtkghostty under LGPL-2.1-or-later; see NOTICE.
 *
 * POSIX: the child becomes a session leader with the PTY slave as its
 * controlling terminal. The master is read from a GLib fd watch and written
 * without blocking, from a queue, so a program that stops reading cannot
 * freeze the window.
 *
 * Windows: Microsoft's "Creating a Pseudoconsole session" recipe. The three
 * pseudo-console calls come from a bundled conpty.dll (which starts the
 * OpenConsole.exe beside it) when one is found, else from kernel32. Output
 * is read on a thread. ClosePseudoConsole on Windows 10 waits for conhost,
 * which waits until its last frame is written to the output pipe, so the
 * console is closed on its own thread while the reader keeps draining.
 *
 * Either way "exited" is emitted once, after the last output.
 */

/* posix_openpt, grantpt, unlockpt, ptsname, setsid, kill: hidden by strict C11. */
#ifndef _WIN32
# define _XOPEN_SOURCE 700
# define _DEFAULT_SOURCE 1
# define _DARWIN_C_SOURCE 1
#endif

#include "gtk-ghostty-pty.h"

#include <string.h>

#ifdef G_OS_WIN32
# include <windows.h>
#else
# include <errno.h>
# include <fcntl.h>
# include <signal.h>
# include <stdlib.h>
# include <sys/ioctl.h>
# include <sys/types.h>
# include <termios.h>
# include <unistd.h>
# include <glib-unix.h>
extern char **environ;
#endif

#define READ_SIZE (64 * 1024)

enum
{
  SIGNAL_OUTPUT,
  SIGNAL_EXITED,
  N_SIGNALS
};

static guint signals[N_SIGNALS];

#ifdef G_OS_WIN32
typedef struct _Io Io;
#endif

struct _GtkGhosttyPty
{
  GObject parent_instance;

  int pid;
  gboolean exit_seen;      /* the process ended */
  gboolean eof;            /* the last output was read */
  gboolean exited_emitted;
  int status;

#ifdef G_OS_WIN32
  Io *io;
#else
  int master;
  guint read_source;
  guint write_source;
  guint child_source;
  GByteArray *pending;     /* input the program has not taken yet */
#endif
};

G_DEFINE_FINAL_TYPE (GtkGhosttyPty, gtk_ghostty_pty, G_TYPE_OBJECT)

static void
emit_output (GtkGhosttyPty *self, const guint8 *data, gsize length)
{
  GBytes *bytes = g_bytes_new (data, length);
  g_signal_emit (self, signals[SIGNAL_OUTPUT], 0, bytes);
  g_bytes_unref (bytes);
}

static void
maybe_emit_exited (GtkGhosttyPty *self)
{
  if (self->exited_emitted || !self->exit_seen || !self->eof)
    return;
  self->exited_emitted = TRUE;
  g_signal_emit (self, signals[SIGNAL_EXITED], 0, self->status);
}

/* ================================================================ POSIX */
#ifndef G_OS_WIN32

static void
posix_close_master (GtkGhosttyPty *self)
{
  g_clear_handle_id (&self->read_source, g_source_remove);
  g_clear_handle_id (&self->write_source, g_source_remove);
  if (self->master >= 0)
    {
      close (self->master);
      self->master = -1;
    }
}

/* Read what is there; FALSE once the output has ended. */
static gboolean
posix_drain (GtkGhosttyPty *self, gboolean once)
{
  guint8 buf[READ_SIZE];
  while (self->master >= 0)
    {
      ssize_t n = read (self->master, buf, sizeof buf);
      if (n > 0)
        {
          emit_output (self, buf, (gsize) n);
          if (once)
            return TRUE;
          continue;
        }
      if (n < 0 && errno == EINTR)
        continue;
      if (n < 0 && errno == EAGAIN)
        return TRUE;
      /* 0, or EIO once the slave side is gone (Linux). */
      return FALSE;
    }
  return FALSE;
}

static gboolean
on_master_readable (int fd, GIOCondition condition, gpointer data)
{
  GtkGhosttyPty *self = data;
  if (posix_drain (self, TRUE))
    return G_SOURCE_CONTINUE;
  self->read_source = 0;
  self->eof = TRUE;
  maybe_emit_exited (self);
  return G_SOURCE_REMOVE;
}

static void posix_flush (GtkGhosttyPty *self);

static gboolean
on_master_writable (int fd, GIOCondition condition, gpointer data)
{
  GtkGhosttyPty *self = data;
  self->write_source = 0;
  posix_flush (self);
  return G_SOURCE_REMOVE;
}

static void
posix_flush (GtkGhosttyPty *self)
{
  while (self->master >= 0 && self->pending->len > 0)
    {
      ssize_t n = write (self->master, self->pending->data, self->pending->len);
      if (n > 0)
        {
          g_byte_array_remove_range (self->pending, 0, (guint) n);
          continue;
        }
      if (n < 0 && errno == EINTR)
        continue;
      if (n < 0 && errno == EAGAIN)
        {
          if (self->write_source == 0)
            self->write_source = g_unix_fd_add (self->master, G_IO_OUT, on_master_writable, self);
          return;
        }
      g_byte_array_set_size (self->pending, 0);  /* the program is gone */
      return;
    }
}

/* After disposal nothing is left to report to; the child must still be
 * reaped or it stays a zombie until the application exits. */
static void
reap_only (GPid pid, int wait_status, gpointer data)
{
  (void) wait_status;
  (void) data;
  g_spawn_close_pid (pid);
}

static void
on_child_exited (GPid pid, int wait_status, gpointer data)
{
  GtkGhosttyPty *self = data;
  self->child_source = 0;
  g_spawn_close_pid (pid);
  if (WIFEXITED (wait_status))
    self->status = WEXITSTATUS (wait_status);
  else if (WIFSIGNALED (wait_status))
    self->status = 128 + WTERMSIG (wait_status);
  self->exit_seen = TRUE;
  /* The slave may stay open in a background job: take what is buffered
   * and finish, rather than wait for an EOF that may never come. */
  if (!self->eof)
    {
      posix_drain (self, FALSE);
      self->eof = TRUE;
      posix_close_master (self);
    }
  maybe_emit_exited (self);
}

static void
posix_set_size (int fd, guint columns, guint rows)
{
  struct winsize size = { 0 };
  size.ws_col = (unsigned short) CLAMP (columns, 1, G_MAXUINT16);
  size.ws_row = (unsigned short) CLAMP (rows, 1, G_MAXUINT16);
  ioctl (fd, TIOCSWINSZ, &size);
}

static gboolean
posix_spawn_pty (GtkGhosttyPty *self, const char * const *argv, const char * const *envp,
                 const char *cwd, guint columns, guint rows, GError **error)
{
  int master, slave;
  char *slave_name;
  pid_t pid;
  char **child_argv, **child_envp;

  master = posix_openpt (O_RDWR | O_NOCTTY);
  if (master < 0 || grantpt (master) != 0 || unlockpt (master) != 0)
    {
      int saved = errno;
      if (master >= 0)
        close (master);
      g_set_error (error, G_IO_ERROR, g_io_error_from_errno (saved), "Could not open a pseudo terminal: %s",
                   g_strerror (saved));
      return FALSE;
    }
  slave_name = g_strdup (ptsname (master));
  /* Open the terminal side here, before setting its size: BSD kernels
   * (macOS among them) reset the window size to 0x0 on its first open, so
   * a size set before the child opened it was lost and the program started
   * at 0x0 (as forkpty does it). */
  slave = open (slave_name, O_RDWR | O_NOCTTY | O_CLOEXEC);
  g_free (slave_name);
  if (slave < 0)
    {
      int saved = errno;
      close (master);
      g_set_error (error, G_IO_ERROR, g_io_error_from_errno (saved), "Could not open a pseudo terminal: %s",
                   g_strerror (saved));
      return FALSE;
    }
  posix_set_size (master, columns, rows);
  /* Everything the child needs is made before fork: between fork and exec
   * only async-signal-safe calls are allowed in a GTK process. */
  child_argv = g_strdupv ((char **) argv);
  child_envp = envp != NULL ? g_strdupv ((char **) envp) : NULL;

  pid = fork ();
  if (pid < 0)
    {
      int saved = errno;
      close (master);
      close (slave);
      g_strfreev (child_argv);
      g_strfreev (child_envp);
      g_set_error (error, G_IO_ERROR, g_io_error_from_errno (saved), "fork failed: %s", g_strerror (saved));
      return FALSE;
    }
  if (pid == 0)
    {
      setsid ();
      if (ioctl (slave, TIOCSCTTY, 0) != 0)  /* make it the session's terminal */
        _exit (127);
      dup2 (slave, 0);
      dup2 (slave, 1);
      dup2 (slave, 2);
      if (slave > 2)
        close (slave);
      else
        fcntl (slave, F_SETFD, 0);  /* dup2 onto itself keeps O_CLOEXEC */
      close (master);
      if (cwd != NULL && chdir (cwd) != 0)
        _exit (127);
      if (child_envp != NULL)
        environ = child_envp;
      signal (SIGPIPE, SIG_DFL);
      execvp (child_argv[0], child_argv);
      _exit (127);
    }

  close (slave);
  g_strfreev (child_argv);
  g_strfreev (child_envp);
  fcntl (master, F_SETFL, fcntl (master, F_GETFL) | O_NONBLOCK);
  fcntl (master, F_SETFD, FD_CLOEXEC);
  self->master = master;
  self->pid = (int) pid;
  self->read_source = g_unix_fd_add (master, G_IO_IN | G_IO_HUP | G_IO_ERR, on_master_readable, self);
  self->child_source = g_child_watch_add ((GPid) pid, on_child_exited, self);
  return TRUE;
}

#else
/* ============================================================== Windows */

#ifndef PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE
# define PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE 0x00020016
#endif
#ifndef EXTENDED_STARTUPINFO_PRESENT
# define EXTENDED_STARTUPINFO_PRESENT 0x00080000
#endif

#define CLOSE_TIMEOUT_MS 2000
/* After the client exits, the console is closed once its output has been
 * quiet this long (or after the cap), so the final frame gets through. */
#define EXIT_QUIET_US (100 * 1000)
#define EXIT_QUIET_CAP_US (500 * 1000)

typedef void *GhosttyHPCON;
typedef HRESULT (WINAPI *CreatePseudoConsoleFn) (COORD, HANDLE, HANDLE, DWORD, GhosttyHPCON *);
typedef HRESULT (WINAPI *ResizePseudoConsoleFn) (GhosttyHPCON, COORD);
typedef void (WINAPI *ClosePseudoConsoleFn) (GhosttyHPCON);

typedef struct
{
  CreatePseudoConsoleFn create;
  ResizePseudoConsoleFn resize;
  ClosePseudoConsoleFn close;
} ConPtyApi;

struct _Io
{
  gatomicrefcount ref;
  GMutex lock;             /* console and process handles */
  GMutex write_lock;       /* the input pipe, whose writes can block */
  GMainContext *context;
  GWeakRef object;
  ConPtyApi api;
  GhosttyHPCON hpc;
  HANDLE input_write, output_read, process;
  gint64 last_output;      /* g_get_monotonic_time (), atomically */
  GThread *close_thread;
  gboolean eof;
  int status;
};

static gboolean
load_api_from (HMODULE module, const char *prefix, ConPtyApi *api)
{
  char name[64];
  g_snprintf (name, sizeof name, "%sCreatePseudoConsole", prefix);
  api->create = (CreatePseudoConsoleFn) (void *) GetProcAddress (module, name);
  g_snprintf (name, sizeof name, "%sResizePseudoConsole", prefix);
  api->resize = (ResizePseudoConsoleFn) (void *) GetProcAddress (module, name);
  g_snprintf (name, sizeof name, "%sClosePseudoConsole", prefix);
  api->close = (ClosePseudoConsoleFn) (void *) GetProcAddress (module, name);
  return api->create != NULL && api->resize != NULL && api->close != NULL;
}

static char *
module_directory (HMODULE module)
{
  wchar_t path[MAX_PATH];
  DWORD length = GetModuleFileNameW (module, path, MAX_PATH);
  char *utf8, *dir;
  if (length == 0 || length >= MAX_PATH)
    return NULL;
  utf8 = g_utf16_to_utf8 ((const gunichar2 *) path, length, NULL, NULL, NULL);
  dir = utf8 != NULL ? g_path_get_dirname (utf8) : NULL;
  g_free (utf8);
  return dir;
}

static gboolean
try_conpty_dll (const char *path, ConPtyApi *api)
{
  gunichar2 *wide;
  HMODULE module;
  if (!g_file_test (path, G_FILE_TEST_IS_REGULAR))
    return FALSE;
  wide = g_utf8_to_utf16 (path, -1, NULL, NULL, NULL);
  module = wide != NULL ? LoadLibraryW ((const wchar_t *) wide) : NULL;
  g_free (wide);
  if (module == NULL)
    return FALSE;
  if (!load_api_from (module, "Conpty", api))
    {
      FreeLibrary (module);
      return FALSE;
    }
  g_debug ("ConPTY: using %s", path);
  return TRUE;
}

/* GTK_GHOSTTY_CONPTY_DLL names a conpty.dll (or its folder), or "kernel32"
 * for the inbox console; otherwise a conpty.dll next to the program or next
 * to this library is used when there is one. */
static gboolean
load_conpty_api (ConPtyApi *api)
{
  static ConPtyApi cached;
  static gsize once = 0;
  if (g_once_init_enter (&once))
    {
      const char *override = g_getenv ("GTK_GHOSTTY_CONPTY_DLL");
      gboolean found = FALSE;
      memset (&cached, 0, sizeof cached);
      if (override != NULL && *override != '\0')
        {
          if (g_ascii_strcasecmp (override, "kernel32") != 0)
            {
              char *path = g_file_test (override, G_FILE_TEST_IS_DIR)
                             ? g_build_filename (override, "conpty.dll", NULL) : g_strdup (override);
              found = try_conpty_dll (path, &cached);
              g_free (path);
            }
        }
      else
        {
          HMODULE self_module = NULL;
          char *dirs[2];
          dirs[0] = module_directory (NULL);
          GetModuleHandleExW (GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS | GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT,
                              (LPCWSTR) (void *) load_conpty_api, &self_module);
          dirs[1] = self_module != NULL ? module_directory (self_module) : NULL;
          for (int i = 0; i < 2 && !found; i++)
            if (dirs[i] != NULL)
              {
                char *path = g_build_filename (dirs[i], "conpty.dll", NULL);
                found = try_conpty_dll (path, &cached);
                g_free (path);
              }
          g_free (dirs[0]);
          g_free (dirs[1]);
        }
      if (!found && !load_api_from (GetModuleHandleW (L"kernel32.dll"), "", &cached))
        memset (&cached, 0, sizeof cached);
      g_once_init_leave (&once, 1);
    }
  *api = cached;
  return api->create != NULL;
}

static Io *
io_ref (Io *io)
{
  g_atomic_ref_count_inc (&io->ref);
  return io;
}

static void
io_close_handle (HANDLE *handle)
{
  if (*handle != NULL && *handle != INVALID_HANDLE_VALUE)
    CloseHandle (*handle);
  *handle = NULL;
}

static void
io_unref (Io *io)
{
  if (!g_atomic_ref_count_dec (&io->ref))
    return;
  if (io->close_thread != NULL)
    g_thread_unref (io->close_thread);
  io_close_handle (&io->input_write);
  io_close_handle (&io->output_read);
  io_close_handle (&io->process);
  g_weak_ref_clear (&io->object);
  g_main_context_unref (io->context);
  g_mutex_clear (&io->lock);
  g_mutex_clear (&io->write_lock);
  g_free (io);
}

static COORD
make_coord (guint columns, guint rows)
{
  COORD coord;
  coord.X = (SHORT) CLAMP (columns, 1, 0x7FFF);
  coord.Y = (SHORT) CLAMP (rows, 1, 0x7FFF);
  return coord;
}

typedef struct
{
  Io *io;
  GBytes *bytes;     /* output; NULL for "end of output" / "exited" */
  gboolean exited;
  int status;
} Event;

static void
event_free (gpointer data)
{
  Event *event = data;
  g_clear_pointer (&event->bytes, g_bytes_unref);
  io_unref (event->io);
  g_free (event);
}

static gboolean
dispatch_event (gpointer data)
{
  Event *event = data;
  GtkGhosttyPty *self = g_weak_ref_get (&event->io->object);
  if (self == NULL)
    return G_SOURCE_REMOVE;
  if (event->bytes != NULL)
    g_signal_emit (self, signals[SIGNAL_OUTPUT], 0, event->bytes);
  else if (event->exited)
    {
      self->status = event->status;
      self->exit_seen = TRUE;
    }
  else
    self->eof = TRUE;
  maybe_emit_exited (self);
  g_object_unref (self);
  return G_SOURCE_REMOVE;
}

static void
post (Io *io, GBytes *bytes, gboolean exited, int status)
{
  Event *event = g_new0 (Event, 1);
  event->io = io_ref (io);
  event->bytes = bytes;
  event->exited = exited;
  event->status = status;
  g_main_context_invoke_full (io->context, G_PRIORITY_DEFAULT, dispatch_event, event, event_free);
}

static gpointer
reader_thread (gpointer data)
{
  Io *io = data;
  guint8 *buf = g_malloc (READ_SIZE);
  for (;;)
    {
      DWORD count = 0;
      if (!ReadFile (io->output_read, buf, READ_SIZE, &count, NULL) || count == 0)
        break;
      io->last_output = g_get_monotonic_time ();  /* read by the exit watcher: a hint, races are harmless */
      post (io, g_bytes_new (buf, count), FALSE, 0);
    }
  g_free (buf);
  post (io, NULL, FALSE, 0);  /* end of output */
  io_unref (io);
  return NULL;
}

static gpointer
close_console_thread (gpointer data)
{
  Io *io = data;
  GhosttyHPCON hpc;
  g_mutex_lock (&io->lock);
  hpc = io->hpc;
  io->hpc = NULL;
  g_mutex_unlock (&io->lock);
  if (hpc != NULL)
    io->api.close (hpc);
  io_unref (io);
  return NULL;
}

/* Starts ClosePseudoConsole on its own thread (once) and waits up to
 * timeout_ms for it; TRUE when the console is closed. */
static gboolean
close_console (Io *io, guint timeout_ms)
{
  GThread *thread;
  gint64 deadline = g_get_monotonic_time () + (gint64) timeout_ms * 1000;
  g_mutex_lock (&io->lock);
  if (io->close_thread == NULL)
    io->close_thread = g_thread_new ("gtkghostty-conpty-close", close_console_thread, io_ref (io));
  thread = io->close_thread;
  g_mutex_unlock (&io->lock);
  /* GThread has no timed join: poll the console handle the thread clears. */
  for (;;)
    {
      gboolean closed;
      g_mutex_lock (&io->lock);
      closed = io->hpc == NULL;
      g_mutex_unlock (&io->lock);
      if (closed)
        {
          /* hpc is cleared before the call; give the call itself the rest of the time. */
          break;
        }
      if (g_get_monotonic_time () >= deadline)
        return FALSE;
      g_usleep (10 * 1000);
    }
  (void) thread;
  return TRUE;
}

static void
terminate_if_alive (Io *io)
{
  /* A client closed before it finished attaching survives ClosePseudoConsole
   * together with its console host (measured with cmd.exe and ssh.exe
   * through conpty.dll 1.25); end it as Neovim's ConPTY host does. */
  g_mutex_lock (&io->lock);
  if (io->process != NULL && WaitForSingleObject (io->process, 0) != WAIT_OBJECT_0)
    TerminateProcess (io->process, 1);
  g_mutex_unlock (&io->lock);
}

static gpointer
exit_thread (gpointer data)
{
  Io *io = data;
  DWORD code = 0;
  gint64 deadline;
  WaitForSingleObject (io->process, INFINITE);
  /* The client is gone; conhost may still be rendering its last frame. */
  deadline = g_get_monotonic_time () + EXIT_QUIET_CAP_US;
  while (g_get_monotonic_time () < deadline
         && g_get_monotonic_time () - io->last_output < EXIT_QUIET_US)
    g_usleep (20 * 1000);
  if (close_console (io, CLOSE_TIMEOUT_MS))
    {
      g_mutex_lock (&io->write_lock);
      io_close_handle (&io->input_write);
      g_mutex_unlock (&io->write_lock);
    }
  else
    g_warning ("ClosePseudoConsole did not return within %d ms", CLOSE_TIMEOUT_MS);
  g_mutex_lock (&io->lock);
  GetExitCodeProcess (io->process, &code);
  g_mutex_unlock (&io->lock);
  post (io, NULL, TRUE, (int) code);
  io_unref (io);
  return NULL;
}

/* Quote arguments the way the Microsoft C runtime parses them back: an
 * empty argument or one with a space or tab is wrapped in quotes; a quote is
 * escaped and the backslashes before it doubled. */
static char *
windows_command_line (const char * const *argv)
{
  GString *line = g_string_new (NULL);
  for (int i = 0; argv[i] != NULL; i++)
    {
      const char *arg = argv[i];
      gboolean quote = arg[0] == '\0' || strchr (arg, ' ') != NULL || strchr (arg, '\t') != NULL;
      int backslashes = 0;
      if (i > 0)
        g_string_append_c (line, ' ');
      if (quote)
        g_string_append_c (line, '"');
      for (const char *p = arg; *p != '\0'; p++)
        {
          if (*p == '\\')
            backslashes++;
          else if (*p == '"')
            {
              for (int k = 0; k < backslashes * 2; k++)
                g_string_append_c (line, '\\');
              g_string_append (line, "\\\"");
              backslashes = 0;
            }
          else
            {
              for (int k = 0; k < backslashes; k++)
                g_string_append_c (line, '\\');
              g_string_append_c (line, *p);
              backslashes = 0;
            }
        }
      for (int k = 0; k < (quote ? backslashes * 2 : backslashes); k++)
        g_string_append_c (line, '\\');
      if (quote)
        g_string_append_c (line, '"');
    }
  return g_string_free (line, FALSE);
}

/* "NAME=value\0...\0\0" in UTF-16. Each entry is converted on its own:
 * g_utf8_to_utf16 stops at the first NUL even when given a length. */
static gunichar2 *
environment_block (const char * const *envp)
{
  GArray *block = g_array_new (FALSE, FALSE, sizeof (gunichar2));
  const gunichar2 nul = 0;
  for (int i = 0; envp[i] != NULL; i++)
    {
      glong length = 0;
      gunichar2 *entry = g_utf8_to_utf16 (envp[i], -1, NULL, &length, NULL);
      if (entry == NULL)
        continue;  /* not UTF-8: leave the variable out */
      g_array_append_vals (block, entry, (guint) length);
      g_array_append_val (block, nul);
      g_free (entry);
    }
  g_array_append_val (block, nul);
  if (block->len == 1)
    g_array_append_val (block, nul);  /* an empty block is two NULs */
  return (gunichar2 *) g_array_free (block, FALSE);
}

static gboolean
win_spawn_pty (GtkGhosttyPty *self, const char * const *argv, const char * const *envp,
               const char *cwd, guint columns, guint rows, GError **error)
{
  Io *io;
  HANDLE input_read = NULL, output_write = NULL;
  SIZE_T attribute_size = 0;
  LPPROC_THREAD_ATTRIBUTE_LIST attributes = NULL;
  STARTUPINFOEXW startup;
  PROCESS_INFORMATION process;
  char *command;
  gunichar2 *wide_command = NULL, *wide_cwd = NULL, *wide_env = NULL;
  HRESULT hr;
  BOOL ok;

  io = g_new0 (Io, 1);
  g_atomic_ref_count_init (&io->ref);
  g_mutex_init (&io->lock);
  g_mutex_init (&io->write_lock);
  io->context = g_main_context_ref_thread_default ();
  g_weak_ref_init (&io->object, self);
  io->last_output = g_get_monotonic_time ();

  if (!load_conpty_api (&io->api))
    {
      g_set_error (error, G_IO_ERROR, G_IO_ERROR_NOT_SUPPORTED, "ConPTY is not available (Windows 10 1809 or later)");
      io_unref (io);
      return FALSE;
    }
  if (!CreatePipe (&input_read, &io->input_write, NULL, 0)
      || !CreatePipe (&io->output_read, &output_write, NULL, 0))
    {
      g_set_error (error, G_IO_ERROR, G_IO_ERROR_FAILED, "CreatePipe failed (%lu)", GetLastError ());
      io_close_handle (&input_read);
      io_close_handle (&output_write);
      io_unref (io);
      return FALSE;
    }
  hr = io->api.create (make_coord (columns, rows), input_read, output_write, 0, &io->hpc);
  if (FAILED (hr))
    {
      g_set_error (error, G_IO_ERROR, G_IO_ERROR_FAILED, "CreatePseudoConsole failed: HRESULT 0x%08lX",
                   (unsigned long) hr);
      io_close_handle (&input_read);
      io_close_handle (&output_write);
      io_unref (io);
      return FALSE;
    }

  InitializeProcThreadAttributeList (NULL, 1, 0, &attribute_size);
  attributes = g_malloc0 (attribute_size);
  InitializeProcThreadAttributeList (attributes, 1, 0, &attribute_size);
  /* The attribute value is the HPCON itself, passed by value as lpValue. */
  UpdateProcThreadAttribute (attributes, 0, PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE, io->hpc,
                             sizeof (GhosttyHPCON), NULL, NULL);
  memset (&startup, 0, sizeof startup);
  startup.StartupInfo.cb = sizeof startup;
  /* Without STARTF_USESTDHANDLES a child of a process whose own stdio is
   * redirected inherits those handles instead of the pseudo console's. */
  startup.StartupInfo.dwFlags = STARTF_USESTDHANDLES;
  startup.lpAttributeList = attributes;
  memset (&process, 0, sizeof process);

  command = windows_command_line (argv);
  wide_command = g_utf8_to_utf16 (command, -1, NULL, NULL, NULL);
  wide_cwd = cwd != NULL ? g_utf8_to_utf16 (cwd, -1, NULL, NULL, NULL) : NULL;
  wide_env = envp != NULL ? environment_block (envp) : NULL;
  ok = wide_command != NULL
       && CreateProcessW (NULL, (wchar_t *) wide_command, NULL, NULL, FALSE,
                          EXTENDED_STARTUPINFO_PRESENT | CREATE_UNICODE_ENVIRONMENT, wide_env,
                          (const wchar_t *) wide_cwd, &startup.StartupInfo, &process);
  DeleteProcThreadAttributeList (attributes);
  g_free (attributes);
  g_free (command);
  g_free (wide_command);
  g_free (wide_cwd);
  g_free (wide_env);
  /* The console holds its own references to these ends now. */
  io_close_handle (&input_read);
  io_close_handle (&output_write);
  if (!ok)
    {
      DWORD code = GetLastError ();
      g_set_error (error, G_IO_ERROR, G_IO_ERROR_FAILED, "Could not start %s (error %lu)", argv[0], code);
      /* Nothing reads the output yet; the console's first frame fits in the
       * pipe buffer, so closing it here does not block. */
      io->api.close (io->hpc);
      io->hpc = NULL;
      io_unref (io);
      return FALSE;
    }

  CloseHandle (process.hThread);
  io->process = process.hProcess;
  self->pid = (int) process.dwProcessId;
  self->io = io;
  g_thread_unref (g_thread_new ("gtkghostty-conpty-read", reader_thread, io_ref (io)));
  g_thread_unref (g_thread_new ("gtkghostty-conpty-exit", exit_thread, io_ref (io)));
  return TRUE;
}

#endif /* G_OS_WIN32 */

/* ============================================================ public API */

/**
 * gtk_ghostty_pty_spawn:
 * @argv: (array zero-terminated=1): the program and its arguments; on POSIX
 *   argv[0] is looked up on PATH, on Windows as CreateProcess does
 * @envp: (array zero-terminated=1) (nullable): the environment, or %NULL to
 *   inherit this process's
 * @cwd: (type filename) (nullable): working directory, or %NULL
 * @columns: initial columns
 * @rows: initial rows
 * @error: return location for an error
 *
 * Starts @argv on a new pseudo terminal. Connect #GtkGhosttyPty::output and
 * #GtkGhosttyPty::exited, or call gtk_ghostty_pty_attach().
 *
 * Returns: (transfer full): the running program, or %NULL with @error set
 */
GtkGhosttyPty *
gtk_ghostty_pty_spawn (const char * const *argv, const char * const *envp, const char *cwd,
                       guint columns, guint rows, GError **error)
{
  GtkGhosttyPty *self;
  gboolean ok;

  g_return_val_if_fail (argv != NULL && argv[0] != NULL, NULL);
  g_return_val_if_fail (error == NULL || *error == NULL, NULL);

  self = g_object_new (GTK_GHOSTTY_TYPE_PTY, NULL);
#ifdef G_OS_WIN32
  ok = win_spawn_pty (self, argv, envp, cwd, MAX (1, columns), MAX (1, rows), error);
#else
  ok = posix_spawn_pty (self, argv, envp, cwd, MAX (1, columns), MAX (1, rows), error);
#endif
  if (!ok)
    {
      self->exit_seen = TRUE;  /* nothing was started: dispose has nothing to end */
      self->exited_emitted = TRUE;
      g_object_unref (self);
      return NULL;
    }
  return self;
}

/**
 * gtk_ghostty_pty_write_data:
 * @self: the pty
 * @data: (array length=length): bytes for the program
 * @length: number of bytes
 */
void
gtk_ghostty_pty_write_data (GtkGhosttyPty *self, const guint8 *data, gsize length)
{
  g_return_if_fail (GTK_GHOSTTY_IS_PTY (self));
  if (length == 0 || self->exited_emitted)
    return;
#ifdef G_OS_WIN32
  if (self->io != NULL)
  {
    Io *io = self->io;
    g_mutex_lock (&io->write_lock);
    while (io->input_write != NULL && length > 0)
      {
        DWORD written = 0;
        if (!WriteFile (io->input_write, data, (DWORD) MIN (length, G_MAXINT32), &written, NULL))
          break;
        data += written;
        length -= written;
      }
    g_mutex_unlock (&io->write_lock);
  }
#else
  g_byte_array_append (self->pending, data, (guint) length);
  if (self->write_source == 0)
    posix_flush (self);
#endif
}

/**
 * gtk_ghostty_pty_write:
 * @self: the pty
 * @data: bytes for the program
 */
void
gtk_ghostty_pty_write (GtkGhosttyPty *self, GBytes *data)
{
  gsize length = 0;
  const guint8 *bytes;
  g_return_if_fail (data != NULL);
  bytes = g_bytes_get_data (data, &length);
  gtk_ghostty_pty_write_data (self, bytes, length);
}

/**
 * gtk_ghostty_pty_resize:
 * @self: the pty
 * @columns: columns
 * @rows: rows
 *
 * Tells the program the terminal's new size (SIGWINCH on POSIX, a
 * ConPTY resize on Windows). gtk_ghostty_pty_attach() does this for you.
 */
void
gtk_ghostty_pty_resize (GtkGhosttyPty *self, guint columns, guint rows)
{
  g_return_if_fail (GTK_GHOSTTY_IS_PTY (self));
#ifdef G_OS_WIN32
  if (self->io == NULL)
    return;
  g_mutex_lock (&self->io->lock);
  if (self->io->hpc != NULL)
    self->io->api.resize (self->io->hpc, make_coord (columns, rows));
  g_mutex_unlock (&self->io->lock);
#else
  if (self->master >= 0)
    posix_set_size (self->master, columns, rows);
#endif
}

/**
 * gtk_ghostty_pty_terminate:
 * @self: the pty
 *
 * Ends the program (SIGHUP to its process group on POSIX, as when a
 * terminal window closes; TerminateProcess and closing the console on
 * Windows). #GtkGhosttyPty::exited follows.
 */
void
gtk_ghostty_pty_terminate (GtkGhosttyPty *self)
{
  g_return_if_fail (GTK_GHOSTTY_IS_PTY (self));
  if (self->exit_seen)
    return;
#ifdef G_OS_WIN32
  if (self->io != NULL)
    terminate_if_alive (self->io);
#else
  if (self->pid > 0)
    kill (-(pid_t) self->pid, SIGHUP);
#endif
}

/**
 * gtk_ghostty_pty_get_pid:
 * @self: the pty
 *
 * Returns: the program's process id
 */
int
gtk_ghostty_pty_get_pid (GtkGhosttyPty *self)
{
  g_return_val_if_fail (GTK_GHOSTTY_IS_PTY (self), 0);
  return self->pid;
}

/**
 * gtk_ghostty_pty_is_running:
 * @self: the pty
 *
 * Returns: %FALSE once #GtkGhosttyPty::exited was emitted
 */
gboolean
gtk_ghostty_pty_is_running (GtkGhosttyPty *self)
{
  g_return_val_if_fail (GTK_GHOSTTY_IS_PTY (self), FALSE);
  return !self->exited_emitted;
}

static void
on_terminal_bytes (GtkGhosttyTerminal *terminal, GBytes *data, GtkGhosttyPty *self)
{
  gtk_ghostty_pty_write (self, data);
}

static void
on_terminal_size (GtkGhosttyTerminal *terminal, guint columns, guint rows, guint cell_width,
                  guint cell_height, GtkGhosttyPty *self)
{
  gtk_ghostty_pty_resize (self, columns, rows);
}

static void
on_pty_output (GtkGhosttyPty *self, GBytes *data, GtkGhosttyTerminal *terminal)
{
  gtk_ghostty_terminal_feed (terminal, data);
}

/**
 * gtk_ghostty_pty_attach:
 * @self: the pty
 * @terminal: the terminal to show it in
 *
 * Feeds the program's output to @terminal and writes the terminal's input
 * and replies back; resizes follow the terminal. On Windows the terminal is
 * also set to the legacy keyboard and to keep its history on resize, as
 * ConPTY needs. The connections end when either object is finalized.
 */
void
gtk_ghostty_pty_attach (GtkGhosttyPty *self, GtkGhosttyTerminal *terminal)
{
  g_return_if_fail (GTK_GHOSTTY_IS_PTY (self));
  g_return_if_fail (GTK_GHOSTTY_IS_TERMINAL (terminal));
#ifdef G_OS_WIN32
  gtk_ghostty_terminal_set_legacy_keyboard (terminal, TRUE);
  gtk_ghostty_terminal_set_pull_history_on_grow (terminal, FALSE);
#endif
  g_signal_connect_object (terminal, "input", G_CALLBACK (on_terminal_bytes), self, 0);
  g_signal_connect_object (terminal, "reply", G_CALLBACK (on_terminal_bytes), self, 0);
  g_signal_connect_object (terminal, "size-changed", G_CALLBACK (on_terminal_size), self, 0);
  g_signal_connect_object (self, "output", G_CALLBACK (on_pty_output), terminal, 0);
  gtk_ghostty_pty_resize (self, gtk_ghostty_terminal_get_columns (terminal),
                          gtk_ghostty_terminal_get_rows (terminal));
}

/* ------------------------------------------------------------- GObject */

static void
gtk_ghostty_pty_dispose (GObject *object)
{
  GtkGhosttyPty *self = GTK_GHOSTTY_PTY (object);
  /* Dropping the last reference to a running program ends it. */
  if (!self->exit_seen)
    gtk_ghostty_pty_terminate (self);
#ifdef G_OS_WIN32
  if (self->io != NULL)
    {
      g_weak_ref_set (&self->io->object, NULL);
      io_unref (self->io);
      self->io = NULL;
    }
#else
  if (self->child_source != 0)
    {
      g_clear_handle_id (&self->child_source, g_source_remove);
      g_child_watch_add ((GPid) self->pid, reap_only, NULL);
    }
  posix_close_master (self);
#endif
  G_OBJECT_CLASS (gtk_ghostty_pty_parent_class)->dispose (object);
}

static void
gtk_ghostty_pty_finalize (GObject *object)
{
#ifndef G_OS_WIN32
  GtkGhosttyPty *self = GTK_GHOSTTY_PTY (object);
  g_byte_array_unref (self->pending);
#endif
  G_OBJECT_CLASS (gtk_ghostty_pty_parent_class)->finalize (object);
}

static void
gtk_ghostty_pty_class_init (GtkGhosttyPtyClass *klass)
{
  GObjectClass *object_class = G_OBJECT_CLASS (klass);
  object_class->dispose = gtk_ghostty_pty_dispose;
  object_class->finalize = gtk_ghostty_pty_finalize;

  /**
   * GtkGhosttyPty::output:
   * @self: the pty
   * @data: what the program wrote
   *
   * Output from the program, for gtk_ghostty_terminal_feed().
   * gtk_ghostty_pty_attach() connects it for you.
   */
  signals[SIGNAL_OUTPUT] = g_signal_new ("output", G_TYPE_FROM_CLASS (klass), G_SIGNAL_RUN_LAST,
                                         0, NULL, NULL, NULL, G_TYPE_NONE, 1, G_TYPE_BYTES);
  /**
   * GtkGhosttyPty::exited:
   * @self: the pty
   * @status: exit code (128 + signal number when killed by a signal on POSIX)
   *
   * Emitted once, after the last #GtkGhosttyPty::output.
   */
  signals[SIGNAL_EXITED] = g_signal_new ("exited", G_TYPE_FROM_CLASS (klass), G_SIGNAL_RUN_LAST,
                                         0, NULL, NULL, NULL, G_TYPE_NONE, 1, G_TYPE_INT);
}

static void
gtk_ghostty_pty_init (GtkGhosttyPty *self)
{
#ifndef G_OS_WIN32
  self->master = -1;
  self->pending = g_byte_array_new ();
#endif
}
