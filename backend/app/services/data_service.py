"""数据服务领域服务：将指标和模型发布为对外数据服务API。

跨模块数据流: query_builder -> data_service -> cache_layer 以及
              data_service 调用 query_builder.execute_query 获取数据
"""
from __future__ import annotations

from typing import Any

from app.store import store
from app.services.base import BaseModuleService


class DataServiceService(BaseModuleService):
    MODULE = "data_service"
    ENTITY = "服务接口"
    OPERATION_TERM = "关联"
    LIST_FIELDS = ["接口名称", "关联指标或模型", "请求方式", "限流配额", "发布状态"]
    REQUIRED_FIELDS = ["接口名称", "关联指标或模型", "请求方式"]
    STATUS_ORDER = ["开发中", "已发布", "已下线"]
    ACTION_RULES = {"发布接口": "已发布", "下线接口": "已下线"}
    NEGATIVE_ACTIONS = ["下线接口"]

    def list_entries(self, **kwargs: Any) -> tuple[list[dict[str, Any]], int]:
        """列表保持原有字段契约，额外带上引用状态与稳定引用供页面核对。"""
        rows, total = super().list_entries(**kwargs)
        store.ensure_metric_identity()
        enriched = []
        for row in rows:
            item = dict(row)
            item.setdefault("引用状态", "未迁移")
            if not item.get("旧名称契约"):
                item["旧名称契约"] = item.get("关联指标或模型", "")
            enriched.append(item)
        return enriched, total

    def call_service(self, entry_id: int, operator: str = "客户端",
                     metric_name: str | None = None,
                     metric_ref: str | None = None) -> dict[str, Any]:
        """调用数据服务API接口。

        跨模块数据流: data_service -> query_builder -> cache_layer -> query_journal
        首先验证接口和关联查询模板，然后通过查询构建器获取数据。

        返回同时遵守两套契约：
          * 旧客户端字段：api / request_method / result_count / query_results；
          * 迁移核对字段：contract_name（调用时的名称契约）、
            actual_metric_ref（实际读取的指标稳定引用）。
        旧名调用与迁移后引用调用若 actual_metric_ref 相同，即仍在读同一笔指标。
        """
        api = store.find(self.MODULE, entry_id)
        if api is None:
            return {"error": f"服务接口 {entry_id} 不存在"}
        if api["status"] != "已发布":
            return {"error": f"服务接口 {entry_id} 未发布"}

        from app.services.service_metric_reference import service_metric_reference
        resolved = service_metric_reference.resolve_reference(
            entry_id, metric_name, metric_ref)
        if not resolved.get("ok"):
            return {
                "error": resolved.get("error"),
                "service_id": entry_id,
                "contract_name": resolved.get("contract_name"),
                "ambiguous": bool(resolved.get("ambiguous")),
                "candidates": resolved.get("candidates", []),
                "ambiguity_record": resolved.get("ambiguity_record"),
            }

        metric = resolved["metric"]
        metric_uid = str(metric["指标UID"])
        target = api.get("关联指标或模型", "")
        if not target:
            return {"error": "服务接口未关联指标或模型"}

        # 查找关联的查询模板：优先按指标的稳定引用匹配已发布模板
        query_templates = [
            row for row in store.rows("query_builder")
            if row.get("指标UID") == metric_uid
            or (not row.get("指标UID") and row.get("关联指标") == metric.get("指标名称", ""))
        ]
        if not query_templates:
            return {"error": f"没有找到关联 {resolved.get('contract_name') or target} 的查询模板"}

        from app.services.query_builder import QueryBuilderService
        builder = QueryBuilderService()

        results = []
        for tmpl in query_templates:
            if tmpl.get("status") == "已发布":
                query_result = builder.execute_query(int(tmpl.get("id", 0)),
                                                     operator=operator)
                if "rows" in query_result:
                    results.append(query_result)

        if not results:
            return {"error": "没有可用的已发布查询模板"}

        return {
            "api": api.get("接口名称"),
            "request_method": api.get("请求方式"),
            "result_count": sum(len(r.get("rows", [])) for r in results),
            "query_results": results,
            # 迁移核对：调用时名称 + 实际指标引用
            "contract_name": resolved.get("contract_name"),
            "actual_metric_ref": metric_uid,
            "metric_name": metric.get("指标名称", ""),
            "resolved_by": resolved.get("resolved_by"),
        }

    def get_service_health(self, entry_id: int) -> dict[str, Any]:
        """获取服务接口健康状态。

        检查关联的指标口径版本和数据服务可用性。
        """
        api = store.find(self.MODULE, entry_id)
        if api is None:
            return {"error": f"服务接口 {entry_id} 不存在"}

        from app.services.service_metric_reference import service_metric_reference
        resolved = service_metric_reference.resolve_reference(entry_id)
        if not resolved.get("ok"):
            return {
                "api_id": entry_id,
                "api_name": api.get("接口名称"),
                "status": api.get("status"),
                "health": "degraded",
                "error": resolved.get("error"),
                "ambiguous": bool(resolved.get("ambiguous")),
                "ambiguity_record": resolved.get("ambiguity_record"),
            }
        metric = resolved["metric"]
        target = resolved.get("contract_name") or api.get("关联指标或模型", "")

        # 检查关联指标的口径版本是否有效（按稳定引用归属）
        calibers = store.calibers_for_metric(metric)
        active_caliber = next((c for c in calibers if c.get("status") == "已生效"), None)

        # 检查关联查询模板
        metric_uid = str(metric.get("指标UID", ""))
        query_templates = [
            row for row in store.rows("query_builder")
            if row.get("指标UID") == metric_uid
            or (not row.get("指标UID") and row.get("关联指标") == metric.get("指标名称", ""))
        ]
        published = [t for t in query_templates if t.get("status") == "已发布"]

        return {
            "api_id": entry_id,
            "api_name": api.get("接口名称"),
            "status": api.get("status"),
            "target_metric": target,
            "actual_metric_ref": metric_uid,
            "metric_name": metric.get("指标名称", ""),
            "active_caliber": active_caliber.get("版本号") if active_caliber else None,
            "published_query_templates": len(published),
            "health": "healthy" if active_caliber and published else "degraded",
        }