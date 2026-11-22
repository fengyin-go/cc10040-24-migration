"""指标定义领域服务：管理业务指标的计算口径、聚合维度和数据源映射。

作为度量链的起点，指标定义被口径版本、查询构建器、数据服务等多模块引用。
"""
from __future__ import annotations

import time
from typing import Any

from app.store import store
from app.services.base import BaseModuleService


class MetricDefinitionService(BaseModuleService):
    MODULE = "metric_definition"
    ENTITY = "指标"
    OPERATION_TERM = "聚合"
    LIST_FIELDS = ["指标名称", "计算口径", "统计粒度", "数据源", "业务域"]
    REQUIRED_FIELDS = ["指标名称", "计算口径", "统计粒度"]
    STATUS_ORDER = ["草稿", "已发布", "已停用"]
    ACTION_RULES = {"发布指标": "已发布", "停用指标": "已停用"}
    NEGATIVE_ACTIONS = ["停用指标"]

    def publish_with_caliber(self, entry_id: int, caliber_desc: str) -> tuple[dict[str, Any] | None, str]:
        """发布指标并自动创建初始口径版本。

        跨模块数据流: metric_definition -> caliber_version
        已发布指标必须有至少一个已生效口径版本，查询构建器才能引用。
        """
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在"
        if entry["status"] != "草稿":
            return None, f"{self.ENTITY} {entry_id} 只能从草稿状态发布"

        entry["status"] = "已发布"
        entry["pending"] = True
        entry["abnormal"] = False

        # 自动创建口径版本文档
        version_id = store.next_id("caliber_version")
        timestamp = time.strftime("%Y-%m-%d")
        caliber = {
            "id": version_id,
            "所属指标": entry.get("指标名称", ""),
            "版本号": "v1.0",
            "口径描述": caliber_desc,
            "变更类型": "初始创建",
            "生效时间": timestamp,
            "status": "已生效",
            "pending": True,
            "abnormal": False,
        }
        store.rows("caliber_version").append(caliber)
        return entry, f"指标已发布并创建口径版本 v1.0"

    def rename_metric(self, entry_id: int, new_name: str,
                      operator: str = "指标负责人") -> tuple[dict[str, Any] | None, str]:
        """指标改名：保留稳定身份（指标UID），旧展示名进历史名称。

        改名后迁移过的服务仍通过 UID 读到同一笔指标；未迁移的存量名称
        也可能通过历史名称找回，不会因为改名被悄悄改绑到别的指标。
        """
        store.ensure_metric_identity()
        metric = store.find(self.MODULE, entry_id)
        if metric is None:
            return None, f"{self.ENTITY} {entry_id} 不存在"
        new_name = (new_name or "").strip()
        if not new_name:
            return None, "新指标名称不能为空"
        old_name = str(metric.get("指标名称", ""))
        if new_name == old_name:
            return metric, "指标名称未变化"

        aliases = list(metric.get("历史名称", []) or [])
        if old_name and old_name not in aliases:
            aliases.append(old_name)
        metric["历史名称"] = aliases
        metric["指标名称"] = new_name
        metric["名称变更记录"] = list(metric.get("名称变更记录", []) or []) + [{
            "原名称": old_name,
            "新名称": new_name,
            "操作人": operator,
            "时间": time.strftime("%Y-%m-%d %H:%M:%S"),
        }]

        # 名称变了，按名称生成的缓存键要一并失效，避免读到旧名称的结果。
        invalidated = store.cache_invalidate_by_prefix(f"query:{old_name}")
        invalidated += store.cache_invalidate_by_prefix(f"query:{new_name}")
        return metric, (f"指标已从「{old_name}」改名为「{new_name}」，"
                        f"稳定引用 {metric.get('指标UID')} 不变，缓存失效 {invalidated} 条")

    def get_metric_chain(self, entry_id: int) -> dict[str, Any] | None:
        """获取指标完整数据链：指标 -> 口径 -> 查询模板 -> 数据服务。

        跨模块数据流: metric_definition -> caliber_version -> query_builder -> data_service
        """
        metric_data = store.get_metric_data(entry_id)
        if metric_data is None:
            return None
        metric = metric_data["metric"]
        chain = {
            "metric_id": entry_id,
            "metric_name": metric.get("指标名称", ""),
            "caliber_count": len(metric_data["calibers"]),
            "caliber_versions": [
                {"id": c["id"], "version": c.get("版本号", ""),
                 "status": c.get("status", ""), "desc": c.get("口径描述", "")}
                for c in metric_data["calibers"]
            ],
            "query_templates": len(metric_data["queries"]),
            "service_apis": len(metric_data["services"]),
        }
        return chain

    def recalc_metric_history(self, entry_id: int, new_caliber_version: str,
                              time_range: str) -> tuple[dict[str, Any] | None, str]:
        """触发指标历史数据按新口径重算。

        跨模块数据流: metric_definition -> caliber_version -> recalc_engine
        """
        metric = store.find(self.MODULE, entry_id)
        if metric is None:
            return None, f"{self.ENTITY} {entry_id} 不存在"

        # 查找当前口径（按稳定引用归属，指标改名后仍能找到口径）
        calibers = store.calibers_for_metric(metric)
        active = [c for c in calibers if c.get("status") == "已生效"]

        old_version = active[0].get("版本号", "unknown") if active else "unknown"

        # 创建重算任务
        task_id = store.next_id("recalc_engine")
        task = {
            "id": task_id,
            "关联指标": metric.get("指标名称", ""),
            "指标UID": metric.get("指标UID", ""),
            "新口径版本": new_caliber_version,
            "历史时间范围": time_range,
            "重算状态": "待调度",
            "产出差异": "",
            "status": "待调度",
            "pending": True,
            "abnormal": False,
        }
        store.rows("recalc_engine").append(task)

        record = store.record_recalc(entry_id, old_version, new_caliber_version,
                                     time_range, "待重算后产生")
        return task, f"已创建重算任务 #{task_id}"