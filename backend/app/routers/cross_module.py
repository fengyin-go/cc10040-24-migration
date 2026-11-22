"""跨模块数据流API路由：暴露跨模块数据流链路。

链1: metric_definition -> caliber_version -> query_builder -> data_service -> cache_layer -> query_journal
链2: dimension_model -> drill_down -> pivot_table -> report_template
链3: user_cohort -> experiment_group -> segment_compare -> anomaly_detector
"""
from __future__ import annotations

from fastapi import APIRouter

from app.store import store
from app.services.metric_definition import MetricDefinitionService
from app.services.caliber_version import CaliberVersionService
from app.services.query_builder import QueryBuilderService
from app.services.data_service import DataServiceService
from app.services.cache_layer import CacheLayerService
from app.services.query_journal import QueryJournalService
from app.services.dimension_model import DimensionModelService
from app.services.drill_down import DrillDownService
from app.services.pivot_table import PivotTableService
from app.services.report_template import ReportTemplateService
from app.services.user_cohort import UserCohortService
from app.services.experiment_group import ExperimentGroupService
from app.services.segment_compare import SegmentCompareService
from app.services.anomaly_detector import AnomalyDetectorService
from app.services.recalc_engine import RecalcEngineService
from app.services.field_lineage import FieldLineageService
from app.services.service_metric_reference import ServiceMetricReferenceService

router = APIRouter(prefix="/api/cross", tags=["跨模块数据流"])


# ── 链1: 指标口径查询链 ──

@router.get("/metric-chain/{metric_id}")
def get_metric_chain(metric_id: int):
    """获取指标完整数据链：指标 -> 口径 -> 查询模板 -> 数据服务 -> 缓存 -> 日志"""
    svc = MetricDefinitionService()
    return svc.get_metric_chain(metric_id)


@router.post("/metric/{metric_id}/publish-with-caliber")
def publish_with_caliber(metric_id: int, caliber_desc: str = "默认计算口径"):
    """发布指标并自动创建口径版本"""
    svc = MetricDefinitionService()
    result, msg = svc.publish_with_caliber(metric_id, caliber_desc)
    return {"ok": result is not None, "message": msg, "entry": result}


@router.post("/metric/{metric_id}/rename")
def rename_metric(metric_id: int, new_name: str, operator: str = "指标负责人"):
    """指标改名：稳定引用（指标UID）不变，旧名进历史名称。"""
    svc = MetricDefinitionService()
    result, msg = svc.rename_metric(metric_id, new_name, operator=operator)
    return {"ok": result is not None, "message": msg, "entry": result}


@router.post("/caliber/{version_id}/activate")
def activate_caliber(version_id: int):
    """启用口径版本，自动过期旧版本并失效缓存"""
    svc = CaliberVersionService()
    result, msg = svc.activate_version(version_id)
    return {"ok": result is not None, "message": msg, "entry": result}


@router.get("/caliber/{metric_name}/history")
def get_caliber_history(metric_name: str):
    """获取指标的口径版本历史"""
    svc = CaliberVersionService()
    return svc.get_version_history(metric_name)


@router.post("/query/{template_id}/execute")
def execute_query(template_id: int, operator: str = "系统"):
    """执行查询模板，返回结果与日志"""
    svc = QueryBuilderService()
    return svc.execute_query(template_id, operator=operator)


@router.post("/data-service/{api_id}/call")
def call_data_service(api_id: int, operator: str = "客户端",
                      metric_name: str | None = None,
                      metric_ref: str | None = None):
    """调用数据服务API。

    metric_name 可选：旧客户端继续传原展示名称（名称契约）；
    metric_ref 可选：迁移后客户端直接传稳定指标引用，优先于名称。
    两者都不传时使用服务上保留的旧名称契约。返回里带 actual_metric_ref 供核对。
    """
    svc = DataServiceService()
    return svc.call_service(api_id, operator=operator, metric_name=metric_name,
                            metric_ref=metric_ref)


@router.get("/data-service/{api_id}/health")
def service_health(api_id: int):
    """获取服务接口健康状态"""
    svc = DataServiceService()
    return svc.get_service_health(api_id)


# ── 服务指标引用迁移 ──

def _migration_service() -> ServiceMetricReferenceService:
    return ServiceMetricReferenceService()


@router.get("/service-reference/status")
def service_reference_status():
    """服务指标引用迁移进度：存量、已迁移、冻结歧义、批次概览。"""
    return _migration_service().migration_status()


@router.get("/service-reference/legacy")
def service_reference_legacy():
    """仍按展示名称关联的存量服务清单。"""
    return {"items": _migration_service().list_legacy_services()}


@router.get("/service-reference/plan")
def service_reference_plan(batch_size: int = 1):
    """查看下一批将被转换的服务（冻结的歧义记录不会进入批次）。"""
    items = _migration_service().plan_batch(batch_size)
    return {"batch_size": batch_size, "items": items}


@router.post("/service-reference/migrate-batch")
def migrate_service_reference_batch(batch_size: int = 1, operator: str = "迁移作业"):
    """按批次把名称引用转换为稳定指标引用。

    唯一匹配 -> 改写为 UID 引用并保留旧名称契约；
    同名歧义 -> 冻结原值、登记歧义并返回明确故障（ok=false）。
    """
    return _migration_service().migrate_batch(batch_size=batch_size, operator=operator)


@router.get("/service-reference/{service_id}/audit")
def audit_service_reference(service_id: int):
    """核对服务接口：旧名称契约、实际指标引用、口径、模板与转换前快照。"""
    return _migration_service().audit_service(service_id)


@router.get("/service-reference/{service_id}/resolve")
def resolve_service_reference(service_id: int, metric_name: str | None = None):
    """解析一次调用会读到的实际指标引用（不执行查询）。"""
    return _migration_service().resolve_reference(service_id, metric_name)


@router.get("/service-reference/ambiguities")
def list_service_reference_ambiguities(status: str | None = None):
    """歧义记录列表（冻结原值，供历史配置查证）。"""
    return {"items": _migration_service().list_ambiguities(status)}


@router.post("/service-reference/ambiguities/{ambiguity_id}/resolve")
def resolve_service_reference_ambiguity(
    ambiguity_id: int, metric_uid: str, operator: str = "指标负责人",
):
    """由指标负责人指定唯一目标 UID，解除冻结并完成转换。"""
    record, message = _migration_service().resolve_ambiguity(
        ambiguity_id, metric_uid, operator=operator)
    return {"ok": record is not None, "message": message, "entry": record}


@router.post("/service-reference/batches/{batch_id}/rollback")
def rollback_service_reference_batch(batch_id: int):
    """按转换前快照回滚批次，恢复存量配置并保留关联凭据。"""
    return _migration_service().rollback_batch(batch_id)


@router.get("/service-reference/batches/{batch_id}/verify")
def verify_service_reference_rollback(batch_id: int):
    """回滚后核对：服务配置与凭据指纹是否回到转换前。"""
    return _migration_service().verify_rollback(batch_id)


@router.get("/service-reference/batches")
def list_service_reference_batches():
    """迁移批次历史（含转换前快照位置与回滚时间）。"""
    return {"items": [
        {
            "批次ID": row.get("批次ID"),
            "执行人": row.get("执行人"),
            "执行时间": row.get("执行时间"),
            "计划转换": row.get("计划转换"),
            "转换成功": row.get("转换成功"),
            "冻结歧义": row.get("冻结歧义"),
            "保留凭据": row.get("保留凭据"),
            "回滚时间": row.get("回滚时间"),
        }
        for row in store.migration_batches()
    ]}


@router.post("/cache/invalidate-by-metric/{metric_name}")
def invalidate_cache_by_metric(metric_name: str):
    """根据指标名称失效缓存"""
    svc = CacheLayerService()
    return svc.invalidate_by_metric(metric_name)


@router.post("/cache/warm/{metric_name}")
def warm_cache(metric_name: str):
    """预热指标查询缓存"""
    svc = CacheLayerService()
    return svc.warm_cache(metric_name)


@router.get("/cache/stats")
def cache_stats():
    """获取缓存统计"""
    svc = CacheLayerService()
    return svc.get_cache_stats()


@router.get("/query-journal/slow-queries")
def slow_queries(threshold_ms: float = 100):
    """获取慢查询列表"""
    svc = QueryJournalService()
    return svc.get_slow_queries(threshold_ms)


@router.get("/query-journal/stats-by-metric")
def query_stats_by_metric():
    """按指标统计查询情况"""
    svc = QueryJournalService()
    return svc.get_query_stats_by_metric()


@router.get("/query-journal/performance-insights")
def performance_insights():
    """获取性能洞察"""
    svc = QueryJournalService()
    return svc.get_performance_insights()


# ── 链2: 维度分析下钻链 ──

@router.get("/dimension-model/{model_id}/with-analytics")
def get_dimension_model_with_analytics(model_id: int):
    """获取维度模型及其关联分析配置"""
    svc = DimensionModelService()
    return svc.get_model_with_analytics(model_id)


@router.get("/dimension-model/{model_id}/hierarchy")
def get_dimension_hierarchy(model_id: int):
    """获取维度层次结构"""
    svc = DimensionModelService()
    return svc.get_dimension_hierarchy(model_id)


@router.post("/drill-down/{path_id}/execute")
def execute_drill_down(path_id: int, current_level: int = 0):
    """执行下钻分析"""
    svc = DrillDownService()
    return svc.execute_drill_down(path_id, current_level)


@router.get("/drill-down/{path_id}/tree")
def get_drill_tree(path_id: int):
    """获取下钻路径树"""
    svc = DrillDownService()
    return svc.get_drill_tree(path_id)


@router.get("/drill-down/{path_id}/export-to-pivot")
def export_drill_to_pivot(path_id: int):
    """将下钻路径导出为透视表配置建议"""
    svc = DrillDownService()
    return svc.export_to_pivot(path_id)


@router.post("/pivot-table/{config_id}/compute")
def compute_pivot(config_id: int):
    """计算透视表"""
    svc = PivotTableService()
    return svc.compute_pivot(config_id)


@router.post("/pivot-table/{config_id}/share-to-report")
def share_pivot_to_report(config_id: int, report_template_id: int):
    """将透视表共享到报告模板"""
    svc = PivotTableService()
    return svc.share_to_report(config_id, report_template_id)


@router.get("/report-template/{template_id}/generate")
def generate_report(template_id: int):
    """生成报告"""
    svc = ReportTemplateService()
    return svc.generate_report(template_id)


# ── 链3: 分群对比异常链 ──

@router.post("/user-cohort/{cohort_id}/recompute")
def recompute_cohort(cohort_id: int):
    """重新计算分群"""
    svc = UserCohortService()
    return svc.recompute_cohort(cohort_id)


@router.get("/user-cohort/{cohort_id}/detail")
def get_cohort_detail(cohort_id: int):
    """获取分群详细信息"""
    svc = UserCohortService()
    return svc.get_cohort_detail(cohort_id)


@router.get("/experiment-group/{group_id}/evaluate")
def evaluate_experiment_groups(group_a_id: int, group_b_id: int):
    """评估两个实验分组"""
    svc = ExperimentGroupService()
    return svc.evaluate_groups(group_a_id, group_b_id)


@router.post("/segment-compare/{task_id}/execute")
def execute_segment_compare(task_id: int):
    """执行分群对比分析"""
    svc = SegmentCompareService()
    return svc.execute_compare(task_id)


@router.get("/segment-compare/{task_id}/insights")
def segment_compare_insights(task_id: int):
    """获取跨分群洞察"""
    svc = SegmentCompareService()
    return svc.get_cross_segment_insights(task_id)


@router.post("/anomaly-detector/{rule_id}/detect")
def run_anomaly_detection(rule_id: int):
    """执行异常检测"""
    svc = AnomalyDetectorService()
    return svc.run_detection(rule_id)


@router.get("/anomaly-detector/report")
def anomaly_report(metric_name: str | None = None):
    """获取异常报告"""
    svc = AnomalyDetectorService()
    return svc.get_anomaly_report(metric_name)


@router.get("/anomaly-detector/coverage")
def detection_coverage():
    """获取异常检测覆盖率"""
    svc = AnomalyDetectorService()
    return svc.get_detection_coverage()


# ── 重算引擎 ──

@router.post("/recalc/{task_id}/start")
def start_recalc(task_id: int):
    """启动口径重算"""
    svc = RecalcEngineService()
    return svc.start_recalc(task_id)


@router.get("/recalc/history")
def recalc_history(metric_name: str | None = None):
    """获取重算历史"""
    svc = RecalcEngineService()
    return svc.get_recalc_history(metric_name)


@router.get("/recalc/{task_id}/estimate-impact")
def estimate_recalc_impact(task_id: int):
    """预估算重算影响"""
    svc = RecalcEngineService()
    return svc.estimate_recalc_impact(task_id)


@router.post("/metric/{metric_id}/recalc")
def recalc_metric(metric_id: int, new_caliber_version: str, time_range: str):
    """触发指标历史数据重算"""
    svc = MetricDefinitionService()
    result, msg = svc.recalc_metric_history(metric_id, new_caliber_version, time_range)
    return {"ok": result is not None, "message": msg, "entry": result}


# ── 字段血缘 ──

@router.get("/field-lineage/{field_name}/trace")
def trace_field(field_name: str):
    """追溯字段血缘"""
    svc = FieldLineageService()
    return svc.trace_field(field_name)


@router.get("/field-lineage/{field_name}/downstream-impact")
def field_downstream_impact(field_name: str):
    """分析字段变更下游影响"""
    svc = FieldLineageService()
    return svc.get_downstream_impact(field_name)


# ── 全链路快照 ──

@router.get("/full-chain-snapshot")
def full_chain_snapshot():
    """获取所有跨模块链路的状态快照"""
    return {
        "chain_1_metric_query": {
            "description": "指标口径查询链: metric_definition -> caliber_version -> query_builder -> data_service -> cache_layer -> query_journal",
            "modules": {
                "metric_definition": MetricDefinitionService().domain_snapshot(),
                "caliber_version": CaliberVersionService().domain_snapshot(),
                "query_builder": QueryBuilderService().domain_snapshot(),
                "data_service": DataServiceService().domain_snapshot(),
                "cache_layer": CacheLayerService().domain_snapshot(),
                "query_journal": QueryJournalService().domain_snapshot(),
            },
        },
        "chain_2_dimension_analysis": {
            "description": "维度分析下钻链: dimension_model -> drill_down -> pivot_table -> report_template",
            "modules": {
                "dimension_model": DimensionModelService().domain_snapshot(),
                "drill_down": DrillDownService().domain_snapshot(),
                "pivot_table": PivotTableService().domain_snapshot(),
                "report_template": ReportTemplateService().domain_snapshot(),
            },
        },
        "chain_3_cohort_anomaly": {
            "description": "分群对比异常链: user_cohort -> experiment_group -> segment_compare -> anomaly_detector",
            "modules": {
                "user_cohort": UserCohortService().domain_snapshot(),
                "experiment_group": ExperimentGroupService().domain_snapshot(),
                "segment_compare": SegmentCompareService().domain_snapshot(),
                "anomaly_detector": AnomalyDetectorService().domain_snapshot(),
            },
        },
        "engines": {
            "recalc_engine": RecalcEngineService().domain_snapshot(),
            "field_lineage": FieldLineageService().domain_snapshot(),
        },
    }