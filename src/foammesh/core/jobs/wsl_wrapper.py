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
#: Plan 48 PM. ``FOAMMESH_PEAK <run_id> kb=<n> measure=<how> reason=<why>``
#: goes out just before the exit line: the peak resident set of the mesher, or
#: ``measure=unavailable`` with the reason. ``kb`` is empty when unavailable.
PEAK_PREFIX = 'FOAMMESH_PEAK '
PEAK_MEASURE = 'time'
#: The environment variables ``JobManager`` passes through ``WSLENV``.
RUN_ID_VARIABLE = 'FOAMMESH_RUN_ID'
RUN_DIR_VARIABLE = 'FOAMMESH_RUN_DIR'
#: TERM grace before KILL on transport loss, seconds.
TERM_GRACE_SECONDS = 10
HEARTBEAT_SECONDS = 2
#: DP-6457. Where Open MPI's session folder goes: 4.x reads ``orte_tmpdir_base``,
#: 5.x ``prte_tmpdir_base``.
#:
#: DP-6476. A killed ``mpirun`` cannot remove what it made, and ``/dev/shm`` is
#: memory nothing sweeps: a cancelled 4-rank Snap left its session folder
#: (``ompi.<host>.<uid>/pid.<mpirun pid>``) and four 4 MiB
#: ``vader_segment.<host>.<uid>.<jobid>.<rank>`` files (Open MPI 4.1's shared
#: memory, put in ``btl_vader_backing_directory``, ``/dev/shm`` by default).
#: Each run now has a folder of its own, ``run.<wrapper pid>``, holding both,
#: so one removal clears the run and touches nothing of anyone else's. Before
#: it is made, a run folder whose wrapper is gone and whose every ``mpirun``
#: (the ``pid.<n>`` folders inside it) is gone too is swept: that is a run the
#: whole WSL side was killed under. ``fmrun`` is empty when there is no run
#: folder, and then nothing is ever removed.
MPI_SESSION_BASE = (
    'fmtmp="/dev/shm/foammesh-$(id -u)"; fmrun=""; '
    'if mkdir -p "$fmtmp" 2>/dev/null && [ -w "$fmtmp" ]; then '
    'for d in "$fmtmp"/run.*; do [ -d "$d" ] || continue; n="${d##*/run.}"; '
    'case "$n" in ""|*[!0-9]*) continue;; esac; '
    '[ "$n" != "$$" ] && kill -0 "$n" 2>/dev/null && continue; '
    'held=""; for p in "$d"/ompi.*/pid.*; do [ -e "$p" ] || continue; '
    'kill -0 "${p##*/pid.}" 2>/dev/null && held=1; done; '
    '[ -n "$held" ] || rm -rf "$d" 2>/dev/null; done; '
    'if mkdir "$fmtmp/run.$$" 2>/dev/null; then fmrun="$fmtmp/run.$$"; '
    'export OMPI_MCA_btl_vader_backing_directory="$fmrun"; fmtmp="$fmrun"; fi; '
    'export OMPI_MCA_orte_tmpdir_base="$fmtmp" '
    'PRTE_MCA_prte_tmpdir_base="$fmtmp"; fi')
#: DP-6459. hwloc lists PCI devices by reading each one's ``config`` file under
#: ``/sys/bus/pci/devices``. A mesher needs the processors, not the devices, so
#: the three components that read them are left out (``pci`` is libpciaccess,
#: ``linuxio`` is hwloc 2's sysfs reader, ``linuxpci`` hwloc 1's). A value the
#: user set is kept.
HWLOC_WITHOUT_PCI = (
    'export HWLOC_COMPONENTS='
    '"${HWLOC_COMPONENTS:--pci,-linuxio,-linuxpci}"')

# The watcher, run as ``setsid bash -c WATCHER fm-watch <pgid> <record> <rid>
# <run folder>`` (DP-6476: the folder is removed once the run is dead)
# with the lifetime pipe as its stdin. ``cat`` returns on EOF only.
_WATCHER = r'''
cat >/dev/null
pg="$1"; rec="$2"; rid="$3"; run="$4"
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
if alive; then note false; else note true; [ -n "$run" ] && rm -rf "$run" 2>/dev/null; fi
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
    # Plan 48 PM. `/usr/bin/time -f %M` (GNU time) execs the mesher as its
    # child, reports the peak resident set in kB to a side file (never to the
    # mesher's stdout or stderr) and exits with the mesher's own status. When
    # the distro has no /usr/bin/time the mesher runs bare and the peak is
    # reported as unavailable; a missing measure never fails a run.
    inner = (
        'if [ -x /usr/bin/time ] && [ -n "$FM_TIME_FILE" ]; then '
        'exec /usr/bin/time -f %M -o "$FM_TIME_FILE" stdbuf -oL -eL ' + run
        + '; else exec stdbuf -oL -eL ' + run + '; fi')
    grouped = 'setsid bash -c ' + shlex.quote(inner)
    watcher = shlex.quote(_WATCHER)
    parts = [
        f'rid="${{{RUN_ID_VARIABLE}:-{token}}}"',
        f'rdir="${{{RUN_DIR_VARIABLE}:-}}"',
        'rec=""; [ -n "$rdir" ] && mkdir -p "$rdir" 2>/dev/null && rec="$rdir/$rid.linux"',
        'child=""',
        'peak=""; pmeasure=""; preason=""',
        'export FM_TIME_FILE="${rdir:-/tmp}/.foammesh-$rid.time"',
        # DP-6457. Open MPI keeps its session folder under /tmp, and a distro
        # with systemd empties /tmp while it boots (tmpfiles `D /tmp`). A run
        # started into a distro that was still booting lost that folder and
        # every rank failed in MPI_Init. /dev/shm is mounted before any
        # command runs and is not emptied afterwards, so the session folder
        # goes there; a distro without a writable /dev/shm keeps the default.
        MPI_SESSION_BASE,
        # DP-6459. MEASURED 2026-11-11 in a WSL 2 VM: `mpirun -np 4
        # snappyHexMesh` sat at 90 % of a processor for 13 minutes before it
        # started a rank, its one open file a virtual PCI device's `config`
        # (hwloc listing devices); with the PCI components left out the same
        # `mpirun` started 2, 4 and 8 ranks in 45 to 51 ms.
        HWLOC_WITHOUT_PCI,
        # A record only ever says `exited` when nothing of the run is left.
        'fm_exit() { rc=$?; '
        'if [ -n "$child" ] && { kill -0 -- "-$child" 2>/dev/null '
        '|| pgrep -s "$child" >/dev/null 2>&1; }; then exit "$rc"; fi; '
        # DP-6476. Nothing of the run is left, so neither is its MPI folder
        # (session folder and shared-memory segments, finished or killed).
        '[ -n "$fmrun" ] && rm -rf "$fmrun" 2>/dev/null; '
        '[ -n "$rec" ] && printf \'{"run_id":"%s","pgid":%s,"sid":%s,'
        '"state":"exited","rc":%s,"peak_kb":%s,"peak_measure":"%s",'
        '"at":"%s"}\\n\' "$rid" "${child:-null}" '
        '"${child:-null}" "$rc" "${peak:-null}" "${pmeasure:-unavailable}" '
        '"$(date -u +%Y-%m-%dT%H:%M:%SZ)" '
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
        f'rm -f {pid} "$FM_TIME_FILE"; '
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
        '"$child" "$rec" "$rid" "$fmrun" <&3 >/dev/null 2>&1 & watch=$!; fi; '
        'exec 3<&-; '
        '( while kill -0 "$child" 2>/dev/null; do '
        f'echo "FOAMMESH_HB $rid" >&2; sleep {HEARTBEAT_SECONDS}; done ) & hb=$!; '
        'wait "$child"; status=$?; '
        # The measure file is the mesher's, read once it has ended.
        'pmeasure=unavailable; preason=no-usr-bin-time; '
        'if [ -x /usr/bin/time ]; then preason=no-measure-file; '
        'if [ -s "$FM_TIME_FILE" ]; then '
        'peak=$(tail -n 1 "$FM_TIME_FILE" 2>/dev/null); '
        'case "$peak" in ""|*[!0-9]*) peak=""; preason=unreadable-measure;; '
        f'*) pmeasure={PEAK_MEASURE}; preason="";; esac; fi; fi; '
        'rm -f "$FM_TIME_FILE" 2>/dev/null; '
        'echo "FOAMMESH_PEAK $rid kb=$peak measure=$pmeasure reason=$preason" >&2; '
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
