<template>
  <section class="page" data-testid="reference-migration">
    <header class="page-head">
      <div>
        <h2>服务指标引用迁移</h2>
        <p class="page-desc">
          把数据服务的展示名称关联转换为稳定指标引用；名称只作旧客户端契约，实际来源以指标引用核对。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn" data-testid="reload" type="button" @click="reload">刷新</button>
        <button
          class="btn primary"
          data-testid="migrate-batch"
          type="button"
          :disabled="busy"
          @click="runMigrateBatch"
        >
          转换下一批
        </button>
      </div>
    </header>

    <div class="stat-row">
      <article class="stat-card">
        <span class="stat-label">服务接口</span>
        <strong class="stat-value">{{ status?.total_services ?? 0 }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">已迁移</span>
        <strong class="stat-value">{{ status?.migrated ?? 0 }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">待处理歧义</span>
        <strong class="stat-value">{{ status?.open_ambiguities ?? 0 }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">迁移批次</span>
        <strong class="stat-value">{{ status?.batches ?? 0 }}</strong>
      </article>
    </div>

    <div class="progress-track" :title="`${progress}%`">
      <div class="progress-fill" :style="{ width: `${progress}%` }" />
    </div>
    <p class="status-legend">转换进度 {{ progress }}%（{{ status?.migrated ?? 0 }}/{{ status?.total_services ?? 0 }}）</p>

    <h3 class="section-title">服务核对</h3>
    <table class="data-table">
      <thead>
        <tr>
          <th>接口名称</th>
          <th>旧名称契约</th>
          <th>引用状态</th>
          <th>实际指标引用</th>
          <th>当前指标名称</th>
          <th>核对 / 调用</th>
        </tr>
      </thead>
      <tbody>
        <tr
          v-for="service in services"
          :key="String(service.id)"
          :data-testid="`service-${service.id}`"
        >
          <td>{{ service['接口名称'] }}</td>
          <td>{{ auditOf(Number(service.id))?.旧名称契约 ?? service['关联指标或模型'] }}</td>
          <td>{{ referenceStateLabel(auditOf(Number(service.id))?.引用状态) }}</td>
          <td><code data-testid="actual-ref">{{ auditOf(Number(service.id))?.actual_metric_ref ?? '—' }}</code></td>
          <td>{{ auditOf(Number(service.id))?.当前指标名称 ?? '—' }}</td>
          <td class="row-actions">
            <button class="link" type="button" @click="audit(Number(service.id))">核对</button>
            <button
              class="link"
              type="button"
              :data-testid="`call-${service.id}`"
              @click="callOnce(Number(service.id))"
            >
              以旧名调用
            </button>
          </td>
        </tr>
        <tr v-if="!services.length">
          <td colspan="6" class="empty-state">暂无服务接口</td>
        </tr>
      </tbody>
    </table>

    <section v-if="selectedAudit" class="panel" data-testid="audit-panel">
      <header class="panel-head">
        <h3>{{ selectedAudit.接口名称 }} · 核对结果</h3>
        <button class="link" type="button" @click="selectedAudit = null">关闭</button>
      </header>
      <dl class="detail-grid">
        <dt>旧名称契约</dt><dd>{{ selectedAudit.旧名称契约 }}</dd>
        <dt>实际指标引用</dt><dd><code>{{ selectedAudit.actual_metric_ref ?? '—' }}</code></dd>
        <dt>当前指标名称</dt><dd>{{ selectedAudit.当前指标名称 ?? '—' }}</dd>
        <dt>生效口径</dt><dd>{{ selectedAudit.active_caliber ?? '—' }}</dd>
        <dt>引用指纹</dt><dd>{{ selectedAudit.关联引用指纹 || '—' }}</dd>
        <dt>已发布模板</dt>
        <dd>{{ selectedAudit.published_query_templates.map((item) => item.模板名称).join('、') || '—' }}</dd>
        <dt>转换前原值</dt>
        <dd>{{ selectedAudit.转换前快照?.['关联指标原值'] ?? '—' }}</dd>
        <dt>解析故障</dt>
        <dd>
          <span v-if="selectedAudit.解析结果.error" class="error-text">{{ selectedAudit.解析结果.error }}</span>
          <span v-else>无</span>
        </dd>
      </dl>
    </section>

    <section class="panel" data-testid="call-compare">
      <header class="panel-head">
        <h3>两笔调用核对</h3>
        <button class="link" type="button" @click="clearCalls">清空</button>
      </header>
      <p class="page-desc">
        同一个已发布服务先后收到旧名调用与迁移后引用调用；名称可能因指标改名而不同，
        只要实际指标引用一致就仍在读同一指标。
      </p>
      <p class="filter-bar">
        <label class="filter-item">
          <span>服务接口 ID</span>
          <input v-model="callServiceId" data-testid="call-service-id" />
        </label>
        <label class="filter-item">
          <span>旧客户端名称契约（可留空）</span>
          <input v-model="callContractName" data-testid="call-contract-name" />
        </label>
        <label class="filter-item">
          <span>迁移后指标引用（优先，可留空）</span>
          <input v-model="callMetricRef" data-testid="call-metric-ref" />
        </label>
        <button class="btn" type="button" @click="recordCall">记录一笔调用</button>
      </p>
      <ol class="call-list">
        <li v-for="(item, index) in calls" :key="index">
          <span>#{{ index + 1 }}</span>
          <span>按{{ item.resolved_by === 'reference' ? '引用' : '名称' }}解析</span>
          <span>契约：{{ item.contract_name ?? '—' }}</span>
          <span>实际引用：<code>{{ item.actual_metric_ref ?? '—' }}</code></span>
          <span>当前名称：{{ item.metric_name ?? '—' }}</span>
          <span v-if="item.error" class="error-text">{{ item.error }}</span>
        </li>
        <li v-if="!calls.length" class="empty-state">还没有记录调用</li>
      </ol>
      <p v-if="comparison" :class="['compare-result', comparison.same_metric ? 'ok' : 'bad']" data-testid="compare-result">
        {{ comparison.summary }}
      </p>
    </section>

    <h3 class="section-title">歧义记录（原值冻结，供历史查证）</h3>
    <table class="data-table">
      <thead>
        <tr>
          <th>服务</th><th>冻结名称</th><th>候选指标</th><th>冻结时间</th><th>状态</th><th>处理</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="record in ambiguities" :key="record.id" :data-testid="`ambiguity-${record.id}`">
          <td>{{ record.接口名称 }}</td>
          <td>{{ record.冻结值 }}</td>
          <td>
            <span v-for="candidate in record.候选指标" :key="candidate.指标UID" class="candidate">
              {{ candidate.指标UID }}（{{ candidate.指标名称 }}）
            </span>
          </td>
          <td>{{ record.冻结时间 }}</td>
          <td>{{ record.状态 }}</td>
          <td>
            <select v-model="ambiguityChoice[record.id]" :data-testid="`choice-${record.id}`">
              <option value="">选择目标指标</option>
              <option v-for="candidate in record.候选指标" :key="candidate.指标UID" :value="candidate.指标UID">
                {{ candidate.指标UID }}（{{ candidate.指标名称 }}）
              </option>
            </select>
            <button class="link" type="button" @click="resolveAmbiguityRecord(record)">绑定</button>
          </td>
        </tr>
        <tr v-if="!ambiguities.length">
          <td colspan="6" class="empty-state">没有待处理歧义</td>
        </tr>
      </tbody>
    </table>

    <h3 class="section-title">迁移批次</h3>
    <table class="data-table">
      <thead>
        <tr><th>批次</th><th>转换</th><th>冻结</th><th>执行时间</th><th>回滚</th></tr>
      </thead>
      <tbody>
        <tr v-for="batch in status?.batch_summaries ?? []" :key="batch.批次ID">
          <td>#{{ batch.批次ID }}</td>
          <td>{{ batch.转换 }}</td>
          <td>{{ batch.冻结 }}</td>
          <td>{{ batch.执行时间 }}</td>
          <td class="row-actions">
            <button class="link" type="button" @click="rollback(batch.批次ID)">回滚</button>
            <button class="link" type="button" @click="verify(batch.批次ID)">核对</button>
          </td>
        </tr>
        <tr v-if="!status?.batch_summaries.length">
          <td colspan="5" class="empty-state">还没有迁移批次</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>{{ message }}</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { callService, loadAmbiguities, loadMigrationStatus, migrateBatch, resolveAmbiguity, rollbackBatch, verifyRollback, auditService } from '@/api/reference-service'
import { listEntries } from '@/api/backend-service'
import {
  compareCalls,
  progressPercent,
  referenceStateLabel,
} from '@/domain/service_metric_reference'
import type {
  AmbiguityRecord,
  CallComparison,
  CallSnapshot,
  MigrationStatus,
  ServiceAudit,
} from '@/domain/service_metric_reference'

const status = ref<MigrationStatus | null>(null)
const services = ref<Record<string, string | number | boolean>[]>([])
const audits = ref<Record<number, ServiceAudit>>({})
const ambiguities = ref<AmbiguityRecord[]>([])
const ambiguityChoice = ref<Record<number, string>>({})
const selectedAudit = ref<ServiceAudit | null>(null)
const calls = ref<CallSnapshot[]>([])
const callServiceId = ref('11')
const callContractName = ref('')
const callMetricRef = ref('')
const busy = ref(false)
const message = ref('')
const errorMessage = ref('')

const progress = computed(() => progressPercent(status.value))
const comparison = computed<CallComparison | null>(() => {
  if (calls.value.length < 2) return null
  return compareCalls(calls.value[calls.value.length - 2], calls.value[calls.value.length - 1])
})

function auditOf(id: number): ServiceAudit | undefined {
  return audits.value[id]
}

async function reload() {
  busy.value = true
  errorMessage.value = ''
  try {
    const [statusPayload, servicePage] = await Promise.all([
      loadMigrationStatus(),
      listEntries('data_service', {}),
    ])
    status.value = statusPayload
    services.value = servicePage.items
    ambiguities.value = await loadAmbiguities()
    const entries = await Promise.all(
      services.value.map((service) => auditService(Number(service.id)).catch(() => null)),
    )
    audits.value = Object.fromEntries(
      entries.filter((item): item is ServiceAudit => item !== null)
        .map((item) => [item.服务接口ID, item]),
    )
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '迁移状态读取失败'
  } finally {
    busy.value = false
  }
}

async function runMigrateBatch() {
  busy.value = true
  errorMessage.value = ''
  try {
    const result = await migrateBatch(1)
    message.value = result.message
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '分批转换失败'
  } finally {
    busy.value = false
  }
}

async function audit(serviceId: number) {
  await loadAudit(serviceId)
  selectedAudit.value = audits.value[serviceId] ?? null
}

async function loadAudit(serviceId: number) {
  try {
    audits.value[serviceId] = await auditService(serviceId)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '服务核对失败'
  }
}

async function callOnce(serviceId: number) {
  try {
    calls.value.push(await callService(serviceId))
    message.value = `已记录服务 ${serviceId} 的一笔调用`
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '服务调用失败'
  }
}

async function recordCall() {
  const serviceId = Number(callServiceId.value)
  if (!Number.isFinite(serviceId)) {
    errorMessage.value = '请填写服务接口 ID'
    return
  }
  try {
    calls.value.push(await callService(serviceId, {
      metricName: callContractName.value || undefined,
      metricRef: callMetricRef.value || undefined,
    }))
    message.value = `已记录服务 ${serviceId} 的一笔调用`
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '服务调用失败'
  }
}

function clearCalls() {
  calls.value = []
  message.value = '已清空调用记录'
}

async function resolveAmbiguityRecord(record: AmbiguityRecord) {
  const metricUid = ambiguityChoice.value[record.id]
  if (!metricUid) {
    errorMessage.value = '请先选择目标指标'
    return
  }
  try {
    const result = await resolveAmbiguity(record.id, metricUid)
    message.value = result.message
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '歧义处理失败'
  }
}

async function rollback(batchId: number) {
  try {
    const result = await rollbackBatch(batchId)
    message.value = result.message
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '批次回滚失败'
  }
}

async function verify(batchId: number) {
  try {
    const result = await verifyRollback(batchId)
    message.value = result.ok ? `批次 #${batchId} 回滚核对通过` : `批次 #${batchId} 核对未通过`
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '回滚核对失败'
  }
}

onMounted(reload)
</script>

<style scoped>
.section-title { font-size: 14px; margin: 20px 0 8px; }
.progress-track { height: 8px; background: #e8edf3; border-radius: 4px; overflow: hidden; }
.progress-fill { height: 100%; background: #1f6feb; transition: width 0.2s; }
.panel { border: 1px solid var(--border); border-radius: 8px; background: #fff; padding: 12px 14px; margin: 16px 0; }
.panel-head { display: flex; justify-content: space-between; align-items: center; }
.panel-head h3 { margin: 0; font-size: 14px; }
.detail-grid { display: grid; grid-template-columns: 140px 1fr; gap: 6px 12px; margin: 10px 0 0; }
.detail-grid dt { color: var(--muted); font-size: 12px; }
.detail-grid dd { margin: 0; font-size: 13px; }
.call-list { margin: 0; padding-left: 18px; font-size: 13px; display: flex; flex-direction: column; gap: 4px; }
.compare-result { padding: 8px 10px; border-radius: 6px; font-size: 13px; }
.compare-result.ok { background: #ecfdf3; color: #067647; }
.compare-result.bad { background: #fef3f2; color: #b42318; }
.candidate { display: inline-block; margin-right: 8px; font-size: 12px; }
</style>
