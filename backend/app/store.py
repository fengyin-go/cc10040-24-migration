"""内存数据仓库：给每个业务模块准备一份可筛选、可流转的示例数据。

真实项目里这里会换成数据库访问层；当前实现只依赖标准库，保证克隆下来就能起。
同时提供跨模块查询、数据流处理能力和服务指标引用迁移所需的审计存储。
"""
from __future__ import annotations

import copy
import hashlib
import time
import threading
from collections import Counter
from typing import Any

from app.seed import SEED_ROWS
from app.demo import DEMO_ROWS


class Store:
    def __init__(self) -> None:
        self._tables: dict[str, list[dict[str, Any]]] = {
            name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()
        }
        # 追加演示链路数据（服务指标引用迁移验收用），不改动通用样例。
        for name, rows in DEMO_ROWS.items():
            self._tables.setdefault(name, [])
            existing_ids = {int(row.get("id", 0)) for row in self._tables[name]}
            self._tables[name].extend(
                dict(row) for row in rows if int(row.get("id", 0)) not in existing_ids
            )
        self._id_counters: dict[str, int] = {}
        # Cache layer storage
        self._cache: dict[str, tuple[float, Any]] = {}
        # Cross-module journal
        self._query_log: list[dict[str, Any]] = []
        # Cohort snapshots
        self._cohort_snapshots: dict[str, list[dict[str, Any]]] = {}
        # Anomaly records
        self._anomaly_records: list[dict[str, Any]] = []
        # Recalc history
        self._recalc_history: list[dict[str, Any]] = []
        # 服务指标引用迁移：转换批次、原配置快照、歧义记录
        self._migration_batches: list[dict[str, Any]] = []
        self._service_reference_snapshots: dict[str, dict[str, Any]] = {}
        self._migration_ambiguities: list[dict[str, Any]] = []
        # 服务关联凭据（token 等），迁移/回滚期间原样保留，仅以指纹对外展示
        self._credentials: dict[str, dict[str, Any]] = {}
        # Lock for thread safety
        self._lock = threading.Lock()

    @property
    def lock(self):
        return self._lock

    def module_names(self) -> list[str]:
        return sorted(self._tables)

    def rows(self, module: str) -> list[dict[str, Any]]:
        return self._tables.setdefault(module, [])

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        for row in self.rows(module):
            if int(row.get("id", 0)) == entry_id:
                return row
        return None

    def next_id(self, module: str) -> int:
        rows = self.rows(module)
        if not rows:
            return 1
        return max(int(row.get("id", 0)) for row in rows) + 1

    def overview(self) -> dict[str, object]:
        modules: list[dict[str, object]] = []
        for name in self.module_names():
            rows = self.rows(name)
            modules.append({
                "name": name,
                "created": len(rows),
                "pending": sum(1 for row in rows if row.get("pending")),
                "abnormal": sum(1 for row in rows if row.get("abnormal")),
            })
        cards = [
            {"label": "业务模块", "value": len(modules)},
            {"label": "今日新增", "value": sum(int(item["created"]) for item in modules)},
            {"label": "待处理", "value": sum(int(item["pending"]) for item in modules)},
            {"label": "异常量", "value": sum(int(item["abnormal"]) for item in modules)},
        ]
        return {"cards": cards, "modules": modules}

    # ── Cross-module query helpers ──

    def find_by_field(self, module: str, field: str, value: Any) -> list[dict[str, Any]]:
        """查找模块中 field 等于 value 的所有记录。"""
        return [row for row in self.rows(module) if row.get(field) == value]

    def find_by_status(self, module: str, status: str) -> list[dict[str, Any]]:
        return [row for row in self.rows(module) if row.get("status") == status]

    # ── Query journal ──

    def log_query(self, metric_id: int, operator: str, sql_summary: str,
                  row_count: int, duration_ms: float, status: str = "正常",
                  metric_uid: str = "", metric_name: str = "") -> dict[str, Any]:
        entry = {
            "id": len(self._query_log) + 1,
            "查询时间": time.strftime("%Y-%m-%d %H:%M:%S"),
            "操作人": operator,
            "涉及指标": metric_name or f"metric_{metric_id}",
            "指标UID": metric_uid,
            "查询语句摘要": sql_summary,
            "返回行数": str(row_count),
            "status": status,
            "duration_ms": duration_ms,
            "pending": status != "正常",
            "abnormal": status in ("慢查询", "异常终止"),
        }
        self._query_log.append(entry)
        return entry

    def query_logs(self) -> list[dict[str, Any]]:
        return self._query_log

    # ── Cache operations ──

    def cache_get(self, key: str) -> Any | None:
        """获取缓存，过期返回 None。"""
        entry = self._cache.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if time.time() > expires_at:
            del self._cache[key]
            return None
        return value

    def cache_set(self, key: str, value: Any, ttl: int) -> None:
        """设置缓存，ttl 单位为秒。"""
        self._cache[key] = (time.time() + ttl, value)

    def cache_invalidate(self, key: str) -> bool:
        return self._cache.pop(key, None) is not None

    def cache_invalidate_by_prefix(self, prefix: str) -> int:
        count = 0
        for key in list(self._cache):
            if key.startswith(prefix):
                del self._cache[key]
                count += 1
        return count

    # ── Cohort operations ──

    def save_cohort_snapshot(self, cohort_id: int, members: list[dict[str, Any]]) -> None:
        self._cohort_snapshots[str(cohort_id)] = members

    def get_cohort_snapshot(self, cohort_id: int) -> list[dict[str, Any]] | None:
        return self._cohort_snapshots.get(str(cohort_id))

    # ── Anomaly detection operations ──

    def record_anomaly(self, metric_id: int, severity: str, description: str,
                       value: float, threshold: float) -> dict[str, Any]:
        record = {
            "id": len(self._anomaly_records) + 1,
            "指标ID": metric_id,
            "严重程度": severity,
            "描述": description,
            "当前值": value,
            "阈值": threshold,
            "时间": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        self._anomaly_records.append(record)
        return record

    def anomaly_records(self) -> list[dict[str, Any]]:
        return self._anomaly_records

    # ── Recalc operations ──

    def record_recalc(self, metric_id: int, old_version: str, new_version: str,
                      time_range: str, diff_summary: str) -> dict[str, Any]:
        record = {
            "id": len(self._recalc_history) + 1,
            "关联指标": f"metric_{metric_id}",
            "旧口径": old_version,
            "新口径": new_version,
            "历史时间范围": time_range,
            "产出差异": diff_summary,
            "重算时间": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        self._recalc_history.append(record)
        return record

    def recalc_history(self) -> list[dict[str, Any]]:
        return self._recalc_history

    # ── 服务指标引用迁移 ──

    def migration_batches(self) -> list[dict[str, Any]]:
        return self._migration_batches

    def service_reference_snapshots(self) -> dict[str, dict[str, Any]]:
        return self._service_reference_snapshots

    def migration_ambiguities(self) -> list[dict[str, Any]]:
        return self._migration_ambiguities

    def service_credentials(self) -> dict[str, dict[str, Any]]:
        return self._credentials

    def ensure_metric_identity(self) -> None:
        """给存量指标补齐稳定身份（指标UID）。

        身份只分配一次，之后改名或出现同名指标都不会重写，
        因此引用方可以凭 UID 判断两笔请求是否在读同一笔指标。
        """
        used = {
            str(row.get("指标UID")) for row in self.rows("metric_definition")
            if row.get("指标UID")
        }
        for row in self.rows("metric_definition"):
            if row.get("指标UID"):
                continue
            serial = int(row.get("id") or 0)
            candidate = f"metric-{serial:04d}"
            while candidate in used:
                serial += 1
                candidate = f"metric-{serial:04d}"
            row["指标UID"] = candidate
            used.add(candidate)

        # 口径版本按 UID 归属，改名后仍能找回真实指标的口径。
        metrics_by_name: dict[str, list[dict[str, Any]]] = {}
        for metric in self.rows("metric_definition"):
            metrics_by_name.setdefault(str(metric.get("指标名称", "")), []).append(metric)
        for caliber in self.rows("caliber_version"):
            if caliber.get("指标UID"):
                continue
            candidates = metrics_by_name.get(str(caliber.get("所属指标", "")), [])
            if len(candidates) == 1:
                caliber["指标UID"] = candidates[0]["指标UID"]

    def metric_by_uid(self, metric_uid: str) -> dict[str, Any] | None:
        for row in self.rows("metric_definition"):
            if row.get("指标UID") == metric_uid:
                return row
        return None

    def metrics_by_name(self, metric_name: str) -> list[dict[str, Any]]:
        """按展示名称找指标。

        先用当前名称精确匹配；没有命中时再查历史名称，这样指标改名后
        旧客户端或回滚后的存量配置仍能找回同一笔指标。
        """
        current = [row for row in self.rows("metric_definition")
                   if row.get("指标名称") == metric_name]
        if current:
            return current
        return [
            row for row in self.rows("metric_definition")
            if metric_name in [str(alias) for alias in (row.get("历史名称") or [])]
        ]

    def calibers_for_metric(self, metric: dict[str, Any]) -> list[dict[str, Any]]:
        """优先按 UID 找口径，找不到再退回名称（兼容未迁移的存量数据）。"""
        metric_uid = metric.get("指标UID")
        if metric_uid:
            by_uid = [row for row in self.rows("caliber_version")
                      if row.get("指标UID") == metric_uid]
            if by_uid:
                return by_uid
        return self.find_by_field("caliber_version", "所属指标",
                                  metric.get("指标名称", ""))

    def reference_fingerprint(self, payload: dict[str, Any]) -> str:
        """给引用/历史配置生成稳定指纹，用于核对回滚前后是否为同一份配置。"""
        material = "|".join(
            f"{key}={payload.get(key, '')}"
            for key in ("服务接口ID", "目标类型", "目标ID", "指标UID",
                        "指标名称快照", "旧名称契约", "关联凭据指纹")
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]

    def copy_row(self, row: dict[str, Any]) -> dict[str, Any]:
        return copy.deepcopy(row)

    # ── Data artifacts for cross-module consumption ──

    def get_metric_data(self, metric_id: int) -> dict[str, Any] | None:
        """Get metric data with its calibers, queries, and services."""
        metric = self.find("metric_definition", metric_id)
        if metric is None:
            return None
        self.ensure_metric_identity()
        metric_uid = metric.get("指标UID", "")
        metric_name = metric.get("指标名称", "")
        return {
            "metric": metric,
            "calibers": self.calibers_for_metric(metric),
            "queries": [row for row in self.rows("query_builder")
                        if row.get("关联指标") == metric_name
                        or (metric_uid and row.get("指标UID") == metric_uid)],
            "services": [row for row in self.rows("data_service")
                         if metric_name in str(row.get("关联指标或模型", ""))
                         or (metric_uid and row.get("指标UID") == metric_uid)],
            "subscriptions": self.find_by_field("data_subscription", "关联指标",
                                                 metric_name),
        }

    def get_dimension_model_data(self, model_id: int) -> dict[str, Any] | None:
        """Get dimension model with its drill-down paths and pivot configs."""
        model = self.find("dimension_model", model_id)
        if model is None:
            return None
        return {
            "model": model,
            "drill_paths": self.find_by_field("drill_down", "起始指标",
                                              model.get("模型名称", "")),
            "pivot_configs": self.find_by_field("pivot_table", "数据源指标",
                                                 model.get("模型名称", "")),
        }

    def get_cohort_analysis(self, cohort_id: int) -> dict[str, Any] | None:
        """Get cohort with experiments and segment comparisons."""
        cohort = self.find("user_cohort", cohort_id)
        if cohort is None:
            return None
        cohort_name = cohort.get("分群名称", "")
        return {
            "cohort": cohort,
            "experiments": [row for row in self.rows("experiment_group")
                            if cohort_name in str(row.get("筛选条件", ""))],
            "comparisons": [row for row in self.rows("segment_compare")
                            if cohort_name in (str(row.get("分群A", "")) + str(row.get("分群B", "")))],
            "anomaly_rules": [row for row in self.rows("anomaly_detector")
                              if cohort_name in str(row.get("目标指标", ""))],
        }

    def get_field_lineage(self, field_name: str) -> dict[str, Any]:
        """Trace a field's complete lineage chain."""
        entries = self.find_by_field("field_lineage", "目标字段", field_name)
        if not entries:
            return {"field": field_name, "lineage": [], "depth": 0}

        lineage = [{
            "source_fields": entry.get("来源字段列表", ""),
            "transform": entry.get("转换逻辑", ""),
            "intermediate_table": entry.get("中间表", ""),
            "depth": entry.get("血缘深度", ""),
            "status": entry.get("status", ""),
        } for entry in entries]

        return {
            "field": field_name,
            "lineage": lineage,
            "depth": len(lineage),
        }


store = Store()