"""服务指标引用迁移测试：验证稳定身份、名称契约、分批升级、歧义冻结与回滚。

覆盖用户验收步骤：
1. 旧客户端调用唯一匹配服务，记下实际指标；
2. 旧客户端调用同名歧义配置，确认明确故障；
3. 分批转换，其中一项指标改名；
4. 重放原接口请求，核对名称契约与实际指标引用；
5. 恢复原部署后重新查询，核对名称和实际指标引用。
"""
from __future__ import annotations

import pytest

from app.services.data_service import DataServiceService
from app.services.metric_definition import MetricDefinitionService
from app.services.query_builder import QueryBuilderService
from app.services.service_metric_reference import (
    FROZEN_STATUS,
    MIGRATED_STATUS,
    ServiceMetricReferenceService,
)
from app.store import store

UNIQUE_SERVICE = 11     # 演示-活跃用户服务 -> 唯一匹配「演示活跃用户数」
AMBIGUOUS_SERVICE = 12  # 演示-订单量服务 -> 「演示订单量」对应两笔指标


@pytest.fixture(autouse=True)
def _restore_demo_services():
    """每个用例前把演示服务恢复成迁移前状态，保证用例互不影响。"""
    def reset(service_id: int, name: str) -> None:
        service = store.find("data_service", service_id)
        service.update({
            "关联指标或模型": name,
            "旧名称契约": name,
            "指标UID": "",
            "指标名称快照": "",
            "目标类型": "",
            "目标ID": None,
            "关联凭据指纹": "",
            "引用状态": "未迁移",
            "关联引用": {},
            "关联引用指纹": "",
        })
        service.pop("迁移批次", None)

    reset(UNIQUE_SERVICE, "演示活跃用户数")
    reset(AMBIGUOUS_SERVICE, "演示订单量")
    store.migration_batches().clear()
    store.service_reference_snapshots().clear()
    store.migration_ambiguities().clear()
    store.service_credentials().pop(str(UNIQUE_SERVICE), None)
    store.service_credentials().pop(str(AMBIGUOUS_SERVICE), None)
    yield


def test_identity_is_stable_and_unique():
    store.ensure_metric_identity()
    uids = [row.get("指标UID") for row in store.rows("metric_definition")]
    assert all(uids)
    assert len(uids) == len(set(uids))
    # 每次补齐都不会改动既有身份
    before = dict((row["id"], row["指标UID"]) for row in store.rows("metric_definition"))
    store.ensure_metric_identity()
    after = dict((row["id"], row["指标UID"]) for row in store.rows("metric_definition"))
    assert before == after


def test_legacy_call_records_actual_ref():
    """旧客户端调用唯一匹配服务：名称契约与实际指标引用同时可读。"""
    result = DataServiceService().call_service(UNIQUE_SERVICE, operator="旧客户端")
    assert "error" not in result
    assert result["contract_name"] == "演示活跃用户数"
    assert result["actual_metric_ref"] == "metric-0011"
    assert result["result_count"] >= 1
    # 旧字段一个不少
    for field in ("api", "request_method", "result_count", "query_results"):
        assert field in result


def test_ambiguous_call_fails_explicitly():
    """同名歧义配置：调用明确报错并列出候选，不猜测实际来源。"""
    result = DataServiceService().call_service(AMBIGUOUS_SERVICE, operator="旧客户端")
    assert result["error"]
    assert result["ambiguous"] is True
    assert set(result["candidates"]) == {"metric-0012", "metric-0013"}


def test_batch_migration_and_rename_keeps_same_metric():
    """分批转换后改名，重放旧请求仍读同一指标。"""
    ds = DataServiceService()
    svc = ServiceMetricReferenceService()

    first = ds.call_service(UNIQUE_SERVICE, operator="旧客户端")
    assert first["actual_metric_ref"] == "metric-0011"

    batch = svc.migrate_batch(1, operator="迁移作业")
    assert batch["ok"] is True
    assert batch["converted"][0]["服务接口ID"] == UNIQUE_SERVICE
    assert store.find("data_service", UNIQUE_SERVICE)["指标UID"] == "metric-0011"

    _, msg = MetricDefinitionService().rename_metric(11, "演示活跃用户数（新）")
    assert "metric-0011" in msg

    replay = ds.call_service(UNIQUE_SERVICE, operator="旧客户端",
                             metric_name="演示活跃用户数")
    assert replay["contract_name"] == "演示活跃用户数"
    assert replay["actual_metric_ref"] == "metric-0011"
    assert replay["metric_name"] == "演示活跃用户数（新）"

    comparison = svc.compare_calls(first, replay)
    assert comparison["same_metric"] is True
    assert comparison["name_changed"] is True


def test_ambiguous_service_frozen_keeps_original_value():
    """歧义记录冻结原值：服务配置字段与状态一律不改，并登记可查证记录。"""
    service = store.find("data_service", AMBIGUOUS_SERVICE)
    before = {key: service.get(key) for key in
              ("关联指标或模型", "请求方式", "限流配额", "status")}

    result = ServiceMetricReferenceService().migrate_batch(5, operator="迁移作业")
    frozen = [row for row in result["frozen"] if row["服务接口ID"] == AMBIGUOUS_SERVICE]
    assert frozen, "同名歧义应被冻结并登记"
    assert "冻结" in result["message"]
    assert result["frozen"][0]["候选数量"] == 2

    after = {key: service.get(key) for key in before}
    assert after == before
    assert service["引用状态"] == FROZEN_STATUS

    records = ServiceMetricReferenceService().list_ambiguities(FROZEN_STATUS)
    record = next(r for r in records if r["服务接口ID"] == AMBIGUOUS_SERVICE)
    assert record["原值"] == "演示订单量"
    assert record["候选数量"] == 2
    assert record["状态"] == FROZEN_STATUS

    # 冻结期间调用仍明确报错，并带回歧义记录
    call = DataServiceService().call_service(AMBIGUOUS_SERVICE)
    assert call["error"]
    assert call["ambiguity_record"]["id"] == record["id"]


def test_resolve_ambiguity_binds_explicit_uid():
    svc = ServiceMetricReferenceService()
    svc.migrate_batch(5)
    record = next(r for r in svc.list_ambiguities(FROZEN_STATUS)
                  if r["服务接口ID"] == AMBIGUOUS_SERVICE)
    resolved, msg = svc.resolve_ambiguity(record["id"], "metric-0013", operator="指标负责人")
    assert resolved is not None
    assert "metric-0013" in msg
    assert store.find("data_service", AMBIGUOUS_SERVICE)["指标UID"] == "metric-0013"
    audit = svc.audit_service(AMBIGUOUS_SERVICE)
    assert audit["actual_metric_ref"] == "metric-0013"
    assert audit["转换前快照"]["关联指标原值"] == "演示订单量"


def test_rollback_restores_config_and_credentials():
    svc = ServiceMetricReferenceService()
    batch = svc.migrate_batch(1)
    batch_id = batch["batch"]["批次ID"]
    service = store.find("data_service", UNIQUE_SERVICE)
    credential_before = store.service_credentials()[str(UNIQUE_SERVICE)]["token"]
    assert service["指标UID"] == "metric-0011"

    rollback = svc.rollback_batch(batch_id)
    assert rollback["ok"] is True
    assert service["关联指标或模型"] == "演示活跃用户数"
    assert service["指标UID"] == ""
    assert store.service_credentials()[str(UNIQUE_SERVICE)]["token"] == credential_before

    verify = svc.verify_rollback(batch_id)
    assert verify["ok"] is True

    # 恢复原部署后重新查询，名称契约与实际指标引用仍可核对
    result = DataServiceService().call_service(UNIQUE_SERVICE, operator="回滚后客户端")
    assert result["contract_name"] == "演示活跃用户数"
    assert result["actual_metric_ref"] == "metric-0011"


def test_migrated_client_sends_reference_directly():
    """迁移后客户端可直接带稳定引用调用，与旧名调用读同一指标。"""
    ds = DataServiceService()
    legacy = ds.call_service(UNIQUE_SERVICE, operator="旧客户端")
    migrated_call = ds.call_service(UNIQUE_SERVICE, operator="新客户端",
                                   metric_ref="metric-0011")
    assert migrated_call["resolved_by"] == "reference"
    assert migrated_call["actual_metric_ref"] == legacy["actual_metric_ref"]
    assert ServiceMetricReferenceService().compare_calls(
        legacy, migrated_call)["same_metric"] is True


def test_query_builder_resolves_published_template_by_reference():
    """查询构建器按模板稳定引用解析真实指标，不靠名称。"""
    store.ensure_metric_identity()
    template = store.find("query_builder", 11)
    result = QueryBuilderService().execute_query(11, operator="构建器测试")
    assert "error" not in result
    assert result["metric_ref"] == template["指标UID"] == "metric-0011"
    assert result["rows"][0]["指标引用"] == "metric-0011"

    # 指标改名后，模板引用不变，查询仍命中同一指标
    MetricDefinitionService().rename_metric(11, "演示活跃用户数（改名）")
    again = QueryBuilderService().execute_query(11, operator="构建器测试")
    assert again["metric_ref"] == "metric-0011"
    assert again["metric_name"] == "演示活跃用户数（改名）"


def test_query_builder_rejects_ambiguous_name():
    """没有稳定引用的模板遇到同名指标时明确失败，而不是任选一笔。"""
    template = store.find("query_builder", 12)
    template["指标UID"] = ""
    result = QueryBuilderService().execute_query(12, operator="构建器测试")
    assert "error" in result
    assert "无法唯一确定" in result["error"]


def test_migration_status_counts_migrated_and_frozen():
    svc = ServiceMetricReferenceService()
    svc.migrate_batch(1)
    svc.migrate_batch(5)
    status = svc.migration_status()
    assert status["migrated"] >= 1
    assert status["frozen"] >= 1
    assert status["open_ambiguities"] >= 1
    assert status["batches"] >= 2
