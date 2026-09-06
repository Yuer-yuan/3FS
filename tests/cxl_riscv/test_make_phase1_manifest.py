from __future__ import annotations

import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "deploy" / "cxl-riscv"
sys.path.insert(0, str(DEPLOY))

import make_phase1_manifest as manifest_tool


class Phase1ManifestTest(unittest.TestCase):
    def setUp(self) -> None:
        self.topology = manifest_tool.load_topology(
            DEPLOY / "phase1_topology.json"
        )

    def build(self, **updates: object) -> dict[str, object]:
        args = {
            "scenario": "chain",
            "replication_factor": 3,
            "clients": 4,
            "session_generation": 91,
        }
        args.update(updates)
        return manifest_tool.build_manifest(self.topology, **args)

    def test_routes_are_unique_data_plane_bindings(self) -> None:
        manifest = self.build()
        participants = manifest["participants"]
        routes = manifest["routes"]
        participant_ids = {item["endpoint"] for item in participants}
        keys = {(item["address"], item["plane"]) for item in routes}
        self.assertEqual(len(keys), len(routes))
        self.assertTrue(all(item["plane"] == "Data" for item in routes))
        self.assertTrue(
            all(item["targetEndpoint"] in participant_ids for item in routes)
        )

    def test_rf_and_client_growth_do_not_renumber_existing_roles(self) -> None:
        small = self.build(replication_factor=1, clients=1)
        large = self.build(replication_factor=3, clients=4)
        small_by_address = {
            item["localAddress"]: item["endpoint"]
            for item in small["participants"]
        }
        large_by_address = {
            item["localAddress"]: item["endpoint"]
            for item in large["participants"]
        }
        for address, endpoint in small_by_address.items():
            self.assertEqual(large_by_address[address], endpoint)

    def test_layout_is_ordered_disjoint_and_satisfies_lane_geometry(self) -> None:
        manifest = self.build()
        ranges = manifest["ranges"]
        self.assertEqual(
            [item["kind"] for item in ranges], list(manifest_tool.RANGE_KINDS)
        )
        previous_end = 4096
        for item in ranges:
            self.assertGreaterEqual(item["offset"], previous_end)
            self.assertEqual(item["offset"] % 64, 0)
            self.assertEqual(item["length"] % 64, 0)
            previous_end = item["offset"] + item["length"]
        self.assertLessEqual(previous_end, manifest["totalRegionBytes"])
        self.assertGreaterEqual(manifest["laneCount"], manifest["endpointCount"])
        self.assertEqual(ranges[2]["length"] // manifest["laneCount"], 4 * 4096)

    def test_digest_is_deterministic_and_binds_scenario(self) -> None:
        first = self.build()
        second = self.build()
        other = self.build(scenario="storage", replication_factor=1, clients=1)
        self.assertEqual(first["manifestSha256"], second["manifestSha256"])
        self.assertNotEqual(first["manifestSha256"], other["manifestSha256"])
        self.assertEqual(len(first["manifestSha256"]), 64)

    def test_invalid_capacity_and_duplicate_endpoint_fail_closed(self) -> None:
        with self.assertRaises(manifest_tool.ManifestError):
            self.build(total_region_bytes=16 * 1024 * 1024)
        duplicate = copy.deepcopy(self.topology)
        duplicate["roles"]["meta"]["endpoint"] = 2
        with self.assertRaises(manifest_tool.ManifestError):
            manifest_tool.build_manifest(
                duplicate,
                scenario="storage",
                replication_factor=1,
                clients=1,
                session_generation=1,
            )

    def test_writer_refuses_to_replace_unowned_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "manifest.json"
            output.write_text("{}\n", encoding="utf-8")
            with self.assertRaises(manifest_tool.ManifestError):
                manifest_tool.write_manifest(output, self.build(), True)

    def test_role_address_override_binds_run_owned_network(self) -> None:
        topology = manifest_tool.override_role_addresses(
            self.topology,
            ["mgmtd=CXL://127.0.0.1:12501", "admin=CXL://127.0.0.1:12507"],
        )
        manifest = manifest_tool.build_manifest(
            topology,
            scenario="storage",
            replication_factor=1,
            clients=1,
            session_generation=3,
        )
        participants = {item["endpoint"]: item["localAddress"] for item in manifest["participants"]}
        self.assertEqual(participants[2], "CXL://127.0.0.1:12501")
        self.assertEqual(participants[7], "CXL://127.0.0.1:12507")
        with self.assertRaises(manifest_tool.ManifestError):
            manifest_tool.override_role_addresses(self.topology, ["missing=CXL://127.0.0.1:1"])


if __name__ == "__main__":
    unittest.main()
