"""服务指标引用迁移领域服务。

背景：数据服务早期按「展示名称」关联指标（关联指标或模型 字段就是指标名）。
指标一旦改名或出现同名指标，旧服务究竟是查哪一笔数据就无法确认。

本模块把存量数据服务的名称引用迁移为稳定的「服务指标引用」：
  * 指标拥有稳定身份（指标UID），改名/重名都不会改变；
  * 查询构建器执行模板时优先用引用解析到真实指标，没有引用的存量模板仍按名称走；
  * 调用返回里同时给出「调用时名称」与「实际指标引用（UID）」，
    旧客户端按名称契约读字段不受影响，同时可以核对实际来源；
  * 同一已发布服务可能先后收到旧名调用和迁移后引用调用，两次返回的
    actual_metric_ref 一致即说明仍在读同一指标；
  * 转换前保留服务配置原值、关联凭据和请求契约快照，供回滚与历史查证；
  * 同名导致无法唯一确定目标时冻结原值并登记歧义，调用直接明确报错，
    处理完毕前不会猜测绑定到某一笔指标。
"""
from __future__ import annotations

import copy
import hashlib
import time
from typing import Any

from app.store import store

FROZEN_STATUS = "待处理"
MIGRATED_STATUS = "已迁移"


class AmbiguousServiceReference(Exception):
    """服务引用在目标指标上存在同名歧义，本次转换/调用被阻塞。"""

    def __init__(self, service_id: int, metric_name: str, candidates: list[int]):
        self.service_id = service_id
        self.metric_name = metric_name
        self.candidates = candidates
        super().__init__(
            f"服务接口 {service_id} 引用的指标名称「{metric_name}」对应 "
            f"{len(candidates)} 笔指标（id={', '.join(map(str, candidates))}），"
            "无法唯一确定目标；原值已冻结，请先处理歧义记录"
        )


class ServiceMetricReferenceService:
    """服务指标引用的转换、解析、核对和回滚。"""

    MODULE = "data_service"
    REFERENCE_FIELD = "关联指标或模型"
    CONTRACT_FIELD = "接口名称"

    # ── 基础查询 ──

    def ensure_identity(self) -> None:
        store.ensure_metric_identity()

    def _metric_by_uid(self, metric_uid: str) -> dict[str, Any] | None:
        self.ensure_identity()
        return store.metric_by_uid(metric_uid)

    @staticmethod
    def _legacy_metric(contract_name: str) -> dict[str, Any]:
        """没有指标定义时，用名称构造一个只读占位，保证存量调用字段不中断。"""
        return {
            "指标UID": f"legacy-name:{contract_name}",
            "指标名称": contract_name,
            "计算口径": "",
            "统计粒度": "",
            "legacy_unresolved": True,
        }

    def _published_query_templates_for_metric(self, metric: dict[str, Any]) -> list[dict[str, Any]]:
        """按 UID 找已发布模板，找不到时退回名称（兼容存量模板）。"""
        metric_uid = str(metric.get("指标UID", ""))
        metric_name = str(metric.get("指标名称", ""))
        templates = [
            row for row in store.rows("query_builder")
            if row.get("status") == "已发布"
            and (row.get("指标UID") == metric_uid
                 or (not row.get("指标UID") and row.get("关联指标") == metric_name))
        ]
        return templates

    def _credential_for(self, service: dict[str, Any]) -> dict[str, Any]:
        service_id = str(service.get("id"))
        cred = store.service_credentials().get(service_id)
        if cred is None:
            # 存量服务默认带一条凭据：迁移过程只读不写，回滚后仍可原样使用。
            cred = {
                "token": f"svc-token-{service.get('id')}",
                "rotation": "保留原值",
            }
            store.service_credentials()[service_id] = cred
        return cred

    @staticmethod
    def _token_fingerprint(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]

    def _service_snapshot(self, service: dict[str, Any]) -> dict[str, Any]:
        """转换前快照：服务配置原值 + 关联凭据 + 旧客户端名称契约。"""
        cred = self._credential_for(service)
        return {
            "服务接口ID": service.get("id"),
            "服务名称快照": service.get(self.CONTRACT_FIELD, ""),
            "旧名称契约": service.get("旧名称契约") or service.get(self.REFERENCE_FIELD, ""),
            "关联指标原值": service.get(self.REFERENCE_FIELD, ""),
            "目标类型": service.get("目标类型", ""),
            "目标ID": service.get("目标ID"),
            "指标UID": service.get("指标UID", ""),
            "指标名称快照": service.get("指标名称快照", ""),
            "状态快照": service.get("status", ""),
            "凭据指纹": self._token_fingerprint(cred.get("token", "")),
            "凭据轮换": cred.get("rotation", ""),
            "快照时间": time.strftime("%Y-%m-%d %H:%M:%S"),
        }

    def _reference_payload(self, service: dict[str, Any]) -> dict[str, Any]:
        return {
            "服务接口ID": service.get("id"),
            "目标类型": service.get("目标类型", ""),
            "目标ID": service.get("目标ID"),
            "指标UID": service.get("指标UID", ""),
            "指标名称快照": service.get("指标名称快照", ""),
            "旧名称契约": service.get("旧名称契约", ""),
            "关联凭据指纹": service.get("关联凭据指纹", ""),
        }

    def _ambiguity_for_name(self, metric_name: str) -> list[dict[str, Any]]:
        return [
            row for row in store.migration_ambiguities()
            if row.get("指标名称") == metric_name and row.get("状态") == FROZEN_STATUS
        ]

    # ── 解析：名称契约 -> 稳定引用 ──

    def resolve_reference(self, service_id: int,
                          raw_name: str | None = None,
                          metric_ref: str | None = None) -> dict[str, Any]:
        """解析一次调用要读哪笔指标。

        raw_name 为旧客户端传来的名称契约（缺省用服务配置里的原值）；
        metric_ref 为迁移后客户端直接带来的稳定引用，优先于名称。
        返回结构里同时包含调用方看到的名称和内部实际指标引用。
        """
        self.ensure_identity()
        service = store.find(self.MODULE, service_id)
        if service is None:
            return {"ok": False, "error": f"服务接口 {service_id} 不存在"}

        bound_uid = service.get("指标UID")
        contract_name = raw_name if raw_name not in (None, "") else (
            service.get("旧名称契约") or service.get(self.REFERENCE_FIELD, ""))
        requested_uid = (metric_ref or "").strip() or (
            str(bound_uid) if bound_uid else "")

        # 迁移后的引用（请求携带或服务已绑定）：引用就是真相，名称只作为契约返回。
        if requested_uid:
            metric = self._metric_by_uid(requested_uid)
            if metric is None:
                return {"ok": False,
                        "error": f"服务接口 {service_id} 的指标引用 {requested_uid} 找不到对应指标",
                        "actual_metric_ref": requested_uid}
            return {
                "ok": True,
                "service_id": service_id,
                "contract_name": contract_name,
                "actual_metric_ref": metric["指标UID"],
                "metric": metric,
                "resolved_by": "reference",
            }

        # 未迁移：按名称解析。
        candidates = store.metrics_by_name(str(contract_name))
        if not candidates:
            # 分批升级期间存量名称可能还没有对应指标定义：保持旧客户端可调用，
            # 但明确标记为未绑定稳定身份的 legacy 来源。
            return {
                "ok": True,
                "service_id": service_id,
                "contract_name": contract_name,
                "actual_metric_ref": f"legacy-name:{contract_name}",
                "metric": self._legacy_metric(contract_name),
                "legacy_unresolved": True,
                "resolved_by": "name",
            }
        if len(candidates) > 1:
            ambiguity = self._ambiguity_for_name(str(contract_name))
            record = ambiguity[0] if ambiguity else None
            return {
                "ok": False,
                "error": (f"关联指标名称「{contract_name}」对应 {len(candidates)} 笔指标，"
                          "无法唯一确定实际来源"),
                "contract_name": contract_name,
                "ambiguous": True,
                "candidates": [c["指标UID"] for c in candidates],
                "ambiguity_record": record,
            }
        metric = candidates[0]
        return {
            "ok": True,
            "service_id": service_id,
            "contract_name": contract_name,
            "actual_metric_ref": metric["指标UID"],
            "metric": metric,
            "resolved_by": "name",
        }

    # ── 转换（分批） ──

    def list_legacy_services(self) -> list[dict[str, Any]]:
        self.ensure_identity()
        return [
            row for row in store.rows(self.MODULE)
            if not row.get("指标UID") or row.get("引用状态") != MIGRATED_STATUS
        ]

    def plan_batch(self, batch_size: int = 1) -> list[dict[str, Any]]:
        """列出待转换队列。

        唯一匹配优先转换，其次是需要暴露的同名歧义，最后是名称还未对应
        指标定义的存量（待补充）；冻结的歧义记录不会再次进入批次。
        """
        legacy = self.list_legacy_services()
        legacy = [row for row in legacy
                  if row.get("引用状态") not in (FROZEN_STATUS, MIGRATED_STATUS)]

        def priority(row: dict[str, Any]) -> tuple[int, int]:
            name = str(row.get(self.REFERENCE_FIELD, ""))
            match_count = len(store.metrics_by_name(name))
            if match_count == 1:
                return (0, int(row.get("id", 0)))
            if match_count > 1:
                return (1, int(row.get("id", 0)))
            return (2, int(row.get("id", 0)))

        legacy.sort(key=priority)
        if batch_size <= 0:
            batch_size = 1
        return legacy[:batch_size]

    def migrate_batch(self, batch_size: int = 1,
                      operator: str = "迁移作业") -> dict[str, Any]:
        """按小批次转换，单笔失败不影响同批其他服务。

        唯一匹配 -> 改写为 UID 引用并保留旧名称契约；
        同名歧义 -> 冻结原值，登记歧义，返回明确故障。
        """
        self.ensure_identity()
        batch = self.plan_batch(batch_size)
        batch_id = len(store.migration_batches()) + 1
        converted: list[dict[str, Any]] = []
        frozen: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        kept_credentials: list[dict[str, Any]] = []

        for service in batch:
            raw_name = str(service.get(self.REFERENCE_FIELD, ""))
            candidates = store.metrics_by_name(raw_name)
            if not candidates:
                # 名称还没对应到指标定义：不冻结、不改原值，标记待补充后继续按名称服务。
                service["引用状态"] = "待补充"
                skipped.append({
                    "服务接口ID": service.get("id"),
                    "接口名称": service.get(self.CONTRACT_FIELD, ""),
                    "指标名称": raw_name,
                    "原因": "名称未匹配到指标定义，保持名称关联待补充",
                })
                continue
            if len(candidates) != 1:
                record = self._freeze_ambiguity(service, raw_name, candidates, batch_id, operator)
                frozen.append(record)
                continue

            metric = candidates[0]
            snapshot = self._service_snapshot(service)
            store.service_reference_snapshots()[str(service.get("id"))] = snapshot
            cred = self._credential_for(service)

            service["旧名称契约"] = raw_name
            service["指标UID"] = metric["指标UID"]
            service["指标名称快照"] = metric.get("指标名称", raw_name)
            service["目标类型"] = "指标"
            service["目标ID"] = metric.get("id")
            service["关联凭据指纹"] = self._token_fingerprint(cred.get("token", ""))
            service["引用状态"] = MIGRATED_STATUS
            service["关联引用"] = self._reference_payload(service)
            service["关联引用指纹"] = store.reference_fingerprint(service["关联引用"])
            service["迁移批次"] = batch_id

            converted.append({
                "服务接口ID": service.get("id"),
                "接口名称": service.get(self.CONTRACT_FIELD, ""),
                "旧名称契约": raw_name,
                "actual_metric_ref": metric["指标UID"],
                "指标名称快照": metric.get("指标名称", ""),
                "关联引用指纹": service["关联引用指纹"],
            })
            kept_credentials.append({
                "服务接口ID": service.get("id"),
                "凭据指纹": snapshot["凭据指纹"],
                "凭据轮换": snapshot["凭据轮换"],
            })

        batch_record = {
            "批次ID": batch_id,
            "批次大小": batch_size,
            "执行人": operator,
            "执行时间": time.strftime("%Y-%m-%d %H:%M:%S"),
            "计划转换": [row.get("id") for row in batch],
            "转换成功": converted,
            "冻结歧义": frozen,
            "待补充": skipped,
            "保留凭据": kept_credentials,
        }
        store.migration_batches().append(batch_record)

        if frozen and not converted:
            return {
                "ok": False,
                "batch": batch_record,
                "converted": converted,
                "frozen": frozen,
                "skipped": skipped,
                "message": "批次内服务均因同名歧义被冻结，原值未改写，需先处理歧义记录",
            }
        return {
            "ok": True,
            "batch": batch_record,
            "converted": converted,
            "frozen": frozen,
            "skipped": skipped,
            "message": (f"批次 {batch_id} 转换 {len(converted)} 笔，"
                        f"冻结 {len(frozen)} 笔，待补充 {len(skipped)} 笔"),
        }

    def _freeze_ambiguity(self, service: dict[str, Any], raw_name: str,
                          candidates: list[dict[str, Any]], batch_id: int,
                          operator: str) -> dict[str, Any]:
        """冻结原值：服务配置一个字段都不改，原样登记以便查证。"""
        service["引用状态"] = FROZEN_STATUS
        existing = next(
            (row for row in store.migration_ambiguities()
             if row.get("服务接口ID") == service.get("id")
             and row.get("指标名称") == raw_name
             and row.get("状态") == FROZEN_STATUS),
            None,
        )
        if existing is None:
            existing = {
                "id": len(store.migration_ambiguities()) + 1,
                "服务接口ID": service.get("id"),
                "接口名称": service.get(self.CONTRACT_FIELD, ""),
                "指标名称": raw_name,
                "原值": service.get(self.REFERENCE_FIELD, ""),
                "原状态": service.get("status", ""),
                "候选指标": [
                    {"id": c.get("id"), "指标UID": c.get("指标UID"),
                     "指标名称": c.get("指标名称")}
                    for c in candidates
                ],
                "候选数量": len(candidates),
                "冻结值": raw_name,
                "冻结时间": time.strftime("%Y-%m-%d %H:%M:%S"),
                "批次ID": batch_id,
                "处理人": operator,
                "状态": FROZEN_STATUS,
                "异常": True,
                "pending": True,
            }
            store.migration_ambiguities().append(existing)
        return existing

    # ── 歧义处理 ──

    def list_ambiguities(self, status: str | None = None) -> list[dict[str, Any]]:
        rows = store.migration_ambiguities()
        if status:
            rows = [row for row in rows if row.get("状态") == status]
        return rows

    def resolve_ambiguity(self, ambiguity_id: int, metric_uid: str,
                          operator: str = "指标负责人") -> tuple[dict[str, Any] | None, str]:
        """由负责人明确指定目标指标 UID，解除冻结并完成该服务转换。"""
        record = next(
            (row for row in store.migration_ambiguities()
             if int(row.get("id", 0)) == ambiguity_id),
            None,
        )
        if record is None:
            return None, f"歧义记录 {ambiguity_id} 不存在"
        if record.get("状态") != FROZEN_STATUS:
            return None, f"歧义记录 {ambiguity_id} 已处理（{record.get('状态')}）"

        metric = self._metric_by_uid(metric_uid)
        if metric is None:
            return None, f"指标引用 {metric_uid} 找不到对应指标"
        if int(metric.get("id", 0)) not in [
            int(c.get("id", 0)) for c in record.get("候选指标", [])
        ]:
            return None, f"指标 {metric_uid} 不在歧义记录的候选范围内"

        service = store.find(self.MODULE, int(record.get("服务接口ID", 0)))
        if service is None:
            return None, "歧义记录关联的服务接口已不存在"

        raw_name = str(record.get("指标名称", ""))
        snapshot = self._service_snapshot(service)
        store.service_reference_snapshots()[str(service.get("id"))] = snapshot
        cred = self._credential_for(service)

        service["旧名称契约"] = service.get("旧名称契约") or raw_name
        service["指标UID"] = metric["指标UID"]
        service["指标名称快照"] = metric.get("指标名称", raw_name)
        service["目标类型"] = "指标"
        service["目标ID"] = metric.get("id")
        service["关联凭据指纹"] = self._token_fingerprint(cred.get("token", ""))
        service["引用状态"] = MIGRATED_STATUS
        service["关联引用"] = self._reference_payload(service)
        service["关联引用指纹"] = store.reference_fingerprint(service["关联引用"])

        record["状态"] = "已处理"
        record["指定指标UID"] = metric["指标UID"]
        record["处理人"] = operator
        record["处理时间"] = time.strftime("%Y-%m-%d %H:%M:%S")
        record["异常"] = False
        record["pending"] = False
        return record, f"歧义记录 {ambiguity_id} 已绑定到 {metric['指标UID']}"

    # ── 回滚 ──

    def rollback_batch(self, batch_id: int) -> dict[str, Any]:
        """按转换前快照恢复服务配置，凭据原样保留。"""
        batch = next(
            (row for row in store.migration_batches() if int(row.get("批次ID", 0)) == batch_id),
            None,
        )
        if batch is None:
            return {"ok": False, "message": f"迁移批次 {batch_id} 不存在"}

        restored: list[dict[str, Any]] = []
        snapshots = store.service_reference_snapshots()
        for item in batch.get("转换成功", []):
            service_id = int(item.get("服务接口ID", 0))
            service = store.find(self.MODULE, service_id)
            snapshot = snapshots.get(str(service_id))
            if service is None or snapshot is None:
                continue
            service[self.REFERENCE_FIELD] = (
                snapshot.get("关联指标原值") or snapshot.get("旧名称契约") or "")
            service["指标UID"] = ""
            service["指标名称快照"] = ""
            service["目标类型"] = ""
            service["目标ID"] = None
            service["关联凭据指纹"] = snapshot.get("凭据指纹", "")
            service["引用状态"] = "未迁移"
            service["关联引用"] = {}
            service["关联引用指纹"] = ""
            service.pop("迁移批次", None)
            restored.append({
                "服务接口ID": service_id,
                "关联指标或模型": service.get(self.REFERENCE_FIELD),
                "旧名称契约": snapshot.get("旧名称契约", ""),
                "凭据指纹": snapshot.get("凭据指纹", ""),
            })

        for record in store.migration_ambiguities():
            if int(record.get("批次ID", 0)) == batch_id and record.get("状态") == FROZEN_STATUS:
                record["状态"] = "已回滚"
                record["pending"] = False
                record["异常"] = False
                for service_id in [record.get("服务接口ID")]:
                    service = store.find(self.MODULE, int(service_id or 0))
                    if service is not None:
                        service["引用状态"] = "未迁移"

        batch["回滚时间"] = time.strftime("%Y-%m-%d %H:%M:%S")
        return {
            "ok": True,
            "batch_id": batch_id,
            "restored": restored,
            "message": f"批次 {batch_id} 已回滚，恢复 {len(restored)} 笔服务配置，凭据保持原值",
        }

    def verify_rollback(self, batch_id: int) -> dict[str, Any]:
        """回滚后核对：配置与凭据指纹是否与转换前快照一致。"""
        batch = next(
            (row for row in store.migration_batches() if int(row.get("批次ID", 0)) == batch_id),
            None,
        )
        if batch is None:
            return {"ok": False, "message": f"迁移批次 {batch_id} 不存在"}
        snapshots = store.service_reference_snapshots()
        checks: list[dict[str, Any]] = []
        for item in batch.get("转换成功", []):
            service_id = int(item.get("服务接口ID", 0))
            service = store.find(self.MODULE, service_id)
            snapshot = snapshots.get(str(service_id), {})
            if service is None:
                checks.append({"服务接口ID": service_id, "ok": False,
                               "reason": "服务接口不存在"})
                continue
            cred = self._credential_for(service)
            checks.append({
                "服务接口ID": service_id,
                "ok": (service.get(self.REFERENCE_FIELD) == snapshot.get("关联指标原值")
                       and self._token_fingerprint(cred.get("token", ""))
                       == snapshot.get("凭据指纹")
                       and not service.get("指标UID")),
                "关联指标或模型": service.get(self.REFERENCE_FIELD),
                "原值": snapshot.get("关联指标原值"),
                "凭据指纹": self._token_fingerprint(cred.get("token", "")),
                "原凭据指纹": snapshot.get("凭据指纹"),
                "指标UID": service.get("指标UID", ""),
            })
        return {
            "ok": all(check["ok"] for check in checks) if checks else False,
            "batch_id": batch_id,
            "checks": checks,
        }

    # ── 核对 ──

    def audit_service(self, service_id: int) -> dict[str, Any]:
        """核对一笔服务：名称契约、实际指标引用、口径、模板和转换前快照。"""
        service = store.find(self.MODULE, service_id)
        if service is None:
            return {"error": f"服务接口 {service_id} 不存在"}
        self.ensure_identity()

        resolved = self.resolve_reference(service_id)
        snapshot = store.service_reference_snapshots().get(str(service_id))
        metric = resolved.get("metric") if resolved.get("ok") else None
        calibers = store.calibers_for_metric(metric) if metric else []
        active = next((c for c in calibers if c.get("status") == "已生效"), None)
        templates = [
            {"id": t.get("id"), "模板名称": t.get("模板名称"),
             "指标UID": t.get("指标UID", ""), "关联指标": t.get("关联指标", "")}
            for t in self._published_query_templates_for_metric(metric)
        ] if metric else []

        return {
            "服务接口ID": service_id,
            "接口名称": service.get(self.CONTRACT_FIELD, ""),
            "旧名称契约": service.get("旧名称契约") or service.get(self.REFERENCE_FIELD, ""),
            "引用状态": service.get("引用状态", "未迁移"),
            "actual_metric_ref": resolved.get("actual_metric_ref"),
            "当前指标名称": metric.get("指标名称") if metric else None,
            "指标名称快照": service.get("指标名称快照", ""),
            "active_caliber": active.get("版本号") if active else None,
            "published_query_templates": templates,
            "关联引用指纹": service.get("关联引用指纹", ""),
            "转换前快照": snapshot,
            "解析结果": resolved,
        }

    def compare_calls(self, first: dict[str, Any],
                      second: dict[str, Any]) -> dict[str, Any]:
        """核对两次调用是否仍在读同一笔指标（名称可因改名而不同）。"""
        first_ref = first.get("actual_metric_ref")
        second_ref = second.get("actual_metric_ref")
        return {
            "same_metric": bool(first_ref) and first_ref == second_ref,
            "first_actual_metric_ref": first_ref,
            "second_actual_metric_ref": second_ref,
            "first_contract_name": first.get("contract_name"),
            "second_contract_name": second.get("contract_name"),
            "first_metric_name": first.get("metric_name"),
            "second_metric_name": second.get("metric_name"),
            "name_changed": first.get("metric_name") != second.get("metric_name"),
        }

    def migration_status(self) -> dict[str, Any]:
        self.ensure_identity()
        services = store.rows(self.MODULE)
        migrated = [s for s in services if s.get("引用状态") == MIGRATED_STATUS]
        frozen = [s for s in services if s.get("引用状态") == FROZEN_STATUS]
        legacy = [s for s in services
                  if s.get("引用状态") not in (MIGRATED_STATUS, FROZEN_STATUS)]
        return {
            "total_services": len(services),
            "migrated": len(migrated),
            "frozen": len(frozen),
            "legacy": len(legacy),
            "batches": len(store.migration_batches()),
            "open_ambiguities": len(self.list_ambiguities(FROZEN_STATUS)),
            "batch_summaries": [
                {
                    "批次ID": row.get("批次ID"),
                    "转换": len(row.get("转换成功", [])),
                    "冻结": len(row.get("冻结歧义", [])),
                    "执行时间": row.get("执行时间"),
                    "已回滚": bool(row.get("回滚时间")),
                }
                for row in store.migration_batches()
            ],
        }

    # ── 名称契约字段 ──

    def public_contract(self, service: dict[str, Any]) -> dict[str, Any]:
        """旧客户端字段约定：字段名和值都保持迁移前的名称形态。

        迁移后服务内部存的是 UID 引用，这里仍回填旧名称契约，避免旧客户端
        读「关联指标或模型」时看到一串 UID。
        """
        return {
            "接口名称": service.get(self.CONTRACT_FIELD, ""),
            "关联指标或模型": (service.get("旧名称契约")
                              or service.get(self.REFERENCE_FIELD, "")),
            "请求方式": service.get("请求方式", ""),
            "限流配额": service.get("限流配额", ""),
            "发布状态": service.get("发布状态", ""),
        }


service_metric_reference = ServiceMetricReferenceService()
