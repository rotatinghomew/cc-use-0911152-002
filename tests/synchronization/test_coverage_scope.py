from __future__ import annotations

import unittest

from robot_corpus.session.contracts import DomainRecord
from robot_corpus.synchronization.timeline import (
    COVERAGE_METHODOLOGY,
    LEGACY_COVERAGE_METHODOLOGY,
    coverage_lattice,
    coverage_review,
    legacy_coverage_lattice,
)


def rec(session_id: str, robot: str, part: str, angle: float = 32.0, **overrides) -> DomainRecord:
    payload = {"robot_id": robot, "part_family": part, "grip_angle": angle,
               "deformation": 4, "illumination": 500, "dust_level": 8, "camera_fps": 30,
               "force_hz": 100, "clock_skew_ms": 5, "dropped_frames": 0,
               "operator_id": "op", "consent_scope": "training"}
    payload.update(overrides)
    return DomainRecord(session_id, payload)


class CoverageLatticeV2Tests(unittest.TestCase):
    def test_robot_part_and_condition_share_one_statistical_space(self):
        summary = coverage_lattice([rec("s1", "r1", "axle"), rec("s2", "r2", "wheel")])
        self.assertEqual(summary["methodology"], COVERAGE_METHODOLOGY)
        # 2 机器人 × 2 零件 × 1 工况 = 4 个适用三元组，观测只覆盖其中 2 个。
        self.assertEqual(summary["applicable_triple_count"], 4)
        self.assertEqual(summary["distinct_covered_triple_count"], 2)
        self.assertEqual(summary["missing_slot_count"], 2)
        self.assertEqual(summary["effective_coverage"], 0.5)
        self.assertEqual(summary["inapplicable_slot_count"], 0)

    def test_filling_missing_combinations_is_distinct_from_duplicate_capture(self):
        before = [rec("s1", "r1", "axle"), rec("s2", "r2", "wheel")]
        after = before + [
            rec("s3", "r1", "wheel"),       # 真正补齐的组合
            rec("s4", "r2", "axle"),        # 真正补齐的组合
            rec("s1-dup", "r1", "axle"),    # 同一三元组的重复采集
        ]
        summary = coverage_lattice(after)
        self.assertEqual(summary["distinct_covered_triple_count"], 4)
        self.assertEqual(summary["duplicate_session_count"], 1)
        self.assertEqual(summary["duplicate_session_ids"], ["s1-dup"])
        self.assertEqual(summary["effective_session_count"], 4)
        self.assertEqual(summary["effective_coverage"], 1.0)

        review = coverage_review(before, after)
        self.assertEqual(review["newly_covered_count"], 2)
        self.assertEqual(
            {(item["robot_id"], item["part_family"]) for item in review["newly_covered"]},
            {("r1", "wheel"), ("r2", "axle")})
        self.assertEqual(review["duplicate_session_ids"], ["s1-dup"])
        # 设备与零件范围未变，分母保持不变，比例变化完全来自新增覆盖。
        self.assertEqual(review["denominator_before"], review["denominator_after"])
        self.assertEqual(review["denominator_delta"], 0)

    def test_changing_robot_composition_changes_scope_not_silently_the_ratio(self):
        before = [rec("s1", "r1", "axle"), rec("s2", "r2", "wheel")]
        after = [rec("t1", "r9", "axle"), rec("t2", "r8", "wheel")]
        review = coverage_review(before, after)
        self.assertEqual(review["scope_robots_before"], ["r1", "r2"])
        self.assertEqual(review["scope_robots_after"], ["r8", "r9"])
        # 分母变化被显式解释，而不是给出无法解释的不同结论。
        self.assertEqual(review["denominator_before"], review["denominator_after"])
        self.assertEqual(review["effective_coverage_before"], review["effective_coverage_after"])

    def test_quarantined_sessions_keep_identity_but_not_effective_coverage(self):
        rows = [rec("q1", "r1", "axle", clock_skew_ms=250), rec("s1", "r1", "axle")]
        summary = coverage_lattice(rows)
        self.assertEqual(summary["quarantined_session_count"], 1)
        self.assertEqual(summary["quarantined_session_ids"], ["q1"])
        self.assertEqual(summary["distinct_covered_triple_count"], 1)
        self.assertEqual(summary["distinct_quarantined_triple_count"], 1)
        self.assertEqual(summary["effective_session_count"], 1)

    def test_declared_inapplicable_space_is_excluded_from_denominator(self):
        rows = [rec("s1", "r1", "axle"), rec("s2", "r1", "wheel"), rec("s3", "r2", "wheel")]
        scope = {"inapplicable_pairs": [{"robot_id": "r2", "part_family": "axle"}]}
        summary = coverage_lattice(rows, applicability_scope=scope)
        self.assertEqual(summary["applicable_pair_count"], 3)
        self.assertEqual(summary["inapplicable_pair_count"], 1)
        # 不适用位置只按工况单元数折算，不进入分母。
        self.assertEqual(summary["inapplicable_slot_count"], summary["observed_condition_count"])
        self.assertEqual(summary["applicable_triple_count"], 3 * summary["observed_condition_count"])

    def test_condition_cells_expand_the_shared_space(self):
        rows = [rec("s1", "r1", "axle", angle=0), rec("s2", "r1", "axle", angle=60)]
        summary = coverage_lattice(rows)
        self.assertEqual(summary["observed_condition_count"], 2)
        self.assertEqual(summary["applicable_triple_count"], 2)
        self.assertEqual(summary["distinct_covered_triple_count"], 2)

    def test_review_rejects_mixed_methodologies(self):
        legacy = legacy_coverage_lattice([rec("s1", "r1", "axle")])
        with self.assertRaisesRegex(ValueError, "methodology mismatch"):
            coverage_review(legacy, coverage_lattice([rec("s1", "r1", "axle")]))

    def test_legacy_lattice_remains_available_for_historical_recheck(self):
        rows = [rec("s1", "r1", "axle"), rec("s2", "r2", "wheel")]
        legacy = legacy_coverage_lattice(rows)
        summary = coverage_lattice(rows)
        self.assertNotIn("methodology", legacy)
        self.assertEqual(summary["legacy_v1"]["coverage"], legacy["coverage"])
        self.assertEqual(summary["legacy_v1"]["cell_count"], legacy["cell_count"])
        self.assertEqual(LEGACY_COVERAGE_METHODOLOGY, "robot-part-condition.v1")


if __name__ == "__main__":
    unittest.main()
