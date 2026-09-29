"""The bash wrapper every WSL mesher runs inside (Plan 35 CR5).

One builder for both engines, so OpenFOAM and Gmsh cannot drift apart in how a
run is started, reported and stopped.

What the wrapper does, in order:

1. ``setsid`` starts the mesher as the leader of a new session and process
   group. Its stdin is ``/dev/null``; only the wrapper holds the lifetime pipe.
2. It reports ``FOAMMESH_RUN <run_id> pgid=<n> sid=<n>`` and writes the Linux
   mirror of the run record, ``<run_dir>/<run_id>.linux``.
3. **The lifetime pipe.** When ``JobManager`` launched it (``FOAMMESH_RUN_ID``
   is set, and stdin is a pipe whose write end the GUI holds), a watcher in a
   session of its own blocks on that pipe. MEASURED 2026-09-29 on
   OpenFOAM13Runtime: killing ``wsl.exe`` takes the wrapper's own session with
   it but leaves a ``setsid`` child running, so the watcher has to live outside
   the wrapper's session to survive and act. On EOF -- the GUI died, or
   ``wsl.exe`` was killed -- it sends TERM to the group and the session
   (``pkill -s`` reaches ``mpirun`` ranks and ``orted``/``prted``), waits up to
   10 s, sends KILL, and records ``killed-on-transport-loss``. Without
   ``FOAMMESH_RUN_ID`` (a probe run by ``subprocess.run``, whose stdin may be
   NUL and read EOF at once) no watcher is armed: that was the r1 regression.
4. ``FOAMMESH_HB <run_id>`` goes to stderr every 2 s while the mesher lives,
   independent of the mesher's own output (CR6 reads it).
5. When the mesher exits, anything left in its group or session is killed,
   and only then does ``FOAMMESH_EXIT <run_id> rc=<n>`` go out: it is the one
   acknowledgement that no writer is left.

All control lines go to stderr, so a caller reading stdout alone sees only the
mesher.
"""
from __future__ import annotations

import shlex

#: Control-line prefixes the wrapper writes; ``JobManager`` consumes them.
RUN_PREFIX = 'FOAMMESH_RUN '
EXIT_PREFIX = 'FOAMMESH_EXIT '
HEARTBEAT_PREFIX = 'FOAMMESH_HB '
#: The environment variables ``JobManager`` passes through ``WSLENV``.
RUN_ID_VARIABLE = 'FOAMMESH_RUN_ID'
RUN_DIR_VARIABLE = 'FOAMMESH_RUN_DIR'
#: TERM grace before KILL on transport loss, seconds.
TERM_GRACE_SECONDS = 10
HEARTBEAT_SECONDS = 2

# The watcher, run as ``setsid bash -c WATCHER fm-watch <pgid> <record> <rid>``
# with the lifetime pipe as its stdin. ``cat`` returns on EOF only.
_WATCHER = r'''
cat >/dev/null
pg="$1"; rec="$2"; rid="$3"
alive() { kill -0 -- "-$pg" 2>/dev/null || pgrep -s "$pg" >/dev/null 2>&1; }
alive || exit 0
note() {
  [ -n "$rec" ] || return 0
  printf '{"run_id":"%s","pgid":%s,"sid":%s,"state":"killed-on-transport-loss","confirmed_dead":%s,"at":"%s"}\n' \
    "$rid" "$pg" "$pg" "$1" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$rec.tmp" 2>/dev/null \
    && mv -f "$rec.tmp" "$rec" 2>/dev/null
}
note false
kill -TERM -- "-$pg" 2>/dev/null; pkill -TERM -s "$pg" 2>/dev/null
i=0
while [ "$i" -lt GRACE ] && alive; do sleep 1; i=$((i+1)); done
kill -KILL -- "-$pg" 2>/dev/null; pkill -KILL -s "$pg" 2>/dev/null
sleep 1
if alive; then note false; else note true; fi
'''.replace('GRACE', str(TERM_GRACE_SECONDS))


def wrapped_script(*, prelude: str, runtime_cwd: str, run: str,
                   pid_name: str, token: str) -> str:
    """The wrapper around one mesher command line.

    *prelude* is shell that must succeed before the mesher starts (the
    OpenFOAM ``source`` clause, the Gmsh exports); *run* is the already
    quoted mesher command. *pid_name* is the pid file the cancel cleanup
    reads, relative to *runtime_cwd*.
    """
    pid = shlex.quote(pid_name)
    grouped = 'setsid bash -c ' + shlex.quote('exec stdbuf -oL -eL ' + run)
    watcher = shlex.quote(_WATCHER)
    parts = [
        f'rid="${{{RUN_ID_VARIABLE}:-{token}}}"',
        f'rdir="${{{RUN_DIR_VARIABLE}:-}}"',
        'rec=""; [ -n "$rdir" ] && mkdir -p "$rdir" 2>/dev/null && rec="$rdir/$rid.linux"',
        'child=""',
        # A record only ever says `exited` when nothing of the run is left.
        'fm_exit() { rc=$?; '
        'if [ -n "$child" ] && { kill -0 -- "-$child" 2>/dev/null '
        '|| pgrep -s "$child" >/dev/null 2>&1; }; then exit "$rc"; fi; '
        '[ -n "$rec" ] && printf \'{"run_id":"%s","pgid":%s,"sid":%s,'
        '"state":"exited","rc":%s,"at":"%s"}\\n\' "$rid" "${child:-null}" '
        '"${child:-null}" "$rc" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" '
        '> "$rec.tmp" 2>/dev/null && mv -f "$rec.tmp" "$rec" 2>/dev/null; '
        'echo "FOAMMESH_EXIT $rid rc=$rc" >&2; exit "$rc"; }',
        'trap fm_exit EXIT',
        # fd 3 keeps the lifetime pipe; stdin itself is closed to everyone else.
        'exec 3<&0 0</dev/null',
    ]
    script = '; '.join(parts) + '; '
    if prelude:
        script += f'{prelude} || exit $?; '
    script += (
        f'cd {shlex.quote(runtime_cwd)} || exit $?; '
        f'rm -f {pid}; '
        f'{grouped} </dev/null 3<&- & child=$!; '
        f'echo "$child" > {pid}; '
        '[ -n "$rec" ] && printf \'{"run_id":"%s","pgid":%s,"sid":%s,'
        '"state":"running","at":"%s"}\\n\' "$rid" "$child" "$child" '
        '"$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$rec.tmp" 2>/dev/null '
        '&& mv -f "$rec.tmp" "$rec" 2>/dev/null; '
        'echo "FOAMMESH_RUN $rid pgid=$child sid=$child" >&2; '
        'watch=""; '
        f'if [ -n "${{{RUN_ID_VARIABLE}:-}}" ]; then '
        f'env -u {RUN_ID_VARIABLE} setsid bash -c {watcher} fm-watch '
        '"$child" "$rec" "$rid" <&3 >/dev/null 2>&1 & watch=$!; fi; '
        'exec 3<&-; '
        '( while kill -0 "$child" 2>/dev/null; do '
        f'echo "FOAMMESH_HB $rid" >&2; sleep {HEARTBEAT_SECONDS}; done ) & hb=$!; '
        'wait "$child"; status=$?; '
        'kill "$hb" 2>/dev/null; '
        # Anything the mesher left behind in its group or session is a stray
        # writer; nothing is reported finished while one lives.
        'kill -KILL -- "-$child" 2>/dev/null; pkill -KILL -s "$child" 2>/dev/null; '
        'n=0; while [ "$n" -lt 20 ] && { kill -0 -- "-$child" 2>/dev/null '
        '|| pgrep -s "$child" >/dev/null 2>&1; }; do sleep 0.1; n=$((n+1)); done; '
        '[ -n "$watch" ] && kill -- "-$watch" 2>/dev/null; '
        f'rm -f {pid}; '
        'exit "$status"'
    )
    return script


def cleanup_script(*, runtime_cwd: str, pid_name: str, extra_names=()) -> str:
    """The cancel cleanup: TERM the group and session, then KILL."""
    names = ' '.join(shlex.quote(item) for item in (pid_name, *extra_names))
    pid = shlex.quote(pid_name)
    return (
        f'cd {shlex.quote(runtime_cwd)} && '
        f'if test -s {pid}; then '
        f'pid=$(cat {pid}); '
        'kill -TERM -- "-$pid" 2>/dev/null || true; '
        'pkill -TERM -s "$pid" 2>/dev/null || true; '
        'sleep 1; kill -KILL -- "-$pid" 2>/dev/null || true; '
        'pkill -KILL -s "$pid" 2>/dev/null || true; '
        f'rm -f {names}; fi'
    )
