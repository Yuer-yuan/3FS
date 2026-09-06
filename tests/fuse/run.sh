#!/bin/bash

set -euo pipefail

# ======== Helper Functions ========

function check_exists {
    if ! [ -e "$1" ]; then 
        echo "$1 not exists"
        exit 1
    fi
}

function start_fdb {
    local PORT="$1"
    local DATA="$2"
    local LOG="$3"
    local CLUSTER="${DATA}/fdb.cluster"

    echo "fdbserver: $FDB, fdbclient: $FDBCLI, start fdb @ port ${PORT}"
    echo "test${FDB_PORT}:testdb${FDB_PORT}@127.0.0.1:${FDB_PORT}" > "${CLUSTER}"
    "${FDB}" -p auto:"${PORT}" -d "${DATA}" -L "${LOG}" -C "${CLUSTER}" &> "${LOG}/fdbserver.log" &
    FDB_PID=$!
    sleep 5 && "$FDBCLI" -C "${CLUSTER}" --exec "configure new memory single"
    sleep 5 && "$FDBCLI" -C "${CLUSTER}" --exec "status minimal"
}

function finish_process {
    local pid="${1:-}"
    local send_term="${2:-true}"
    if [ -z "${pid}" ]; then return 0; fi
    if ${send_term}; then kill "${pid}" 2>/dev/null; fi
    for _ in $(seq 1 200); do
        if ! kill -0 "${pid}" 2>/dev/null; then break; fi
        sleep 0.1
    done
    if kill -0 "${pid}" 2>/dev/null; then
        echo "Process ${pid} did not finish shutdown" >&2
        kill -9 "${pid}" 2>/dev/null
    fi
    local status=0
    wait "${pid}" || status=$?
    echo "Process ${pid} shutdown: exit=${status}, sent_term=${send_term}"
    # A process without a SIGTERM handler (including the private fdbserver)
    # exits as 128 + SIGTERM when stopped by this cleanup function.
    if ${send_term} && ((status == 143)); then return 0; fi
    return "${status}"
}

function cleanup {
    rv=$?
    trap - EXIT
    set +e
    echo "Exit with ${rv}"
    if [ -n "${FUSE_PID:-}" ]; then
        # mountpoint may fail to stat a disconnected FUSE filesystem after a
        # crash. Attempt to unmount our path even in that state.
        fusermount3 -u "${MOUNT}" 2>/dev/null
    fi
    # Unmount returns before FuseClients::stop and endpoint retirement finish.
    # Its libfuse signal handlers are already removed at that point, so a
    # SIGTERM here would abort normal shutdown. Leave its servers alive too.
    if ! finish_process "${FUSE_PID:-}" false; then rv=1; fi
    # Meta shutdown still needs storage, mgmtd and FDB. Retire every process
    # before its dependencies, then retire the global fabric last.
    if ! finish_process "${META_PID:-}"; then rv=1; fi
    if ! finish_process "${STORAGE_PID:-}"; then rv=1; fi
    if ! finish_process "${MGMTD_PID:-}"; then rv=1; fi
    if ! finish_process "${FDB_PID:-}"; then rv=1; fi
    if ! finish_process "${CXL_FABRIC_PID:-}"; then rv=1; fi
    if [ -n "${FUSE_PID:-}" ] && findmnt -rn --mountpoint "${MOUNT}" >/dev/null; then
        if ! fusermount3 -u "${MOUNT}"; then rv=1; fi
    fi
    echo "Cleanup finished with ${rv}"
    exit $rv
}

# ======== Main Script ========

# Parameter validation
if (($# < 2)); then
    echo "${0} <binary> <test-dir> <log-dir>"
    exit 1
fi

if [ -n "${HF3FS_FUSE_SMOKE_BYTES:-}" ]; then
    if ! [[ "${HF3FS_FUSE_SMOKE_BYTES}" =~ ^[1-9][0-9]*$ ]] || \
       ((HF3FS_FUSE_SMOKE_BYTES % 65536 != 0)); then
        echo "HF3FS_FUSE_SMOKE_BYTES must be a positive multiple of 65536" >&2
        exit 1
    fi
fi

# Storage opens chunk-engine files across four targets. Raise only this
# process's soft limit, within the inherited hard limit; no host config changes.
FD_SOFT_LIMIT=$(ulimit -Sn)
if [ "${FD_SOFT_LIMIT}" != unlimited ] && ((FD_SOFT_LIMIT < 65536)); then
    ulimit -Sn 65536
fi
echo "File descriptor soft limit: $(ulimit -Sn)"

# Set variables
BINARY=${1%/}
TEST_DIR=${2%/}
LOG_DIR=${3:-}
mkdir -p "${TEST_DIR}"
TEST_DIR=$(realpath "${TEST_DIR}")

# Define paths
DATA="${TEST_DIR}/data"
CONFIG="${TEST_DIR}/config"
DEFAULT_LOG="${TEST_DIR}/log"
LOG=${LOG_DIR:=${DEFAULT_LOG}}
MOUNT=$(realpath "${TEST_DIR}/mnt")
trap cleanup EXIT
trap 'exit 143' TERM
trap 'exit 130' INT
export CXL_MANIFEST=${CONFIG}/cxl-runtime-manifest.json
export CXL_REGION=${DATA}/cxl-region.bin

# Script related tools
SCRIPT_DIR=$(dirname "$0")
UPDATE_CONFIG="python3 ${SCRIPT_DIR}/update-config.py"

# Set environment variables
export TOKEN="AADHHSOs8QA92iRe2wB1fmuL"
export TOKEN_FILE="${CONFIG}/token"

export PKEY_INDEX=0
export FS_CLUSTER="ci_test"
export FDB_TEST_CLUSTER="${DATA}/foundationdb/fdb.cluster"
export TARGETS="'${DATA}/storage/data1', '${DATA}/storage/data2', '${DATA}/storage/data3', '${DATA}/storage/data4'"

# Set FoundationDB paths
if [ -z "${FDB_PATH+x}" ]; then
    FDB=$(which fdbserver)
    FDBCLI=$(which fdbcli)
    FDB_CLIENT_LIB=${FDB_CLIENT_LIB:-/lib/libfdb_c.so}
elif [ -x "${FDB_PATH}/fdbserver" ] && [ -x "${FDB_PATH}/fdbcli" ]; then
    FDB="$FDB_PATH/fdbserver"
    FDBCLI="$FDB_PATH/fdbcli"
    FDB_CLIENT_LIB=${FDB_CLIENT_LIB:-${FDB_PATH}/libfdb_c.so}
else
    FDB="$FDB_PATH/usr/sbin/fdbserver"
    FDBCLI="$FDB_PATH/usr/bin/fdbcli"
    FDB_CLIENT_LIB=${FDB_CLIENT_LIB:-${FDB_PATH}/usr/lib/libfdb_c.so}
    export LD_LIBRARY_PATH="${FDB_PATH}/usr/lib${LD_LIBRARY_PATH:+:${LD_LIBRARY_PATH}}"
fi
export FDB_CLIENT_LIB

# ======== Network Configuration ========
echo "### Check network"
export ADDRESS=$(ip -o -4 addr show | grep -E '(en|eth|ib)' | awk '{print $4}' | head -n 1 | awk -F'[/ ]' '{print $1}')
export FDB_PORT=12500
export MGMTD_PORT=12501
export META_PORT=12502
export STORAGE_PORT=12503
export CXL_AUTHORITY_PORT=12499
export CXL_ADMIN_PORT=12507
export CXL_CLIENT_PORT=12516

# ======== Check Directories and Files ========
echo "### Check directory and file"
check_exists "${TEST_DIR}"
if [ -z "$(ls -A ${TEST_DIR})" ]; then
    echo ${TEST_DIR} is empty
else
    echo ${TEST_DIR} is not empty
    exit 1
fi
mkdir -p ${DATA}/storage/data{1..4}

check_exists "${BINARY}"
check_exists "${BINARY}/mgmtd_main"
check_exists "${BINARY}/meta_main"
check_exists "${BINARY}/storage_main"
check_exists "${BINARY}/hf3fs_fuse_main"
check_exists "${BINARY}/admin_cli"
check_exists "${BINARY}/cxl-fabricd"
check_exists "${FDB}"
check_exists "${FDBCLI}"
check_exists "${FDB_CLIENT_LIB}"

# ======== Create Directories ========
echo "### Create subdirectories"
mkdir -p "${CONFIG}" "${LOG}" "${DATA}" "${MOUNT}" "${DATA}/foundationdb" "${DATA}/storage"
LOG=$(realpath "${LOG}")
export MGMTD_LOG=${LOG}/mgmtd.log
export META_LOG=${LOG}/meta.log
export STORAGE_LOG=${LOG}/storage.log
export FUSE_LOG=${LOG}/fuse.log
export ADMIN_LOG=${LOG}/admin.log

# ======== Generate Configuration Files ========
echo "Generate config"
# mgmtd app configuration
echo "allow_empty_node_id = false" > "${CONFIG}/mgmtd_main_app.toml"
echo "node_id = 1" >> "${CONFIG}/mgmtd_main_app.toml"
# meta app configuration
echo "allow_empty_node_id = false" > "${CONFIG}/meta_main_app.toml"
echo "node_id = 50" >> "${CONFIG}/meta_main_app.toml"
# storage app configuration
echo "allow_empty_node_id = false" > "${CONFIG}/storage_main_app.toml"
echo "node_id = 10000" >> "${CONFIG}/storage_main_app.toml"
echo ${TOKEN} > ${TOKEN_FILE}

# Copy and process template configurations
for file in "${SCRIPT_DIR}"/config/*.toml; do
    echo "- Generate $(basename "$file")"
    cat "${file}" | envsubst > "${CONFIG}/$(basename "$file")"
done
ADMIN_CLI="${BINARY}/admin_cli --cfg ${CONFIG}/admin_cli.toml --"

# Generate a run-owned CXL manifest and backing file. The platform itself is
# not modified: this host test uses a regular file, while QEMU runs use the
# existing /dev/dax0.0 aperture with the same manifest schema.
CXL_SESSION_GENERATION=$(date +%s)
python3 "${SCRIPT_DIR}/../../deploy/cxl-riscv/make_phase1_manifest.py" \
    --scenario storage \
    --replication-factor 1 \
    --clients 1 \
    --session-generation "${CXL_SESSION_GENERATION}" \
    --role-address "fabric-authority=CXL://${ADDRESS}:${CXL_AUTHORITY_PORT}" \
    --role-address "mgmtd=CXL://${ADDRESS}:${MGMTD_PORT}" \
    --role-address "meta=CXL://${ADDRESS}:${META_PORT}" \
    --role-address "storage-0=CXL://${ADDRESS}:${STORAGE_PORT}" \
    --role-address "admin=CXL://${ADDRESS}:${CXL_ADMIN_PORT}" \
    --role-address "client-0=CXL://${ADDRESS}:${CXL_CLIENT_PORT}" \
    --output "${CXL_MANIFEST}"
truncate -s 256M "${CXL_REGION}"

CXL_AUTHORITY_LOCK=${DATA}/cxl-authority.lock
CXL_AUTHORITY_RECEIPT="hf3fs-fuse-test-${CXL_SESSION_GENERATION}"
printf '%s' "${CXL_AUTHORITY_RECEIPT}" > "${CXL_AUTHORITY_LOCK}"
printf '%s\n' \
    '[cxl]' \
    'enabled = true' \
    'mode = '\''InitializeAuthority'\''' \
    'region_type = '\''File'\''' \
    "region_path = '${CXL_REGION}'" \
    "region_length = '256MB'" \
    "manifest_path = '${CXL_MANIFEST}'" \
    'endpoint = 1' \
    'endpoint_generation = 1' \
    "authority_owner_lock = '${CXL_AUTHORITY_LOCK}'" \
    "authority_receipt = '${CXL_AUTHORITY_RECEIPT}'" \
    > "${CONFIG}/cxl-fabricd.toml"

"${BINARY}/cxl-fabricd" --cfg "${CONFIG}/cxl-fabricd.toml" \
    > "${LOG}/cxl-fabricd.stdout" 2> "${LOG}/cxl-fabricd.stderr" &
CXL_FABRIC_PID=$!
for _ in $(seq 1 100); do
    if grep -q HF3FS_CXL_FABRIC_READY "${LOG}/cxl-fabricd.stdout"; then
        break
    fi
    kill -0 "${CXL_FABRIC_PID}"
    sleep 0.1
done
grep -q HF3FS_CXL_FABRIC_READY "${LOG}/cxl-fabricd.stdout"

# ======== Create FoundationDB ========
echo "### Create foundationdb"
start_fdb $FDB_PORT "${DATA}/foundationdb" "${LOG}/"

# ======== Initialize Cluster ========
echo "### Init cluster"
${ADMIN_CLI} user-add --root --admin --token "${TOKEN}" 0 root
${ADMIN_CLI} user-set-token --new 0
${ADMIN_CLI} user-list
${ADMIN_CLI} init-cluster \
    --mgmtd "${CONFIG}/mgmtd_main.toml" \
    --meta "${CONFIG}/meta_main.toml" \
    --storage "${CONFIG}/storage_main.toml" \
    --fuse "${CONFIG}/hf3fs_fuse_main.toml" \
    --skip-config-check 1 524288 1

# ======== Start Services ========
echo "### Start mgmtd"
"${BINARY}/mgmtd_main" \
    --app_cfg "${CONFIG}/mgmtd_main_app.toml" \
    --launcher_cfg "${CONFIG}/mgmtd_main_launcher.toml" \
    --cfg "${CONFIG}/mgmtd_main.toml" \
    > "${LOG}/mgmtd_main.stdout" 2> "${LOG}/mgmtd_main.stderr" &
MGMTD_PID=$!
sleep 5
kill -0 "${MGMTD_PID}"

echo "### Start meta & storage"
"${BINARY}/storage_main" \
    --app_cfg "${CONFIG}/storage_main_app.toml" \
    --launcher_cfg "${CONFIG}/storage_main_launcher.toml" \
    --cfg "${CONFIG}/storage_main.toml" \
    > "${LOG}/storage_main.stdout" 2> "${LOG}/storage_main.stderr" &
STORAGE_PID=$!

"${BINARY}/meta_main" \
    --app_cfg "${CONFIG}/meta_main_app.toml" \
    --launcher_cfg "${CONFIG}/meta_main_launcher.toml" \
    --cfg "${CONFIG}/meta_main.toml" \
    > "${LOG}/meta_main.stdout" 2> "${LOG}/meta_main.stderr" &
META_PID=$!

sleep 15
kill -0 "${MGMTD_PID}" "${META_PID}" "${STORAGE_PID}"

# ======== Configure Nodes and Chains ========
echo "### List nodes"
${ADMIN_CLI} list-nodes

echo "### Create targets and chain table"
echo "ChainId,TargetId" > "${CONFIG}/chains.csv"
echo "ChainId" > "${CONFIG}/chain-table.csv"
for disk in $(seq 1 4); do
    target=$(printf "%d%02d001" 10000 $disk)
    ${ADMIN_CLI} create-target --node-id 10000 --disk-index $((disk - 1)) --target-id "${target}" --chain-id "${target}"
    echo "${target},${target}" >> "${CONFIG}/chains.csv"
    echo "${target}" >> "${CONFIG}/chain-table.csv"
done

${ADMIN_CLI} upload-chains "${CONFIG}/chains.csv"
${ADMIN_CLI} upload-chain-table 1 "${CONFIG}/chain-table.csv" --desc replica-1
sleep 10
${ADMIN_CLI} list-chains
${ADMIN_CLI} list-chain-tables

if [ $UID -ne 0 ]; then ${ADMIN_CLI} user-add ${UID} test; fi
${ADMIN_CLI} user-list
${ADMIN_CLI} mkdir --perm 0755 test
${ADMIN_CLI} set-perm --uid ${UID} --gid ${UID} test

# ======== Start FUSE ========
echo "### Start fuse"
"${BINARY}/hf3fs_fuse_main" \
    --launcher_cfg "${CONFIG}/hf3fs_fuse_main_launcher.toml" \
    --launcher_config.mountpoint="${MOUNT}" \
    > "${LOG}/fuse.stdout" 2> "${LOG}/fuse.stderr" &
FUSE_PID=$!

# Wait for mount point to be ready
SECONDS=0
while [ ! -d "${MOUNT}/test" ] && [ ${SECONDS} -lt 30 ]; do
    kill -0 "${FUSE_PID}"
    sleep 1
done
if [ ! -d "${MOUNT}/test" ]; then
    echo "Mount point not ready within 30 seconds"
    exit 1
fi

echo "A temporary 3FS has been mounted to ${MOUNT}"
echo "This file system is for testing purposes only."
echo "Do not store any important data or you will lose them."

if [ -n "${HF3FS_FUSE_SMOKE_BYTES:-}" ]; then
    SMOKE_INPUT="${DATA}/cxl-smoke.input"
    SMOKE_OUTPUT="${DATA}/cxl-smoke.output"
    SMOKE_FILE="${MOUNT}/test/cxl-smoke.bin"
    dd if=/dev/urandom of="${SMOKE_INPUT}" bs=65536 iflag=fullblock \
        count=$((HF3FS_FUSE_SMOKE_BYTES / 65536)) status=none
    dd if="${SMOKE_INPUT}" of="${SMOKE_FILE}" bs=65536 conv=fsync status=none
    dd if="${SMOKE_FILE}" of="${SMOKE_OUTPUT}" bs=65536 iflag=direct status=none
    cmp "${SMOKE_INPUT}" "${SMOKE_OUTPUT}"
    sha256sum "${SMOKE_INPUT}" "${SMOKE_OUTPUT}"
    rm "${SMOKE_FILE}"
    echo "HF3FS_CXL_FUSE_SMOKE_OK bytes=${HF3FS_FUSE_SMOKE_BYTES}"
    if [ "${HF3FS_CXL_STORAGE_BENCH_SMOKE:-0}" = 1 ]; then
        "${BINARY}/admin_cli" --cfg "${CONFIG}/admin_cli.toml" -- \
            get-config --node-id 10000 --output-file "${LOG}/storage-core-config.toml"
        # The admin endpoint has already retired. A separate benchmark process
        # attaches its next generation and uses real mgmtd routing/storage RPC.
        # This validates --cxlConfig against external services in this owned
        # smoke cluster without adding another authority or in-process server.
        python3 - "${CONFIG}" <<'PY'
import json
from pathlib import Path
import sys
import tomllib
directory = Path(sys.argv[1])
config = tomllib.loads((directory / "admin_cli.toml").read_text())["cxl"]
assert config["mode"] == "Attach" and config["endpoint_generation"] == 0
(directory / "bench-runtime.toml").write_text("\n".join(f"{key} = {json.dumps(value)}" for key, value in config.items()) + "\n")
(directory / "bench-client.toml").write_text("[net_client.io_worker.cxlsocket]\nqueue_depth = 8\ncell_bytes = 65536\n")
PY
        "${BINARY}/storage_bench" --clusterMode --clusterId="${FS_CLUSTER}" \
            --mgmtdEndpoints="CXL://${ADDRESS}:${MGMTD_PORT}" --chainTableId=1 \
            --cxlConfig="${CONFIG}/bench-runtime.toml" --clientConfig="${CONFIG}/bench-client.toml" \
            --numChunks=4 --chunkSizeKB=512 --readSize=65536 --writeSize=65536 \
            --batchSize=4 --numCoroutines=2 --numTestThreads=2 --numReadSecs=1 --numWriteSecs=1 \
            --verifyReadData --truncateChunks --cleanupChunks \
            --statsFilePath="${LOG}/storage-bench.csv" >"${LOG}/storage-bench.stdout" 2>"${LOG}/storage-bench.stderr"
        echo "HF3FS_CXL_STORAGE_BENCH_SMOKE_OK"
    fi
else
    sleep infinity || true
fi

echo "Unmount fuse and stop all servers"
fusermount3 -u "${MOUNT}"
