from __future__ import annotations

import json
import sqlite3
from typing import Any, Iterable

from robot_corpus.session.contracts import ContractError, DomainRecord, canonical_json, stable_hash, utc_now
from robot_corpus.synchronization.timeline import (
    COVERAGE_METHODOLOGY,
    LEGACY_COVERAGE_METHODOLOGY,
    LEGACY_SCOPE_METHODOLOGY,
    alignment,
    condition_cell,
    coverage_lattice,
    coverage_review,
    legacy_coverage_lattice,
)


def prepare_datasets(connection: sqlite3.Connection) -> None:
    connection.execute("""CREATE TABLE IF NOT EXISTS sealed_datasets(
        dataset_id TEXT PRIMARY KEY, snapshot_hash TEXT NOT NULL, item_count INTEGER NOT NULL,
        coverage_json TEXT NOT NULL, created_at TEXT NOT NULL
    )""")
    connection.execute("""CREATE TABLE IF NOT EXISTS sealed_dataset_items(
        dataset_id TEXT NOT NULL, session_id TEXT NOT NULL, session_version INTEGER NOT NULL,
        record_fingerprint TEXT NOT NULL, robot_id TEXT NOT NULL, part_family TEXT NOT NULL,
        condition_json TEXT NOT NULL, quarantined INTEGER NOT NULL,
        PRIMARY KEY(dataset_id, session_id),
        FOREIGN KEY(dataset_id) REFERENCES sealed_datasets(dataset_id)
    )""")
    # 历史数据库没有口径列：幂等迁移，旧行的口径为 NULL，读取时按 v1 处理。
    existing = {row[1] for row in connection.execute("PRAGMA table_info(sealed_datasets)")}
    if "coverage_methodology" not in existing:
        connection.execute("ALTER TABLE sealed_datasets ADD COLUMN coverage_methodology TEXT")


def assemble_dataset(records: Iterable[DomainRecord]) -> dict[str, Any]:
    rows = list(records)
    if not rows or len({row.record_id for row in rows}) != len(rows):
        raise ContractError("dataset requires unique capture sessions")
    items = [{"session_id": row.record_id, "session_version": row.version,
              "record_fingerprint": row.fingerprint,
              "robot_id": str(row.payload.get("robot_id", "unknown")),
              "part_family": str(row.payload.get("part_family", "unknown")),
              "condition": list(condition_cell(row)),
              "quarantined": alignment(row).quarantine} for row in rows]
    items.sort(key=lambda item: item["session_id"])
    return {"items": items, "coverage": coverage_lattice(rows)}


def _dataset_items(connection: sqlite3.Connection, dataset_id: str) -> list[dict[str, Any]]:
    rows = [dict(row) for row in connection.execute(
        "SELECT * FROM sealed_dataset_items WHERE dataset_id=? ORDER BY session_id", (dataset_id,))]
    for item in rows:
        item["condition"] = json.loads(item.pop("condition_json"))
        item["quarantined"] = bool(item["quarantined"])
    return rows


def seal_dataset(connection: sqlite3.Connection, dataset_id: str, records: Iterable[DomainRecord],
                 min_effective_coverage: float = 0.0) -> dict[str, Any]:
    prepare_datasets(connection)
    assembled = assemble_dataset(records)
    coverage = assembled["coverage"]

    # 会话门禁：封存准入使用同一覆盖口径，并要求摘要可由条目重新计算（可追溯的同一口径）。
    recomputed = coverage_lattice(assembled["items"])
    if canonical_json(recomputed) != canonical_json(coverage):
        raise ContractError("coverage summary cannot be reproduced from dataset items")
    if not 0.0 <= float(min_effective_coverage) <= 1.0:
        raise ContractError("min_effective_coverage must be within [0, 1]")
    if coverage["effective_coverage"] < float(min_effective_coverage):
        raise ContractError(
            f"effective coverage {coverage['effective_coverage']:.4f} below admission floor "
            f"{float(min_effective_coverage):.4f} under {coverage['methodology']}")

    identity = stable_hash(assembled)
    existing = connection.execute(
        "SELECT snapshot_hash,coverage_methodology FROM sealed_datasets WHERE dataset_id=?",
        (dataset_id,)).fetchone()
    if existing:
        if existing[0] != identity:
            raise ContractError("a sealed dataset cannot change after publication")
        return read_dataset(connection, dataset_id)
    connection.execute(
        "INSERT INTO sealed_datasets(dataset_id,snapshot_hash,item_count,coverage_json,created_at,coverage_methodology) VALUES(?,?,?,?,?,?)",
        (dataset_id, identity, len(assembled["items"]),
         canonical_json(coverage), utc_now(), coverage["methodology"]))
    for item in assembled["items"]:
        connection.execute("INSERT INTO sealed_dataset_items VALUES(?,?,?,?,?,?,?,?)",
                           (dataset_id, item["session_id"], item["session_version"],
                            item["record_fingerprint"], item["robot_id"], item["part_family"],
                            canonical_json(item["condition"]), int(item["quarantined"])))
    return read_dataset(connection, dataset_id)


def read_dataset(connection: sqlite3.Connection, dataset_id: str) -> dict[str, Any]:
    prepare_datasets(connection)
    header = connection.execute("SELECT * FROM sealed_datasets WHERE dataset_id=?", (dataset_id,)).fetchone()
    if header is None:
        raise ContractError("unknown sealed dataset")
    items = _dataset_items(connection, dataset_id)
    stored_methodology = header["coverage_methodology"] if "coverage_methodology" in header.keys() else None
    methodology = stored_methodology or LEGACY_SCOPE_METHODOLOGY
    coverage = json.loads(header["coverage_json"])
    # 旧数据集保持原口径数字：方法论标记只放在外层，绝不静默改写历史结果。
    return {"dataset_id": dataset_id, "snapshot_hash": header["snapshot_hash"],
            "created_at": header["created_at"], "coverage": coverage,
            "coverage_methodology": methodology,
            "legacy_methodology": stored_methodology is None,
            "item_count": header["item_count"], "items": items}


def migrate_coverage_view(connection: sqlite3.Connection, dataset_id: str) -> dict[str, Any]:
    """对历史封存数据集做口径迁移复核：原 v1 数字保留，新增 v2 重算与逐项差异说明。

    不修改任何已封存行；迁移结果说明变化来自哪些会话及质量判断（隔离/重复/补齐）。
    """
    prepare_datasets(connection)
    header = connection.execute(
        "SELECT coverage_json,coverage_methodology FROM sealed_datasets WHERE dataset_id=?",
        (dataset_id,)).fetchone()
    if header is None:
        raise ContractError("unknown sealed dataset")
    items = _dataset_items(connection, dataset_id)
    stored_coverage = json.loads(header["coverage_json"])
    stored_methodology = header["coverage_methodology"]
    v1 = legacy_coverage_lattice(items)
    v2 = coverage_lattice(items)
    if stored_methodology == COVERAGE_METHODOLOGY:
        # 已是新口径：核对封存摘要仍可重放。
        if canonical_json(stored_coverage) != canonical_json(v2):
            raise ContractError("sealed v2 coverage no longer reproduces from stored items")
        review = coverage_review(v2, v2)
        change_nature = "already-on-v2"
    else:
        # 旧口径：用 v1 快照与 v2 快照做同口径不可比时的结构化对照（分别重算，不做跨口径比值）。
        legacy_snapshot = dict(v1)
        legacy_snapshot["methodology"] = LEGACY_COVERAGE_METHODOLOGY
        legacy_snapshot["covered_triples"] = []
        legacy_snapshot["robots"] = sorted({item["robot_id"] for item in items})
        legacy_snapshot["parts"] = sorted({item["part_family"] for item in items})
        review = {
            "methodology": COVERAGE_METHODOLOGY,
            "legacy_v1_summary": legacy_snapshot,
            "v2_summary": v2,
            "newly_covered_count": v2["distinct_covered_triple_count"],
            "newly_covered": v2["covered_triples"],
            "duplicate_session_count": v2["duplicate_session_count"],
            "duplicate_session_ids": v2["duplicate_session_ids"],
            "quarantined_session_ids": v2["quarantined_session_ids"],
            "inapplicable_slot_count": v2["inapplicable_slot_count"],
            "denominator_legacy_v1": max(
                1, v2["observed_robot_count"] * v2["observed_part_count"] * 4),
            "denominator_v2": v2["applicable_triple_count"],
        }
        change_nature = "legacy-v1-recomputed-under-v2"
    return {
        "dataset_id": dataset_id,
        "stored_methodology": stored_methodology or LEGACY_SCOPE_METHODOLOGY,
        "target_methodology": COVERAGE_METHODOLOGY,
        "change_nature": change_nature,
        "stored_coverage_preserved": stored_coverage,
        "legacy_v1_recomputed": v1,
        "v2_recomputed": v2,
        "review": review,
        "session_judgements": [
            {"session_id": item["session_id"], "quarantined": item["quarantined"],
             "robot_id": item["robot_id"], "part_family": item["part_family"],
             "condition": item["condition"]} for item in items
        ],
        "recomputed_at": utc_now(),
    }
