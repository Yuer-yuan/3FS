#!/usr/bin/env python3
"""Adopt verified pinned IO500/Hydra artifacts into a private 3FS bundle."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess

import full_deps
import stage_guest_rootfs as stage

SCHEMA = 'hf3fs.io500-bundle.v1'
HERE = Path(__file__).resolve().parent
PINS = {
    'mpich': ('target/build/sources/mpich', '15f59ab2b740539472dfd130f7fe01b61c28bba4'),
    'io500': ('target/build/sources/io500', 'a69cf60cf76538a34c1332bc448838cf9a560a9b'),
    'ior': ('target/build/sources/io500/build/ior', '5fcf0ba995fd92164d50e344597e2d8203298c08'),
    'pfind': ('target/build/sources/io500/build/pfind', 'd08501f9976caf1adabdebfb883d4701dd98fe35'),
}
BINARIES = {'io500': 'io500', 'io500_verify': 'io500-verify',
            'mpiexec': 'mpiexec.hydra', 'hydra_proxy': 'hydra_pmi_proxy'}
LOADER = '/lib/ld-musl-riscv64.so.1'
ALLOWED = {'libmpi.so.0', 'libc.so'}


def command(args: list[str], cwd: Path | None = None) -> str:
    return subprocess.check_output(args, cwd=cwd, text=True, stderr=subprocess.STDOUT).strip()


def validate_elf(path: Path) -> dict:
    info = stage.inspect_required_elf(path, path.name)
    if info.interpreter not in (None, LOADER) or not set(info.needed) <= ALLOWED:
        raise ValueError(f'benchmark ELF has an unexpected loader/dependency: {path}')
    return dict(interpreter=info.interpreter, needed=list(info.needed))


def verify(manifest: Path) -> dict:
    record = json.loads(manifest.read_text())
    if record.get('schema') != SCHEMA or record.get('status') != 'passed':
        raise ValueError('benchmark bundle did not pass')
    root = manifest.parent.resolve(strict=True)
    for relative, item in record['files'].items():
        source = full_deps.contained_file(root, relative)
        if full_deps.sha256(source) != item['sha256']:
            raise ValueError('benchmark artifact changed: ' + relative)
        if 'elf' in item and validate_elf(source) != item['elf']:
            raise ValueError('benchmark ELF identity changed: ' + relative)
    required = {f'bin/{n}' for n in (*BINARIES.values(), 'io500-probe', 'io500-linebuf',
                                     'run-io500-rank', 'io500-job', 'stop-owned-job')}
    required |= {'lib/libc.so', 'lib/libmpi.so.0', 'etc/standard.ini', 'etc/reference-standard.ini'}
    if set(record['files']) != required:
        raise ValueError('benchmark bundle file set differs from the approved closure')
    if {n: s['commit'] for n, s in record.get('sources', {}).items()} != {n: p[1] for n, p in PINS.items()}:
        raise ValueError('benchmark source pins changed')
    for source, target in (('io500_rank.sh', 'run-io500-rank'),
                           ('io500_job.sh', 'io500-job'), ('io500_stop_job.sh', 'stop-owned-job')):
        if record['files']['bin/' + target]['sha256'] != full_deps.sha256(HERE / source):
            raise ValueError('benchmark helper differs from current source: ' + source)
    if record.get('probe_build', {}).get('source_sha256') != full_deps.sha256(HERE / 'io500_probe.c'):
        raise ValueError('benchmark probe differs from current source')
    if record.get('linebuf_build', {}).get('source_sha256') != full_deps.sha256(HERE / 'io500_linebuf.c'):
        raise ValueError('benchmark line-buffer relay differs from current source')
    normalize_config((root / 'etc/reference-standard.ini').read_text(), (root / 'etc/standard.ini').read_text())
    return record


def normalize_config(reference: str, candidate: str | None = None) -> str:
    # Freeze the reference itself as well as the allowed mount-path rewrite.
    if hashlib.sha256(reference.encode()).hexdigest() != 'f2719d791f9730f0ba23ad344b2acbcd01cb6430300f11c17e0666a7241fd28a':
        raise ValueError('reference standard configuration hash changed')
    import configparser
    parsed = configparser.ConfigParser(interpolation=None)
    parsed.read_string(reference)
    if (parsed.getint('debug', 'stonewall-time') != 300 or parsed.getboolean('global', 'scc') or
        parsed.get('global', 'api') != 'POSIX' or set(parsed.sections()) != {'global', 'debug'} or
        parsed.get('global', 'datadir') != '/badfs/io500-standard' or
        parsed.get('global', 'resultdir') != '/badfs/io500-standard-results'):
        raise ValueError('reference is not the approved standard IO500 profile')
    expected = reference.replace('/badfs/io500-standard', '/mnt/3fs/io500-standard')
    if candidate is not None and candidate != expected:
        raise ValueError('standard configuration changed beyond the mount root')
    return expected


def execute(reference_root: Path, output: Path) -> Path:
    reference = reference_root.resolve(strict=True)
    output = output.resolve()
    if not output.is_relative_to(stage.PROJECT_ROOT / 'out/cxl-riscv/io500'):
        raise ValueError('benchmark output must be private to the selected 3FS checkout')
    if output.exists():
        raise ValueError('refuse to overwrite an existing benchmark bundle')
    sources = {}
    for name, (relative, pin) in PINS.items():
        source = (reference / relative).resolve(strict=True)
        if not source.is_relative_to(reference):
            raise ValueError('benchmark source escaped the reference checkout')
        commit = command(['git', 'rev-parse', 'HEAD'], source)
        dirty = command(['git', 'status', '--short', '--untracked-files=no'], source)
        if commit != pin or dirty:
            raise ValueError('benchmark source pin or tracked contents changed: ' + name)
        sources[name] = dict(path=str(source), commit=commit, tracked_clean=True)
    provenance_path = reference / 'target/results/legofs-io500/build-manifest.json'
    provenance = json.loads(provenance_path.read_text())
    inputs = {}
    for key, name in BINARIES.items():
        item = provenance['artifacts'][key]
        source = full_deps.contained_file(reference, item['path'])
        if full_deps.sha256(source) != item['sha256']:
            raise ValueError('reference benchmark hash changed: ' + name)
        inputs['bin/' + name] = source
    prefix = reference / 'target/build/riscv-io500'
    inputs['lib/libmpi.so.0'] = prefix / 'mpich-rv64imafdc-compiler-rt-install/lib/libmpi.so.0.0.0'
    inputs['lib/libc.so'] = prefix / 'musl-rv64imafdc-compiler-rt/lib/libc.so'
    for source in inputs.values():
        validate_elf(source)
    config = (reference / 'configs/io500-standard.ini').read_text()
    normalized = normalize_config(config)
    for name in ('bin', 'lib', 'etc'):
        (output / name).mkdir(parents=True)
    files = {}
    for relative, source in inputs.items():
        shutil.copyfile(source, output / relative)
        (output / relative).chmod(0o755)
        files[relative] = dict(source_path=str(source.resolve()), source_sha256=full_deps.sha256(source))
    cc = prefix / 'mpich-rv64imafdc-compiler-rt-install/bin/mpicc'
    compile_cmd = [str(cc), '-O2', '-Wall', '-Wextra', '-Werror', str(HERE / 'io500_probe.c'),
                   '-o', str(output / 'bin/io500-probe')]
    compile_log = command(compile_cmd)
    linebuf_compile_cmd = [str(cc), '-O2', '-Wall', '-Wextra', '-Werror',
                           str(HERE / 'io500_linebuf.c'), '-o', str(output / 'bin/io500-linebuf')]
    linebuf_compile_log = command(linebuf_compile_cmd)
    for source, target in (('io500_rank.sh', 'run-io500-rank'),
                           ('io500_job.sh', 'io500-job'), ('io500_stop_job.sh', 'stop-owned-job')):
        shutil.copyfile(HERE / source, output / 'bin' / target)
        (output / 'bin' / target).chmod(0o755)
    (output / 'etc/reference-standard.ini').write_text(config)
    (output / 'etc/standard.ini').write_text(normalized)
    for path in sorted(output.rglob('*')):
        if not path.is_file():
            continue
        relative = str(path.relative_to(output))
        entry = files.setdefault(relative, {})
        entry.update(sha256=full_deps.sha256(path), bytes=path.stat().st_size)
        if path.read_bytes()[:4] == b'\x7fELF':
            entry['elf'] = validate_elf(path)
    record = dict(schema=SCHEMA, status='passed', sources=sources, files=files,
                  reference_manifest=dict(path=str(provenance_path), sha256=full_deps.sha256(provenance_path)),
                  adoption='existing pinned benchmark binaries; no source modifications or preload; PTY output relay only',
                  probe_build=dict(command=compile_cmd, output=compile_log,
                      source_sha256=full_deps.sha256(HERE / 'io500_probe.c'), compiler_sha256=full_deps.sha256(cc)),
                  linebuf_build=dict(command=linebuf_compile_cmd, output=linebuf_compile_log,
                      source_sha256=full_deps.sha256(HERE / 'io500_linebuf.c'), compiler_sha256=full_deps.sha256(cc)),
                  musl=dict(version='1.2.5', archive_sha256=full_deps.sha256(
                      reference / 'out/legofs-type3/toolchain-src/musl-1.2.5.tar.gz')))
    manifest = output / 'manifest.json'
    manifest.write_text(json.dumps(record, indent=2) + '\n')
    verify(manifest)
    return manifest


def install(manifest: Path, root: Path) -> dict[str, Path]:
    bundle = verify(manifest)
    binaries = {}
    for relative, record in bundle['files'].items():
        target = root / 'opt/io500' / relative
        stage.install_file(manifest.parent / relative, target, executable=relative.startswith('bin/'))
        if 'elf' in record:
            binaries['io500/' + relative] = target
    loader = root / LOADER.lstrip('/')
    if loader.exists() or loader.is_symlink():
        existing = loader.resolve(strict=True)
        if (not existing.is_relative_to(root.resolve(strict=True)) or
            full_deps.sha256(existing) != bundle['files']['lib/libc.so']['sha256']):
            raise ValueError('existing musl loader differs from the benchmark runtime')
    else:
        loader.symlink_to('../opt/io500/lib/libc.so')
    shutil.copyfile(manifest, root / 'opt/io500/manifest.json')
    return binaries


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--reference-root', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    print(execute(args.reference_root, args.output))


if __name__ == '__main__':
    main()
