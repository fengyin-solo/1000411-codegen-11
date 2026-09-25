<template>
  <section class="page" data-module="chemical">
    <header class="page-head">
      <div>
        <h2>药剂出入管理</h2>
        <p class="page-desc">维护药剂单据，围绕单据编号、出入数量做登记、筛选与状态流转；支持按单据编号批量导入并自动重算结存。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记药剂单据</button>
        <button class="btn" type="button" @click="downloadTemplate">下载导入模板</button>
        <button class="btn" type="button" @click="openImport">单据导入/结存重算</button>
        <button class="btn" type="button" @click="openArchives">对账文件归档</button>
        <button class="btn" type="button" @click="exportRows">导出药剂出入清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>出入方向</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td>{{ row['出入方向'] ?? '入库' }}</td>
          <td class="row-actions">
            <button
              v-for="action in actions"
              :key="action"
              class="link"
              type="button"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 2" class="empty-state">暂无药剂出入数据，可先登记药剂单据或导入</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条药剂出入记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>

    <!-- 单据导入：选文件 → 差异预览 → 确认落库 -->
    <div v-if="importOpen" class="modal-mask" @click.self="closeImport">
      <div class="modal-card import-modal">
        <div class="modal-head">
          <h3>单据导入与结存重算</h3>
          <button class="link" type="button" @click="closeImport">关闭</button>
        </div>

        <div v-if="importStep === 'upload'" class="modal-body">
          <p class="modal-tip">
            按模板填写单据编号、药剂名称、规格型号、出入数量、供应商与出入方向（入库/出库，留空按入库）；
            重复单据编号、数量为负或为 0、供应商缺失等问题会逐条拦下。
          </p>
          <div class="upload-row">
            <input ref="fileInput" type="file" accept=".csv,text/csv" @change="onFilePicked" />
            <button class="btn" type="button" @click="downloadTemplate">下载模板</button>
            <button class="btn" type="button" @click="runRecalc">不改数据，仅重算当前结存</button>
          </div>
          <p v-if="pickedName" class="file-hint">已选择：{{ pickedName }}（{{ importContent.length }} 字符）</p>
          <div class="modal-foot">
            <button class="btn primary" type="button" :disabled="!importContent || previewLoading" @click="doPreview">
              {{ previewLoading ? '正在校验并预演结存…' : '上传并生成差异预览' }}
            </button>
            <span v-if="importError" class="error-text">{{ importError }}</span>
          </div>
        </div>

        <div v-else-if="preview" class="modal-body">
          <section class="preview-summary" v-if="preview">
            <span>总行数 <strong>{{ preview.summary.total_rows }}</strong></span>
            <span>可导入 <strong class="ok-text">{{ preview.summary.valid_rows }}</strong></span>
            <span>问题行 <strong :class="{ 'error-text': preview.summary.error_rows > 0 }">{{ preview.summary.error_rows }}</strong></span>
            <span>入库 <strong>{{ preview.summary.inbound_rows }}</strong></span>
            <span>出库 <strong>{{ preview.summary.outbound_rows }}</strong></span>
          </section>

          <div v-if="preview.errors.length" class="preview-block">
            <div class="preview-block-head">
              <h4>错误明细（{{ preview.errors.length }} 条，需修正后重新上传）</h4>
              <button class="link" type="button" @click="downloadErrorReport">下载错误明细清单</button>
            </div>
            <table class="data-table mini-table">
              <thead><tr><th>行号</th><th>单据编号</th><th>错误原因</th></tr></thead>
              <tbody>
                <tr v-for="(err, idx) in preview.errors" :key="idx">
                  <td>{{ err['行号'] || '-' }}</td>
                  <td>{{ err['单据编号'] || '—' }}</td>
                  <td class="error-text">{{ err['错误原因'] }}</td>
                </tr>
              </tbody>
            </table>
          </div>

          <div v-if="preview.summary.negative_balances.length" class="preview-block">
            <h4 class="error-text">结存预警：以下药剂导入后结存为负，请确认是否继续</h4>
            <table class="data-table mini-table">
              <thead><tr><th>药剂名称</th><th>规格型号</th><th>导入后期末结存</th></tr></thead>
              <tbody>
                <tr v-for="(item, idx) in preview.summary.negative_balances" :key="idx">
                  <td>{{ item['药剂名称'] }}</td><td>{{ item['规格型号'] }}</td><td class="error-text">{{ item['期末结存'] }}</td>
                </tr>
              </tbody>
            </table>
          </div>

          <div class="preview-block">
            <h4>结存差异（按药剂汇总）</h4>
            <table class="data-table mini-table">
              <thead><tr><th>药剂名称</th><th>规格型号</th><th>导入前结存</th><th>导入后结存</th><th>差异</th></tr></thead>
              <tbody>
                <tr v-for="(item, idx) in preview.balance_changes" :key="idx">
                  <td>{{ item['药剂名称'] }}</td><td>{{ item['规格型号'] }}</td>
                  <td>{{ item['导入前结存'] }}</td><td>{{ item['导入后结存'] }}</td>
                  <td :class="String(item['差异']).startsWith('-') ? 'error-text' : 'ok-text'">{{ item['差异'] }}</td>
                </tr>
                <tr v-if="!preview.balance_changes.length"><td colspan="5" class="empty-state">本次导入不改变任何药剂的期末结存</td></tr>
              </tbody>
            </table>
          </div>

          <div class="preview-block">
            <h4>导入单据逐单结存预览（{{ preview.row_changes.length }} 张）</h4>
            <table class="data-table mini-table">
              <thead><tr><th>单据编号</th><th>药剂名称</th><th>方向</th><th>出入数量</th><th>单据结存</th><th>供应商</th></tr></thead>
              <tbody>
                <tr v-for="(item, idx) in preview.row_changes" :key="idx">
                  <td>{{ item['单据编号'] }}</td><td>{{ item['药剂名称'] }}</td>
                  <td>{{ item['出入方向'] }}</td><td>{{ item['出入数量'] }}</td>
                  <td>{{ item['结存数量'] }}</td><td>{{ item['供应商'] }}</td>
                </tr>
              </tbody>
            </table>
          </div>

          <div class="modal-foot">
            <button class="btn" type="button" @click="backToUpload">重新选择文件</button>
            <button
              class="btn primary"
              type="button"
              :disabled="!preview.can_commit || commitLoading"
              :title="preview.can_commit ? '' : '存在未通过的校验明细，不能确认落库'"
              @click="doCommit"
            >
              {{ commitLoading ? '正在原子落库并归档…' : '确认差异并落库' }}
            </button>
            <span v-if="commitMessage" :class="commitOk ? 'ok-text' : 'error-text'">{{ commitMessage }}</span>
          </div>
        </div>
      </div>
    </div>

    <!-- 结存重算结果 -->
    <div v-if="recalcOpen" class="modal-mask" @click.self="recalcOpen = false">
      <div class="modal-card">
        <div class="modal-head">
          <h3>结存重算与核对</h3>
          <button class="link" type="button" @click="recalcOpen = false">关闭</button>
        </div>
        <div class="modal-body">
          <p :class="recalcResult?.ok ? 'ok-text' : 'error-text'">{{ recalcResult?.message }}</p>
          <div v-if="recalcResult?.changed?.length" class="preview-block">
            <h4>本次被修正的结存（{{ recalcResult.changed.length }} 张）</h4>
            <table class="data-table mini-table">
              <thead><tr><th>单据编号</th><th>重算前</th><th>重算后</th></tr></thead>
              <tbody>
                <tr v-for="(item, idx) in recalcResult.changed" :key="idx">
                  <td>{{ item['单据编号'] }}</td><td>{{ item['重算前'] }}</td><td>{{ item['重算后'] }}</td>
                </tr>
              </tbody>
            </table>
          </div>
          <div v-else class="preview-block"><p class="empty-state">结存与明细一致，无需修正。</p></div>
        </div>
      </div>
    </div>

    <!-- 对账文件归档 -->
    <div v-if="archivesOpen" class="modal-mask" @click.self="archivesOpen = false">
      <div class="modal-card wide-modal">
        <div class="modal-head">
          <h3>对账文件归档</h3>
          <div>
            <button class="btn" type="button" @click="loadArchives">刷新</button>
            <button class="link" type="button" @click="archivesOpen = false">关闭</button>
          </div>
        </div>
        <div class="modal-body">
          <table class="data-table mini-table">
            <thead><tr><th>归档号</th><th>归档时间</th><th>导入单数</th><th>确认人</th><th>文件</th></tr></thead>
            <tbody>
              <tr v-for="arc in archives" :key="arc.archive_id">
                <td>{{ arc.archive_id }}</td>
                <td>{{ arc.archived_at }}</td>
                <td>{{ arc.imported }}</td>
                <td>{{ arc.operator }}</td>
                <td class="row-actions">
                  <button
                    v-for="file in arc.files"
                    :key="file"
                    class="link"
                    type="button"
                    @click="downloadArchiveFile(arc.archive_id, file)"
                  >
                    {{ archiveFileLabel(file) }}
                  </button>
                </td>
              </tr>
              <tr v-if="!archives.length"><td colspan="5" class="empty-state">暂无归档，确认导入后原始文件与对账文件会自动归档到这里</td></tr>
            </tbody>
          </table>
        </div>
      </div>
    </div>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | null>
type Preview = {
  token: string
  can_commit: boolean
  errors: Array<Record<string, string | number>>
  row_changes: Row[]
  balance_changes: Row[]
  summary: {
    total_rows: number
    valid_rows: number
    error_rows: number
    inbound_rows: number
    outbound_rows: number
    negative_balances: Row[]
  }
}

const ENDPOINT = '/api/chemical'
const columns = ["单据编号", "药剂名称", "规格型号", "出入数量", "结存数量", "供应商", "经办人员", "单据状态"]
const actions = ["审核单据", "确认出入库", "作废单据"]
const stats = [{"label": "待审核单据", "value": 0}, {"label": "本月药剂消耗", "value": 0}, {"label": "结存偏低药剂", "value": 0}]

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)

// 导入向导状态
const importOpen = ref(false)
const importStep = ref<'upload' | 'preview'>('upload')
const fileInput = ref<HTMLInputElement | null>(null)
const pickedName = ref('')
const importContent = ref('')
const previewLoading = ref(false)
const preview = ref<Preview | null>(null)
const importError = ref('')
const commitLoading = ref(false)
const commitMessage = ref('')
const commitOk = ref(false)

// 结存重算
const recalcOpen = ref(false)
const recalcResult = ref<{ ok: boolean; message: string; changed: Row[] } | null>(null)

// 归档
const archivesOpen = ref(false)
const archives = ref<Array<Record<string, any>>>([])

function resetFilters() {
  filters.value = {}
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function openCreate() {
  errorMessage.value = '药剂单据登记入口尚未接入审批流'
}

function downloadTemplate() {
  window.open(`${ENDPOINT}/import/template`, '_blank')
}

function openImport() {
  importOpen.value = true
  importStep.value = 'upload'
  importContent.value = ''
  pickedName.value = ''
  importError.value = ''
  commitMessage.value = ''
  preview.value = null
}

function closeImport() {
  importOpen.value = false
}

function backToUpload() {
  importStep.value = 'upload'
  preview.value = null
  commitMessage.value = ''
}

function onFilePicked(event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  importError.value = ''
  importContent.value = ''
  pickedName.value = ''
  if (!file) return
  if (!/\.csv$/i.test(file.name)) {
    importError.value = '请使用 CSV 格式文件（可在模板里填好后另存为 CSV）'
    return
  }
  const reader = new FileReader()
  reader.onload = () => {
    // 去掉 UTF-8 BOM，后端会统一处理，这里也先剥一遍避免重复
    importContent.value = String(reader.result ?? '').replace(/^﻿/, '')
    pickedName.value = file.name
  }
  reader.onerror = () => {
    importError.value = '文件读取失败，请重新选择'
  }
  reader.readAsText(file, 'utf-8')
}

async function doPreview() {
  if (!importContent.value) {
    importError.value = '请先选择导入文件'
    return
  }
  previewLoading.value = true
  importError.value = ''
  try {
    const response = await request(`${ENDPOINT}/import/preview`, {
      method: 'POST',
      body: JSON.stringify({ content: importContent.value }),
    })
    if (!response.ok) throw new Error(`接口返回 ${response.status}`)
    preview.value = (await response.json()) as Preview
    importStep.value = 'preview'
    commitMessage.value = ''
  } catch (error) {
    importError.value = error instanceof Error ? error.message : '差异预览生成失败'
  } finally {
    previewLoading.value = false
  }
}

function triggerCsvDownload(filename: string, body: string) {
  const blob = new Blob(["﻿" + body], { type: 'text/csv;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = filename
  link.click()
  URL.revokeObjectURL(url)
}

async function downloadErrorReport() {
  if (!preview.value) return
  try {
    const response = await request(`${ENDPOINT}/import/error-report`, {
      method: 'POST',
      body: JSON.stringify({ errors: preview.value.errors }),
    })
    if (!response.ok) throw new Error(`接口返回 ${response.status}`)
    triggerCsvDownload('药剂单据导入错误明细.csv', await response.text())
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '错误明细清单生成失败'
  }
}

async function doCommit() {
  if (!preview.value) return
  commitLoading.value = true
  commitMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/import/commit`, {
      method: 'POST',
      body: JSON.stringify({
        token: preview.value.token,
        content: importContent.value,
        operator: '',
      }),
    })
    const payload = await response.json()
    commitOk.value = Boolean(payload.ok)
    commitMessage.value = payload.message || '落库失败'
    if (payload.ok) {
      importStep.value = 'upload'
      preview.value = null
      importContent.value = ''
      pickedName.value = ''
      if (fileInput.value) fileInput.value.value = ''
      await reload()
    }
  } catch (error) {
    commitOk.value = false
    commitMessage.value = error instanceof Error ? error.message : '确认落库失败，台账未被修改'
  } finally {
    commitLoading.value = false
  }
}

async function runRecalc() {
  recalcOpen.value = true
  recalcResult.value = null
  try {
    const response = await request(`${ENDPOINT}/recalc`, { method: 'POST' })
    const payload = await response.json()
    recalcResult.value = {
      ok: Boolean(payload.ok),
      message: payload.message,
      changed: payload.entry?.changed ?? [],
    }
    await reload()
  } catch (error) {
    recalcResult.value = {
      ok: false,
      message: error instanceof Error ? error.message : '结存重算失败',
      changed: [],
    }
  }
}

function openArchives() {
  archivesOpen.value = true
  void loadArchives()
}

async function loadArchives() {
  try {
    const response = await request(`${ENDPOINT}/archives`)
    if (!response.ok) throw new Error(`接口返回 ${response.status}`)
    const payload = await response.json()
    archives.value = payload.items ?? []
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '归档列表读取失败'
  }
}

function archiveFileLabel(file: string): string {
  if (file === 'import_source.csv') return '原始导入文件'
  if (file === 'reconciliation.json') return '对账汇总'
  if (file === 'reconciliation_detail.csv') return '对账明细'
  if (file === 'manifest.json') return '归档清单'
  return file
}

function downloadArchiveFile(archiveId: string, file: string) {
  window.open(`${ENDPOINT}/archives/${archiveId}/files/${encodeURIComponent(file)}`, '_blank')
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ values: { action } }),
    })
    if (!response.ok) {
      throw new Error('药剂出入动作未生效，请稍后重试')
    }
    const payload = await response.json().catch(() => null)
    if (payload && payload.ok === false) {
      throw new Error(payload.message || '药剂出入动作未生效')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '药剂出入操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('药剂单据列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '药剂出入列表读取失败'
  }
}

onMounted(reload)
</script>

<style scoped>
.page-actions { display: flex; gap: 8px; flex-wrap: wrap; }
.modal-mask {
  position: fixed; inset: 0; background: rgba(15, 23, 42, 0.45);
  display: flex; align-items: center; justify-content: center; z-index: 100;
}
.modal-card {
  background: #fff; border-radius: 10px; width: 640px; max-width: 94vw;
  max-height: 88vh; display: flex; flex-direction: column; overflow: hidden;
}
.modal-card.wide-modal { width: 860px; }
.import-modal { width: 900px; }
.modal-head {
  display: flex; justify-content: space-between; align-items: center;
  padding: 14px 18px; border-bottom: 1px solid var(--border);
}
.modal-head h3 { margin: 0; font-size: 16px; }
.modal-body { padding: 14px 18px; overflow-y: auto; }
.modal-tip { font-size: 13px; color: var(--muted); margin: 0 0 12px; line-height: 1.7; }
.upload-row { display: flex; gap: 10px; align-items: center; }
.file-hint { font-size: 12px; color: var(--muted); margin-top: 8px; }
.modal-foot { display: flex; gap: 10px; align-items: center; margin-top: 16px; padding-top: 12px; border-top: 1px solid var(--border); }
.preview-summary { display: flex; gap: 18px; background: #f1f5f9; border-radius: 8px; padding: 10px 14px; font-size: 13px; margin-bottom: 12px; }
.preview-block { margin-bottom: 14px; }
.preview-block h4 { margin: 0 0 8px; font-size: 13px; }
.preview-block-head { display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; }
.mini-table { font-size: 12px; }
.mini-table th, .mini-table td { padding: 5px 8px; }
.ok-text { color: #15803d; }
.btn:disabled { opacity: 0.55; cursor: not-allowed; }
</style>
