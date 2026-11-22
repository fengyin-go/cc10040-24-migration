/** 服务指标引用迁移的纯前端领域逻辑：类型、状态描述和核对规则。 */

export type ReferenceState = '未迁移' | '已迁移' | '待处理' | '待补充' | '已回滚' | string

export type MigrationStatus = {
  total_services: number
  migrated: number
  frozen: number
  legacy: number
  batches: number
  open_ambiguities: number
  batch_summaries: {
    批次ID: number
    转换: number
    冻结: number
    执行时间: string
    已回滚: boolean
  }[]
}

export type ServiceAudit = {
  服务接口ID: number
  接口名称: string
  旧名称契约: string
  引用状态: ReferenceState
  actual_metric_ref: string | null
  当前指标名称: string | null
  指标名称快照: string
  active_caliber: string | null
  published_query_templates: { id: number; 模板名称: string; 指标UID: string; 关联指标: string }[]
  关联引用指纹: string
  转换前快照: Record<string, string | number | null> | null
  解析结果: {
    ok: boolean
    error?: string
    ambiguous?: boolean
    candidates?: string[]
  }
}

export type AmbiguityRecord = {
  id: number
  服务接口ID: number
  接口名称: string
  指标名称: string
  原值: string
  冻结值: string
  候选数量: number
  候选指标: { id: number; 指标UID: string; 指标名称: string }[]
  冻结时间: string
  状态: string
}

export type CallSnapshot = {
  contract_name?: string | null
  actual_metric_ref?: string | null
  metric_name?: string | null
  resolved_by?: 'name' | 'reference' | string | null
  error?: string
}

export type CallComparison = {
  same_metric: boolean
  first_actual_metric_ref: string | null
  second_actual_metric_ref: string | null
  name_changed: boolean
  summary: string
}

/** 两笔调用是否仍在读同一指标：只看稳定引用，不看名称。 */
export function compareCalls(first: CallSnapshot, second: CallSnapshot): CallComparison {
  const firstRef = first.actual_metric_ref ?? null
  const secondRef = second.actual_metric_ref ?? null
  const same = Boolean(firstRef) && firstRef === secondRef
  const nameChanged = (first.metric_name ?? '') !== (second.metric_name ?? '')
  let summary: string
  if (!firstRef || !secondRef) {
    summary = '至少一笔调用没有返回实际指标引用，无法核对'
  } else if (same) {
    summary = nameChanged
      ? `两次调用仍读同一指标 ${firstRef}，只是展示名称变了`
      : `两次调用读同一指标 ${firstRef}`
  } else {
    summary = `两次调用读的不是同一指标：${firstRef} ≠ ${secondRef}`
  }
  return {
    same_metric: same,
    first_actual_metric_ref: firstRef,
    second_actual_metric_ref: secondRef,
    name_changed: nameChanged,
    summary,
  }
}

export function referenceStateLabel(state: ReferenceState | undefined): string {
  switch (state) {
    case '已迁移':
      return '已迁移：按稳定指标引用'
    case '待处理':
      return '待处理：同名歧义已冻结原值'
    case '待补充':
      return '待补充：名称尚未对应指标'
    case '已回滚':
      return '已回滚：恢复存量名称关联'
    default:
      return '未迁移：仍按展示名称关联'
  }
}

export function progressPercent(status: MigrationStatus | null): number {
  if (!status || status.total_services === 0) return 0
  return Math.round((status.migrated / status.total_services) * 100)
}

export function canMigrate(status: MigrationStatus | null): boolean {
  return Boolean(status && status.legacy > 0)
}
