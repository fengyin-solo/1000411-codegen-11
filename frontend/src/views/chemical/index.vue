<template>
  <section class="page" data-module="chemical">
    <header class="page-head">
      <div>
        <h2>药剂出入管理</h2>
        <p class="page-desc">维护药剂单据，围绕单据编号、出入数量做登记、结存重算与状态流转；支持按单据批量导入并先预览差异再落库。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记药剂单据</button>
        <button class="btn" type="button" @click="openImport">单据导入与结存重算</button>
        <button class="btn" type="button" @click="exportRows">导出现有清单</button>
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
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
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
          <td :colspan="columns.length + 1" class="empty-state">暂无药剂出入数据，可先登记药剂单据或下载模板批量导入</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条药剂出入记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>

    <!-- 单据导入向导 -->
    <div v-if="importOpen" class="import-mask" @click.self="closeImport">
      <div class="import-dialog">
        <div class="import-head">
          <h3>单据导入与结存重算</h3>
          <button class="link" type="button" @click="closeImport">关闭</button>
        </div>

        <!-- 第 1 步：下载模板、选择文件 -->
        <template v-if="!preview && !committed">
          <p class="import-tip">
            按模板填写单据编号、出入方向（入库/出库）、出入数量与供应商；上传后先做逐行校验并给出结存差异预览，确认后才会落库。
          </p>
          <div class="import-toolbar">
            <button class="btn" type="button" @click="downloadTemplate">下载导入模板</button>
            <label class="btn primary file-picker">
              选择 CSV 文件
              <input type="file" accept=".csv,text/csv" @change="onFilePicked" hidden />
            </label>
            <span v-if="pendingFileName" class="file-name">{{ pendingFileName }}</span>
          </div>
          <p v-if="importMessage" class="error-text">{{ importMessage }}</p>
        </template>

        <!-- 第 2a 步：校验不通过，逐条列出原因 -->
        <template v-else-if="preview && !preview.valid">
          <div class="result-banner error">
            共解析 {{ preview.total_rows }} 行，发现 {{ preview.error_count }} 处问题，已全部拦下；本次不会写入任何结存，请按清单修正后重新上传。
          </div>
          <div class="import-toolbar">
            <button class="btn" type="button" @click="downloadErrors(preview!.batch_id)">下载错误明细清单（CSV）</button>
            <button class="btn primary" type="button" @click="resetWizard">重新上传文件</button>
          </div>
          <div class="preview-scroll">
            <table class="data-table">
              <thead>
                <tr><th>文件行号</th><th>单据编号</th><th>错误字段</th><th>拦截原因</th></tr>
              </thead>
              <tbody>
                <tr v-for="(item, idx) in preview.errors" :key="idx">
                  <td>{{ item.line }}</td>
                  <td>{{ item.code || '—' }}</td>
                  <td>{{ item.field || '—' }}</td>
                  <td class="error-text">{{ item.reason }}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </template>

        <!-- 第 2b 步：校验通过，展示差异预览 -->
        <template v-else-if="preview && preview.valid && !committed">
          <div class="result-banner success">
            共 {{ preview.total_rows }} 行全部校验通过，将新增 {{ preview.accepted_rows }} 条单据；请核对结存差异，确认后落库并归档对账文件。
          </div>
          <div class="summary-grid">
            <div class="summary-item"><span>新增单据</span><strong>{{ preview.summary['新增单据数'] }}</strong></div>
            <div class="summary-item"><span>入库合计</span><strong>{{ preview.summary['入库合计'] }}</strong></div>
            <div class="summary-item"><span>出库合计</span><strong>{{ preview.summary['出库合计'] }}</strong></div>
            <div class="summary-item"><span>涉及药剂</span><strong>{{ preview.summary['涉及药剂数'] }}</strong></div>
          </div>

          <h4 class="preview-title">结存差异预览（重算前 → 重算后）</h4>
          <div class="preview-scroll">
            <table class="data-table">
              <thead>
                <tr>
                  <th>药剂名称</th><th>规格型号</th><th>期初结存</th><th>本次入库</th>
                  <th>本次出库</th><th>重算后结存</th><th>回写既有单据</th><th>新增单据</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="(item, idx) in preview.balance_changes" :key="idx">
                  <td>{{ item['药剂名称'] }}</td>
                  <td>{{ item['规格型号'] }}</td>
                  <td>{{ item['期初结存'] }}</td>
                  <td>{{ item['本次入库'] }}</td>
                  <td>{{ item['本次出库'] }}</td>
                  <td><strong>{{ item['期末结存'] }}</strong></td>
                  <td>{{ item['影响既有单据数'] }}</td>
                  <td>{{ item['新增单据数'] }}</td>
                </tr>
              </tbody>
            </table>
          </div>

          <h4 v-if="preview.affected_entries.length" class="preview-title">既有单据结存回写明细</h4>
          <div v-if="preview.affected_entries.length" class="preview-scroll compact">
            <table class="data-table">
              <thead><tr><th>单据编号</th><th>重算前结存</th><th>重算后结存</th></tr></thead>
              <tbody>
                <tr v-for="(item, idx) in preview.affected_entries" :key="idx">
                  <td>{{ item['单据编号'] }}</td>
                  <td>{{ item['重算前结存'] }}</td>
                  <td :class="item['重算前结存'] === item['重算后结存'] ? '' : 'diff-after'">
                    {{ item['重算后结存'] }}
                  </td>
                </tr>
              </tbody>
            </table>
          </div>

          <h4 class="preview-title">待导入单据（{{ preview.entries.length }} 条）</h4>
          <div class="preview-scroll compact">
            <table class="data-table">
              <thead>
                <tr><th>行号</th><th>单据编号</th><th>药剂名称</th><th>出入方向</th><th>出入数量</th><th>滚动后结存</th><th>供应商</th><th>发生日期</th></tr>
              </thead>
              <tbody>
                <tr v-for="(item, idx) in preview.entries" :key="idx">
                  <td>{{ item['文件行号'] }}</td>
                  <td>{{ item['单据编号'] }}</td>
                  <td>{{ item['药剂名称'] }}</td>
                  <td>{{ item['出入方向'] }}</td>
                  <td>{{ item['出入数量'] }}</td>
                  <td>{{ item['结存数量'] }}</td>
                  <td>{{ item['供应商'] }}</td>
                  <td>{{ item['发生日期'] || '—' }}</td>
                </tr>
              </tbody>
            </table>
          </div>

          <div v-if="preview.warnings.length" class="warn-box">
            <p v-for="(w, idx) in preview.warnings" :key="idx">⚠️ {{ w }}（仅提示，仍可确认导入）</p>
          </div>

          <p v-if="importMessage" class="error-text">{{ importMessage }}</p>
          <div class="import-toolbar footer">
            <button class="btn" type="button" :disabled="committing" @click="resetWizard">取消重传</button>
            <button class="btn primary" type="button" :disabled="committing" @click="confirmCommit">
              {{ committing ? '正在落库…' : '确认差异并落库' }}
            </button>
          </div>
        </template>

        <!-- 第 3 步：落库完成 -->
        <template v-else-if="committed">
          <div class="result-banner success">{{ committed.message }}</div>
          <div class="summary-grid">
            <div class="summary-item"><span>导入单据</span><strong>{{ committed.accepted_rows }}</strong></div>
            <div class="summary-item"><span>入库合计</span><strong>{{ committed.in_total }}</strong></div>
            <div class="summary-item"><span>出库合计</span><strong>{{ committed.out_total }}</strong></div>
            <div class="summary-item"><span>归档编号</span><strong class="archive-id">{{ committed.archive_id }}</strong></div>
          </div>
          <p v-if="committed.warnings?.length" class="warn-box">
            <span v-for="(w, idx) in committed.warnings" :key="idx">⚠️ {{ w }}<br /></span>
          </p>
          <div class="import-toolbar">
            <button class="btn" type="button" @click="loadArchives">查看归档与对账文件</button>
            <button class="btn primary" type="button" @click="closeImport">完成并刷新台账</button>
          </div>
        </template>

        <!-- 归档列表 -->
        <template v-if="archives.length">
          <h4 class="preview-title">对账归档记录</h4>
          <div class="preview-scroll compact">
            <table class="data-table">
              <thead>
                <tr><th>归档编号</th><th>原始文件</th><th>归档时间</th><th>导入条数</th><th>入库/出库</th><th>对账文件下载</th></tr>
              </thead>
              <tbody>
                <tr v-for="arch in archives" :key="arch.archive_id">
                  <td class="archive-id">{{ arch.archive_id }}</td>
                  <td>{{ arch.source_filename }}</td>
                  <td>{{ arch.committed_at }}</td>
                  <td>{{ arch.accepted_rows }}/{{ arch.total_rows }}</td>
                  <td>{{ arch.in_total }} / {{ arch.out_total }}</td>
                  <td class="row-actions">
                    <button v-for="file in arch.files" :key="file" class="link" type="button" @click="downloadArchive(arch.archive_id, file)">
                      {{ file.replace(/\.csv$/, '') }}
                    </button>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
        </template>
      </div>
    </div>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | null>

interface PreviewError { line: number; code: string; field: string; reason: string }
interface BalanceChange {
  ['药剂名称']: string; ['规格型号']: string; ['期初结存']: number
  ['本次入库']: number; ['本次出库']: number; ['期末结存']: number
  ['影响既有单据数']: number; ['新增单据数']: number
}
interface AffectedEntry { ['单据编号']: string; ['重算前结存']: number | null; ['重算后结存']: number }
interface PreviewEntry {
  ['文件行号']: number; ['单据编号']: string; ['药剂名称']: string
  ['出入方向']: string; ['出入数量']: number; ['结存数量']: number
  ['供应商']: string; ['发生日期']: string
}
interface Preview {
  batch_id: string
  valid: boolean
  total_rows: number
  accepted_rows: number
  error_count: number
  errors: PreviewError[]
  warnings: string[]
  summary: Record<string, number>
  balance_changes: BalanceChange[]
  affected_entries: AffectedEntry[]
  entries: PreviewEntry[]
}
interface CommitResult {
  archive_id: string
  message: string
  accepted_rows: number
  in_total: number
  out_total: number
  warnings: string[]
}
interface ArchiveItem {
  archive_id: string
  source_filename: string
  committed_at: string
  total_rows: number
  accepted_rows: number
  in_total: number
  out_total: number
  files: string[]
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

const importOpen = ref(false)
const pendingFileName = ref('')
const preview = ref<Preview | null>(null)
const committed = ref<CommitResult | null>(null)
const committing = ref(false)
const importMessage = ref('')
const archives = ref<ArchiveItem[]>([])
let pendingContent = ''

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
    const payload = await response.json()
    if (!payload.ok) {
      throw new Error(payload.message || '药剂出入动作被拦下')
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

// ---------------- 单据导入 ----------------

function openImport() {
  importOpen.value = true
  resetWizard()
  void loadArchives()
}

function closeImport() {
  importOpen.value = false
  if (committed.value) {
    void reload()
  }
}

function resetWizard() {
  preview.value = null
  committed.value = null
  importMessage.value = ''
  pendingFileName.value = ''
  pendingContent = ''
  committing.value = false
}

function downloadTemplate() {
  window.open(`${ENDPOINT}/import/template`, '_blank')
}

function downloadErrors(batchId: string) {
  window.open(`${ENDPOINT}/import/${batchId}/errors`, '_blank')
}

function downloadArchive(archiveId: string, filename: string) {
  window.open(`${ENDPOINT}/archives/${archiveId}/files/${encodeURIComponent(filename)}`, '_blank')
}

async function loadArchives() {
  try {
    const response = await request(`${ENDPOINT}/archives`)
    if (response.ok) {
      archives.value = (await response.json()).items ?? []
    }
  } catch {
    archives.value = []
  }
}

function onFilePicked(event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  if (!file) {
    return
  }
  importMessage.value = ''
  const reader = new FileReader()
  reader.onload = async () => {
    pendingContent = String(reader.result ?? '')
    pendingFileName.value = file.name
    await uploadPreview()
  }
  reader.onerror = () => {
    importMessage.value = '文件读取失败，请重新选择'
  }
  reader.readAsText(file, 'utf-8')
  input.value = ''
}

async function uploadPreview() {
  importMessage.value = '正在校验并模拟结存重算…'
  try {
    const response = await request(`${ENDPOINT}/import/preview`, {
      method: 'POST',
      body: JSON.stringify({ filename: pendingFileName.value, content: pendingContent }),
    })
    const payload = await response.json()
    if (!response.ok) {
      throw new Error(payload.detail || '导入预览失败')
    }
    preview.value = payload as Preview
    committed.value = null
    importMessage.value = ''
  } catch (error) {
    preview.value = null
    importMessage.value = error instanceof Error ? error.message : '导入预览失败'
  }
}

async function confirmCommit() {
  if (!preview.value) {
    return
  }
  committing.value = true
  importMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/import/${preview.value.batch_id}/commit`, { method: 'POST' })
    const payload = await response.json()
    if (!response.ok) {
      throw new Error(payload.detail || '落库失败，结存未被修改')
    }
    committed.value = payload as CommitResult
    preview.value = null
    await reload()
    await loadArchives()
  } catch (error) {
    importMessage.value = error instanceof Error ? error.message : '确认落库失败'
  } finally {
    committing.value = false
  }
}

onMounted(reload)
</script>

<style scoped>
.page-actions { display: flex; gap: 8px; }
.import-mask {
  position: fixed; inset: 0; background: rgba(15, 23, 42, 0.45);
  display: flex; align-items: flex-start; justify-content: center;
  padding: 32px 16px; z-index: 50; overflow-y: auto;
}
.import-dialog {
  background: #fff; border-radius: 10px; width: min(960px, 100%);
  padding: 18px 22px; box-shadow: 0 18px 48px rgba(15, 23, 42, 0.25);
}
.import-head { display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px; }
.import-head h3 { margin: 0; font-size: 16px; }
.import-tip { font-size: 13px; color: var(--muted); margin: 4px 0 12px; }
.import-toolbar { display: flex; gap: 10px; align-items: center; margin: 12px 0; flex-wrap: wrap; }
.import-toolbar.footer { justify-content: flex-end; }
.file-picker { display: inline-flex; align-items: center; }
.file-name { font-size: 13px; color: var(--muted); }
.result-banner { border-radius: 8px; padding: 10px 12px; font-size: 13px; margin-bottom: 10px; }
.result-banner.error { background: #fef3f2; border: 1px solid #fecdca; color: #b42318; }
.result-banner.success { background: #ecfdf3; border: 1px solid #abefc6; color: #067647; }
.summary-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; margin: 10px 0; }
.summary-item { background: #f8fafc; border: 1px solid var(--border); border-radius: 8px; padding: 8px 10px; }
.summary-item span { display: block; font-size: 12px; color: var(--muted); }
.summary-item strong { font-size: 18px; }
.archive-id { font-size: 12px; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
.preview-title { font-size: 13px; margin: 14px 0 6px; }
.preview-scroll { max-height: 260px; overflow: auto; border: 1px solid var(--border); border-radius: 6px; }
.preview-scroll.compact { max-height: 180px; }
.preview-scroll table { border: none; }
.diff-after { color: #b54708; font-weight: 600; }
.warn-box { background: #fffaeb; border: 1px solid #fedf89; border-radius: 8px; padding: 8px 10px; font-size: 12px; color: #b54708; margin: 10px 0; }
button:disabled { opacity: 0.55; cursor: not-allowed; }
</style>
