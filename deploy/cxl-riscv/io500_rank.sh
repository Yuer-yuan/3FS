#!/bin/sh
set -eu
unset LD_PRELOAD
export LD_LIBRARY_PATH=/opt/io500/lib
rank=${PMI_RANK:?missing MPI rank}
export HF3FS_IO500_RANK=$rank
guest=$(cat /opt/io500/etc/guest-id)
test "$rank" -eq "$((guest - 1))"
test "$(hostname)" = "client$rank"
grep -q ' /mnt/3fs fuse' /proc/mounts
printf 'HF3FS_IO500_RANK rank=%s guest=%s host=%s pid=%s endpoint=%s\n' "$rank" "$guest" "$(hostname)" "$$" "$((guest + 15))"
exec /opt/io500/bin/io500-linebuf /opt/io500/bin/io500 /opt/io500/etc/standard.ini
