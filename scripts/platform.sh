# Platform differences, in one place. Sourced by every pipeline script after it has set ROOT.
#
# macOS is the original platform. On Windows the same scripts run under Git Bash (the bash that ships with
# Git for Windows); the tools themselves (Siril, GraXpert, ASTAP, Python) are native Windows programs.
#
# Sets:     PLATFORM (mac | windows), PY, SIRIL, GRAX, ASTAP, ASTAP_DB, SIRIL_CFG, GRAX_PREFS
#           SIRIL, GRAX, ASTAP and ASTAP_DB keep any value already in the environment.
# Defines:  native_path P     P as a native program must see it when it reads P from a file
#           file_size P       size of P in bytes
#           link_dir TARGET LINK   make LINK point at the folder TARGET, replacing an earlier link

case "$(uname -s)" in
  MINGW*|MSYS*) PLATFORM=windows;;
  *)            PLATFORM=mac;;
esac

if [ "$PLATFORM" = windows ]; then
  PY="$ROOT/.venv/Scripts/python.exe"
  SIRIL="${SIRIL:-/c/Program Files/Siril/bin/siril-cli.exe}"
  LOCALAPP="$(cygpath -u "${LOCALAPPDATA:-$USERPROFILE/AppData/Local}")"
  GRAX="${GRAX:-$LOCALAPP/Programs/GraXpert/GraXpert.exe}"   # the installer's per-user default
  ASTAP="${ASTAP:-$ROOT/tools/astap/cli/astap_cli.exe}"
  SIRIL_CFG="$LOCALAPP/siril/config.1.4.ini"
  GRAX_PREFS="$LOCALAPP/GraXpert/GraXpert/preferences.json"
  # Python on Windows defaults to the legacy code page for files and pipes; the scripts read and write UTF-8.
  export PYTHONUTF8=1
else
  PY="$ROOT/.venv/bin/python"
  SIRIL="${SIRIL:-/Applications/Siril.app/Contents/MacOS/siril-cli}"
  GRAX="${GRAX:-/Applications/GraXpert.app/Contents/MacOS/GraXpert}"
  ASTAP="${ASTAP:-$ROOT/tools/astap/cli/astap_cli}"
  SIRIL_CFG="$HOME/Library/Application Support/org.siril.Siril/siril/config.1.4.ini"
  GRAX_PREFS="$HOME/Library/Application Support/GraXpert/preferences.json"
fi
ASTAP_DB="${ASTAP_DB:-$ROOT/tools/astap/db}"

# Git Bash rewrites /c/... paths in a native program's arguments, but not inside files that the program
# reads. Paths written into Siril scripts therefore have to be in Windows form (C:/...) already.
native_path() {
  if [ "$PLATFORM" = windows ]; then cygpath -m "$1"; else printf '%s\n' "$1"; fi
}

# GNU stat (Git Bash) and BSD stat (macOS) take different flags.
file_size() {
  stat -c %s "$1" 2>/dev/null || stat -f %z "$1"
}

# A symlink on macOS. On Windows a junction: `ln -s` under Git Bash copies the folder unless Developer Mode
# is on, while a junction needs no privilege. rmdir removes only the junction, never the folder behind it,
# and refuses a real, non-empty folder.
link_dir() {
  local target="$1" link="$2"
  if [ "$PLATFORM" = windows ]; then
    if [ -e "$link" ] || [ -L "$link" ]; then cmd //c rmdir "$(cygpath -w "$link")" || return 1; fi
    cmd //c mklink //J "$(cygpath -w "$link")" "$(cygpath -w "$target")" >/dev/null
  else
    ln -sfn "$target" "$link"
  fi
}
