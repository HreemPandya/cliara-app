"""
Cross-platform command translation for Cliara.

Detects when a command fails because it doesn't exist on the current platform
and offers the equivalent command for the user's OS and shell.

Covers the most common Unix <-> Windows translations:
  Unix -> PowerShell (Select-String, Get-ChildItem, etc.)
  Unix -> CMD        (findstr, dir, type, etc.)
  Windows -> Unix    (CMD & PowerShell cmdlets -> bash/zsh equivalents)
"""

import re
import shlex
import platform
from shutil import which
from typing import Optional, Tuple, List, Dict, Callable


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------

def is_powershell(shell_path: str) -> bool:
    """Return True if *shell_path* points to PowerShell or pwsh."""
    lower = (shell_path or "").lower()
    return "powershell" in lower or "pwsh" in lower


def get_base_command(command: str) -> Optional[str]:
    """
    Extract the base command name from a full command string.

    Handles pipes, ``&&`` chains, and ``VAR=val`` prefixes so that
    ``FOO=1 grep -r pattern dir | wc -l`` returns ``"grep"``.
    """
    command = command.strip()
    if not command:
        return None

    # Take only the first command in a pipeline / chain
    first_cmd = re.split(r"\||\&\&|;", command)[0].strip()

    parts = first_cmd.split()
    for part in parts:
        # Skip environment-variable assignments like VAR=value
        if "=" in part and not part.startswith("-"):
            continue
        return part

    return None


def command_exists(cmd_name: str) -> bool:
    """Return True if *cmd_name* is found on the system PATH."""
    return which(cmd_name) is not None


# ---------------------------------------------------------------------------
# Argument parsing helper
# ---------------------------------------------------------------------------

def _parse_args(command: str) -> Tuple[str, List[str]]:
    """Split *command* into ``(base_command, [args...])``."""
    try:
        parts = shlex.split(command, posix=(platform.system() != "Windows"))
    except ValueError:
        parts = command.split()
    if not parts:
        return "", []
    return parts[0], parts[1:]


# ===================================================================
# UNIX -> PowerShell translators
# ===================================================================

def _grep_to_ps(cmd: str) -> str:
    _, args = _parse_args(cmd)
    recursive = False
    invert = False
    count_only = False
    files_only = False
    positional: List[str] = []

    i = 0
    while i < len(args):
        a = args[i]
        if a.startswith("--"):
            flag = a.lstrip("-")
            if flag in ("recursive",):
                recursive = True
            elif flag == "invert-match":
                invert = True
            elif flag == "count":
                count_only = True
            elif flag == "files-with-matches":
                files_only = True
        elif a.startswith("-") and not a.startswith("--"):
            for ch in a[1:]:
                if ch in "rR":
                    recursive = True
                elif ch == "i":
                    pass  # Select-String is case-insensitive by default
                elif ch == "n":
                    pass  # line numbers shown by default
                elif ch == "v":
                    invert = True
                elif ch == "c":
                    count_only = True
                elif ch == "l":
                    files_only = True
                elif ch == "e":
                    i += 1
                    if i < len(args):
                        positional.insert(0, args[i])
        else:
            positional.append(a)
        i += 1

    pattern = positional[0] if positional else ""
    paths = positional[1:] if len(positional) > 1 else []

    parts = ["Select-String"]
    parts.append(f'-Pattern "{pattern}"')

    if recursive:
        adjusted = []
        for p in (paths or ["."]):
            p = p.rstrip("/").rstrip("\\")
            adjusted.append(f"{p}\\*")
        path_str = ", ".join(f'"{p}"' for p in adjusted)
        parts.append(f"-Path {path_str}")
        parts.append("-Recurse")
    elif paths:
        path_str = ", ".join(f'"{p}"' for p in paths)
        parts.append(f"-Path {path_str}")

    if invert:
        parts.append("-NotMatch")

    result = " ".join(parts)
    if count_only:
        result = f"({result} | Measure-Object).Count"
    elif files_only:
        result = f"{result} | Select-Object -Unique Path"
    return result


def _find_to_ps(cmd: str) -> str:
    _, args = _parse_args(cmd)
    directory = "."
    name_filter = None
    file_type = None
    maxdepth = None

    i = 0
    while i < len(args):
        a = args[i]
        if a in ("-name", "-iname") and i + 1 < len(args):
            name_filter = args[i + 1]
            i += 2
            continue
        elif a == "-type" and i + 1 < len(args):
            file_type = args[i + 1]
            i += 2
            continue
        elif a == "-maxdepth" and i + 1 < len(args):
            maxdepth = args[i + 1]
            i += 2
            continue
        elif not a.startswith("-"):
            directory = a
        i += 1

    parts = ["Get-ChildItem", f'-Path "{directory}"', "-Recurse"]
    if name_filter:
        parts.append(f'-Filter "{name_filter}"')
    if file_type == "f":
        parts.append("-File")
    elif file_type == "d":
        parts.append("-Directory")
    if maxdepth:
        parts.append(f"-Depth {maxdepth}")
    return " ".join(parts)


def _head_to_ps(cmd: str) -> str:
    _, args = _parse_args(cmd)
    lines = "10"
    files: List[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "-n" and i + 1 < len(args):
            lines = args[i + 1]
            i += 2
            continue
        elif a.startswith("-") and a[1:].isdigit():
            lines = a[1:]
        elif not a.startswith("-"):
            files.append(a)
        i += 1
    if files:
        return f'Get-Content "{files[0]}" -Head {lines}'
    return f"Get-Content <file> -Head {lines}"


def _tail_to_ps(cmd: str) -> str:
    _, args = _parse_args(cmd)
    lines = "10"
    files: List[str] = []
    follow = False
    i = 0
    while i < len(args):
        a = args[i]
        if a == "-n" and i + 1 < len(args):
            lines = args[i + 1]
            i += 2
            continue
        elif a.startswith("-") and len(a) > 1 and a[1:].isdigit():
            lines = a[1:]
        elif a in ("-f", "--follow"):
            follow = True
        elif not a.startswith("-"):
            files.append(a)
        i += 1
    if files:
        result = f'Get-Content "{files[0]}" -Tail {lines}'
    else:
        result = f"Get-Content <file> -Tail {lines}"
    if follow:
        result += " -Wait"
    return result


def _touch_to_ps(cmd: str) -> str:
    _, args = _parse_args(cmd)
    files = [a for a in args if not a.startswith("-")]
    if not files:
        return 'New-Item -ItemType File -Name "<filename>"'
    if len(files) == 1:
        f = files[0]
        return (
            f'if (Test-Path "{f}") '
            f'{{ (Get-Item "{f}").LastWriteTime = Get-Date }} '
            f'else {{ New-Item -ItemType File -Path "{f}" }}'
        )
    return "; ".join(f'New-Item -ItemType File -Path "{f}" -Force' for f in files)


def _wc_to_ps(cmd: str) -> str:
    _, args = _parse_args(cmd)
    count_lines = count_words = count_chars = False
    files: List[str] = []
    for a in args:
        if a.startswith("-"):
            for ch in a[1:]:
                if ch == "l":
                    count_lines = True
                elif ch == "w":
                    count_words = True
                elif ch in "cm":
                    count_chars = True
        else:
            files.append(a)
    if not (count_lines or count_words or count_chars):
        count_lines = count_words = count_chars = True
    measures = []
    if count_lines:
        measures.append("-Line")
    if count_words:
        measures.append("-Word")
    if count_chars:
        measures.append("-Character")
    measure_str = " ".join(measures)
    if files:
        return f'Get-Content "{files[0]}" | Measure-Object {measure_str}'
    return f"Measure-Object {measure_str}"


def _which_to_ps(cmd: str) -> str:
    _, args = _parse_args(cmd)
    names = [a for a in args if not a.startswith("-")]
    return f"Get-Command {names[0]}" if names else "Get-Command <name>"


def _kill_to_ps(cmd: str) -> str:
    _, args = _parse_args(cmd)
    force = False
    pids: List[str] = []
    for a in args:
        if a in ("-9", "-KILL"):
            force = True
        elif a.lstrip("-").isdigit() and not a.startswith("-"):
            pids.append(a)
        elif a.isdigit():
            pids.append(a)
    if not pids:
        return "Stop-Process -Id <PID>"
    result = f"Stop-Process -Id {', '.join(pids)}"
    if force:
        result += " -Force"
    return result


def _ps_to_ps(cmd: str) -> str:
    return "Get-Process"


def _wget_to_ps(cmd: str) -> str:
    _, args = _parse_args(cmd)
    url = None
    output = None
    i = 0
    while i < len(args):
        a = args[i]
        if a == "-O" and i + 1 < len(args):
            output = args[i + 1]
            i += 2
            continue
        elif a.startswith("--output-document="):
            output = a.split("=", 1)[1]
        elif not a.startswith("-"):
            url = a
        i += 1
    if not url:
        return 'Invoke-WebRequest -Uri "<URL>" -OutFile "<file>"'
    fname = output or url.rsplit("/", 1)[-1]
    return f'Invoke-WebRequest -Uri "{url}" -OutFile "{fname}"'


def _chmod_to_ps(cmd: str) -> str:
    _, args = _parse_args(cmd)
    files = [a for a in args if not a.startswith("-") and not a.isdigit()
             and "+" not in a and not a.startswith("u") and not a.startswith("g")
             and not a.startswith("o")]
    target = f'"{files[-1]}"' if files else "<file>"
    return f"icacls {target} /grant Everyone:F"


def _ln_to_ps(cmd: str) -> str:
    _, args = _parse_args(cmd)
    symbolic = False
    targets: List[str] = []
    for a in args:
        if a in ("-s", "--symbolic"):
            symbolic = True
        elif not a.startswith("-"):
            targets.append(a)
    kind = "SymbolicLink" if symbolic else "HardLink"
    if len(targets) >= 2:
        return f'New-Item -ItemType {kind} -Path "{targets[1]}" -Target "{targets[0]}"'
    return f'New-Item -ItemType {kind} -Path "<link>" -Target "<target>"'


def _df_to_ps(_cmd: str) -> str:
    return "Get-PSDrive -PSProvider FileSystem"


def _du_to_ps(cmd: str) -> str:
    _, args = _parse_args(cmd)
    target = "."
    for a in args:
        if not a.startswith("-"):
            target = a
    return (
        f'(Get-ChildItem -Path "{target}" -Recurse '
        f"| Measure-Object -Property Length -Sum).Sum / 1MB"
    )


def _export_to_ps(cmd: str) -> str:
    _, args = _parse_args(cmd)
    results = []
    for a in args:
        if "=" in a:
            var, val = a.split("=", 1)
            results.append(f'$env:{var} = "{val}"')
    return "; ".join(results) if results else '$env:VAR = "value"'


def _env_to_ps(_cmd: str) -> str:
    return "Get-ChildItem Env:"


def _uname_to_ps(_cmd: str) -> str:
    return "[System.Environment]::OSVersion"


def _cat_to_ps(cmd: str) -> str:
    _, args = _parse_args(cmd)
    files = [a for a in args if not a.startswith("-")]
    if files:
        return "Get-Content " + " ".join(f'"{f}"' for f in files)
    return "Get-Content <file>"


def _rm_to_ps(cmd: str) -> str:
    _, args = _parse_args(cmd)
    recursive = force = False
    files: List[str] = []
    for a in args:
        if a.startswith("-") and not a.startswith("--"):
            for ch in a[1:]:
                if ch in "rR":
                    recursive = True
                elif ch == "f":
                    force = True
        elif a == "--recursive":
            recursive = True
        elif a == "--force":
            force = True
        else:
            files.append(a)
    parts = ["Remove-Item"]
    if files:
        parts.append(", ".join(f'"{f}"' for f in files))
    if recursive:
        parts.append("-Recurse")
    if force:
        parts.append("-Force")
    return " ".join(parts)


def _cp_to_ps(cmd: str) -> str:
    _, args = _parse_args(cmd)
    recursive = False
    files: List[str] = []
    for a in args:
        if a.startswith("-"):
            for ch in a[1:]:
                if ch in "rR":
                    recursive = True
        else:
            files.append(a)
    parts = ["Copy-Item"]
    if len(files) >= 2:
        parts += [f'-Path "{files[0]}"', f'-Destination "{files[1]}"']
    if recursive:
        parts.append("-Recurse")
    return " ".join(parts)


def _mv_to_ps(cmd: str) -> str:
    _, args = _parse_args(cmd)
    files = [a for a in args if not a.startswith("-")]
    if len(files) >= 2:
        return f'Move-Item -Path "{files[0]}" -Destination "{files[1]}"'
    return "Move-Item -Path <source> -Destination <dest>"


def _diff_to_ps(cmd: str) -> str:
    _, args = _parse_args(cmd)
    files = [a for a in args if not a.startswith("-")]
    if len(files) >= 2:
        return f'Compare-Object (Get-Content "{files[0]}") (Get-Content "{files[1]}")'
    return "Compare-Object (Get-Content <file1>) (Get-Content <file2>)"


def _sort_to_ps(cmd: str) -> str:
    _, args = _parse_args(cmd)
    reverse = unique = False
    files: List[str] = []
    for a in args:
        if a in ("-r", "--reverse"):
            reverse = True
        elif a in ("-u", "--unique"):
            unique = True
        elif not a.startswith("-"):
            files.append(a)
    if files:
        base = f'Get-Content "{files[0]}" | Sort-Object'
    else:
        base = "Sort-Object"
    if reverse:
        base += " -Descending"
    if unique:
        base += " -Unique"
    return base


def _sed_to_ps(cmd: str) -> str:
    _, args = _parse_args(cmd)
    in_place = False
    expression = None
    files: List[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "-i":
            in_place = True
        elif a == "-e" and i + 1 < len(args):
            expression = args[i + 1]
            i += 1
        elif re.match(r"^s[/|]", a):
            expression = a
        elif not a.startswith("-"):
            files.append(a)
        i += 1
    if expression:
        m = re.match(r"s[/|](.+?)[/|](.+?)[/|]?([gi]*)", expression)
        if m:
            old, new = m.group(1), m.group(2)
            if files and in_place:
                return (
                    f'(Get-Content "{files[0]}") '
                    f'-replace "{old}", "{new}" '
                    f'| Set-Content "{files[0]}"'
                )
            elif files:
                return f'(Get-Content "{files[0]}") -replace "{old}", "{new}"'
            return f'-replace "{old}", "{new}"'
    return '(Get-Content <file>) -replace "<old>", "<new>"'


def _ls_to_ps(cmd: str) -> str:
    _, args = _parse_args(cmd)
    show_all = recursive = False
    dirs: List[str] = []
    for a in args:
        if a.startswith("-") and not a.startswith("--"):
            for ch in a[1:]:
                if ch == "a":
                    show_all = True
                elif ch == "l":
                    pass  # default in PowerShell
                elif ch == "R":
                    recursive = True
        elif a == "--all":
            show_all = True
        elif a == "--recursive":
            recursive = True
        elif not a.startswith("-"):
            dirs.append(a)
    parts = ["Get-ChildItem"]
    if dirs:
        parts.extend(f'"{d}"' for d in dirs)
    if show_all:
        parts.append("-Force")
    if recursive:
        parts.append("-Recurse")
    return " ".join(parts)


# ===================================================================
# UNIX -> CMD translators
# ===================================================================

def _grep_to_cmd(cmd: str) -> str:
    _, args = _parse_args(cmd)
    recursive = ignore_case = False
    positional: List[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a.startswith("-"):
            for ch in a[1:]:
                if ch in "rR":
                    recursive = True
                elif ch == "i":
                    ignore_case = True
                elif ch == "e":
                    i += 1
                    if i < len(args):
                        positional.insert(0, args[i])
        else:
            positional.append(a)
        i += 1
    pattern = positional[0] if positional else ""
    files = positional[1:] if len(positional) > 1 else ["*"]
    parts = ["findstr"]
    if recursive:
        parts.append("/S")
    if ignore_case:
        parts.append("/I")
    parts.append(f'"{pattern}"')
    parts.extend(files)
    return " ".join(parts)


def _ls_to_cmd(cmd: str) -> str:
    _, args = _parse_args(cmd)
    recursive = show_all = False
    dirs: List[str] = []
    for a in args:
        if a.startswith("-"):
            for ch in a[1:]:
                if ch == "a":
                    show_all = True
                elif ch == "R":
                    recursive = True
        elif not a.startswith("-"):
            dirs.append(a)
    parts = ["dir"]
    if dirs:
        parts.extend(dirs)
    if show_all:
        parts.append("/A")
    if recursive:
        parts.append("/S")
    return " ".join(parts)


def _cat_to_cmd(cmd: str) -> str:
    _, args = _parse_args(cmd)
    files = [a for a in args if not a.startswith("-")]
    return ("type " + " ".join(files)) if files else "type <file>"


def _rm_to_cmd(cmd: str) -> str:
    _, args = _parse_args(cmd)
    recursive = force = False
    files: List[str] = []
    for a in args:
        if a.startswith("-"):
            for ch in a[1:]:
                if ch in "rR":
                    recursive = True
                elif ch == "f":
                    force = True
        else:
            files.append(a)
    target = " ".join(files) if files else "<path>"
    if recursive:
        parts = ["rmdir", "/S"]
        if force:
            parts.append("/Q")
        parts.append(target)
        return " ".join(parts)
    parts = ["del"]
    if force:
        parts.append("/F")
    parts.append(target)
    return " ".join(parts)


def _cp_to_cmd(cmd: str) -> str:
    _, args = _parse_args(cmd)
    recursive = False
    files: List[str] = []
    for a in args:
        if a.startswith("-"):
            for ch in a[1:]:
                if ch in "rR":
                    recursive = True
        else:
            files.append(a)
    if len(files) >= 2:
        if recursive:
            return f'xcopy /E /I "{files[0]}" "{files[1]}"'
        return f'copy "{files[0]}" "{files[1]}"'
    return "xcopy /E /I <source> <dest>" if recursive else "copy <source> <dest>"


def _mv_to_cmd(cmd: str) -> str:
    _, args = _parse_args(cmd)
    files = [a for a in args if not a.startswith("-")]
    if len(files) >= 2:
        return f'move "{files[0]}" "{files[1]}"'
    return "move <source> <dest>"


def _touch_to_cmd(cmd: str) -> str:
    _, args = _parse_args(cmd)
    files = [a for a in args if not a.startswith("-")]
    if files:
        return f'type nul > "{files[0]}"'
    return "type nul > <filename>"


def _head_to_cmd(cmd: str) -> str:
    _, args = _parse_args(cmd)
    lines = "10"
    files: List[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "-n" and i + 1 < len(args):
            lines = args[i + 1]
            i += 2
            continue
        elif a.startswith("-") and a[1:].isdigit():
            lines = a[1:]
        elif not a.startswith("-"):
            files.append(a)
        i += 1
    target = f'"{files[0]}"' if files else "<file>"
    return f"powershell -Command \"Get-Content {target} -Head {lines}\""


def _tail_to_cmd(cmd: str) -> str:
    _, args = _parse_args(cmd)
    lines = "10"
    files: List[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "-n" and i + 1 < len(args):
            lines = args[i + 1]
            i += 2
            continue
        elif a.startswith("-") and a[1:].isdigit():
            lines = a[1:]
        elif not a.startswith("-"):
            files.append(a)
        i += 1
    target = f'"{files[0]}"' if files else "<file>"
    return f"powershell -Command \"Get-Content {target} -Tail {lines}\""


def _which_to_cmd(cmd: str) -> str:
    _, args = _parse_args(cmd)
    names = [a for a in args if not a.startswith("-")]
    return f"where {names[0]}" if names else "where <command>"


def _kill_to_cmd(cmd: str) -> str:
    _, args = _parse_args(cmd)
    force = False
    pids: List[str] = []
    for a in args:
        if a in ("-9", "-KILL"):
            force = True
        elif a.isdigit():
            pids.append(a)
    if pids:
        parts = ["taskkill", f"/PID {pids[0]}"]
        if force:
            parts.append("/F")
        return " ".join(parts)
    return "taskkill /PID <pid>"


def _ps_to_cmd(_cmd: str) -> str:
    return "tasklist"


def _wget_to_cmd(cmd: str) -> str:
    _, args = _parse_args(cmd)
    url = output = None
    i = 0
    while i < len(args):
        a = args[i]
        if a == "-O" and i + 1 < len(args):
            output = args[i + 1]
            i += 2
            continue
        elif not a.startswith("-"):
            url = a
        i += 1
    if url:
        fname = output or url.rsplit("/", 1)[-1]
        return f'powershell -Command "Invoke-WebRequest -Uri \\"{url}\\" -OutFile \\"{fname}\\""'
    return 'powershell -Command "Invoke-WebRequest -Uri <URL> -OutFile <file>"'


# ===================================================================
# Windows -> Unix translators  (CMD + PowerShell cmdlets)
# ===================================================================

def _dir_to_unix(cmd: str) -> str:
    _, args = _parse_args(cmd)
    flags: List[str] = []
    dirs: List[str] = []
    for a in args:
        upper = a.upper()
        if upper == "/A":
            flags.append("-a")
        elif upper == "/S":
            flags.append("-R")
        elif upper == "/B":
            flags.append("-1")
        elif not a.startswith("/"):
            dirs.append(a)
    parts = ["ls", "-la"] + flags + dirs
    return " ".join(parts)


def _type_to_unix(cmd: str) -> str:
    _, args = _parse_args(cmd)
    files = [a for a in args if not a.startswith("/")]
    return ("cat " + " ".join(files)) if files else "cat <file>"


def _del_to_unix(cmd: str) -> str:
    _, args = _parse_args(cmd)
    force = False
    files: List[str] = []
    for a in args:
        upper = a.upper()
        if upper in ("/F", "/Q"):
            force = True
        elif not a.startswith("/"):
            files.append(a)
    parts = ["rm"]
    if force:
        parts.append("-f")
    parts.extend(files)
    return " ".join(parts)


def _copy_to_unix(cmd: str) -> str:
    _, args = _parse_args(cmd)
    files = [a for a in args if not a.startswith("/")]
    if len(files) >= 2:
        return f"cp {files[0]} {files[1]}"
    return "cp <source> <dest>"


def _move_to_unix(cmd: str) -> str:
    _, args = _parse_args(cmd)
    files = [a for a in args if not a.startswith("/")]
    if len(files) >= 2:
        return f"mv {files[0]} {files[1]}"
    return "mv <source> <dest>"


def _xcopy_to_unix(cmd: str) -> str:
    _, args = _parse_args(cmd)
    files = [a for a in args if not a.startswith("/")]
    if len(files) >= 2:
        return f"cp -r {files[0]} {files[1]}"
    return "cp -r <source> <dest>"


def _findstr_to_unix(cmd: str) -> str:
    _, args = _parse_args(cmd)
    recursive = ignore_case = False
    pattern = None
    files: List[str] = []
    for a in args:
        upper = a.upper()
        if upper == "/S":
            recursive = True
        elif upper == "/I":
            ignore_case = True
        elif pattern is None and not a.startswith("/"):
            pattern = a
        elif not a.startswith("/"):
            files.append(a)
    parts = ["grep"]
    if recursive:
        parts.append("-r")
    if ignore_case:
        parts.append("-i")
    if pattern:
        parts.append(f'"{pattern}"')
    parts.extend(files)
    return " ".join(parts)


def _tasklist_to_unix(_cmd: str) -> str:
    return "ps aux"


def _taskkill_to_unix(cmd: str) -> str:
    _, args = _parse_args(cmd)
    force = False
    pid = None
    i = 0
    while i < len(args):
        upper = args[i].upper()
        if upper == "/F":
            force = True
        elif upper == "/PID" and i + 1 < len(args):
            pid = args[i + 1]
            i += 1
        i += 1
    if pid:
        return f"kill -9 {pid}" if force else f"kill {pid}"
    return "kill <pid>"


def _ipconfig_to_unix(_cmd: str) -> str:
    return "ip addr"


def _systeminfo_to_unix(_cmd: str) -> str:
    return "uname -a"


def _cls_to_unix(_cmd: str) -> str:
    return "clear"


def _robocopy_to_unix(cmd: str) -> str:
    _, args = _parse_args(cmd)
    files = [a for a in args if not a.startswith("/")]
    if len(files) >= 2:
        return f"rsync -av {files[0]}/ {files[1]}/"
    return "rsync -av <source>/ <dest>/"


# PowerShell cmdlets -> Unix
def _selectstring_to_unix(cmd: str) -> str:
    _, args = _parse_args(cmd)
    pattern = path = None
    recursive = invert = case_sensitive = False
    i = 0
    while i < len(args):
        a = args[i]
        if a == "-Pattern" and i + 1 < len(args):
            pattern = args[i + 1]; i += 1
        elif a == "-Path" and i + 1 < len(args):
            path = args[i + 1]; i += 1
        elif a == "-Recurse":
            recursive = True
        elif a == "-NotMatch":
            invert = True
        elif a == "-CaseSensitive":
            case_sensitive = True
        i += 1
    parts = ["grep"]
    if recursive:
        parts.append("-r")
    if invert:
        parts.append("-v")
    if not case_sensitive:
        parts.append("-i")
    if pattern:
        parts.append(f'"{pattern}"')
    if path:
        parts.append(path)
    return " ".join(parts)


def _getchilditem_to_unix(cmd: str) -> str:
    _, args = _parse_args(cmd)
    force = recursive = False
    path = None
    i = 0
    while i < len(args):
        a = args[i]
        if a == "-Force":
            force = True
        elif a == "-Recurse":
            recursive = True
        elif a == "-Path" and i + 1 < len(args):
            path = args[i + 1]; i += 1
        elif not a.startswith("-"):
            path = a
        i += 1
    parts = ["ls", "-la"]
    if force:
        parts.append("-a")
    if recursive:
        parts.append("-R")
    if path:
        parts.append(path)
    return " ".join(parts)


def _getcontent_to_unix(cmd: str) -> str:
    _, args = _parse_args(cmd)
    head_n = tail_n = path = None
    i = 0
    while i < len(args):
        a = args[i]
        if a == "-Head" and i + 1 < len(args):
            head_n = args[i + 1]; i += 1
        elif a == "-Tail" and i + 1 < len(args):
            tail_n = args[i + 1]; i += 1
        elif a == "-Path" and i + 1 < len(args):
            path = args[i + 1]; i += 1
        elif not a.startswith("-"):
            path = a
        i += 1
    if head_n and path:
        return f"head -n {head_n} {path}"
    if tail_n and path:
        return f"tail -n {tail_n} {path}"
    return f"cat {path}" if path else "cat <file>"


def _removeitem_to_unix(cmd: str) -> str:
    _, args = _parse_args(cmd)
    recursive = force = False
    path = None
    i = 0
    while i < len(args):
        a = args[i]
        if a == "-Recurse":
            recursive = True
        elif a == "-Force":
            force = True
        elif a == "-Path" and i + 1 < len(args):
            path = args[i + 1]; i += 1
        elif not a.startswith("-"):
            path = a
        i += 1
    parts = ["rm"]
    if recursive:
        parts.append("-r")
    if force:
        parts.append("-f")
    if path:
        parts.append(path)
    return " ".join(parts)


def _invokewebrequest_to_unix(cmd: str) -> str:
    _, args = _parse_args(cmd)
    uri = outfile = None
    i = 0
    while i < len(args):
        a = args[i]
        if a == "-Uri" and i + 1 < len(args):
            uri = args[i + 1]; i += 1
        elif a == "-OutFile" and i + 1 < len(args):
            outfile = args[i + 1]; i += 1
        i += 1
    if uri and outfile:
        return f'curl -o "{outfile}" "{uri}"'
    if uri:
        return f'curl "{uri}"'
    return "curl <url>"


def _getprocess_to_unix(_cmd: str) -> str:
    return "ps aux"


def _stopprocess_to_unix(cmd: str) -> str:
    _, args = _parse_args(cmd)
    pid = None
    force = False
    i = 0
    while i < len(args):
        a = args[i]
        if a == "-Id" and i + 1 < len(args):
            pid = args[i + 1]; i += 1
        elif a == "-Force":
            force = True
        i += 1
    if pid:
        return f"kill -9 {pid}" if force else f"kill {pid}"
    return "kill <pid>"


def _getcommand_to_unix(cmd: str) -> str:
    _, args = _parse_args(cmd)
    names = [a for a in args if not a.startswith("-")]
    return f"which {names[0]}" if names else "which <command>"


# ===================================================================
# Translation lookup tables
# ===================================================================

UNIX_TO_POWERSHELL: Dict[str, Callable[[str], str]] = {
    "grep":   _grep_to_ps,
    "egrep":  _grep_to_ps,
    "fgrep":  _grep_to_ps,
    "find":   _find_to_ps,
    "head":   _head_to_ps,
    "tail":   _tail_to_ps,
    "touch":  _touch_to_ps,
    "wc":     _wc_to_ps,
    "which":  _which_to_ps,
    "kill":   _kill_to_ps,
    "ps":     _ps_to_ps,
    "wget":   _wget_to_ps,
    "chmod":  _chmod_to_ps,
    "ln":     _ln_to_ps,
    "df":     _df_to_ps,
    "du":     _du_to_ps,
    "export": _export_to_ps,
    "env":    _env_to_ps,
    "uname":  _uname_to_ps,
    "cat":    _cat_to_ps,
    "rm":     _rm_to_ps,
    "cp":     _cp_to_ps,
    "mv":     _mv_to_ps,
    "diff":   _diff_to_ps,
    "sort":   _sort_to_ps,
    "sed":    _sed_to_ps,
    "ls":     _ls_to_ps,
}

UNIX_TO_CMD: Dict[str, Callable[[str], str]] = {
    "grep":  _grep_to_cmd,
    "egrep": _grep_to_cmd,
    "fgrep": _grep_to_cmd,
    "ls":    _ls_to_cmd,
    "cat":   _cat_to_cmd,
    "rm":    _rm_to_cmd,
    "cp":    _cp_to_cmd,
    "mv":    _mv_to_cmd,
    "touch": _touch_to_cmd,
    "head":  _head_to_cmd,
    "tail":  _tail_to_cmd,
    "which": _which_to_cmd,
    "kill":  _kill_to_cmd,
    "ps":    _ps_to_cmd,
    "wget":  _wget_to_cmd,
}

WINDOWS_TO_UNIX: Dict[str, Callable[[str], str]] = {
    # CMD built-ins
    "dir":        _dir_to_unix,
    "type":       _type_to_unix,
    "del":        _del_to_unix,
    "copy":       _copy_to_unix,
    "move":       _move_to_unix,
    "xcopy":      _xcopy_to_unix,
    "findstr":    _findstr_to_unix,
    "tasklist":   _tasklist_to_unix,
    "taskkill":   _taskkill_to_unix,
    "ipconfig":   _ipconfig_to_unix,
    "systeminfo": _systeminfo_to_unix,
    "cls":        _cls_to_unix,
    "robocopy":   _robocopy_to_unix,
    # PowerShell cmdlets (keys are lowercase for matching)
    "select-string":      _selectstring_to_unix,
    "get-childitem":      _getchilditem_to_unix,
    "get-content":        _getcontent_to_unix,
    "remove-item":        _removeitem_to_unix,
    "invoke-webrequest":  _invokewebrequest_to_unix,
    "get-process":        _getprocess_to_unix,
    "stop-process":       _stopprocess_to_unix,
    "get-command":        _getcommand_to_unix,
}


# ===================================================================
# Public API
# ===================================================================

def translate_command(command: str, target_os: str, shell: str) -> Optional[str]:
    """
    Translate *command* to the equivalent for the current OS and shell.

    Parameters
    ----------
    command : str
        The full command string that failed.
    target_os : str
        The current operating system (``platform.system()``).
    shell : str
        The user's configured shell path.

    Returns
    -------
    str or None
        The translated command, or ``None`` if no translation is available.
    """
    base_cmd = get_base_command(command)
    if not base_cmd:
        return None

    base_lower = base_cmd.lower()

    if target_os == "Windows":
        # User is on Windows — the failing command is likely a Unix command
        table = UNIX_TO_POWERSHELL if is_powershell(shell) else UNIX_TO_CMD
        translator = table.get(base_lower)
        if translator:
            return translator(command)
    else:
        # User is on Unix/macOS — the failing command is likely Windows-specific
        translator = WINDOWS_TO_UNIX.get(base_lower)
        if translator:
            return translator(command)

    return None


def translate_pipeline(command: str, target_os: str, shell: str) -> Optional[str]:
    """
    Translate a full pipeline (``cmd1 | cmd2 | …``) by translating each
    segment independently.

    Returns the translated pipeline, or ``None`` if no segment was translated.
    """
    if "|" not in command:
        return translate_command(command, target_os, shell)

    segments = [s.strip() for s in command.split("|")]
    translated = []
    any_changed = False

    for seg in segments:
        result = translate_command(seg, target_os, shell)
        if result:
            translated.append(result)
            any_changed = True
        else:
            translated.append(seg)

    return " | ".join(translated) if any_changed else None
