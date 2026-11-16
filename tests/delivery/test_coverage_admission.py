from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from robot_corpus.control_plane import Application
from robot_corpus.session.contracts import ContractError, DomainRecord
from robot_corpus.synchronization.timeline import (
    COVERAGE_METHODOLOGY,
    LEGACY_SCOPE_METHODOLOGY,
    coverage_lattice,
    legacy_coverage_lattice,
)


def rec(session_id: str, robot: str = "r1", part: str = "axle", **overrides) -> DomainRecord:
    payload = {"robot_id": robot, "part_family": part, "grip_angle": 0, "deformation": 0,
               "illumination": 100, "dust_level": 0, "camera_fps": 30, "force_hz": 30,
               "clock_skew_ms": 0, "dropped_frames": 0, "operator_id": "op",
               "consent_scope": "training"}
    payload.update(overrides)
    return DomainRecord(session_id, payload)


class CoverageAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.application = Application(Path(self.temp.name) / "admission.sqlite3")
        self.first = rec("s1", robot="r1", part="axle")
        self.second = rec("s2", robot="r2", part="wheel")
        for record in (self.first, self.second):
            self.application.repository.save_record(record)

    def test_sealed_manifest_uses_traceable_v2_scope(self):
        manifest = self.application.publish_dataset("batch", ["s1", "s2"])
        self.assertEqual(manifest["coverage_methodology"], COVERAGE_METHODOLOGY)
        self.assertFalse(manifest["legacy_methodology"])
        self.assertEqual(manifest["coverage"], coverage_lattice(manifest["items"]))
        self.assertEqual(manifest["coverage"]["applicable_triple_count"], 4)
        self.assertEqual(manifest["coverage"]["distinct_covered_triple_count"], 2)

    def test_admission_floor_blocks_insufficient_effective_coverage(self):
        with self.assertRaisesRegex(ContractError, "below admission floor"):
            self.application.publish_dataset("blocked", ["s1", "s2"], min_effective_coverage=0.6)
        with self.assertRaisesRegex(ContractError, "unknown sealed dataset"):
            self.application.dataset_manifest("blocked")

    def test_admission_floor_must_be_a_ratio(self):
        with self.assertRaisesRegex(ContractError, "min_effective_coverage"):
            self.application.publish_dataset("bad", ["s1", "s2"], min_effective_coverage=1.5)

    def test_reseal_with_same_evidence_remains_idempotent(self):
        first = self.application.publish_dataset("batch", ["s1", "s2"], min_effective_coverage=0.5)
        second = self.application.publish_dataset("batch", ["s2", "s1"], min_effective_coverage=0.5)
        self.assertEqual(first, second)


class LegacyCoverageMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.database = Path(self.temp.name) / "legacy.sqlite3"
        self.good = rec("healthy")
        self.bad = rec("isolated", grip_angle=60, clock_skew_ms=250)
        application = Application(self.database)
        application.repository.save_record(self.good)
        application.repository.save_record(self.bad)
        self._downgrade_to_legacy_schema()

    def _downgrade_to_legacy_schema(self) -> None:
        v1 = legacy_coverage_lattice([self.good, self.bad])
        with sqlite3.connect(self.database) as connection:
            connection.execute("ALTER TABLE sealed_datasets RENAME TO sealed_datasets_v1")
            connection.execute("""CREATE TABLE sealed_datasets(
                dataset_id TEXT PRIMARY KEY, snapshot_hash TEXT NOT NULL, item_count INTEGER NOT NULL,
                coverage_json TEXT NOT NULL, created_at TEXT NOT NULL)""")
            connection.execute(
                "INSERT INTO sealed_datasets VALUES(?,?,?,?,?)",
                ("old-batch", "legacy-snapshot", 2, json.dumps(v1), "2026-01-01T00:00:00+00:00"))
            items = (("healthy", self.good, [0, 0, 1, 0], 0),
                     ("isolated", self.bad, [4, 0, 1, 0], 1))
            for session_id, record, condition, quarantined in items:
                connection.execute(
                    "INSERT INTO sealed_dataset_items(dataset_id,session_id,session_version,"
                    "record_fingerprint,robot_id,part_family,condition_json,quarantined) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    ("old-batch", session_id, 1, record.fingerprint,
                     record.payload["robot_id"], record.payload["part_family"],
                     json.dumps(condition), quarantined))
            connection.execute("DROP TABLE sealed_datasets_v1")
        self.legacy_coverage = v1

    def test_legacy_dataset_reads_under_original_scope_without_rewrite(self):
        application = Application(self.database)
        manifest = application.dataset_manifest("old-batch")
        self.assertTrue(manifest["legacy_methodology"])
        self.assertEqual(manifest["coverage_methodology"], LEGACY_SCOPE_METHODOLOGY)
        self.assertEqual(manifest["coverage"], self.legacy_coverage)
        with sqlite3.connect(self.database) as connection:
            row = connection.execute(
                "SELECT coverage_json,coverage_methodology FROM sealed_datasets WHERE dataset_id='old-batch'"
            ).fetchone()
        self.assertEqual(json.loads(row[0]), self.legacy_coverage)
        self.assertIsNone(row[1])

    def test_migration_explains_change_by_session_and_quality_judgement(self):
        application = Application(self.database)
        report = application.migrate_dataset_coverage("old-batch")
        self.assertEqual(report["stored_methodology"], LEGACY_SCOPE_METHODOLOGY)
        self.assertEqual(report["target_methodology"], COVERAGE_METHODOLOGY)
        self.assertEqual(report["change_nature"], "legacy-v1-recomputed-under-v2")
        # 历史数字原样保留，v2 为独立重算结果。
        self.assertEqual(report["stored_coverage_preserved"], self.legacy_coverage)
        self.assertEqual(report["legacy_v1_recomputed"], self.legacy_coverage)
        v2 = report["v2_recomputed"]
        self.assertEqual(v2["methodology"], COVERAGE_METHODOLOGY)
        self.assertEqual(v2["quarantined_session_ids"], ["isolated"])
        self.assertEqual(v2["distinct_covered_triple_count"], 1)
        self.assertEqual(
            {item["session_id"]: item["quarantined"] for item in report["session_judgements"]},
            {"healthy": False, "isolated": True})
        # 迁移报告不产生任何封存行写入。
        with sqlite3.connect(self.database) as connection:
            row = connection.execute(
                "SELECT coverage_json FROM sealed_datasets WHERE dataset_id='old-batch'").fetchone()
        self.assertEqual(json.loads(row[0]), self.legacy_coverage)

    def test_v2_dataset_migration_view_is_self_consistent(self):
        database = Path(self.temp.name) / "current.sqlite3"
        application = Application(database)
        application.repository.save_record(self.good)
        application.publish_dataset("new-batch", ["healthy"])
        report = application.migrate_dataset_coverage("new-batch")
        self.assertEqual(report["change_nature"], "already-on-v2")
        self.assertEqual(report["v2_recomputed"], coverage_lattice(report["session_judgements"]))


if __name__ == "__main__":
    unittest.main()
