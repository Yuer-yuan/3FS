#!/bin/sh
# Only the dedicated session whose leader identity was recorded may be killed.
set -eu
job_dir=${HF3FS_IO500_JOB_DIR:-/run}
phase=$1
index=$2
read -r pid start <"$job_dir/io500-job-$phase-$index"
case "$pid" in ''|*[!0-9]*) exit 64;; esac
case "$start" in ''|*[!0-9]*) exit 64;; esac
[ "$pid" -gt 1 ]
if [ -r "/proc/$pid/stat" ]; then
    read -r line <"/proc/$pid/stat"
    fields=${line##*) }
    set -- $fields
    test "$4" = "$pid"
    shift 19
    test "$1" = "$start"
fi
printf 'HF3FS_IO500_JOB_STOP phase=%s index=%s pid=%s start=%s\n' "$phase" "$index" "$pid" "$start"
/bin/busybox kill -TERM "-$pid" 2>/dev/null || true
attempt=0
while [ "$attempt" -lt 50 ]; do
    found=0
    for stat in /proc/[0-9]*/stat; do
        read -r line 2>/dev/null <"$stat" || continue
        fields=${line##*) }
        set -- $fields
        # The console reaps its child after this command returns. Zombies are
        # already dead; waiting for them here deadlocks cleanup with that wait.
        case "$1" in Z|X) continue;; esac
        [ "$4" != "$pid" ] || found=1
    done
    [ "$found" -ne 0 ] || exit 0
    attempt=$((attempt + 1))
    sleep 0.1
done
/bin/busybox kill -KILL "-$pid" 2>/dev/null || true
attempt=0
while [ "$attempt" -lt 20 ]; do
    found=0
    for stat in /proc/[0-9]*/stat; do
        read -r line 2>/dev/null <"$stat" || continue
        fields=${line##*) }
        set -- $fields
        case "$1" in Z|X) continue;; esac
        [ "$4" != "$pid" ] || found=1
    done
    [ "$found" -ne 0 ] || exit 0
    attempt=$((attempt + 1))
    sleep 0.1
done
exit 1
