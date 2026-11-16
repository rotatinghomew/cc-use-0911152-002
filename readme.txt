工业机器人具身数据采集编排中心

数据集封存：Application.publish_dataset(dataset_id, session_ids, min_effective_coverage=0.0)
从已登记采集会话建立可重放的封存数据集，Application.dataset_manifest(dataset_id) 读取当时
的会话版本、质量隔离标记、工况单元与覆盖摘要。重复封存相同证据保持幂等；
不同证据不得覆盖同一个已发布的数据集。清单与采集记录共用运行时数据库。

覆盖口径（robot-part-condition.v2）：覆盖单位是 (机器人, 零件, 工况单元) 三元组，
机器人、零件与工况共享同一统计空间。分母 = 已观测机器人 × 已观测零件 × 已观测工况单元，
可通过 applicability_scope 显式声明不适用配对（inapplicable_pairs / inapplicable_robots /
inapplicable_parts），不适用位置不计入分母。摘要区分：
- distinct_covered_triple_count：去重后真正被有效会话覆盖的组合（新增有效覆盖的身份）；
- duplicate_session_count / duplicate_session_ids：落入已覆盖三元组的重复采集；
- missing_slot_count：适用但尚未补齐的位置；
- inapplicable_slot_count：显式声明不适用、不要求补采的位置；
- quarantined_session_*：质量隔离会话，保留清单但不充当有效覆盖。
coverage_review(before, after) 对比两次采集，给出新增/丢失覆盖、重复采集与分母变化解释；
跨口径比较会被拒绝。会话门禁（derive_context）、运行汇总（coverage.json / summary.json）
与封存清单引用同一 methodology 标记，且封存时摘要必须能由清单条目重新计算，否则拒绝准入。

历史数据集迁移：旧库读取时标记为 robot-part-condition.v1.legacy，原始 v1 数字保持不变；
Application.migrate_dataset_coverage(dataset_id)（CLI: migrate-coverage）在不写入、不覆盖
历史行的前提下，按 v2 重算并输出变化来自哪些会话及其质量判断（隔离/重复/补齐）。

运行验收：
python3 -m robot_corpus run --fixture fixtures/acceptance.json --database /tmp/robot_corpus.sqlite3 --output /tmp/robot_corpus-output

核验：
python3 -m robot_corpus verify --database /tmp/robot_corpus.sqlite3 --output /tmp/robot_corpus-output

全量测试：
python3 -m unittest discover -s tests -v

按模块测试：
python3 -m unittest tests.synchronization.test_quality_gates -v

构建检查：
python3 -m compileall -q robot_corpus tests
