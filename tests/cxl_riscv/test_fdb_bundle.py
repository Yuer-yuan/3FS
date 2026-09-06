import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


DEPLOY_DIR = Path(__file__).parents[2] / "deploy" / "cxl-riscv"
FDB_DIR = DEPLOY_DIR / "fdb"
PROJECT_ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(FDB_DIR))

import verify_fdb_bundle
import build_fdb_riscv
import prepare_fdb_environment


LOCK_PATH = FDB_DIR / "fdb-riscv.lock.json"


def valid_bundle():
    commit = "5140696da2df47c143ae74c0f4207b65d0d94876"
    return {
        "schema": "hf3fs.fdb-riscv-bundle.v1",
        "version": "7.3.63",
        "source": {
            "url": "https://github.com/apple/foundationdb.git",
            "annotated_tag_object": "70a1f603842f8e7d4289cbd5e18441d74042a783",
            "commit": commit,
        },
        "sysroot": "/opt/riscv/sysroot",
        "api_versions": [710],
        "source_modifications": [
            {
                "path": "bindings/c/CMakeLists.txt",
                "sha256": (
                    "d3e5477e279f0b4365cb4d3705c664f4"
                    "db1b3ad22f395444cbea3a9b4aa56672"
                ),
            },
            {
                "path": "bindings/c/generate_asm.py",
                "sha256": (
                    "36e9f1a1021694459d18b922a10b18f2"
                    "5d1c9ce8ef727687a5f58657e419007d"
                ),
            },
            {
                "path": "cmake/FDBComponents.cmake",
                "sha256": (
                    "44c18b82dc7fea3ad9790ab383fc84de"
                    "1552d8a5e97420a19b47795eea946625"
                ),
            },
            {
                "path": "contrib/crc32/crc32c.cpp",
                "sha256": (
                    "4d28892b18d44248837a3927574bffdc"
                    "ac427eba961aac496a168e2a42347e15"
                ),
            },
            {
                "path": "contrib/stacktrace/stacktrace.amalgamation.cpp",
                "sha256": (
                    "ebe97b9f1bd064fbecb4f02f9ad4ba6d"
                    "1f9010f393c4b6f28a75227557c177cc"
                ),
            },
            {
                "path": "fdbserver/include/fdbserver/art.h",
                "sha256": (
                    "de971c0ca5046a98745ae86220f1bf4e9"
                    "32c129fe6ab322214d45f0b9e1a9f9d"
                ),
            },
            {
                "path": "fdbserver/include/fdbserver/art_impl.h",
                "sha256": (
                    "17a3b76995829ad3d0c32d406a3d7888c"
                    "d7eb0a832d6d72c2a098f07b95e1a50"
                ),
            },
            {
                "path": "flow/include/flow/Platform.h",
                "sha256": (
                    "d8d9e51a099c1b1a8f80d591ad34042f"
                    "d3638f01f187aa744b5035f9041b6931"
                ),
            },
            {
                "path": "flow/Platform.actor.cpp",
                "sha256": (
                    "7d459498f43aa56b9a9d3fd02855c400"
                    "cf9143cd2df7855ecff2dab747ea5eb7"
                ),
            },
        ],
        "fdbserver": {
            "path": "/opt/riscv/sysroot/usr/bin/fdbserver",
            "sha256": "1" * 64,
            "machine": "RISC-V",
            "source_commit": commit,
            "interpreter": "/lib/ld-linux-riscv64-lp64d.so.1",
            "needed": [
                "/opt/riscv/sysroot/usr/lib/libstdc++.so.6",
            ],
        },
        "libfdb_c": {
            "path": "/opt/riscv/sysroot/usr/lib/libfdb_c.so",
            "sha256": "2" * 64,
            "machine": "RISC-V",
            "source_commit": commit,
            "interpreter": None,
            "needed": [
                "/opt/riscv/sysroot/usr/lib/libstdc++.so.6",
            ],
        },
        "headers": {
            "root": "/opt/riscv/sysroot/usr/include",
            "files": {
                "foundationdb/fdb_c.h": "3" * 64,
                "foundationdb/fdb_c_options.g.h": "4" * 64,
                "foundationdb/fdb_c_types.h": "5" * 64,
                "foundationdb/fdb_c_apiversion.g.h": "7" * 64,
            },
        },
        "dependencies": [
            {
                "path": "/opt/riscv/sysroot/usr/lib/libstdc++.so.6",
                "sha256": "6" * 64,
                "machine": "RISC-V",
            }
        ],
    }


class FdbBundleTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.lock = json.loads(LOCK_PATH.read_text())

    def verify(self, bundle):
        return verify_fdb_bundle.verify(bundle, self.lock, check_files=False)

    def test_lock_pins_the_verified_official_tag(self):
        self.assertEqual(self.lock["version"], "7.3.63")
        self.assertEqual(
            self.lock["source"]["annotated_tag_object"],
            "70a1f603842f8e7d4289cbd5e18441d74042a783",
        )
        self.assertEqual(
            self.lock["source"]["commit"],
            "5140696da2df47c143ae74c0f4207b65d0d94876",
        )
        self.assertEqual(self.lock["api_versions"], [710])
        modifications = {
            item["path"]: item["sha256"]
            for item in self.lock["source_modifications"]
        }
        self.assertEqual(
            modifications,
            {
                "bindings/c/CMakeLists.txt": (
                    "d3e5477e279f0b4365cb4d3705c664f4"
                    "db1b3ad22f395444cbea3a9b4aa56672"
                ),
                "bindings/c/generate_asm.py": (
                    "36e9f1a1021694459d18b922a10b18f2"
                    "5d1c9ce8ef727687a5f58657e419007d"
                ),
                "cmake/FDBComponents.cmake": (
                    "44c18b82dc7fea3ad9790ab383fc84de"
                    "1552d8a5e97420a19b47795eea946625"
                ),
                "contrib/crc32/crc32c.cpp": (
                    "4d28892b18d44248837a3927574bffdc"
                    "ac427eba961aac496a168e2a42347e15"
                ),
                "contrib/stacktrace/stacktrace.amalgamation.cpp": (
                    "ebe97b9f1bd064fbecb4f02f9ad4ba6d"
                    "1f9010f393c4b6f28a75227557c177cc"
                ),
                "fdbserver/include/fdbserver/art.h": (
                    "de971c0ca5046a98745ae86220f1bf4e9"
                    "32c129fe6ab322214d45f0b9e1a9f9d"
                ),
                "fdbserver/include/fdbserver/art_impl.h": (
                    "17a3b76995829ad3d0c32d406a3d7888c"
                    "d7eb0a832d6d72c2a098f07b95e1a50"
                ),
                "flow/include/flow/Platform.h": (
                    "d8d9e51a099c1b1a8f80d591ad34042f"
                    "d3638f01f187aa744b5035f9041b6931"
                ),
                "flow/Platform.actor.cpp": (
                    "7d459498f43aa56b9a9d3fd02855c400"
                    "cf9143cd2df7855ecff2dab747ea5eb7"
                ),
            },
        )

    def test_direct_third_party_checkout_matches_lock(self):
        source = PROJECT_ROOT / "third_party" / "foundationdb"
        build_fdb_riscv.verify_source_checkout(source, self.lock)

    def test_lock_pins_target_zlib_and_openssl_packages(self):
        packages = self.lock["dependencies"]["target_packages"]
        self.assertEqual(
            {item["package"] for item in packages},
            {"zlib1g", "zlib1g-dev", "libssl3t64", "libssl-dev"},
        )
        for item in packages:
            self.assertEqual(item["architecture"], "riscv64")
            self.assertTrue(
                item["url"].startswith(
                    "https://ports.ubuntu.com/ubuntu-ports/pool/main/"
                )
            )
            self.assertEqual(
                prepare_fdb_environment.package_record_errors(
                    item, expected_architectures={"riscv64"}
                ),
                [],
            )

    def test_lock_pins_complete_mono_compiler_package_closure(self):
        packages = self.lock["dependencies"]["host_packages"]
        required = {
            "mono-mcs",
            "mono-runtime",
            "mono-runtime-sgen",
            "mono-runtime-common",
            "mono-gac",
            "mono-4.0-gac",
            "libmono-corlib4.5-cil",
            "libmono-corlib4.5-dll",
            "libmono-system-core4.0-cil",
            "libmono-system-xml4.0-cil",
            "libmono-system4.0-cil",
            "libmono-microsoft-csharp4.0-cil",
            "libmono-system-configuration4.0-cil",
            "libmono-security4.0-cil",
            "libmono-system-numerics4.0-cil",
            "libmono-system-security4.0-cil",
            "libmono-system-data4.0-cil",
            "libmono-system-xml-linq4.0-cil",
            "libmono-system-data-datasetextensions4.0-cil",
            "libmono-system-enterpriseservices4.0-cil",
            "libmono-system-transactions4.0-cil",
        }
        self.assertEqual({item["package"] for item in packages}, required)
        for item in packages:
            self.assertEqual(
                prepare_fdb_environment.package_record_errors(
                    item, expected_architectures={"amd64", "all"}
                ),
                [],
            )

    def test_lock_pins_the_boost_archive_used_by_fdb(self):
        archive = self.lock["upstream_archives"]["boost_1_78_0"]
        self.assertEqual(archive["filename"], "boost_1_78_0.tar.bz2")
        self.assertEqual(archive["size"], 110675550)
        self.assertEqual(
            archive["sha256"],
            "8681f175d4bdb26c52222665793eef08490d7758529330f98d3b29dd0735bccc",
        )

    def test_seed_boost_archive_rejects_wrong_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            archive = temporary / "boost.tar.bz2"
            archive.write_bytes(b"wrong")
            lock = {
                "upstream_archives": {
                    "boost_1_78_0": {
                        "filename": "boost_1_78_0.tar.bz2",
                        "size": 5,
                        "sha256": "0" * 64,
                    }
                }
            }
            with self.assertRaises(build_fdb_riscv.BuildError):
                build_fdb_riscv.seed_boost_archive(
                    archive, temporary / "build", lock
                )

    def test_seed_boost_archive_places_verified_bytes_at_cmake_path(self):
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            archive = temporary / "locked-boost.tar.bz2"
            contents = b"locked boost archive"
            archive.write_bytes(contents)
            lock = {
                "upstream_archives": {
                    "boost_1_78_0": {
                        "filename": "boost_1_78_0.tar.bz2",
                        "size": len(contents),
                        "sha256": hashlib.sha256(contents).hexdigest(),
                    }
                }
            }
            seeded = build_fdb_riscv.seed_boost_archive(
                archive, temporary / "build", lock
            )
            self.assertEqual(seeded.read_bytes(), contents)
            self.assertEqual(
                seeded.relative_to(temporary / "build").as_posix(),
                "boost_targetProject-prefix/src/boost_1_78_0.tar.bz2",
            )

    def test_dependency_output_cannot_escape_3fs_generated_tree(self):
        accepted = prepare_fdb_environment.require_output_root(
            Path("/work/3FS/out/cxl-riscv/fdb/deps"),
            project_root=Path("/work/3FS"),
            resolve_project=False,
        )
        self.assertEqual(accepted, Path("/work/3FS/out/cxl-riscv/fdb/deps"))
        with self.assertRaises(prepare_fdb_environment.PreparationError):
            prepare_fdb_environment.require_output_root(
                Path("/work/elsewhere"),
                project_root=Path("/work/3FS"),
                resolve_project=False,
            )

    def test_prepared_sysroot_adds_relative_dynamic_loader_alias(self):
        with tempfile.TemporaryDirectory() as directory:
            sysroot = Path(directory) / "sysroot"
            loader = (
                sysroot
                / "usr/riscv64-linux-gnu/lib/ld-linux-riscv64-lp64d.so.1"
            )
            loader.parent.mkdir(parents=True)
            loader.write_bytes(b"riscv loader")
            aliases = prepare_fdb_environment.ensure_target_runtime_aliases(sysroot)
            visible = sysroot / "lib/ld-linux-riscv64-lp64d.so.1"
            self.assertEqual(
                aliases,
                {
                    "lib/ld-linux-riscv64-lp64d.so.1": (
                        "usr/riscv64-linux-gnu/lib/ld-linux-riscv64-lp64d.so.1"
                    )
                },
            )
            self.assertTrue(visible.is_symlink())
            self.assertFalse(Path(os.readlink(visible)).is_absolute())
            self.assertEqual(visible.resolve(), loader.resolve())
            self.assertEqual(
                prepare_fdb_environment.ensure_target_runtime_aliases(sysroot),
                aliases,
            )

    def test_accepts_complete_consistent_manifest(self):
        self.assertEqual(self.verify(valid_bundle()), [])

    def test_rejects_mixed_server_and_client_source(self):
        bundle = valid_bundle()
        bundle["libfdb_c"]["source_commit"] = "0" * 40
        self.assertIn(
            "FDB server/client source identity differs", self.verify(bundle)
        )

    def test_rejects_host_or_wrong_api_artifact(self):
        bundle = valid_bundle()
        bundle["fdbserver"]["machine"] = "Advanced Micro Devices X86-64"
        bundle["api_versions"] = [720]
        errors = self.verify(bundle)
        self.assertIn("fdbserver is not RISC-V", errors)
        self.assertIn("FoundationDB C API 710 is unsupported", errors)

    def test_rejects_unrecorded_source_modification_or_dependency(self):
        bundle = valid_bundle()
        bundle["source_modifications"].append(
            {"path": "unknown.cpp", "sha256": "0" * 64}
        )
        bundle["fdbserver"]["needed"].append(
            "/usr/lib/x86_64-linux-gnu/libstdc++.so.6"
        )
        errors = self.verify(bundle)
        self.assertIn("FDB source modification is not lock-pinned", errors)
        self.assertIn(
            "FDB target dependency escapes the RISC-V sysroot", errors
        )

    def test_rejects_missing_required_generated_header(self):
        bundle = valid_bundle()
        del bundle["headers"]["files"]["foundationdb/fdb_c_options.g.h"]
        self.assertIn("FDB C API header set is incomplete", self.verify(bundle))

    def test_rejects_missing_generated_api_version_header(self):
        bundle = valid_bundle()
        del bundle["headers"]["files"]["foundationdb/fdb_c_apiversion.g.h"]
        self.assertIn("FDB C API header set is incomplete", self.verify(bundle))

    def test_rejects_changed_tag_or_commit(self):
        for key in ("annotated_tag_object", "commit"):
            with self.subTest(key=key):
                bundle = valid_bundle()
                bundle["source"][key] = "0" * 40
                self.assertIn(
                    "FDB source identity does not match the lock",
                    self.verify(bundle),
                )

    def test_input_is_not_mutated(self):
        bundle = valid_bundle()
        before = copy.deepcopy(bundle)
        self.verify(bundle)
        self.assertEqual(bundle, before)

    def test_source_modification_hash_mismatch_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            target = source / "tracked.cpp"
            target.write_text("expected\n")
            lock = {
                "source_modifications": [
                    {
                        "path": "tracked.cpp",
                        "sha256": hashlib.sha256(b"different\n").hexdigest(),
                    }
                ]
            }
            with self.assertRaises(build_fdb_riscv.BuildError):
                build_fdb_riscv.locked_source_modifications(source, lock)

    def test_configure_command_is_target_only_and_minimal(self):
        command = build_fdb_riscv.configure_command(
            source=Path("/work/source"),
            build=Path("/work/build"),
            cmake=Path("/usr/bin/cmake"),
            ninja=Path("/usr/bin/ninja"),
            sysroot=Path("/opt/riscv/sysroot"),
            c_compiler=Path("/opt/riscv/bin/riscv64-linux-gnu-gcc"),
            cxx_compiler=Path("/opt/riscv/bin/riscv64-linux-gnu-g++"),
            mono=Path("/work/host-tools/bin/mono"),
            mcs=Path("/work/host-tools/bin/mcs"),
        )
        joined = " ".join(command)
        self.assertIn("-DCMAKE_SYSTEM_PROCESSOR=riscv64", command)
        self.assertIn("-DCMAKE_LIBRARY_ARCHITECTURE=riscv64-linux-gnu", command)
        self.assertIn(
            "-DCMAKE_SYSTEM_LIBRARY_PATH=/usr/lib/riscv64-linux-gnu;"
            "/usr/riscv64-linux-gnu/lib",
            command,
        )
        self.assertIn("-DCMAKE_FIND_ROOT_PATH_MODE_LIBRARY=ONLY", command)
        self.assertIn("-DCMAKE_FIND_ROOT_PATH_MODE_INCLUDE=ONLY", command)
        self.assertIn("-DBUILD_DOCUMENTATION=OFF", command)
        self.assertIn("-DBUILD_PYTHON_BINDING=OFF", command)
        self.assertIn("-DBUILD_C_BINDING=ON", command)
        self.assertIn("-DUSE_JEMALLOC=OFF", command)
        self.assertIn("-DUSE_AVX=OFF", command)
        self.assertIn("-DUSE_AVX512F=OFF", command)
        self.assertIn("-DSSD_ROCKSDB_EXPERIMENTAL=OFF", command)
        self.assertIn("-DMONO_EXECUTABLE=/work/host-tools/bin/mono", command)
        self.assertIn("-DMCS_EXECUTABLE=/work/host-tools/bin/mcs", command)
        self.assertNotIn("-DWITH_TLS=ON", command)
        self.assertNotIn("-DBUILD_SHARED_LIBS=ON", command)
        self.assertNotIn("-DWITH_ROCKSDB_EXPERIMENTAL=OFF", command)
        self.assertNotIn("=BOTH", joined)

    def test_tool_environment_finds_compiler_private_host_libraries(self):
        with tempfile.TemporaryDirectory() as directory:
            toolchain = Path(directory) / "toolchain"
            compiler = toolchain / "usr/bin/riscv64-linux-gnu-g++-13"
            host_libraries = toolchain / "usr/lib/x86_64-linux-gnu"
            compiler.parent.mkdir(parents=True)
            compiler.touch()
            host_libraries.mkdir(parents=True)

            environment = build_fdb_riscv.tool_environment(
                compiler,
                {
                    "PATH": "/usr/bin:/bin",
                    "LD_LIBRARY_PATH": "/uncontrolled/host/path",
                    "LANG": "zh_CN.UTF-8",
                },
            )

        self.assertEqual(environment["PATH"], "/usr/bin:/bin")
        self.assertEqual(environment["LD_LIBRARY_PATH"], str(host_libraries))
        self.assertEqual(environment["LC_ALL"], "C")
        self.assertEqual(environment["LANG"], "C")

    def test_tool_environment_omits_unresolved_ambient_library_path(self):
        environment = build_fdb_riscv.tool_environment(
            Path("/usr/bin/riscv64-linux-gnu-g++"),
            {"PATH": "/usr/bin:/bin", "LD_LIBRARY_PATH": "/tmp/host-leak"},
        )
        self.assertNotIn("LD_LIBRARY_PATH", environment)

    def test_attempt_id_cannot_escape_the_output_tree(self):
        for attempt_id in ("../escape", "nested/path", "", ".hidden"):
            with self.subTest(attempt_id=attempt_id):
                with self.assertRaises(build_fdb_riscv.BuildError):
                    build_fdb_riscv.validate_attempt_id(attempt_id)
        self.assertEqual(
            build_fdb_riscv.validate_attempt_id("configure-002"),
            "configure-002",
        )

    def test_failed_command_records_output_and_exit_status(self):
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "attempt.log"
            with self.assertRaises(build_fdb_riscv.BuildError):
                build_fdb_riscv._run(
                    [
                        sys.executable,
                        "-c",
                        "print('first boundary', flush=True); raise SystemExit(7)",
                    ],
                    log=log,
                )
            recorded = log.read_text()
        self.assertIn("first boundary", recorded)
        self.assertIn("EXIT 7", recorded)

    def test_rewrite_manifest_keeps_every_artifact_in_new_sysroot(self):
        rewritten = build_fdb_riscv.rewrite_manifest_paths(
            valid_bundle(),
            Path("/opt/riscv/sysroot"),
            Path("/work/3FS/out/cxl-riscv/fdb/import/sysroot"),
        )
        self.assertEqual(
            rewritten["fdbserver"]["path"],
            "/work/3FS/out/cxl-riscv/fdb/import/sysroot/usr/bin/fdbserver",
        )
        self.assertEqual(
            rewritten["dependencies"][0]["path"],
            "/work/3FS/out/cxl-riscv/fdb/import/sysroot/usr/lib/libstdc++.so.6",
        )
        self.assertEqual(
            verify_fdb_bundle.verify(rewritten, self.lock, check_files=False), []
        )

    def test_runtime_dependency_lookup_preserves_the_needed_filename(self):
        with tempfile.TemporaryDirectory() as directory:
            sysroot = Path(directory) / "sysroot"
            library_directory = sysroot / "usr/lib/riscv64-linux-gnu"
            library_directory.mkdir(parents=True)
            implementation = library_directory / "libexample.so.1.2"
            implementation.write_bytes(b"target library")
            soname = library_directory / "libexample.so.1"
            soname.symlink_to(implementation.name)

            found = build_fdb_riscv.find_target_runtime_file(
                sysroot, "libexample.so.1"
            )

        self.assertEqual(
            found.relative_to(sysroot).as_posix(),
            "usr/lib/riscv64-linux-gnu/libexample.so.1",
        )

    def test_runtime_dependency_lookup_rejects_distinct_ambiguous_files(self):
        with tempfile.TemporaryDirectory() as directory:
            sysroot = Path(directory) / "sysroot"
            for relative in ("lib", "usr/lib"):
                destination = sysroot / relative / "libambiguous.so"
                destination.parent.mkdir(parents=True)
                destination.write_text(relative)

            with self.assertRaises(build_fdb_riscv.BuildError):
                build_fdb_riscv.find_target_runtime_file(
                    sysroot, "libambiguous.so"
                )

    def test_staged_runtime_file_keeps_loader_visible_name(self):
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            source_sysroot = temporary / "source"
            target_sysroot = temporary / "target"
            library_directory = source_sysroot / "lib/riscv64-linux-gnu"
            library_directory.mkdir(parents=True)
            implementation = library_directory / "libexample.so.1.2"
            implementation.write_bytes(b"target library")
            soname = library_directory / "libexample.so.1"
            soname.symlink_to(implementation.name)

            staged = build_fdb_riscv.copy_target_runtime_file(
                soname, source_sysroot, target_sysroot
            )

            self.assertEqual(staged.read_bytes(), b"target library")
            self.assertFalse(staged.is_symlink())
            self.assertEqual(
                staged.relative_to(target_sysroot).as_posix(),
                "lib/riscv64-linux-gnu/libexample.so.1",
            )

    def test_stage_built_bundle_copies_recursive_riscv_runtime_closure(self):
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            source = temporary / "source"
            build = temporary / "build"
            runtime_sysroot = temporary / "runtime-sysroot"
            output_root = temporary / "out"
            (source / "bindings/c/foundationdb").mkdir(parents=True)
            (build / "bin").mkdir(parents=True)
            (build / "lib").mkdir(parents=True)
            (build / "bindings/c/foundationdb").mkdir(parents=True)
            (output_root / "attempts/test-stage").mkdir(parents=True)

            fdbserver = build / "bin/fdbserver"
            fdbserver.write_bytes(b"fdbserver")
            fdbserver.chmod(0o755)
            (build / "lib/libfdb_c.so").write_bytes(b"fdb client")
            (source / "bindings/c/foundationdb/fdb_c.h").write_text("c api")
            (source / "bindings/c/foundationdb/fdb_c_types.h").write_text(
                "c types"
            )
            (
                build / "bindings/c/foundationdb/fdb_c_options.g.h"
            ).write_text("c options")
            (
                build / "bindings/c/foundationdb/fdb_c_apiversion.g.h"
            ).write_text("c api version")

            runtime_files = (
                "lib/ld-linux-riscv64-lp64d.so.1",
                "usr/lib/riscv64-linux-gnu/libstdc++.so.6",
                "lib/riscv64-linux-gnu/libgcc_s.so.1",
                "lib/riscv64-linux-gnu/libc.so.6",
            )
            for relative in runtime_files:
                target = runtime_sysroot / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(relative.encode())

            def fake_elf(path):
                name = Path(path).name
                interpreter = (
                    "/lib/ld-linux-riscv64-lp64d.so.1"
                    if name == "fdbserver"
                    else None
                )
                needed = {
                    "fdbserver": ("libstdc++.so.6",),
                    "libfdb_c.so": ("libstdc++.so.6",),
                    "libstdc++.so.6": ("libgcc_s.so.1", "libc.so.6"),
                }.get(name, ())
                return build_fdb_riscv.preflight.ElfInfo(
                    machine=243,
                    elf_class=2,
                    byte_order=1,
                    interpreter=interpreter,
                    needed=needed,
                )

            with mock.patch.object(
                build_fdb_riscv.preflight, "inspect_elf", side_effect=fake_elf
            ):
                manifest_path = build_fdb_riscv.stage_built_bundle(
                    source=source,
                    build=build,
                    source_sysroot=runtime_sysroot,
                    output_root=output_root,
                    lock=self.lock,
                    attempt_id="test-stage",
                )

            manifest = json.loads(manifest_path.read_text())
            self.assertTrue(Path(manifest["fdbserver"]["path"]).is_file())
            self.assertTrue(Path(manifest["libfdb_c"]["path"]).is_file())
            self.assertEqual(
                {
                    Path(record["path"]).relative_to(manifest["sysroot"]).as_posix()
                    for record in manifest["dependencies"]
                },
                set(runtime_files),
            )
            self.assertFalse(
                (output_root / "attempts/test-stage/bundle-staging").exists()
            )

            incomplete = copy.deepcopy(manifest)
            incomplete["dependencies"] = [
                record
                for record in incomplete["dependencies"]
                if not record["path"].endswith("/libc.so.6")
            ]
            with mock.patch.object(
                build_fdb_riscv.preflight, "inspect_elf", side_effect=fake_elf
            ):
                errors = verify_fdb_bundle.verify(
                    incomplete, self.lock, check_files=True
                )
            self.assertIn(
                "FDB recursive target dependency is absent from the manifest",
                errors,
            )

    def test_riscv_c_api_generator_uses_argument_preserving_pic_tail_call(self):
        source_root = PROJECT_ROOT / "third_party/foundationdb"
        generator = source_root / "bindings/c/generate_asm.py"
        cmake = (source_root / "bindings/c/CMakeLists.txt").read_text()
        self.assertIn('set(cpu "riscv64")', cmake)
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            api_source = temporary / "api.cpp"
            assembly = temporary / "api.S"
            header = temporary / "api.h"
            api_source.write_text("FDB_API_CHANGED(fdb_test_api, 14)\n")
            subprocess.run(
                [
                    sys.executable,
                    str(generator),
                    "linux",
                    "riscv64",
                    str(api_source),
                    str(assembly),
                    str(header),
                ],
                check=True,
            )
            generated = assembly.read_text()
        self.assertNotIn(".intel_syntax", generated)
        self.assertIn(".option pic", generated)
        self.assertIn("la t0, fdb_api_ptr_fdb_test_api", generated)
        self.assertIn("ld t0, 0(t0)", generated)
        self.assertIn("jr t0", generated)
        for argument_register in ("a0", "a1", "a2", "a3", "a4", "a5", "a6", "a7"):
            self.assertNotIn(argument_register, generated)

    def test_riscv_art_node16_keeps_x86_simd_and_has_scalar_operations(self):
        source_root = PROJECT_ROOT / "third_party/foundationdb/fdbserver/include/fdbserver"
        interface = (source_root / "art.h").read_text()
        implementation = (source_root / "art_impl.h").read_text()
        self.assertIn("#if !defined(__riscv)", interface)
        self.assertGreaterEqual(implementation.count("#if defined(__riscv)"), 4)
        for operation in (
            "p.p2->keys[i] == c",
            "p.p2->keys[i] > c",
            "n->keys[idx] < c",
            "p.p2->keys[i - 1] < c",
        ):
            self.assertIn(operation, implementation)
        self.assertIn("_mm_cmpeq_epi8", implementation)


if __name__ == "__main__":
    unittest.main()
