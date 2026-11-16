from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from robot_corpus.session.contracts import DomainRecord, bounded, finite_number, normalized_text


# 覆盖口径版本：v2 以 (机器人, 零件, 工况单元) 三元组作为唯一覆盖单位，
# 机器人、零件与工况共享同一个统计空间；v1 为历史封存数据集使用的旧口径。
COVERAGE_METHODOLOGY = "robot-part-condition.v2"
LEGACY_COVERAGE_METHODOLOGY = "robot-part-condition.v1"
LEGACY_SCOPE_METHODOLOGY = "robot-part-condition.v1.legacy"


@dataclass(frozen=True)
class AlignmentWindow:
    session_id: str
    skew_ms: float
    frame_loss: float
    quality: float
    quarantine: bool


def alignment(record: DomainRecord) -> AlignmentWindow:
    skew = abs(finite_number(record.payload.get("clock_skew_ms", 0.0)))
    drops = max(0.0, finite_number(record.payload.get("dropped_frames", 0.0)))
    camera_rate = max(1.0, finite_number(record.payload.get("camera_fps", 1.0)))
    force_rate = max(1.0, finite_number(record.payload.get("force_hz", 1.0)))
    normalized_skew = bounded(skew / 250.0)
    loss = bounded(drops / (camera_rate * 30.0))
    rate_fit = bounded(min(camera_rate, force_rate) / max(camera_rate, force_rate))
    quality = bounded(rate_fit * 0.45 + (1.0 - normalized_skew) * 0.35 + (1.0 - loss) * 0.20)
    return AlignmentWindow(record.record_id, skew, loss, quality, quality < 0.55 or skew > 150.0)


def condition_cell(record: DomainRecord) -> tuple[int, int, int, int]:
    angle = int(finite_number(record.payload.get("grip_angle", 0.0)) // 15)
    deformation = int(finite_number(record.payload.get("deformation", 0.0)) // 5)
    illumination = int(finite_number(record.payload.get("illumination", 0.0)) // 100)
    dust = int(finite_number(record.payload.get("dust_level", 0.0)) // 10)
    return angle, deformation, illumination, dust


def _coverage_point(value: Any) -> dict[str, Any]:
    """把采集会话或封存清单条目归一成同一覆盖口径所需的字段。"""
    if isinstance(value, DomainRecord):
        payload = value.payload
        return {"session_id": value.record_id,
                "robot_id": str(payload.get("robot_id", "unknown")),
                "part_family": str(payload.get("part_family", "unknown")),
                "condition": condition_cell(value),
                "quarantined": alignment(value).quarantine}
    if isinstance(value, dict):
        condition = value.get("condition")
        if condition is None:
            raise ValueError("coverage item requires a condition cell")
        return {"session_id": str(value.get("session_id", "")),
                "robot_id": str(value.get("robot_id", "unknown")),
                "part_family": str(value.get("part_family", "unknown")),
                "condition": tuple(int(dimension) for dimension in condition),
                "quarantined": bool(value.get("quarantined", False))}
    raise TypeError("coverage inputs must be DomainRecord or coverage item mappings")


def legacy_coverage_lattice(records: Iterable[Any]) -> dict[str, float]:
    """旧口径（v1）：仅用于复核历史封存数据集，不得用于新的准入决策。"""
    rows = [_coverage_point(row) for row in records]
    cells = {row["condition"] for row in rows}
    robots = {row["robot_id"] for row in rows}
    parts = {row["part_family"] for row in rows}
    quarantined = sum(row["quarantined"] for row in rows)
    theoretical = max(1, len(robots) * len(parts) * 4)
    return {"cell_count": float(len(cells)), "robot_count": float(len(robots)),
            "part_count": float(len(parts)),
            "coverage": bounded(len(cells) / theoretical),
            "quarantine_rate": quarantined / max(1, len(rows))}


def coverage_lattice(records: Iterable[Any],
                     applicability_scope: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """统一覆盖口径（v2）。

    覆盖单位是 (robot_id, part_family, condition_cell) 三元组：机器人、零件与
    工况处于同一个统计空间，分母里的每个位置都能追溯到具体的机器人/零件组合。

    统计总体（分母）= 已观测机器人 × 已观测零件 × 已观测工况单元，再扣除显式
    声明为不适用的机器人/零件配对（由 applicability_scope 传入；无声明时默认全部
    适用，保证不同数据集之间可比较且分母可解释）。

    - observed_*：实际出现的机器人/零件/工况单元；
    - applicable_*：适用空间内的位置，是 effective_coverage 的分母；
    - inapplicable_*：被显式排除的机器人/零件组合，不计入分母也不要求补采；
    - distinct_covered_*：去重后真正被有效会话覆盖的三元组，重复采集不再抬高分子；
    - duplicate_session_*：落入已覆盖三元组的重复采集，与真正补齐的组合分开计数；
    - quarantined 会话保留在清单中，但单列，不充当有效覆盖。
    """
    rows = [_coverage_point(row) for row in records]
    robots = sorted({row["robot_id"] for row in rows})
    parts = sorted({row["part_family"] for row in rows})
    conditions = sorted({row["condition"] for row in rows})

    observed_pairs = {(row["robot_id"], row["part_family"]) for row in rows}
    excluded_pairs = _excluded_pairs(applicability_scope, robots, parts)
    # 不适用配对按声明统计：即使该组合当前没有会话，也从适用总体中排除并保留可追溯身份。
    inapplicable_pairs = sorted(excluded_pairs)
    universe_pairs = {(robot, part) for robot in robots for part in parts}
    applicable_pairs = sorted(universe_pairs - excluded_pairs)

    covered_keys: set[tuple[str, str, tuple[int, int, int, int]]] = set()
    quarantined_keys: set[tuple[str, str, tuple[int, int, int, int]]] = set()
    duplicate_sessions = 0
    duplicate_session_ids: list[str] = []
    quarantined_sessions = 0
    quarantine_session_ids: list[str] = []
    effective_session_ids: list[str] = []
    for row in rows:
        key = (row["robot_id"], row["part_family"], row["condition"])
        if row["quarantined"]:
            quarantined_sessions += 1
            quarantine_session_ids.append(row["session_id"])
            quarantined_keys.add(key)
            if key in covered_keys:
                duplicate_sessions += 1
                duplicate_session_ids.append(row["session_id"])
            continue
        if key in covered_keys:
            duplicate_sessions += 1
            duplicate_session_ids.append(row["session_id"])
            continue
        covered_keys.add(key)
        effective_session_ids.append(row["session_id"])

    full_universe = {(robot, part, condition)
                     for robot in robots for part in parts for condition in conditions}
    applicable_triples = {
        (robot, part, condition) for robot, part in applicable_pairs for condition in conditions}
    inapplicable_triples = full_universe - applicable_triples
    effective_covered = len(covered_keys)
    denominator = len(applicable_triples)
    effective_coverage = effective_covered / denominator if denominator else 0.0
    missing_slots = sorted(
        [{"robot_id": robot, "part_family": part, "condition": list(condition)}
         for robot, part, condition in applicable_triples
         if (robot, part, condition) not in covered_keys],
        key=lambda item: (item["robot_id"], item["part_family"], item["condition"]))

    # 向后兼容 v1 字段（同一次计算同时给出，便于历史数字对照），新增字段均为 int/结构化。
    legacy = legacy_coverage_lattice(rows)
    return {
        "methodology": COVERAGE_METHODOLOGY,
        "cell_count": legacy["cell_count"],
        "robot_count": legacy["robot_count"],
        "part_count": legacy["part_count"],
        "coverage": bounded(effective_coverage),
        "effective_coverage": bounded(effective_coverage),
        "quarantine_rate": quarantined_sessions / len(rows) if rows else 0.0,
        "session_count": len(rows),
        "observed_robot_count": len(robots),
        "observed_part_count": len(parts),
        "observed_condition_count": len(conditions),
        "observed_pair_count": len(observed_pairs),
        "applicable_pair_count": len(applicable_pairs),
        "inapplicable_pair_count": len(inapplicable_pairs),
        "applicable_triple_count": denominator,
        "inapplicable_slot_count": len(inapplicable_triples),
        "distinct_covered_triple_count": effective_covered,
        "distinct_quarantined_triple_count": len(quarantined_keys),
        "missing_slot_count": len(missing_slots),
        "duplicate_session_count": duplicate_sessions,
        "effective_session_count": len(effective_session_ids),
        "quarantined_session_count": quarantined_sessions,
        "robots": robots,
        "parts": parts,
        "conditions": [list(condition) for condition in conditions],
        "applicable_pairs": [{"robot_id": robot, "part_family": part} for robot, part in applicable_pairs],
        "inapplicable_pairs": [{"robot_id": robot, "part_family": part} for robot, part in inapplicable_pairs],
        "covered_triples": [{"robot_id": robot, "part_family": part, "condition": list(condition)}
                            for robot, part, condition in sorted(covered_keys)],
        "missing_slots": missing_slots,
        "duplicate_session_ids": sorted(duplicate_session_ids),
        "effective_session_ids": sorted(effective_session_ids),
        "quarantined_session_ids": sorted(quarantine_session_ids),
        "legacy_v1": {key: legacy[key] for key in
                      ("cell_count", "robot_count", "part_count", "coverage", "quarantine_rate")},
    }


def _excluded_pairs(applicability_scope: Mapping[str, Any] | None,
                    robots: Sequence[str], parts: Sequence[str]) -> set[tuple[str, str]]:
    """解析不适用空间声明：支持 inapplicable_pairs=[{robot_id, part_family}, ...]，
    或按机器人/零件整体排除（inapplicable_robots / inapplicable_parts）。"""
    if not applicability_scope:
        return set()
    robot_set, part_set = set(robots), set(parts)
    excluded: set[tuple[str, str]] = set()
    for item in applicability_scope.get("inapplicable_pairs", []) or []:
        pair = (str(item["robot_id"]), str(item["part_family"]))
        if pair[0] in robot_set and pair[1] in part_set:
            excluded.add(pair)
    for robot in applicability_scope.get("inapplicable_robots", []) or []:
        excluded.update((str(robot), part) for part in parts)
    for part in applicability_scope.get("inapplicable_parts", []) or []:
        excluded.update((robot, str(part)) for robot in robots)
    return excluded


def _index_triples(summary: Mapping[str, Any]) -> set[tuple[str, str, tuple[int, ...]]]:
    return {(item["robot_id"], item["part_family"], tuple(item["condition"]))
            for item in summary.get("covered_triples", [])}


def coverage_review(before: Sequence[Any] | Mapping[str, Any],
                    after: Sequence[Any] | Mapping[str, Any]) -> dict[str, Any]:
    """对比两次采集的覆盖快照，区分重复采集、真正补齐的组合与不适用空间。

    入参可以是记录/清单条目序列，也可以是 coverage_lattice 产出的快照字典；
    两者必须使用同一口径（methodology），否则拒绝比较，避免静默混用统计空间。
    """
    before_summary = coverage_lattice(before) if not isinstance(before, Mapping) else dict(before)
    after_summary = coverage_lattice(after) if not isinstance(after, Mapping) else dict(after)
    before_method = before_summary.get("methodology", LEGACY_SCOPE_METHODOLOGY)
    after_method = after_summary.get("methodology", LEGACY_SCOPE_METHODOLOGY)
    if before_method != after_method:
        raise ValueError(
            f"coverage methodology mismatch: {before_method!r} vs {after_method!r}; recompute under one scope")

    before_keys = _index_triples(before_summary)
    after_keys = _index_triples(after_summary)
    newly_covered = sorted(
        [{"robot_id": robot, "part_family": part, "condition": list(condition)}
         for robot, part, condition in after_keys - before_keys],
        key=lambda item: (item["robot_id"], item["part_family"], item["condition"]))
    lost_cover = sorted(
        [{"robot_id": robot, "part_family": part, "condition": list(condition)}
         for robot, part, condition in before_keys - after_keys],
        key=lambda item: (item["robot_id"], item["part_family"], item["condition"]))
    denominator_before = int(before_summary.get("applicable_triple_count", len(before_keys)))
    denominator_after = int(after_summary.get("applicable_triple_count", len(after_keys)))
    return {
        "methodology": after_method,
        "newly_covered_count": len(newly_covered),
        "newly_covered": newly_covered,
        "lost_coverage_count": len(lost_cover),
        "lost_coverage": lost_cover,
        "duplicate_session_count": int(after_summary.get("duplicate_session_count", 0)),
        "duplicate_session_ids": list(after_summary.get("duplicate_session_ids", [])),
        "inapplicable_slot_count": int(after_summary.get("inapplicable_slot_count", 0)),
        "denominator_before": denominator_before,
        "denominator_after": denominator_after,
        "denominator_delta": denominator_after - denominator_before,
        "effective_coverage_before": float(before_summary.get("effective_coverage",
                                                              before_summary.get("coverage", 0.0))),
        "effective_coverage_after": float(after_summary.get("effective_coverage",
                                                            after_summary.get("coverage", 0.0))),
        "scope_robots_before": list(before_summary.get("robots", [])),
        "scope_robots_after": list(after_summary.get("robots", [])),
        "scope_parts_before": list(before_summary.get("parts", [])),
        "scope_parts_after": list(after_summary.get("parts", [])),
    }


def sequence_health(records: Iterable[DomainRecord]) -> dict[str, float]:
    windows = [alignment(row) for row in records]
    qualities = [window.quality for window in windows]
    return {"mean_alignment": statistics.fmean(qualities) if qualities else 0.0,
            "alignment_jitter": statistics.pstdev(qualities) if len(qualities) > 1 else 0.0,
            "quarantined": float(sum(window.quarantine for window in windows))}


def derive_context(record: DomainRecord, repository: Any) -> dict[str, Any]:
    rows = repository.list_records() or [record]
    window = alignment(record)
    coverage = coverage_lattice(rows)
    health = sequence_health(rows)
    return {"sensor_alignment_weight": 0.6 + window.quality * 0.6,
            "condition_coverage_weight": 0.7 + coverage["effective_coverage"] * 0.5,
            "sequence_integrity_weight": 0.7 + health["mean_alignment"] * 0.4,
            "quarantine_pressure_weight": 0.7 + coverage["quarantine_rate"] * 0.8,
            "reuse_readiness_weight": 0.6 + (1.0 - health["alignment_jitter"]) * 0.5,
            "coverage_methodology": coverage["methodology"],
            "coverage_applicable_triple_count": coverage["applicable_triple_count"],
            "coverage_distinct_covered_triple_count": coverage["distinct_covered_triple_count"]}
