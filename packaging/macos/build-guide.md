# Bundling a Python App on macOS (Modern venv + pip workflow)

This workflow uses **Python venv** (built-in since Python 3.3) and
**pip** to create a redistributable `.app` bundle for macOS. It updates
the older virtualenv approach to modern Python packaging standards.

------------------------------------------------------------------------

## 1. Prepare the Bundle Skeleton

Create the `.app` directory structure:

    MyApp.app/
      Contents/
        Info.plist
        MacOS/
        Resources/

-   `Info.plist` → app metadata (name, version, identifier,
    executable).\
-   `Contents/MacOS/` → will hold the launcher script.\
-   `Contents/Resources/` → icons, schemas, GTK resources, etc.

------------------------------------------------------------------------

## 2. Embed Python with venv

Use `venv` to create an isolated Python inside the bundle:

``` bash
PYVER=3.12
APP=MyApp.app
INSTALLDIR=$APP/Contents

python$PYVER -m venv $INSTALLDIR
```

This provides a private Python interpreter and `pip` inside the app
bundle.

------------------------------------------------------------------------

## 3. Install Dependencies

Install your project and dependencies directly into the bundle's
environment:

``` bash
$INSTALLDIR/bin/pip install --upgrade pip wheel setuptools
$INSTALLDIR/bin/pip install pygobject pycairo
$INSTALLDIR/bin/pip install .   # if building from your project root
```

This ensures site-packages inside the bundle contain everything needed.

------------------------------------------------------------------------

## 4. Bundle Native Libraries

If your app depends on GTK or other native libraries (from Homebrew,
etc.), copy them into the bundle and fix paths.

Helper functions:

``` bash
function resolve_deps() {
  local lib=$1
  otool -L $lib | grep -e "^/usr/local/" | while read dep _; do
    echo $dep
  done
}

function fix_paths() {
  local lib=$1
  for dep in $(resolve_deps $lib); do
    install_name_tool -change $dep @executable_path/../lib/$(basename $dep) $lib
  done
}
```

Process `.so` files:

``` bash
binlibs=$(find $INSTALLDIR -type f -name '*.so')

for lib in $binlibs; do
  resolve_deps $lib
  fix_paths $lib
done | sort -u | while read lib; do
  cp $lib $INSTALLDIR/lib
  chmod u+w $INSTALLDIR/lib/$(basename $lib)
  fix_paths $INSTALLDIR/lib/$(basename $lib)
done
```

------------------------------------------------------------------------

## 5. Add Launcher Script

In `Contents/MacOS/run`:

``` bash
#!/bin/sh
SELF="$(cd "$(dirname "$0")"; pwd)"
CONTENTS="$SELF/.."
INSTALLDIR="$CONTENTS"

# Setup environment paths for GTK etc.
export DYLD_FALLBACK_LIBRARY_PATH="$CONTENTS/lib:$DYLD_FALLBACK_LIBRARY_PATH"
export GI_TYPELIB_PATH="$CONTENTS/lib/girepository-1.0:$GI_TYPELIB_PATH"
export XDG_DATA_DIRS="$CONTENTS/share:/usr/local/share:/usr/share"
export GSETTINGS_SCHEMA_DIR="$CONTENTS/share/glib-2.0/schemas"

exec "$INSTALLDIR/bin/python3" -m myapp "$@"
```

Make it executable:

``` bash
chmod +x Contents/MacOS/run
```

------------------------------------------------------------------------

## 6. Test the App

Run:

``` bash
open MyApp.app
```

Verify GTK loads, Python runs, and all modules resolve. Use `otool -L`
to troubleshoot any missing libraries.

------------------------------------------------------------------------

## 7. Build a Wheel for Distribution (optional)

Instead of installing from source, package your app first:

``` bash
python3 -m build --wheel
$INSTALLDIR/bin/pip install dist/myapp-*.whl
```

This gives a reproducible install inside the bundle.

------------------------------------------------------------------------

## 8. Codesign (optional but recommended)

Sign the app bundle so it runs on other Macs without Gatekeeper
warnings:

``` bash
codesign --deep --force --options runtime --sign "Developer ID Application: Your Name (TEAMID)" --timestamp MyApp.app
```

------------------------------------------------------------------------

## Benefits of the Modern Approach

-   **Uses built-in venv**: No need for external virtualenv.\
-   **pip + pyproject.toml**: Modern dependency management.\
-   **Compatible with wheels**: Can install from PyPI or prebuilt
    wheels.\
-   **Future-proof**: Avoids legacy `easy_install` and egg handling.

------------------------------------------------------------------------

✅ This is the recommended modern workflow for packaging Python apps
into a `.app` bundle on macOS.
