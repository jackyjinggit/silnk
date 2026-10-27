<script setup>
import {
  IconFileTypeDocx,
  IconBackspace,
  IconFileTypeDoc,
  IconFileTypePdf,
  IconFileTypeXls,
  IconAlertCircleFilled,
  IconPhoto,
  IconMinus
} from '@tabler/icons-vue';
import axios from "axios";
import {useMessage} from 'naive-ui'
import {ElMessage} from 'element-plus'
import {onBeforeMount, onBeforeUnmount, onMounted, onUnmounted, onUpdated, reactive, ref, toRef, watch} from "vue";
import getFilesAttributes from '../pages/Home.vue'
const props = defineProps({
  fileList: {
    type: Object
  },
  getFilesAttributes: {
    type: Object
  }
})
const emit = defineEmits(['check', 'fixed'])
const data = ref({
  copies: 1,
  startPage: "",
  endPage: "",
  isDoubleSide: false,
  loading: true,
  isConvertFinished: false
})
const sideOption = [
  {
    value: 'one-sided',
    label: '单面',
  },
  {
    value: 'two-sided-default',
    label: '双面(默认)',
  },
  {
    value: 'two-sided-long-edge',
    label: '双面(长边翻页)',
  },
  {
    value: 'two-sided-short-edge',
    label: '双面(短边翻页)',
  }
]
const paperOption = ['A4', 'A5', 'A6']
// 色彩：后端按页计价，黑白 0.3 元/页、彩色 0.8 元/页（以所选机器的生效价为准）
const colorOption = [
  {value: 'bw', label: '黑白'},
  {value: 'color', label: '彩色'}
]
const value = ref('')
const removeFile = (filename, index) => {
  axios.post(window.config.baseURL + "/uploadfile/remove", {
    "filename": filename
  }, {
    headers: {
      'Accept': 'application/json',
      'Authorization': 'Bearer ' + window.localStorage.getItem("token")
    }
  }).then(res => {
    if (res.data.message === "success") {
      console.log(props.getFilesAttributes(window.localStorage.getItem("token")))
      props.getFilesAttributes(window.localStorage.getItem("token")).then(res => {
        props.fileList.value = res
      })
      props.fileList.value = []
    }
  }).catch(error => {
        ElMessage.error("删除失败")
        console.log(error)
      }
  )
}
const handleConvertState = (state) => {
  if (state === "success") {
    return false
  }
  if (state === "processing") {
    return true
  }
  return true
}
const preview = async (filename) => {
  // token 只放 Authorization header，不拼进 URL（避免进浏览器历史/访问日志）
  const token = window.localStorage.getItem("token")
  try {
    const res = await axios.get(
      window.config.baseURL + '/preview/' + encodeURIComponent(filename),
      {responseType: 'blob', headers: {Authorization: 'Bearer ' + token}}
    )
    const url = URL.createObjectURL(res.data)
    const win = window.open(url, '_blank')
    // 新窗口已加载后即可释放，60 秒兜底
    setTimeout(() => URL.revokeObjectURL(url), 60000)
    if (!win) ElMessage.warning('浏览器拦截了新窗口，请允许弹出窗口')
  } catch (e) {
    ElMessage.error('预览加载失败，请刷新后重试')
  }
}

/* 页缩略图：后端返回 PNG，这里用 fetch + header 取 blob，再交给 <img> */
const thumbPages = (item) => Math.min(Number(item.total_pages) || 1, 3)
const thumbSrc = reactive({})
const thumbLoading = reactive({})

const thumbKey = (item, page) => (item.filename || '') + '#' + page

const thumbSrcFor = (item, page) => {
  const key = thumbKey(item, page)
  if (thumbSrc[key] || thumbLoading[key]) return thumbSrc[key] || ''
  thumbLoading[key] = true
  const token = window.localStorage.getItem("token")
  axios.get(
    window.config.baseURL + '/api/user/thumb/' + encodeURIComponent(item.filename),
    {params: {page}, responseType: 'blob', headers: {Authorization: 'Bearer ' + token}}
  ).then(res => {
    thumbSrc[key] = URL.createObjectURL(res.data)
  }).catch(() => {
    // 缩略图失败不打扰用户，详情里仍可预览
  }).finally(() => {
    thumbLoading[key] = false
  })
  return ''
}

onUnmounted(() => {
  Object.values(thumbSrc).forEach(url => {
    try { URL.revokeObjectURL(url) } catch (e) { /* ignore */ }
  })
})

/* 打印前检查：转换完成后调后端做规则分析（空白页/彩色页/贴边/横竖版）。
   结果按文件名缓存，只在文件第一次转换成功时请求一次。 */
const checks = reactive({})
const checkRequested = reactive({})
const loadCheck = async (item) => {
  if (checkRequested[item.filename]) return
  checkRequested[item.filename] = true
  const token = window.localStorage.getItem("token")
  try {
    const res = await axios.get('/api/user/check/' + token + '/' + encodeURIComponent(item.filename))
    if (res.data && res.data.message === 'success') {
      checks[item.filename] = res.data.data
      // 把覆盖率数据交给 Home → PriceCounter，用于按覆盖率分档计价
      emit('check', {filename: item.filename, data: res.data.data})
      loadFormat(item)
    }
  } catch (e) {
    checkRequested[item.filename] = false
  }
}
watch(() => (props.fileList.value || []).map(f => f.filename + ':' + f.convert_state).join('|'), () => {
  for (const f of props.fileList.value || []) {
    if (f.convert_state === 'success') loadCheck(f)
  }
}, {immediate: true})

const pageList = (pages) => pages.join('、')

/* ---------------- 格式合规检查与一键修复 ---------------- */
const formatChecks = reactive({})
const formatRequested = reactive({})
const fixing = reactive({})

const loadFormat = async (item) => {
  if (formatRequested[item.filename]) return
  formatRequested[item.filename] = true
  const token = window.localStorage.getItem("token")
  const template = item.format_template || 'general_thesis'
  try {
    const res = await axios.get(
      '/api/user/format/' + token + '/' + encodeURIComponent(item.filename) +
      '?template=' + encodeURIComponent(template))
    if (res.data && res.data.message === 'success') {
      formatChecks[item.filename] = res.data.data
    }
  } catch (e) {
    formatRequested[item.filename] = false
  }
}

const reloadFormat = (item) => {
  formatRequested[item.filename] = false
  formatChecks[item.filename] = null
  loadFormat(item)
}

const formatIssues = (item) => {
  const data = formatChecks[item.filename]
  return (data && data.issues) || []
}

const formatScoreText = (item) => {
  const data = formatChecks[item.filename]
  if (!data) return ''
  if (data.free_format) return '自由格式 · 不做格式检查'
  if (data.scanned) return '扫描件 · 检查降级'
  if (!data.issue_count) return '格式通过'
  const low = (data.confidence_counts && data.confidence_counts.low) || 0
  return `${data.score} 分 · ${data.issue_count} 处待处理${low ? ` · ${low} 处需复核` : ''}`
}

const formatScoreClass = (item) => {
  const data = formatChecks[item.filename]
  if (!data) return ''
  if (data.errors) return 'bad'
  if (data.warnings) return 'warn'
  return 'ok'
}

const fixFormat = async (item) => {
  const data = formatChecks[item.filename]
  if (!data) return
  fixing[item.filename] = true
  const token = window.localStorage.getItem("token")
  try {
    const res = await axios.post(
      '/api/user/fix/' + token + '/' + encodeURIComponent(item.filename),
      {
        remove_blank: true,
        // 自由格式没有版式标准，只做内容级修复（删空白页、补封面、双面配对），不动页边距与页码
        fix_margins: !data.free_format,
        add_page_numbers: !data.free_format,
        // 首页没有大字标题才插封面，避免给已有封面再加一页
        insert_cover: !data.free_format && !data.cover_present,
        duplex: item.print_side !== 'one-sided',
        author: '',
        course: '',
        template: item.format_template || 'general_thesis'
      })
    if (res.data && res.data.message === 'success') {
      const stats = res.data.data
      ElMessage.success(
        `已修复：删空白 ${stats.removed_blank} 页、补页码 ${stats.page_numbers_added} 页、` +
        `调边距 ${stats.margin_fixed} 页，共 ${stats.new_pages} 页`)
      // 修复后重新检查，并通知 Home 刷新页数
      checks[item.filename] = null
      checkRequested[item.filename] = false
      formatChecks[item.filename] = null
      formatRequested[item.filename] = false
      ocrResults[item.filename] = null
      item.total_pages = stats.new_pages
      if (Number(item.print_range_end) > stats.new_pages || !item.print_range_end) {
        item.print_range_end = stats.new_pages
      }
      if (Number(item.print_range_start) > stats.new_pages) item.print_range_start = 1
      emit('fixed', {filename: item.filename, stats})
      await loadCheck(item)
    } else {
      ElMessage.error('修复失败：' + (res.data.message || '未知错误'))
    }
  } catch (e) {
    ElMessage.error('修复请求失败')
    console.log(e)
  } finally {
    fixing[item.filename] = false
  }
}

/* ---------------- A3 拆 A4 ---------------- */
const a3Splitting = reactive({})

const a3Count = (item) => {
  const data = checks[item.filename]
  return (data && data.a3_pages && data.a3_pages.length) || 0
}

const splitA3 = async (item) => {
  if (a3Splitting[item.filename]) return
  a3Splitting[item.filename] = true
  const token = window.localStorage.getItem("token")
  try {
    const res = await axios.post(
      '/api/user/split-a3/' + token + '/' + encodeURIComponent(item.filename),
      {direction: 'auto'})
    if (res.data && res.data.message === 'success') {
      const stats = res.data.data
      ElMessage.success(`已把 ${stats.a3_pages} 页 A3 拆成 ${stats.new_pages} 页 A4`)
      checks[item.filename] = null
      checkRequested[item.filename] = false
      formatChecks[item.filename] = null
      formatRequested[item.filename] = false
      ocrResults[item.filename] = null
      item.total_pages = stats.new_pages
      if (Number(item.print_range_end) > stats.new_pages || !item.print_range_end) {
        item.print_range_end = stats.new_pages
      }
      if (Number(item.print_range_start) > stats.new_pages) item.print_range_start = 1
      emit('fixed', {filename: item.filename, stats})
      await loadCheck(item)
    } else {
      ElMessage.warning(res.data.message || '未检测到 A3 页面')
    }
  } catch (e) {
    ElMessage.error('A3 拆分请求失败')
    console.log(e)
  } finally {
    a3Splitting[item.filename] = false
  }
}

/* ---------------- 多页合一排版（2 / 4 / 6 页每张） ---------------- */
const layoutChoice = reactive({})
const layoutApplying = reactive({})

const applyLayout = async (item, perSheet) => {
  if (layoutApplying[item.filename]) return
  layoutApplying[item.filename] = true
  layoutChoice[item.filename] = perSheet
  const token = window.localStorage.getItem("token")
  try {
    const res = await axios.post(
      '/api/user/layout/' + token + '/' + encodeURIComponent(item.filename),
      {per_sheet: perSheet})
    if (res.data && res.data.message === 'success') {
      const stats = res.data.data
      ElMessage.success(perSheet === 1
        ? `已恢复原始排版，共 ${stats.new_pages} 页`
        : `已排版为每张 ${perSheet} 页，共 ${stats.new_pages} 张纸`)
      checks[item.filename] = null
      checkRequested[item.filename] = false
      formatChecks[item.filename] = null
      formatRequested[item.filename] = false
      ocrResults[item.filename] = null
      item.total_pages = stats.new_pages
      if (Number(item.print_range_end) > stats.new_pages || !item.print_range_end) {
        item.print_range_end = stats.new_pages
      }
      if (Number(item.print_range_start) > stats.new_pages) item.print_range_start = 1
      emit('fixed', {filename: item.filename, stats})
      await loadCheck(item)
    } else {
      ElMessage.warning(res.data.message || '排版失败')
      layoutChoice[item.filename] = 1
    }
  } catch (e) {
    ElMessage.error('排版请求失败')
    console.log(e)
    layoutChoice[item.filename] = 1
  } finally {
    layoutApplying[item.filename] = false
  }
}

/* ---------------- 隐私检查（身份证号 / 手机号 / 银行卡号 / 住址） ---------------- */
const piiChecks = reactive({})
const piiLoading = reactive({})
const piiRedacting = reactive({})

const loadPii = async (item) => {
  if (piiLoading[item.filename] || piiChecks[item.filename]) return
  piiLoading[item.filename] = true
  const token = window.localStorage.getItem("token")
  try {
    const res = await axios.get(
      window.config.baseURL + '/api/user/pii/' + encodeURIComponent(item.filename),
      {headers: {Authorization: 'Bearer ' + token}})
    if (res.data && res.data.message === 'success') {
      piiChecks[item.filename] = res.data.data
    }
  } catch (e) {
    // 隐私检查失败不阻断打印流程，静默即可
  } finally {
    piiLoading[item.filename] = false
  }
}

const piiText = (item) => {
  const data = piiChecks[item.filename]
  if (!data) return ''
  if (data.scanned) return '扫描件 · 无法自动识别，请人工确认'
  if (!data.total) return '未发现身份证号 / 手机号 / 银行卡号 / 住址'
  return Object.entries(data.by_type).map(([k, v]) => `${k} ${v} 处`).join(' · ')
}

const redactPii = async (item) => {
  if (piiRedacting[item.filename]) return
  piiRedacting[item.filename] = true
  const token = window.localStorage.getItem("token")
  try {
    const res = await axios.post(
      window.config.baseURL + '/api/user/pii-redact/' + encodeURIComponent(item.filename),
      {}, {headers: {Authorization: 'Bearer ' + token}})
    if (res.data && res.data.message === 'success') {
      ElMessage.success(`已打码 ${res.data.data.redacted} 处，原件已备份`)
      piiChecks[item.filename] = null
      await loadPii(item)
    } else {
      ElMessage.warning(res.data.message === 'no match' ? '未发现需要打码的内容' : (res.data.message || '打码失败'))
    }
  } catch (e) {
    ElMessage.error('打码请求失败')
  } finally {
    piiRedacting[item.filename] = false
  }
}

watch(() => (props.fileList.value || []).map(f => f.filename + ':' + f.convert_state).join('|'), () => {
  for (const f of props.fileList.value || []) {
    if (f.convert_state === 'success') loadPii(f)
  }
}, {immediate: true})

/* ---------------- 内容审核（GLM 视觉模型，前 N 页） ---------------- */
const reviewResults = reactive({})
const reviewLoading = reactive({})
const reviewRequested = reactive({})

const loadReview = async (item) => {
  if (reviewRequested[item.filename] || reviewLoading[item.filename]) return
  reviewRequested[item.filename] = true
  reviewLoading[item.filename] = true
  const token = window.localStorage.getItem("token")
  try {
    const res = await axios.post(
      window.config.baseURL + '/api/user/review/' + encodeURIComponent(item.filename),
      {}, {headers: {Authorization: 'Bearer ' + token}, timeout: 60000})
    if (res.data && res.data.message === 'success') {
      reviewResults[item.filename] = res.data.data
    } else if (res.data && res.data.message === '未接入') {
      reviewResults[item.filename] = {available: false, message: (res.data.data || {}).message}
    } else {
      reviewRequested[item.filename] = false
    }
  } catch (e) {
    reviewRequested[item.filename] = false
  } finally {
    reviewLoading[item.filename] = false
  }
}

const reviewText = (item) => {
  const data = reviewResults[item.filename]
  if (!data) return ''
  if (data.available === false) return '未接入（缺少 GLM_API_KEY）'
  const map = {pass: '未发现违规', warn: '有风险提示', block: '检测到违规内容', unknown: '审核失败'}
  return map[data.verdict] || data.verdict
}

const reviewClass = (item) => {
  const data = reviewResults[item.filename]
  if (!data || data.available === false) return ''
  if (data.verdict === 'block') return 'bad'
  if (data.verdict === 'warn' || data.verdict === 'unknown') return 'warn'
  return 'ok'
}

watch(() => (props.fileList.value || []).map(f => f.filename + ':' + f.convert_state).join('|'), () => {
  for (const f of props.fileList.value || []) {
    if (f.convert_state === 'success') loadReview(f)
  }
}, {immediate: true})

/* ---------------- 扫描件 OCR（本地 Tesseract，低置信补充检查） ---------------- */
const ocrResults = reactive({})
const ocrLoading = reactive({})

const runOcr = async (item) => {
  if (ocrLoading[item.filename]) return
  ocrLoading[item.filename] = true
  const token = window.localStorage.getItem("token")
  try {
    const res = await axios.get(
      '/api/user/ocr/' + token + '/' + encodeURIComponent(item.filename) +
      '?template=' + encodeURIComponent(item.format_template || 'general_thesis'))
    if (res.data && res.data.message === 'success') {
      const result = res.data.data
      if (!result.available) {
        ElMessage.warning(result.message || 'OCR 不可用')
        return
      }
      ocrResults[item.filename] = result
      if (!result.issue_count) ElMessage.success('OCR 未发现明显问题（仍需人工复核）')
    } else {
      ElMessage.warning(res.data.message || 'OCR 不可用')
    }
  } catch (e) {
    ElMessage.error('OCR 请求失败')
    console.log(e)
  } finally {
    ocrLoading[item.filename] = false
  }
}

/* 彩色覆盖率分档摘要：让用户看到低/中覆盖页确实按折扣价计费 */
const coverageSummary = (item) => {
  if (item.print_color !== 'color') return ''
  const data = checks[item.filename]
  const allPages = (data && data.coverage_pages) || []
  const start = parseInt(item.print_range_start, 10) || 1
  const end = parseInt(item.print_range_end, 10) || start
  const pages = allPages.filter(p => p.page >= start && p.page <= end)
  if (!pages.length) return ''
  const counts = {none: 0, low: 0, mid: 0, high: 0}
  for (const page of pages) counts[page.tier] = (counts[page.tier] || 0) + 1
  const parts = []
  if (counts.low) parts.push(`低覆盖 ${counts.low} 页`)
  if (counts.mid) parts.push(`中覆盖 ${counts.mid} 页`)
  if (counts.high) parts.push(`全彩 ${counts.high} 页`)
  if (counts.none) parts.push(`近黑白 ${counts.none} 页`)
  return parts.length ? `彩色覆盖率分档：${parts.join(' · ')}（低/中覆盖按折扣价）` : ''
}
/* 当前打印范围内的空白页（后端返回的是整份文档的列表） */
const blankInRange = (item) => {
  const c = checks[item.filename]
  if (!c) return []
  const start = parseInt(item.print_range_start, 10) || 1
  const end = parseInt(item.print_range_end, 10) || start
  return (c.blank_pages || []).filter(p => p >= start && p <= end)
}

const checkItems = (item) => {
  const c = checks[item.filename]
  if (!c) return []
  const list = []
  const blanks = blankInRange(item)
  if (blanks.length) {
    if (item.skip_blank !== false) {
      list.push({level: 'ok', text: `已跳过 ${blanks.length} 张空白页（不打印、不计费）`})
    } else {
      list.push({level: 'warn',
        text: `第 ${pageList(blanks.slice(0, 10))} 页是空白页，当前设置会一起打印`})
    }
  }
  if (c.color_pages.length) {
    list.push(item.print_color === 'color'
      ? {level: 'info', text: `第 ${pageList(c.color_pages)} 页含彩色内容`}
      : {level: 'warn', text: `第 ${pageList(c.color_pages)} 页含彩色内容，当前选的是黑白`})
  }
  if (c.tight_pages.length) {
    list.push({level: 'warn', text: `第 ${pageList(c.tight_pages)} 页内容贴边（最小 ${c.min_margin_mm}mm），可能被裁切`})
  }
  if (c.landscape_pages.length) {
    list.push({level: 'info', text: `第 ${pageList(c.landscape_pages)} 页是横向，注意翻面方向`})
  }
  /* 低分辨率图片：位图被放大后打印会发糊，提前提醒换高清原图 */
  if ((c.low_dpi_pages || []).length) {
    list.push({level: 'warn',
      text: `第 ${pageList(c.low_dpi_pages)} 页图片分辨率偏低`
        + `（最低 ${c.min_image_dpi} DPI，建议 ≥${c.dpi_limit}），打印可能发糊`})
  }
  /* 页面尺寸：混合尺寸按各页原尺寸输出；整份非 A4 时提醒纸盒别放错 */
  const sizes = c.size_counts || {}
  const sizeNames = Object.keys(sizes)
  if (sizeNames.length > 1) {
    list.push({level: 'warn',
      text: `页面尺寸不统一（${sizeNames.map(n => `${n}×${sizes[n]}`).join(' / ')}），`
        + '各页按原尺寸打印，装订会不齐'})
  } else if (sizeNames.length === 1 && c.dominant_size
             && c.dominant_size !== 'A4') {
    list.push({level: 'info',
      text: `整份文档是 ${c.dominant_size} 尺寸，请确认打印机纸盒已放对应纸张`})
  }
  /* 双面 + 奇数页：最后一张背面会空着，提前说明避免以为漏印 */
  const pageStart = parseInt(item.print_range_start, 10) || 1
  const pageEnd = parseInt(item.print_range_end, 10) || pageStart
  const pageCount = Math.max(pageEnd - pageStart + 1, 0)
  if (item.print_side !== 'one-sided' && pageCount > 1 && pageCount % 2 === 1) {
    list.push({level: 'info',
      text: `双面打印 ${pageCount} 页（奇数），最后一张背面为空白`})
  }
  if (!list.length) {
    list.push({level: 'ok', text: '检查通过：未发现空白页、彩色页或贴边风险'})
  }
  if (c.truncated) {
    list.push({level: 'info', text: `仅检查了前 ${c.checked_pages} 页`})
  }
  return list
}

/* ---------------- 检查报告：把格式、隐私、空白页、A3 合成一份摘要 ---------------- */
const hasReport = (item) => !!(checks[item.filename]
  || formatChecks[item.filename] || piiChecks[item.filename])

const reportChips = (item) => {
  const chips = []
  const format = formatChecks[item.filename]
  if (format && format.issue_count) {
    chips.push({text: `格式 ${format.issue_count} 处`, level: format.errors ? 'bad' : 'warn'})
  }
  const privacy = piiChecks[item.filename]
  if (privacy && privacy.total) {
    chips.push({text: `隐私 ${privacy.total} 处`, level: 'warn'})
  }
  const blanks = blankInRange(item).length
  if (blanks) {
    chips.push({text: `空白页 ${blanks} 张`,
      level: item.skip_blank !== false ? 'ok' : 'warn'})
  }
  const a3 = a3Count(item)
  if (a3) chips.push({text: `A3 ${a3} 页`, level: 'warn'})
  return chips
}

const reportLevel = (item) => {
  const format = formatChecks[item.filename]
  if (format && format.errors) return 'bad'
  const chips = reportChips(item)
  return chips.some(c => c.level !== 'ok') ? 'warn' : 'ok'
}

const reportSummary = (item) => {
  const pending = reportChips(item).filter(c => c.level !== 'ok').length
  return pending ? `${pending} 类问题待确认` : '检查通过'
}

const reportHint = (item) => {
  const parts = []
  const format = formatChecks[item.filename]
  if (format && format.issue_count) {
    parts.push('格式问题可点「一键修复」，只调整版式，不改正文')
  }
  if ((piiChecks[item.filename] || {}).total) {
    parts.push('隐私信息可点「一键打码」，原件会自动备份')
  }
  const blanks = blankInRange(item).length
  if (blanks && item.skip_blank !== false) {
    parts.push(`空白页已自动跳过 ${blanks} 张，不计费`)
  }
  return parts.join(' · ') || '未发现需要处理的问题，可直接确认打印'
}

</script>

<template>
  <div class="filelist">
    <TransitionGroup name="list" tag="ul" class="upload-file-list">
      <li v-for="(item, index) in props.fileList.value" :key="item">
        <div class="wrapper">
          <div class="upload-file-info">
            <div class="head-warpper">
              <div class="icon-and-filename">
                <icon-file-type-docx class="icon"
                                     v-if="item.filename.split('.')[item.filename.split('.').length - 1] === 'docx'"></icon-file-type-docx>
                <IconFileTypeDoc class="icon"
                                 v-else-if="item.filename.split('.')[item.filename.split('.').length - 1] === 'doc'"></IconFileTypeDoc>
                <IconFileTypeXls class="icon"
                                 v-else-if="item.filename.split('.')[item.filename.split('.').length - 1] === 'xls'"></IconFileTypeXls>
                <IconFileTypeXls class="icon"
                                 v-else-if="item.filename.split('.')[item.filename.split('.').length - 1] === 'xlsx'"></IconFileTypeXls>
                <IconFileTypePdf class="icon"
                                 v-else-if="item.filename.split('.')[item.filename.split('.').length - 1] === 'pdf'"></IconFileTypePdf>
                <IconPhoto class="icon"
                           v-else-if="item.filename.split('.')[item.filename.split('.').length - 1] === 'jpg'"></IconPhoto>
                <IconPhoto class="icon"
                           v-else-if="item.filename.split('.')[item.filename.split('.').length - 1] === 'jpeg'"></IconPhoto>
                <IconPhoto class="icon"
                           v-else-if="item.filename.split('.')[item.filename.split('.').length - 1] === 'png'"></IconPhoto>

                <div class="file-name">
                  <a>{{ item.filename }}</a>
                </div>
              </div>
              <IconBackspace class="icon" @click="removeFile(item.filename)"></IconBackspace>
            </div>
          </div>
          <div class="print-info" :style="item.convert_state==='success'?'display: unset':'display: none'">
            <!-- 页缩略图：转换完成后展示前 3 页，点击可看完整 PDF -->
            <div class="thumbs">
              <img v-for="n in thumbPages(item)" :key="n" :src="thumbSrcFor(item, n)"
                   alt="页缩略图" loading="lazy" @click="preview(item.filename)">
              <span class="thumbs-more" v-if="Number(item.total_pages) > 3">
                +{{ Number(item.total_pages) - 3 }} 页
              </span>
            </div>
            <div class="format-pick">
              <span>格式模板</span>
              <el-select v-model="item.format_template" size="small"
                         @change="reloadFormat(item)">
                <el-option label="通用论文（封面/参考文献）" value="general_thesis"/>
                <el-option label="通用作业（宽松边距）" value="general_homework"/>
                <el-option label="自由格式（不做格式检查）" value="free_format"/>
                <el-option label="学位论文（国标严格）" value="gb_thesis"/>
                <el-option label="实验报告" value="lab_report"/>
                <el-option label="简历（版式自由）" value="resume"/>
                <el-option label="公文（GB/T 9704）" value="official_doc"/>
              </el-select>
            </div>
            <div class="format-pick">
              <span>排版</span>
              <el-select :model-value="layoutChoice[item.filename] || item.layout_per_sheet || 1" size="small"
                         :disabled="layoutApplying[item.filename]"
                         @change="v => applyLayout(item, v)">
                <el-option label="1 页/张（原样）" :value="1"/>
                <el-option label="2 页/张（上下）" :value="2"/>
                <el-option label="4 页/张（讲义）" :value="4"/>
                <el-option label="6 页/张（讲义）" :value="6"/>
              </el-select>
              <span style="color:var(--ink-3)" v-if="layoutApplying[item.filename]">排版中…</span>
              <span style="color:var(--ink-3)" v-else>按排版后的张数计价</span>
            </div>
            <div class="report" v-if="hasReport(item)">
              <div class="report-head">
                <span class="report-title">打印前检查报告</span>
                <span class="report-pill" :class="reportLevel(item)">{{ reportSummary(item) }}</span>
                <span v-for="chip in reportChips(item)" :key="chip.text"
                      class="report-chip" :class="chip.level">{{ chip.text }}</span>
              </div>
              <div class="report-hint">{{ reportHint(item) }}</div>
            <div class="precheck" v-if="checks[item.filename]">
              <span class="pc-title">版面与打印</span>
              <span v-for="(c, i) in checkItems(item)" :key="i" class="pc-msg" :class="c.level">
                {{ c.text }}
              </span>
              <div class="pc-cov" v-if="coverageSummary(item)">{{ coverageSummary(item) }}</div>
              <label class="pc-skip" v-if="blankInRange(item).length">
                <input type="checkbox" v-model="item.skip_blank">
                跳过 {{ blankInRange(item).length }} 张空白页（不打印、不计费）
              </label>
            </div>
            <div class="a3-split" v-if="a3Count(item)">
              <span class="fmt-title">纸张尺寸</span>
              <span class="a3-tip">
                检测到 {{ a3Count(item) }} 页 A3：拆成 A4 后可用 A4 纸打印（横向左右切、纵向上下切）
              </span>
              <button class="fmt-btn" type="button"
                      :disabled="a3Splitting[item.filename]"
                      @click="splitA3(item)">
                {{ a3Splitting[item.filename] ? '拆分中…' : 'A3 拆 A4' }}
              </button>
            </div>
            <div class="format-check" v-if="formatChecks[item.filename]">
              <div class="fmt-head">
                <span class="fmt-title">格式检查</span>
                <span class="fmt-score" :class="formatScoreClass(item)">
                  {{ formatScoreText(item) }}
                </span>
                <button class="fmt-btn" type="button"
                        :disabled="fixing[item.filename]"
                        @click="fixFormat(item)">
                  {{ fixing[item.filename] ? '修复中…' : '一键修复' }}
                </button>
                <button class="fmt-btn ocr-btn" type="button"
                        v-if="formatChecks[item.filename].scanned && formatChecks[item.filename].ocr_available"
                        :disabled="ocrLoading[item.filename]"
                        @click="runOcr(item)">
                  {{ ocrLoading[item.filename] ? '识别中…' : 'OCR 识别' }}
                </button>
              </div>
              <div class="fmt-list">
                <div v-for="(issue, i) in formatIssues(item).slice(0, 6)" :key="i"
                     class="fmt-issue" :class="issue.level">
                  {{ issue.message }}
                  <span class="fmt-conf" v-if="issue.confidence === 'low'">需复核</span>
                </div>
                <div class="fmt-more" v-if="formatChecks[item.filename].issue_count > 6">
                  还有 {{ formatChecks[item.filename].issue_count - 6 }} 处…
                </div>
              </div>
              <div class="ocr-result" v-if="ocrResults[item.filename]">
                <div class="ocr-head">
                  OCR 补充检查（{{ ocrResults[item.filename].lang }} ·
                  {{ ocrResults[item.filename].pages_ocr }} 页{{ ocrResults[item.filename].truncated ? '，已截断' : '' }}）
                  <span class="fmt-conf">低置信 · 需人工复核</span>
                </div>
                <div v-for="(issue, i) in ocrResults[item.filename].issues" :key="i"
                     class="fmt-issue" :class="issue.level">
                  {{ issue.message }}
                </div>
                <div class="fmt-issue ok" v-if="!ocrResults[item.filename].issue_count">
                  未识别到页码或参考文献缺失问题，扫描件建议仍人工翻看一遍
                </div>
              </div>
            </div>
            <div class="review-check" v-if="reviewResults[item.filename]">
              <div class="review-head">
                <span class="review-title">内容审核</span>
                <span class="review-score" :class="reviewClass(item)">{{ reviewText(item) }}</span>
                <span class="review-meta" v-if="reviewResults[item.filename].model">
                  {{ reviewResults[item.filename].pages }}/{{ reviewResults[item.filename].page_count }} 页
                  · {{ reviewResults[item.filename].model }}
                </span>
              </div>
              <div class="review-reason" v-if="reviewResults[item.filename].reason">
                {{ reviewResults[item.filename].reason }}
              </div>
            </div>
            <div class="pii-check" v-if="piiChecks[item.filename]">
              <div class="pii-head">
                <span class="pii-title">隐私检查</span>
                <span class="pii-score" :class="piiChecks[item.filename].total ? 'warn' : 'ok'">
                  {{ piiText(item) }}
                </span>
                <button class="fmt-btn" type="button"
                        v-if="piiChecks[item.filename].total"
                        :disabled="piiRedacting[item.filename]"
                        @click="redactPii(item)">
                  {{ piiRedacting[item.filename] ? '打码中…' : '一键打码' }}
                </button>
              </div>
              <div class="pii-items" v-if="piiChecks[item.filename].total">
                <span v-for="(p, i) in piiChecks[item.filename].items.slice(0, 6)" :key="i" class="pii-item">
                  第 {{ p.page }} 页 · {{ p.type }} {{ p.masked }}
                </span>
              </div>
              <div class="pii-note" v-if="piiChecks[item.filename].scanned">
                没有文字层（可能是扫描件），无法自动检查隐私信息，请人工翻看
              </div>
            </div>
            </div>
            <div class="report report-loading" v-else-if="item.convert_state === 'success'">
              <div class="report-head">
                <span class="report-title">打印前检查报告</span>
                <span class="report-pill">检查中…</span>
              </div>
            </div>
            <div class="print-copies">
              <a> 打印份数</a>
              <el-input-number v-model="item.print_copies" :min="1"/>
            </div>
            <div class="print-range">
              <a>打印范围</a>
              <div class="input-form-wrapper">
                <el-input class="input-form" v-model="item.print_range_start" :placeholder="1"/>
                <a class="slash">  </a>
                <IconMinus/>
                <el-input class="input-form" v-model="item.print_range_end" :placeholder="item.total_pages"/>
<!--                type error-->
              </div>
            </div>
            <div class="print-side">
              <a>双面打印</a>
              <div>
                <el-select
                    class="selector"
                    v-model="item.print_side"
                    placeholder="Select"
                    size="large"
                >
                  <el-option
                      v-for="item in sideOption"
                      :key="item.value"
                      :label="item.label"
                      :value="item.value"
                  />
<!--                                      default-first-option="one-sided"-->

                </el-select>
              </div>
            </div>
            <div class="print-side">
              <a>纸张规格</a>
              <div>
                <el-select class="selector" v-model="item.paper" placeholder="A4" size="large">
                  <el-option v-for="p in paperOption" :key="p" :label="p" :value="p"/>
                </el-select>
              </div>
            </div>
            <div class="print-side">
              <a>色彩</a>
              <div>
                <el-select class="selector" v-model="item.print_color" placeholder="黑白" size="large">
                  <el-option v-for="c in colorOption" :key="c.value" :label="c.label" :value="c.value"/>
                </el-select>
              </div>
            </div>
            <div class="preview">
              <div class="preview-label">
                预览文件
              </div>
              <button class="preview-btn" type="button" @click="preview(item.filename)">
                预览
              </button>
            </div>
          </div>
          <div class="processing" v-if="item.convert_state === 'processing'">
            <div class="loading"></div>
          </div>
          <div class="warning" v-if="item.convert_state === 'error'">
            <div class="warning-text-and-icon"></div>
            <IconAlertCircleFilled class="warning-icon"></IconAlertCircleFilled>
            <a class="warning-text">无法读取文件</a>
          </div>
          <div class="print-info-excel"
               v-if="item.convert_state === 'success' && item.filename.split('.')[item.filename.split('.').length - 1] === 'xlsx' ||
                     item.convert_state === 'success' && item.filename.split('.')[item.filename.split('.').length - 1] === 'xls'">
            <a class="excel-warning-text">Excel表格打印请务必先预览，打印效果可能与实际表格有所不同，建议将表格另存为PDF上传打印</a>
          </div>
        </div>
      </li>
    </TransitionGroup>
  </div>
</template>


<style scoped>

.file-name {
  padding: 8px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 15px;
  font-weight: 600;
  color: var(--ink);
}

.head-warpper {
  width: 100%;
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.icon-and-filename {
  display: flex;
  align-items: center;
  overflow: auto;
}

.icon {
  width: 28px;
  height: 28px;
  color: var(--ink-2);
  flex: 0 0 28px;
  cursor: pointer;
}

.wrapper {
  background: var(--white);
  border: 1px solid var(--line);
  margin-top: 12px;
  padding: 12px 14px;
  border-radius: var(--r);
  box-shadow: var(--sh);
}

.upload-file-info {
  display: flex;
  align-items: center;
}

.upload-file-list {
  list-style: none;
  width: 100%;
  padding: 0px;
  margin: 0px;
}

.print-info {
  padding-bottom: 4px;
}

/* 页缩略图 */
.thumbs {
  display: flex;
  gap: 8px;
  padding: 12px 0 4px;
  align-items: center;
  flex-wrap: wrap;
}
.thumbs img {
  width: 64px;
  height: auto;
  border: 1px solid var(--line);
  border-radius: 6px;
  cursor: pointer;
  background: #fff;
}
.thumbs-more {
  font-size: 12px;
  color: var(--ink-3);
}

/* 打印前检查报告：把格式 / 隐私 / 空白页 / A3 收进同一张卡 */
.report {
  margin: 10px 12px 0;
  border: 1px solid var(--line);
  border-radius: 12px;
  background: var(--white);
  overflow: hidden;
}
.report-head {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  padding: 11px 14px;
  background: var(--blue-50);
  border-bottom: 1px solid var(--line);
}
.report-title { font-size: 13.5px; font-weight: 700; color: var(--ink); }
.report-pill {
  font-size: 11.5px; font-weight: 700; padding: 3px 10px;
  border-radius: 999px; white-space: nowrap;
}
.report-pill.ok { background: var(--ok-bg); color: var(--ok); }
.report-pill.warn { background: var(--warn-bg); color: var(--warn); }
.report-pill.bad { background: #FDECEC; color: var(--crit); }
.report-chip {
  font-size: 11.5px; padding: 3px 9px; border-radius: 999px;
  background: var(--white); border: 1px solid var(--line);
  color: var(--ink-2); white-space: nowrap;
}
.report-chip.ok { color: var(--ok); border-color: #BFE8D2; }
.report-chip.warn { color: var(--warn); border-color: #F3DFB8; }
.report-chip.bad { color: var(--crit); border-color: #F3C9C9; }
.report-hint {
  padding: 8px 14px; font-size: 12px; line-height: 1.6;
  color: var(--ink-2); border-bottom: 1px dashed var(--line);
}
.report-loading .report-pill { background: var(--white); color: var(--ink-2); }
.report .precheck,
.report .a3-split,
.report .format-check,
.report .review-check,
.report .pii-check {
  margin: 0;
  padding: 10px 14px;
  border: 0;
  border-radius: 0;
  background: transparent;
  border-bottom: 1px solid var(--line);
}
.report > :last-child { border-bottom: 0; }
/* 打印前检查结果 */
.precheck {
  margin: 8px 0 2px;
  padding: 8px 10px;
  border-radius: 6px;
  background: var(--line-2);
  border: 1px solid var(--line);
  font-size: 12.5px;
  line-height: 1.8;
}
.pc-title {
  font-weight: 700;
  color: var(--ink-2);
  margin-right: 10px;
}
.pc-msg {
  margin-right: 12px;
}
.pc-msg.warn { color: var(--warn); }
.pc-msg.info { color: var(--ink-2); }
.pc-msg.ok { color: var(--ok); }
.pc-cov {
  margin-top: 2px;
  color: var(--ink-2);
  font-size: 12px;
}
.pc-skip {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-top: 6px;
  font-size: 12px;
  color: var(--ink-2);
  cursor: pointer;
}
.pc-skip input { accent-color: var(--blue); }
/* 格式检查 */
.format-pick {
  display: flex;
  align-items: center;
  gap: 8px;
  margin: 6px 12px 0;
  font-size: 12px;
  color: var(--ink-2);
}
.format-check {
  margin: 6px 12px 0;
  padding: 10px 12px;
  border: 1px solid var(--line);
  border-radius: 10px;
  background: var(--blue-50);
}
.a3-split {
  display: flex;
  align-items: center;
  gap: 10px;
  margin: 6px 12px 0;
  padding: 8px 12px;
  border: 1px solid var(--blue-100);
  border-radius: 10px;
  background: var(--white);
}
.a3-tip { font-size: 12px; color: var(--ink-2); line-height: 1.6; }
.fmt-head { display: flex; align-items: center; gap: 10px; }
.fmt-title { font-size: 12px; font-weight: 700; color: var(--ink); }
.fmt-score { font-size: 12px; color: var(--ink-2); }
.fmt-score.ok { color: var(--ok); }
.fmt-score.warn { color: var(--warn); }
.fmt-score.bad { color: var(--crit); }
.fmt-btn {
  margin-left: auto;
  padding: 4px 10px;
  border: 1px solid var(--blue-100);
  border-radius: 999px;
  background: var(--white);
  color: var(--blue);
  font-size: 12px;
  cursor: pointer;
  font-family: inherit;
}
.fmt-btn:disabled { opacity: .55; cursor: default; }
.ocr-btn { margin-left: 0; }
/* 内容审核 */
.review-check {
  margin: 6px 12px 0;
  padding: 10px 12px;
  border: 1px solid var(--line);
  border-radius: 10px;
  background: var(--white);
}
.review-head { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.review-title { font-size: 12px; font-weight: 700; color: var(--ink); }
.review-score { font-size: 12px; color: var(--ink-2); }
.review-score.ok { color: var(--ok); }
.review-score.warn { color: var(--warn); }
.review-score.bad { color: var(--crit); }
.review-meta { margin-left: auto; font-size: 11px; color: var(--ink-3); }
.review-reason { margin-top: 4px; font-size: 12px; line-height: 1.6; color: var(--ink-2); }
/* 隐私检查 */
.pii-check {
  margin: 6px 12px 0;
  padding: 10px 12px;
  border: 1px solid var(--line);
  border-radius: 10px;
  background: #FFF7F7;
}
.pii-head { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.pii-title { font-size: 12px; font-weight: 700; color: var(--ink); }
.pii-score { font-size: 12px; color: var(--ink-2); }
.pii-score.warn { color: var(--crit); }
.pii-score.ok { color: var(--ok); }
.pii-items { margin-top: 6px; display: flex; flex-wrap: wrap; gap: 8px; }
.pii-item { font-size: 12px; color: var(--ink-2); }
.pii-note { margin-top: 4px; font-size: 12px; color: var(--warn); }
.ocr-result {
  margin-top: 8px;
  padding-top: 8px;
  border-top: 1px dashed var(--line);
}
.ocr-head {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
  font-size: 12px;
  font-weight: 600;
  color: var(--ink-2);
  margin-bottom: 4px;
}
.fmt-list { margin-top: 6px; }
.fmt-issue { font-size: 12px; line-height: 1.7; color: var(--ink-2); }
.fmt-issue.warn { color: var(--warn); }
.fmt-issue.error { color: var(--crit); }
.fmt-issue.info { color: var(--ink-3); }
.fmt-issue.ok { color: var(--ok); }
.fmt-more { font-size: 12px; color: var(--ink-3); }
.fmt-conf {
  margin-left: 6px;
  padding: 1px 6px;
  border-radius: 999px;
  background: #FFF4E5;
  color: #B26A00;
  font-size: 11px;
  white-space: nowrap;
}

.print-copies {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 10px 0 4px;
  font-size: 14.5px;
  color: var(--ink-2);
}

.print-range {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 10px 0 4px;
  font-size: 14.5px;
  color: var(--ink-2);
}

.print-side {
  height: 56px;
  display: flex;
  justify-content: space-between;
  align-items: center;
  font-size: 14.5px;
  color: var(--ink-2);
}
.selector {
  width: 150px;
}
.processing {
  height: 160px;
  display: flex;
  flex-direction: column;
  justify-content: center;
  align-items: center;
}

.loading {
  border: 4px solid var(--line);
  border-top: 4px solid var(--blue);
  border-radius: 50%;
  width: 42px;
  height: 42px;
  display: inline-block;
  animation: turn-around 1.1s linear infinite;
}

@keyframes turn-around {
  0% {
    transform: rotate(0deg);
  }
  100% {
    transform: rotate(360deg);
  }
}

.preview {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 7px 0 2px;
}

.preview-label {
  font-size: 14.5px;
  color: var(--ink-2);
}

.preview-btn {
  height: 34px;
  padding: 0 18px;
  background: var(--white);
  color: var(--blue);
  border: 1px solid var(--blue-100);
  border-radius: var(--r-sm);
  font-size: 14px;
  font-weight: 600;
  cursor: pointer;
  transition: background .15s ease, border-color .15s ease;
}

.preview-btn:hover {
  background: var(--blue-50);
  border-color: var(--blue);
}

.input-form {
  width: 50px;
}

.slash {
  font-size: 20px;
}

.input-form-wrapper {
  display: flex;
  flex-direction: row;
  align-items: center;
}

.warning {
  height: 160px;
  display: flex;
  flex-direction: column;
  justify-content: center;
  align-items: center;
}

.warning-icon {
  width: 44px;
  height: 44px;
  color: var(--danger);
}

.warning-text {
  font-size: 15.5px;
  color: var(--ink);
  margin-top: 8px;
}

.excel-warning-text {
  display: flex;
  padding: 8px 10px;
  margin-top: 10px;
  background: var(--warn-bg);
  border-radius: var(--r-sm);
  color: var(--warn);
  font-size: 13.5px;
  line-height: 1.6;
}

.el-loading-mask {
  z-index: 9;
  border-radius: var(--r);
}

.list-enter-active,
.list-leave-active {
  transition: all 0.5s ease;
}

.list-enter-from,
.list-leave-to {
  opacity: 0;
  transform: translateX(30px);
}
</style>
