"""查询构建器领域服务：构建和缓存多维组合查询模板以加速分析。

消费口径版本的指标定义，生成查询模板供数据服务调用。
跨模块数据流: caliber_version -> query_builder -> data_service -> cache_layer -> query_journal
"""
from __future__ import annotations

import time
from typing import Any

from app.store import store
from app.services.base import BaseModuleService


class QueryBuilderService(BaseModuleService):
    MODULE = "query_builder"
    ENTITY = "查询模板"
    OPERATION_TERM = "关联"
    LIST_FIELDS = ["模板名称", "关联指标", "维度组合", "过滤条件", "缓存策略"]
    REQUIRED_FIELDS = ["模板名称", "关联指标", "维度组合"]
    STATUS_ORDER = ["草稿", "已发布", "已过期"]
    ACTION_RULES = {"发布模板": "已发布", "过期模板": "已过期"}
    NEGATIVE_ACTIONS = []

    def execute_query(self, entry_id: int, operator: str = "系统",
                      use_cache: bool = True) -> dict[str, Any] | None:
        """执行查询模板并返回结果与日志。

        跨模块数据流: query_builder -> caliber_version(口径验证) -> cache_layer(缓存) -> query_journal(日志)
        关联指标优先按已发布模板的稳定指标引用（指标UID）解析，
        存量模板没有引用时退回名称；名称对应多笔指标时明确失败，不做猜测。
        """
        template = store.find(self.MODULE, entry_id)
        if template is None:
            return {"error": f"查询模板 {entry_id} 不存在"}
        if template["status"] != "已发布":
            return {"error": f"查询模板 {entry_id} 未发布"}

        metric_name = template.get("关联指标", "")
        metric_ref = template.get("指标UID", "")

        metric, metric_error = self.resolve_metric(metric_name, metric_ref)
        if metric_error is not None:
            return {"error": metric_error}
        assert metric is not None
        # 指标改名后，存量模板上的旧名称仍指向同一笔指标：模板可自愈到稳定引用。
        if not metric_ref and not metric.get("legacy_unresolved"):
            template["指标UID"] = metric.get("指标UID", "")
        resolved_uid = str(metric.get("指标UID", metric_ref))
        # 结果里的指标名用指标当前的真实名称，模板上的名称只作存量契约。
        actual_metric_name = str(metric.get("指标名称", metric_name))

        # 检查指标口径版本（按 UID 归属，指标改名后口径仍能对上）
        calibers = (
            [] if metric.get("legacy_unresolved") else store.calibers_for_metric(metric)
        )
        active_caliber = next((c for c in calibers if c.get("status") == "已生效"), None)
        caliber_version = active_caliber.get("版本号") if active_caliber else None
        if metric.get("legacy_unresolved"):
            # 分批升级期间指标定义可能还没登记：不阻断存量调用，只标记来源。
            caliber_version = None
        elif active_caliber is None:
            return {"error": f"指标 {actual_metric_name} 没有已生效的口径版本"}

        cache_key = (f"query:{actual_metric_name}:"
                     f"{template.get('维度组合','')}:{template.get('过滤条件','')}")

        # 尝试读取缓存
        if use_cache:
            cached = store.cache_get(cache_key)
            if cached is not None:
                t_start = time.time()
                store.log_query(
                    metric_id=template.get("id", 0),
                    operator=operator,
                    sql_summary=f"缓存命中: {template.get('模板名称')}",
                    row_count=len(cached) if isinstance(cached, list) else 1,
                    duration_ms=(time.time() - t_start) * 1000,
                    status="正常",
                    metric_uid=resolved_uid,
                    metric_name=actual_metric_name,
                )
                return {"source": "cache", "template": template.get("模板名称"),
                        "metric_ref": resolved_uid, "metric_name": actual_metric_name,
                        "caliber": caliber_version,
                        "legacy_unresolved": bool(metric.get("legacy_unresolved")),
                        "rows": cached}

        # 模拟查询执行 - 基于指标数据生成结果
        t_start = time.time()
        metric_data = [metric]
        dimensions = template.get("维度组合", "").split(",") if template.get("维度组合") else []

        result_rows = []
        for idx, m in enumerate(metric_data):
            row = {"序号": idx + 1, "指标": actual_metric_name,
                   "指标引用": resolved_uid,
                   "口径版本": caliber_version or "",
                   "计算口径": m.get("计算口径", ""),
                   "统计粒度": m.get("统计粒度", "")}
            for dim in dimensions:
                row[dim.strip()] = f"维度值_{idx + 1}"
            result_rows.append(row)

        duration_ms = (time.time() - t_start) * 1000
        status = "慢查询" if duration_ms > 100 else "正常"

        # 写入查询日志
        store.log_query(
            metric_id=template.get("id", 0),
            operator=operator,
            sql_summary=f"查询模板: {template.get('模板名称')}, 维度: {template.get('维度组合')}",
            row_count=len(result_rows),
            duration_ms=duration_ms,
            status=status,
            metric_uid=resolved_uid,
            metric_name=actual_metric_name,
        )

        # 写入缓存
        cache_ttl = 300  # 默认 5 分钟
        cache_policy = template.get("缓存策略", "")
        if "长期" in str(cache_policy):
            cache_ttl = 3600
        elif "短期" in str(cache_policy):
            cache_ttl = 60
        store.cache_set(cache_key, result_rows, cache_ttl)

        return {"source": "computed", "template": template.get("模板名称"),
                "metric_ref": resolved_uid, "metric_name": actual_metric_name,
                "caliber": caliber_version, "duration_ms": duration_ms,
                "legacy_unresolved": bool(metric.get("legacy_unresolved")),
                "cached_for_seconds": cache_ttl, "rows": result_rows}

    def resolve_metric(self, metric_name: str,
                       metric_ref: str = "") -> tuple[dict[str, Any] | None, str | None]:
        """把模板上的指标引用解析成唯一一笔已发布指标的真实定义。

        优先级：稳定引用（指标UID）> 名称。按名称命中多笔时返回明确故障，
        不会随便挑其中一笔。
        """
        store.ensure_metric_identity()
        if metric_ref:
            metric = store.metric_by_uid(str(metric_ref))
            if metric is None:
                return None, f"指标引用 {metric_ref} 找不到对应指标"
            return metric, None
        if not metric_name:
            return None, "查询模板未关联指标"
        candidates = store.metrics_by_name(str(metric_name))
        if not candidates:
            # 分批升级期间指标定义可能还没登记，或模板仍挂着一个存量展示名：
            # 保持旧模板可执行，只把来源标记为 legacy，不阻断存量调用。
            return {
                "指标UID": f"legacy-name:{metric_name}",
                "指标名称": metric_name,
                "计算口径": "",
                "统计粒度": "",
                "legacy_unresolved": True,
            }, None
        if len(candidates) > 1:
            return None, (
                f"指标名称「{metric_name}」对应 {len(candidates)} 笔指标，"
                "无法唯一确定已发布模板的真实来源；请为该模板补充指标引用"
            )
        return candidates[0], None

    def invalidate_query_cache(self, metric_name: str) -> int:
        """当口径版本变更时，失效指标相关的所有查询缓存。

        跨模块数据流: caliber_version -> query_builder -> cache_layer
        """
        return store.cache_invalidate_by_prefix(f"query:{metric_name}:")