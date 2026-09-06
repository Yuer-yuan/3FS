#!/bin/sh
# Invoked through setsid: this process owns exactly one benchmark job session.
set -u
job_dir=${HF3FS_IO500_JOB_DIR:-/run}
phase=$1
index=$2
shift 2
pid=$$
start=$(/bin/busybox awk '{print $22}' /proc/$$/stat)
printf '%s %s\n' "$pid" "$start" >"$job_dir/io500-job-$phase-$index"
"$@"
rc=$?
if [ "$index" = mpi ]; then
    printf 'HF3FS_IO500_MPI_EXIT phase=%s rc=%s\n' "$phase" "$rc"
else
    printf 'HF3FS_IO500_PROXY_EXIT phase=%s proxy=%s rc=%s\n' "$phase" "$index" "$rc"
fi
exit "$rc"
