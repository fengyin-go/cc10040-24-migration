import { request } from './client'
import type {
  AmbiguityRecord,
  CallComparison,
  CallSnapshot,
  MigrationStatus,
  ServiceAudit,
} from '@/domain/service_metric_reference'

/** 服务指标引用迁移相关的后端接口。 */

export async function loadMigrationStatus(): Promise<MigrationStatus> {
  const response = await request('/api/cross/service-reference/status')
  if (!response.ok) throw new Error('迁移进度读取失败')
  return await response.json()
}

export async function loadAmbiguities(status = '待处理'): Promise<AmbiguityRecord[]> {
  const response = await request(
    `/api/cross/service-reference/ambiguities?status=${encodeURIComponent(status)}`,
  )
  if (!response.ok) throw new Error('歧义记录读取失败')
  const payload = (await response.json()) as { items: AmbiguityRecord[] }
  return payload.items
}

export async function migrateBatch(batchSize: number): Promise<{
  ok: boolean
  message: string
  converted: { 服务接口ID: number; actual_metric_ref: string }[]
  frozen: AmbiguityRecord[]
}> {
  const response = await request(
    `/api/cross/service-reference/migrate-batch?batch_size=${batchSize}`,
    { method: 'POST' },
  )
  if (!response.ok) throw new Error('分批转换失败')
  return await response.json()
}

export async function auditService(serviceId: number): Promise<ServiceAudit> {
  const response = await request(`/api/cross/service-reference/${serviceId}/audit`)
  if (!response.ok) throw new Error('服务核对失败')
  return await response.json()
}

export async function callService(
  serviceId: number,
  options: { metricName?: string; metricRef?: string } = {},
): Promise<CallSnapshot> {
  const params = new URLSearchParams()
  if (options.metricName) params.set('metric_name', options.metricName)
  if (options.metricRef) params.set('metric_ref', options.metricRef)
  const query = params.toString() ? `?${params}` : ''
  const response = await request(`/api/cross/data-service/${serviceId}/call${query}`, {
    method: 'POST',
  })
  if (!response.ok) throw new Error('服务调用失败')
  return await response.json()
}

export async function resolveAmbiguity(
  ambiguityId: number,
  metricUid: string,
): Promise<{ ok: boolean; message: string }> {
  const response = await request(
    `/api/cross/service-reference/ambiguities/${ambiguityId}/resolve?metric_uid=${encodeURIComponent(metricUid)}`,
    { method: 'POST' },
  )
  if (!response.ok) throw new Error('歧义处理失败')
  return await response.json()
}

export async function rollbackBatch(batchId: number): Promise<{
  ok: boolean
  message: string
}> {
  const response = await request(
    `/api/cross/service-reference/batches/${batchId}/rollback`,
    { method: 'POST' },
  )
  if (!response.ok) throw new Error('批次回滚失败')
  return await response.json()
}

export async function verifyRollback(batchId: number): Promise<{
  ok: boolean
  checks: { 服务接口ID: number; ok: boolean }[]
}> {
  const response = await request(`/api/cross/service-reference/batches/${batchId}/verify`)
  if (!response.ok) throw new Error('回滚核对失败')
  return await response.json()
}

export type { CallComparison }
