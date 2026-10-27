<script setup>
import {computed, onMounted, onUnmounted, reactive, ref, watch} from "vue";
import axios from "axios";
import QRCode from 'qrcode'
import {useRoute, useRouter} from 'vue-router'
import FileList from "@/components/FileList.vue";
import HeroCarousel from "@/components/HeroCarousel.vue";
import {ElMessage} from 'element-plus'
import PayButton from "@/components/PayButton.vue";
import PriceCounter from "@/components/PriceCounter.vue";

const getFilesAttributes = async (token) => {
  let authentication
  if (token === null) {
    authentication = 'none';
  } else {
    authentication = token
  }
  let files_attributes;
  await axios.get(window.config.baseURL + "/uploadfile/filelist", {
    headers: {
      "Authorization": 'Bearer ' + authentication
    }
  }).then(res => {
    if (res.data.message !== 'success') {
      ElMessage.error(res.data.message);
    } else {
      files_attributes = res.data.data.files_attributes
      localStorage.setItem('token', res.data.data.token) // set token
    }
  }).catch(err => {
        console.log(err)
        ElMessage.error('请求服务器失败');
      }
  )
  return files_attributes
};
const lPollConvertStatus = async (token) => { //long polling
  let message
  await axios.get(window.config.baseURL + '/uploadfile/convert_status', {
    headers: {
      "Authorization": 'Bearer ' + token
    }
  }).then(res => {
    message = res.data.message
  })
  return message
}
const fileList = reactive([])

/* ---------------- 选择打印机（GET /api/printer/available，无需密钥） ---------------- */
/* 多租户入口：商家二维码链接带 ?t=<tenant_id>，进入后只看到该点位设备；
   没有参数时按默认租户处理，原有单租户部署不受影响。 */
const tenantId = (() => {
  const fromUrl = new URLSearchParams(window.location.search).get('t')
  if (fromUrl) {
    window.localStorage.setItem('pp_tenant', fromUrl)
    return fromUrl
  }
  return window.localStorage.getItem('pp_tenant') || ''
})()
const tenantInfo = ref(null)
const serviceMessage = ref('')
const printers = ref([])
const printerLoaded = ref(false)
const selectedPrinter = ref(window.localStorage.getItem('pp_printer') || '')
const fetchPrinters = async () => {
  await axios.get(window.config.baseURL + '/api/printer/available',
    {params: tenantId ? {tenant_id: tenantId} : {}}).then(res => {
    const data = (res.data && res.data.data) || {}
    printers.value = data.printers || []
    tenantInfo.value = data.tenant || null
    serviceMessage.value = data.service_active === false ? (data.message || '该点位服务已暂停') : ''
    // 当前选中的机器不在这个点位时清掉，避免把订单下到别的租户
    if (selectedPrinter.value
        && !printers.value.some(p => p.device_id === selectedPrinter.value)) {
      selectedPrinter.value = ''
      window.localStorage.removeItem('pp_printer')
    }
  }).catch(err => {
    console.log(err)
  })
  printerLoaded.value = true
}
const selectPrinter = (p) => {
  selectedPrinter.value = p.device_id
  // 用户手动选机后不再被智能推荐覆盖；点「恢复自动选择」可以切回来
  autoMode.value = false
  window.localStorage.setItem('pp_printer_manual', '1')
  window.localStorage.setItem('pp_printer', p.device_id)
  // 每台机器可能不同价：切换后立刻拉这台机器的价，预估与结算保持一致
  fetchPrice(p.device_id)
}
const printerName = () => {
  const hit = printers.value.find(p => p.device_id === selectedPrinter.value)
  return hit ? (hit.name || hit.device_id) : ''
}
/* 点位信息：地址、营业时间、排队与导航（到店前最关心的信息） */
const printerMeta = (p) => [p.store_name, p.address, p.business_hours]
  .filter(Boolean).join(' · ')
const printerQueue = (p) => Number(p.queue_pages) > 0
  ? `排队 ${p.queue_pages} 页 · 约 ${Number(p.wait_min || 0).toFixed(1)} 分钟`
  : '当前无需排队'
const printerStore = () => {
  const hit = printers.value.find(p => p.device_id === selectedPrinter.value)
  return hit ? (hit.store_name || '') : ''
}
const navUrl = (p) => (p.lat === null || p.lat === undefined
  || p.lng === null || p.lng === undefined)
  ? ''
  : 'https://uri.amap.com/marker?position=' + p.lng + ',' + p.lat
    + '&name=' + encodeURIComponent(p.store_name || p.name || p.device_id)
const openNav = (p) => {
  const url = navUrl(p)
  if (url) window.open(url, '_blank', 'noopener')
}

/* ---------------- 智能选机：距离 + 能力 + 纸量 + 队列 ---------------- */
const autoMode = ref(!window.localStorage.getItem('pp_printer_manual'))
const recommendation = ref(null)
const recommendedId = ref('')
const lastCoords = ref(null)
const locating = ref(false)
let recommendTimer = null

const orderSummary = () => {
  let pages = 0
  let sheets = 0
  let color = false
  let duplex = false
  for (const file of fileList.value || []) {
    if (file.convert_state !== 'success') continue
    const start = parseInt(file.print_range_start, 10)
    const end = parseInt(file.print_range_end, 10)
    if (!Number.isFinite(start) || !Number.isFinite(end) || end < start) continue
    const pageCount = end - start + 1
    const copies = Math.max(parseInt(file.print_copies, 10) || 1, 1)
    pages += pageCount * copies
    sheets += (file.print_side === 'one-sided'
      ? pageCount : Math.ceil(pageCount / 2)) * copies
    if (file.print_color === 'color') color = true
    if (file.print_side !== 'one-sided') duplex = true
  }
  return {pages, sheets, color, duplex, paper: 'A4'}
}

const fetchRecommendation = async () => {
  const body = orderSummary()
  if (lastCoords.value) {
    body.lat = lastCoords.value.latitude
    body.lng = lastCoords.value.longitude
  }
  try {
    const res = await axios.post(window.config.baseURL + '/api/printer/recommend', body,
      {params: tenantId ? {tenant_id: tenantId} : {}})
    const data = res.data && res.data.data
    if (!data) return
    recommendation.value = data
    recommendedId.value = data.recommended || ''
    if (autoMode.value && recommendedId.value) {
      selectedPrinter.value = recommendedId.value
      window.localStorage.setItem('pp_printer', recommendedId.value)
      await fetchPrice(recommendedId.value)
    }
  } catch (err) {
    console.log(err)
  }
}

const recommendationReasons = () => {
  const data = recommendation.value
  if (!data || !recommendedId.value) return ''
  const hit = (data.candidates || []).find(c => c.device_id === recommendedId.value)
  return hit ? (hit.reasons || []).join(' · ') : ''
}

const recommendationName = () => {
  const data = recommendation.value
  if (!data) return ''
  const hit = (data.candidates || []).find(c => c.device_id === recommendedId.value)
  return hit ? (hit.name || hit.device_id) : recommendedId.value
}

const recommendationPenalties = () => {
  const data = recommendation.value
  if (!data || !recommendedId.value) return ''
  const hit = (data.candidates || []).find(c => c.device_id === recommendedId.value)
  return hit ? (hit.penalties || []).join(' · ') : ''
}

const enableAutoSelect = () => {
  autoMode.value = true
  window.localStorage.removeItem('pp_printer_manual')
  fetchRecommendation()
}

const locateAndRecommend = () => {
  if (!navigator.geolocation) {
    ElMessage.warning('当前浏览器不支持定位')
    return
  }
  locating.value = true
  navigator.geolocation.getCurrentPosition(
    (position) => {
      locating.value = false
      lastCoords.value = position.coords
      fetchRecommendation()
      ElMessage.success('已按距离重新推荐打印机')
    },
    () => {
      locating.value = false
      ElMessage.warning(location.protocol === 'https:'
        ? '定位失败，请允许浏览器获取位置'
        : '浏览器定位需要 HTTPS，当前站点是 HTTP')
    },
    {enableHighAccuracy: false, timeout: 8000, maximumAge: 60000}
  )
}

// 打印参数变化后重新推荐（防抖，避免每敲一个字符都请求）
watch(() => (fileList.value || []).map(f => [
  f.filename, f.print_color, f.print_side, f.print_copies,
  f.print_range_start, f.print_range_end, f.convert_state
].join(':')).join('|'), () => {
  clearTimeout(recommendTimer)
  recommendTimer = setTimeout(fetchRecommendation, 400)
})
/* ---------------- UPS 供电状态（设备未上报就如实说「未接入」，不编造分钟数） ---------------- */
const upsText = (p) => {
  if (!p) return ''
  if (p.ups_online === true) return '市电正常'
  if (p.ups_online === false) {
    if (p.ups_runtime_min === null || p.ups_runtime_min === undefined) {
      return 'UPS 供电中（剩余时长待上报）'
    }
    return `UPS 供电中（剩余 ${p.ups_runtime_min} 分钟）`
  }
  return 'UPS 未接入'
}
const upsClass = (p) => {
  if (p && p.ups_online === false) return 'ups-warn'
  if (p && p.ups_online === true) return 'ups-ok'
  return 'ups-none'
}
const selectedUpsText = () => {
  const hit = printers.value.find(p => p.device_id === selectedPrinter.value)
  return hit ? upsText(hit) : ''
}
const selectedUpsClass = () => {
  const hit = printers.value.find(p => p.device_id === selectedPrinter.value)
  return hit ? upsClass(hit) : 'ups-none'
}

/* ---------------- 单价（GET /api/price?device_id=xxx，无需密钥） ---------------- */
/* 每台机器可以单独定价：选机后拉取「这台机器」的生效价（该机独立价或全局默认价），
   既用于下方预估，也与后端 /pay/bill/create 的结算价同源，保证两边一致。 */
const priceCfg = ref(null)
const fetchPrice = async (deviceId) => {
  await axios.get(window.config.baseURL + '/api/price', {
    params: deviceId ? {device_id: deviceId} : {}
  }).then(res => {
    const cfg = res.data && res.data.data
    if (!cfg) return
    priceCfg.value = cfg
    // 同步覆盖 window.config：基座其它组件仍按这几个值兜底
    for (const key of ['bw', 'color', 'bw_duplex', 'color_duplex']) {
      if (cfg[key] > 0) window.config[key] = cfg[key]
    }
  }).catch(err => {
    console.log(err)
  })
}
const priceText = () => {
  const cfg = priceCfg.value
  if (!cfg) return ''
  return `单面 黑白 ${cfg.bw} / 彩色 ${cfg.color} 元/页 · 双面 黑白 ${cfg.bw_duplex} / 彩色 ${cfg.color_duplex} 元/张 · 彩色按覆盖率分档`
}

/* 覆盖率数据：FileList 检查完成后回传，用于按覆盖率分档计价 */
const coverageMap = reactive({})
const onCheck = ({filename, data}) => {
  coverageMap[filename] = data
}
const onFixed = async () => {
  // 修复后页数/范围会变，重新拉一次文件列表，并重新推荐打印机
  await setFileList(window.localStorage.getItem("token"))
  await fetchRecommendation()
}

/* ---------------- 班级 / 社团批量 ----------------
   定位：一个人付全款，打印 N 份，统一取件。
   名单只作备注，不参与份数绑定、不参与计价、不做逐人分账。 */
const batchOn = ref(false)
const batchCopies = ref(40)
const batchNamesText = ref('')
const batchOwner = ref('')
const totalPrice = ref(0)

const batchNames = computed(() => batchNamesText.value
  .split(/\n|,|，/)
  .map(name => name.trim())
  .filter(Boolean))

const batchPayload = computed(() => batchOn.value ? {
  mode: 'unified',
  names: batchNames.value,
  owner: batchOwner.value.trim()
} : null)

const applyBatchCopies = () => {
  if (!batchOn.value) return
  const copies = Math.max(1, Math.min(100, Number(batchCopies.value) || 1))
  for (const file of fileList.value || []) {
    file.print_copies = copies
  }
}

// 关掉批量时恢复原来的份数，避免残留 40 份被普通打印误用
let batchPrevCopies = 1
watch(batchCopies, applyBatchCopies)
watch(batchOn, (on) => {
  if (on) {
    batchPrevCopies = Math.max(1, Number(fileList.value?.[0]?.print_copies) || 1)
    if (batchCopies.value <= 1) batchCopies.value = 40
    applyBatchCopies()
    return
  }
  for (const file of fileList.value || []) {
    file.print_copies = batchPrevCopies
  }
})

/* ---------------- 拼单打印 ----------------
   一个人发起 → 生成分享链接/二维码 → 发到宿舍群、课程群 →
   同学扫码打开 /?group=短码，各自上传自己的文件并填姓名 →
   人数越多折扣越低（3 人 9 折、5 人 8 折、10 人 7 折）→
   发起人统一付款、统一取件，每人一份带姓名封皮的文件便于分拣。 */
const route = useRoute()
const router = useRouter()
/* 登录账号：进入页面前校验 token，未登录或封禁都退回登录页 */
const currentUser = ref(null)
const checkLogin = async () => {
  const token = window.localStorage.getItem('token')
  if (!token) return false
  try {
    const res = await axios.get(window.config.baseURL + '/api/auth/me', {
      headers: {Authorization: 'Bearer ' + token}
    })
    currentUser.value = res.data.data.user
    return true
  } catch (e) {
    window.localStorage.removeItem('token')
    window.localStorage.removeItem('pp_user')
    return false
  }
}
const logout = () => {
  window.localStorage.removeItem('token')
  window.localStorage.removeItem('pp_user')
  router.replace('/login')
}
const joinNo = ref(((route.query.group || '') + '').trim().toUpperCase())
const joinMode = ref(!!joinNo.value)
const joinInfo = ref(null)        // 加入页：拼单实时进度
const groupInfo = ref(null)       // 发起页：我发起的拼单
const groupOwnerName = ref('')
const groupPayMode = ref('aa')
const groupCreating = ref(false)
const groupLink = ref('')
const groupQrData = ref('')
const joinName = ref('')
const joining = ref(false)
const joinFile = ref('')
const leaving = ref(false)
let groupTimer = null

const groupAuthHeader = () => ({
  'Accept': 'application/json',
  'Content-Type': 'application/json',
  'Authorization': 'Bearer ' + (window.localStorage.getItem('token') || 'none')
})
const moneyOf = (v) => Number(v || 0).toFixed(2)
const convertedFiles = computed(() =>
  (fileList.value || []).filter(f => f.convert_state === 'success'))
const myMembers = computed(() =>
  ((joinInfo.value && joinInfo.value.members) || []).filter(m => m.mine))

const discountText = (state) => {
  if (!state || !state.member_count) return '还没有人加入'
  return state.discount >= 1
    ? '当前原价（满 3 人 9 折）'
    : `当前已享 ${(state.discount * 10).toFixed(0)} 折`
}
const nextTierText = (state) => {
  if (!state) return ''
  const next = state.next_tier
  if (!next) return '已是最高折扣'
  return `还差 ${next.more} 人，全单降到 ${(next.factor * 10).toFixed(0)} 折`
}
const ladderText = (state) => {
  const ladder = (state && state.ladder) || []
  return ladder.slice().sort((a, b) => a.count - b.count)
    .map(t => `${t.count} 人 ${(t.factor * 10).toFixed(0)} 折`).join(' · ')
}

const fetchGroup = async (groupNo, target) => {
  if (!groupNo) return
  try {
    const res = await axios.get(
      window.config.baseURL + '/api/group/' + groupNo,
      {headers: groupAuthHeader()})
    if (res.data && res.data.message === 'success') {
      target.value = res.data.data
    }
  } catch (err) {
    console.log(err)
  }
}
const fetchJoinInfo = () => fetchGroup(joinNo.value, joinInfo)
const fetchMyGroup = () => groupInfo.value && fetchGroup(groupInfo.value.group_no, groupInfo)

const buildGroupLink = (groupNo) => `${window.location.origin}/?group=${groupNo}`
const makeQr = async (url) => {
  try {
    // 前端本地生成二维码：不依赖外网服务，现场没网也能扫
    groupQrData.value = await QRCode.toDataURL(url, {margin: 1, width: 260})
  } catch (err) {
    console.log(err)
    groupQrData.value = ''
  }
}
const copyGroupLink = async () => {
  try {
    await navigator.clipboard.writeText(groupLink.value)
    ElMessage.success('链接已复制，发到宿舍群 / 课程群即可')
  } catch (err) {
    ElMessage.warning('复制失败，请手动选中链接复制')
  }
}

const createGroup = async () => {
  const name = groupOwnerName.value.trim()
  if (!name) {
    ElMessage.warning('请填写发起人姓名')
    return
  }
  groupCreating.value = true
  try {
    const res = await axios.post(window.config.baseURL + '/api/group/create',
      {owner_name: name, printer_id: selectedPrinter.value || null,
       pay_mode: groupPayMode.value},
      {headers: groupAuthHeader()})
    if (res.data.message === 'success') {
      groupInfo.value = res.data.data
      groupLink.value = buildGroupLink(res.data.data.group_no)
      await makeQr(groupLink.value)
      ElMessage.success('拼单已发起，把链接发到群里让大家扫码加入')
    } else {
      ElMessage.error(res.data.message)
    }
  } catch (err) {
    ElMessage.error('发起拼单失败')
    console.log(err)
  } finally {
    groupCreating.value = false
  }
}

const collectGroup = async () => {
  if (!groupInfo.value) return
  try {
    const res = await axios.post(
      window.config.baseURL + '/api/group/collect/' + groupInfo.value.group_no,
      {}, {headers: groupAuthHeader()})
    if (res.data.message === 'success') {
      groupInfo.value = res.data.data
      ElMessage.success('已锁定人数，大家可以在拼单页各自付款了')
    } else {
      ElMessage.error(res.data.message)
    }
  } catch (err) {
    ElMessage.error('锁定失败，请重试')
    console.log(err)
  }
}

const joinGroup = async () => {
  const name = joinName.value.trim()
  if (!name) {
    ElMessage.warning('请填写姓名（会印在封皮上）')
    return
  }
  const filename = joinFile.value || (convertedFiles.value[0]
    && convertedFiles.value[0].filename)
  if (!filename) {
    ElMessage.warning('请先上传文件并等转换完成')
    return
  }
  const file = (fileList.value || []).find(f => f.filename === filename) || {}
  joining.value = true
  try {
    const res = await axios.post(window.config.baseURL + '/api/group/join', {
      group_no: joinNo.value,
      name,
      filename,
      print_copies: Number(file.print_copies || 1),
      print_range_start: Number(file.print_range_start || 1),
      print_range_end: Number(file.print_range_end || 1),
      print_side: file.print_side || 'one-sided',
      print_color: file.print_color || 'bw',
      paper: file.paper || 'A4',
      skip_blank: file.skip_blank !== false
    }, {headers: groupAuthHeader()})
    if (res.data.message === 'success') {
      joinInfo.value = res.data.data
      ElMessage.success('已加入拼单；发起人锁定后，你可以在本页支付自己的份额')
    } else {
      ElMessage.error(res.data.message)
    }
  } catch (err) {
    ElMessage.error('加入失败，请重试')
    console.log(err)
  } finally {
    joining.value = false
  }
}

const leaveGroup = async () => {
  leaving.value = true
  try {
    const res = await axios.post(
      window.config.baseURL + '/api/group/leave/' + joinNo.value, {},
      {headers: groupAuthHeader()})
    if (res.data.message === 'success') {
      joinInfo.value = res.data.data
      ElMessage.success('已退出拼单')
    } else {
      ElMessage.error(res.data.message)
    }
  } catch (err) {
    ElMessage.error('退出失败')
  } finally {
    leaving.value = false
  }
}

const removeMember = async (memberId) => {
  try {
    const res = await axios.post(
      window.config.baseURL + '/api/group/remove/' + groupInfo.value.group_no +
      '/' + memberId, {}, {headers: groupAuthHeader()})
    if (res.data.message === 'success') {
      groupInfo.value = res.data.data
    } else {
      ElMessage.error(res.data.message)
    }
  } catch (err) {
    ElMessage.error('移除失败')
  }
}

const reopenGroup = async () => {
  try {
    const res = await axios.post(
      window.config.baseURL + '/api/group/reopen/' + groupInfo.value.group_no, {},
      {headers: groupAuthHeader()})
    if (res.data.message === 'success') {
      groupInfo.value = res.data.data
      ElMessage.success('已取消本次结算，可以继续拉人')
    } else {
      ElMessage.error(res.data.message)
    }
  } catch (err) {
    ElMessage.error('操作失败')
  }
}

// 加入页：转换完成的文件自动选第一个，省得用户再点一次
watch(convertedFiles, (list) => {
  if (!joinFile.value && list.length) joinFile.value = list[0].filename
}, {immediate: true})

/* 打印参数只存在前端：轮询重新拉文件列表时，按文件名把用户改过的值带过去，
   避免另一个文件转换完成时把已选的色彩/纸张/份数/范围重置掉 */
const FILE_SETTING_KEYS = ['paper', 'print_color', 'print_copies',
  'print_range_start', 'print_range_end', 'print_side', 'format_template',
  'skip_blank']

const setFileList = async (token) => {
  const previous = new Map((fileList.value || []).map(f => [f.filename, f]))
  await getFilesAttributes(token).then(res => {
    fileList.value = res
    // 每个文件默认 A4、黑白；未设置时后端也按 A4 / 黑白处理
    if (fileList.value) {
      for (const file of fileList.value) {
        const old = previous.get(file.filename)
        if (old) {
          for (const key of FILE_SETTING_KEYS) {
            if (old[key] !== undefined && old[key] !== null && old[key] !== '') {
              file[key] = old[key]
            }
          }
        }
        if (!file.paper) file.paper = 'A4'
        if (!file.print_color) file.print_color = 'bw'
        if (!file.format_template) file.format_template = 'general_thesis'
        if (file.skip_blank === undefined || file.skip_blank === null) file.skip_blank = true
      }
      // 批量模式下新上传的文件也要跟随份数，避免界面写 40 份实际只打 1 份
      if (batchOn.value) applyBatchCopies()
    }
  })
  for (let file of fileList.value) {
    if (file.convert_state === 'processing' || file.convert_state === 'pending') {
      lPollConvertStatus(token).then(res => {
        if (res === 'success') {
          setFileList(token)
        }
      })
    }
  }
}
const chooseFile = () => {
  inputFiles.click()
}
const chooseImage = () => {
  inputImages.click()
}
let inputImages = reactive()
let inputFiles = reactive()
/* 单文件大小上限：100MB（后端 /uploadfile 同样校验，避免绕过前端） */
const MAX_UPLOAD_MB = 100
const MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024

const firstTooLarge = (files) => {
  for (let i = 0; i < files.length; i++) {
    if (files[i].size > MAX_UPLOAD_BYTES) return files[i].name
  }
  return ''
}

const handleFormFilesInput = () => { //handleFileUpload
  const big = firstTooLarge(inputFiles.files) || firstTooLarge(inputImages.files)
  if (big) {
    ElMessage.error(`「${big}」超过 ${MAX_UPLOAD_MB}MB，无法上传`)
    inputFiles.value = ''
    inputImages.value = ''
    return
  }
  let uploadFiles = new FormData()
  for (let length = inputFiles.files.length, i = 0; i < length; i++) {
    uploadFiles.append("files", inputFiles.files[i])
    fileList.value.push({
      "filename": inputFiles.files[i].name,
      "convert_state": "processing",
      "total_pages": '1'
    })
  }
  for (let length = inputImages.files.length, i = 0; i < length; i++) {
    uploadFiles.append("files", inputImages.files[i])
    fileList.value.push({
      "filename": inputImages.files[i].name,
      "convert_state": "processing",
      "total_pages": '1'
    })
  }
  axios.put(window.config.baseURL + "/uploadfile", uploadFiles, {
    headers: {
      'Accept': 'application/json',
      'Content-Type': "multipart/form-data",
      'Authorization': 'Bearer ' + window.localStorage.getItem("token")
    }
  }).then(
      res => {
        // 后端对超限/类型/文件名不合法的文件返回 code 200 + 错误 message，
        // 这里必须把 message 展示出来，否则用户只看到文件一直"处理中"
        const message = res && res.data && res.data.message
        if (message !== 'success') {
          ElMessage.error(message || '上传失败')
        }
        inputImages.value = ''
        inputFiles.value = ''
        setFileList(window.localStorage.getItem("token"))
      }
  ).then((error) => {
    console.log(error)
  })
}
const hrefTutorial = () => {
  const ua = navigator.userAgent
  if (ua.includes('iphone')) {
    window.open('static/tutorial/ios-tutorial.html')
  } else
    window.open('static/tutorial/android-tutorial.html')
}
/* ---------------- 最近订单 / 订单详情 ---------------- */
/* 订单只在付款后产生，folder 就是本浏览器的会话目录，
   所以这里只能看到当前会话下的订单，不会串到别人。 */
const orders = ref([])
const orderDetailId = ref(null)
// 详情用 id 关联订单列表，轮询刷新时弹窗里的状态也会跟着变
const orderDetail = computed(() => orders.value.find(o => o.id === orderDetailId.value) || null)
const ejectEvidence = ref([])
const ejectEvidenceLoading = ref(false)
const photoUrls = ref({})
let evidenceRequestId = 0
const clearPhotoUrls = () => {
  Object.values(photoUrls.value).forEach(url => URL.revokeObjectURL(url))
  photoUrls.value = {}
}
const loadEjectEvidence = async (orderId) => {
  const requestId = ++evidenceRequestId
  clearPhotoUrls()
  ejectEvidence.value = []
  ejectEvidenceLoading.value = true
  try {
    const token = window.localStorage.getItem('token')
    const res = await axios.get(`${window.config.baseURL}/api/user/order/${orderId}/eject`, {
      headers: {Authorization: 'Bearer ' + token}
    })
    if (requestId !== evidenceRequestId) return
    const events = res.data?.data?.events || []
    ejectEvidence.value = events
    const urls = {}
    await Promise.all(events.filter(event => event.photo_url).map(async event => {
      const image = await axios.get(window.config.baseURL + event.photo_url, {
        headers: {Authorization: 'Bearer ' + token}, responseType: 'blob'
      })
      if (requestId === evidenceRequestId) urls[event.event_id] = URL.createObjectURL(image.data)
    }))
    if (requestId === evidenceRequestId) photoUrls.value = urls
    else Object.values(urls).forEach(url => URL.revokeObjectURL(url))
  } catch (err) {
    if (requestId === evidenceRequestId) ejectEvidence.value = []
  } finally {
    if (requestId === evidenceRequestId) ejectEvidenceLoading.value = false
  }
}
let ordersTimer = null

const statusInfo = (o) => {
  const map = {
    scheduled: {text: '已预约', cls: 'st-sched', step: 0},
    pending: {text: '已支付', cls: 'st-paid', step: 0},
    printing: {text: '打印中', cls: 'st-printing', step: 1},
    completed: {text: '待取件', cls: 'st-done', step: 2},
    failed: {text: '打印失败', cls: 'st-failed', step: -1},
    canceled: {text: '已取消', cls: 'st-mute', step: -1},
    expired: {text: '预约过期', cls: 'st-mute', step: -1}
  }
  return map[o && o.status] || {text: (o && o.status) || '—', cls: 'st-paid', step: 0}
}
const money = (v) => Number(v || 0).toFixed(2)
const sideText = (s) => s === 'one-sided' ? '单面' : '双面'
const colorText = (c) => c === 'color' ? '彩色' : '黑白'
const orderNo = (o) => o.out_trade_no || ('#' + o.id)
const payStatusText = (s) => s === 'confirmed' ? '已核验收款' : (s === 'submitted' ? '待核验收款' : '')
/* 交付承诺：承诺完成时间、免单标识、免费重印申请 */
const etaDeadlineText = (o) => (o && o.eta_deadline) ? String(o.eta_deadline).slice(11, 19) : ''
/* 打印进度：客户端按份上报，显示「已提交 X/Y 份 · N 页」 */
const progressText = (o) => {
  const total = Math.max(Number(o && (o.batch_bundle ? 1 : o.print_copies)) || 1, 1)
  const done = Math.max(Number(o && o.progress_copies) || 0, 0)
  const pages = Math.max(Number(o && o.progress_pages) || 0, 0)
  if (o && o.status === 'completed') return `已完成 · ${pages || Number(o.pages) || 0} 页`
  return `已提交 ${done}/${total} 份 · ${pages} 页`
}
const showProgress = (o) => !!(o && (o.status === 'printing' || Number(o.progress_copies) > 0))
const reprintReason = ref('')
const reprinting = ref(false)
const applyReprint = async () => {
  const o = orderDetail.value
  if (!o) return
  reprinting.value = true
  try {
    const token = window.localStorage.getItem('token')
    const res = await axios.post(
      window.config.baseURL + '/api/user/reprint/' + o.id,
      {reason: reprintReason.value || ''},
      {headers: {'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'}})
    if (res.data && res.data.message === 'success') {
      ElMessage.success('重印申请已提交，管理员审核通过后免费重印')
      reprintReason.value = ''
      await fetchOrders()
    } else {
      ElMessage.error((res.data && res.data.message) || '申请失败')
    }
  } catch (err) {
    ElMessage.error('申请失败，请重试')
  } finally {
    reprinting.value = false
  }
}

/* ---------------- 定时打印（预约出纸） ----------------
   付款即备好文件并落库，但不下发到打印机；到预约时间由后台自动放行排队。
   未到点之前可以取消预约，已付款的记为「待退款」由现场人工退回。 */
const scheduleOn = ref(false)
const scheduleAt = ref('')          // datetime-local 的值："YYYY-MM-DDTHH:MM"
const canceling = ref(false)
const _pad2 = (n) => String(n).padStart(2, '0')
/* 最早可选时间：当前时间 +1 分钟后（后端要求至少 1 分钟） */
const scheduleMin = computed(() => {
  const d = new Date(Date.now() + 60000)
  return `${d.getFullYear()}-${_pad2(d.getMonth() + 1)}-${_pad2(d.getDate())}`
    + `T${_pad2(d.getHours())}:${_pad2(d.getMinutes())}`
})
/* 传给后端：把 "T" 换空格，秒补 00，形如 "2026-10-21 08:30:00" */
const schedulePayload = computed(() => {
  if (!scheduleOn.value || !scheduleAt.value) return ''
  return scheduleAt.value.replace('T', ' ') + ':00'
})
const cancelScheduled = async () => {
  const o = orderDetail.value
  if (!o) return
  canceling.value = true
  try {
    const token = window.localStorage.getItem('token')
    const res = await axios.post(
      window.config.baseURL + '/api/user/order/' + o.id + '/cancel', {},
      {headers: {'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'}})
    if (res.data && res.data.message === 'success') {
      ElMessage.success((res.data.data && res.data.data.note) || '预约已取消')
      await fetchOrders()
    } else {
      ElMessage.error((res.data && res.data.message) || '取消失败')
    }
  } catch (err) {
    ElMessage.error('取消请求失败，请重试')
  } finally {
    canceling.value = false
  }
}
const batchNamesTextOf = (o) => {
  const names = o.batch_names || []
  if (!names.length) return ''
  const head = names.slice(0, 10).join('、')
  return names.length > 10 ? `${head} 等 ${names.length} 人` : head
}
const recentOrders = computed(() => orders.value.slice(0, 3))
const openOrder = (o) => {
  orderDetailId.value = o.id
  loadEjectEvidence(o.id)
}
const closeOrder = () => {
  orderDetailId.value = null
  evidenceRequestId += 1
  ejectEvidence.value = []
  clearPhotoUrls()
}

const fetchOrders = async () => {
  const token = window.localStorage.getItem("token")
  if (!token || token === 'none') { orders.value = []; return }
  await axios.get(window.config.baseURL + '/api/user/orders', {
    headers: {'Authorization': 'Bearer ' + token}
  }).then(res => {
    orders.value = (res.data && res.data.data && res.data.data.orders) || []
  }).catch(err => {
    console.log(err)
  })
}

onMounted(async () => {
  // 必须登录才能上传/下单：未登录或账号被封禁时退回登录页
  if (!(await checkLogin())) {
    router.replace('/login')
    return
  }
  await fetchPrice(selectedPrinter.value || undefined)
  await fetchPrinters()
  await setFileList(window.localStorage.getItem("token"))
  await fetchOrders()
  await fetchLibraryStats()
  await fetchLibraryCategories()
  await fetchRecommendation()
  ordersTimer = setInterval(fetchOrders, 8000)
  // 拼单进度每 5 秒刷新一次：加入页看人数/折扣实时变化，发起页看谁加入了
  if (joinMode.value) {
    await fetchJoinInfo()
  }
  groupTimer = setInterval(() => {
    if (joinMode.value) fetchJoinInfo()
    else if (groupInfo.value) fetchMyGroup()
  }, 5000)
  inputFiles = reactive(
      document.getElementById("input-files")
  )
  inputImages = reactive(
      document.getElementById("input-image")
  )
})
onUnmounted(() => {
  if (ordersTimer) clearInterval(ordersTimer)
  if (groupTimer) clearInterval(groupTimer)
  if (recommendTimer) clearTimeout(recommendTimer)
})
const isShowPayArea = ref(false)

/* ---------------- 首页 UI：设备状态条 / 价格 / 底部导航 ---------------- */
const showPrice = ref(false)
const showPrinterPicker = ref(false)
const showMe = ref(false)
/* 共享资料库：用户自愿勾选，勾选后本单 95 折；批量含他人姓名，禁止共享 */
const shareLibrary = ref(false)
const shareCategory = ref('其他')
/* 分类由管理端维护，前端只拉取，不写死 */
const libraryCategories = ref(['其他'])
const fetchLibraryCategories = async () => {
  try {
    const res = await axios.get(window.config.baseURL + '/api/library/categories')
    const cats = (res.data && res.data.data && res.data.data.categories) || []
    if (cats.length) {
      libraryCategories.value = cats
      if (!cats.includes(shareCategory.value)) {
        shareCategory.value = cats.includes('其他') ? '其他' : cats[0]
      }
    }
  } catch (e) {
    // 分类接口失败时保留「其他」，不阻塞下单
  }
}
/* 资料库贡献统计：已公开资料被真实打印的次数 → 贡献折扣 */
const libraryStats = ref({
  docs: 0, print_count: 0, print_pages: 0,
  contribution_rate: 0, next_need: null, next_rate: null
})
const fetchLibraryStats = async () => {
  const token = window.localStorage.getItem('token')
  if (!token || token === 'none') return
  try {
    const res = await axios.get(window.config.baseURL + '/api/library/my-stats',
      {headers: {Authorization: 'Bearer ' + token}})
    if (res.data && res.data.data) libraryStats.value = res.data.data
  } catch (e) {
    // 资料库统计失败不影响正常下单
  }
}
const effectiveDiscountRate = computed(() => Math.max(
  shareLibrary.value ? 0.05 : 0,
  Number(libraryStats.value.contribution_rate) || 0
))
const discountPercent = computed(() => Math.round(effectiveDiscountRate.value * 100))
const contributionPercent = computed(() =>
  Math.round((Number(libraryStats.value.contribution_rate) || 0) * 100))
const shareTag = computed(() => effectiveDiscountRate.value > 0
  ? discountPercent.value + '% 优惠' : '95 折')
watch(batchOn, (on) => {
  if (on) shareLibrary.value = false
})

const selectedPrinterObj = computed(() =>
    printers.value.find(p => p.device_id === selectedPrinter.value) || null)
const printerOnline = computed(() => {
  const p = selectedPrinterObj.value
  return !p || p.status === 'online'
})
const statusText = computed(() => {
  const p = selectedPrinterObj.value
  if (!p) return '按默认设备排队'
  return p.status === 'online' ? '打印机正常运行' : '打印机离线'
})
const priceRows = computed(() => {
  const c = priceCfg.value
  if (!c) return []
  return [
    {label: 'A4 黑白 · 单面', value: `¥${c.bw} / 页`},
    {label: 'A4 彩色 · 单面', value: `¥${c.color} / 页`},
    {label: 'A4 黑白 · 双面', value: `¥${c.bw_duplex} / 张`},
    {label: 'A4 彩色 · 双面', value: `¥${c.color_duplex} / 张`},
  ]
})
const meName = computed(() =>
    (currentUser.value && (currentUser.value.nickname || currentUser.value.phone)) || '我')
const mePhone = computed(() =>
    (currentUser.value && currentUser.value.phone) || '—')
const avatarLetter = computed(() => (meName.value || '我').slice(0, 1))
const scrollTop = () => window.scrollTo({top: 0, behavior: 'smooth'})
const scrollToOrders = () => {
  const el = document.getElementById('orders-anchor')
  if (el) el.scrollIntoView({behavior: 'smooth', block: 'start'})
  else ElMessage.info('还没有订单')
}

watch(() => fileList.value, () => {
  for (let file of fileList.value) {
    if (file.convert_state === 'processing' || file.convert_state === 'pending') {
      break
    } else {
      isShowPayArea.value = true
    }
  }
}, {deep: true})
</script>

<template>
  <div class="pp">
    <header class="topbar">
      <span class="mark">印</span>
      <div class="brand">
        <div class="bt">思印客</div>
        <div class="bs">SILNK</div>
      </div>
      <div class="pagetitle">首页</div>
      <span class="sp"></span>
      <button class="toplink" type="button" @click="scrollToOrders">我的订单</button>
      <button class="avatar-btn" type="button" @click="showMe = true">{{ avatarLetter }}</button>
    </header>

    <HeroCarousel v-if="!joinMode"/>

    <main class="body">

      <!-- 合规公告：所有上传入口（普通下单 / 拼单加入）都先看到 -->
      <div class="notice" role="alert">
        <b>合规提醒</b>
        <span>请勿上传打印涉黄、涉政、涉密、侵权等不合规的打印内容，一经查实，将同步公安并做永久封号处理！</span>
      </div>

      <!-- 拼单加入页（扫码进入 /?group=短码）：先看进度，再上传自己的文件加入 -->
      <section class="card group-card" v-if="joinMode">
        <div class="ch">
          <h3>拼单打印 · 加入</h3>
          <span class="c">短码 {{ joinNo }}</span>
        </div>
        <div class="group-body" v-if="joinInfo">
          <div class="ghead">
            发起人 <b>{{ joinInfo.owner_name }}</b> 的拼单 · 已加入
            <b>{{ joinInfo.member_count }}</b> 人
          </div>
          <div class="gstatus">
            <span class="gtag">{{ discountText(joinInfo) }}</span>
            <span class="ging">{{ nextTierText(joinInfo) }}</span>
          </div>
          <div class="gmembers">
            <span v-for="m in joinInfo.members" :key="m.id" class="gm">
              {{ m.name }}<small v-if="m.mine"> 我</small>
            </span>
            <span v-if="!joinInfo.members.length" class="gm gm-empty">
              还没有人加入，你可以第一个
            </span>
          </div>
          <div class="gprice">
            当前合计 <b>¥{{ moneyOf(joinInfo.total_price) }}</b>
            <span class="gsmall">原价 ¥{{ moneyOf(joinInfo.raw_total) }}</span>
            <span v-if="joinInfo.saved > 0" class="gsaved">
              已省 ¥{{ moneyOf(joinInfo.saved) }}
            </span>
          </div>
          <div class="gladder">拼单规则：{{ ladderText(joinInfo) }}（按全单合计打折）</div>
          <div class="gwarn" v-if="joinInfo.status === 'collecting'">
            发起人已锁定人数，不能再加入；请支付自己的份额后到机旁取件。
          </div>
          <div class="gok" v-else-if="joinInfo.status === 'paid'">
            全部成员已付款，各自凭取件码到机旁取件。
          </div>
          <div class="gwarn" v-else-if="joinInfo.status !== 'open'">
            该拼单已结算，不能再加入。
          </div>
          <div class="gpay" v-if="joinInfo.status === 'collecting'">
            <template v-if="joinInfo.my_bill">
              <div class="gok" v-if="joinInfo.my_bill.paid">
                你已支付自己的份额<span v-if="joinInfo.my_bill.pickup_code">
                  ，取件码 {{ joinInfo.my_bill.pickup_code }}</span>
              </div>
              <div class="gwarn" v-else-if="joinInfo.my_bill.expired">
                你的账单已过期，请联系发起人重新锁定后再付。
              </div>
              <pay-button v-else :directBill="joinInfo.my_bill"
                          :text="'支付我的份额 ¥' + moneyOf(joinInfo.my_bill.total_price)"/>
            </template>
            <div class="gwarn" v-else>你还没有加入本拼单，无法付款。</div>
          </div>
        </div>
        <div class="empty" v-else>正在读取拼单信息…</div>
      </section>

      <section class="card" v-if="joinMode && joinInfo && joinInfo.status === 'open'">
        <div class="ch">
          <h3>上传文件并加入</h3>
          <span class="c">姓名会印在封皮上，便于分拣</span>
        </div>
        <div class="group-body">
          <div class="gok" v-if="myMembers.length">
            你已加入：{{ myMembers.map(m => m.name).join('、') }}
            <button class="mini" type="button" :disabled="leaving" @click="leaveGroup">
              {{ leaving ? '退出中…' : '退出拼单' }}
            </button>
          </div>
          <div class="brow">
            <span>你的姓名</span>
            <el-input v-model="joinName" maxlength="20" placeholder="例如：张三"/>
          </div>
          <div class="brow" v-if="convertedFiles.length">
            <span>选择要加入的文件</span>
            <el-select v-model="joinFile" placeholder="选择文件" style="width:100%">
              <el-option v-for="f in convertedFiles" :key="f.filename"
                         :label="f.filename" :value="f.filename"/>
            </el-select>
          </div>
          <div class="gwarn" v-else>
            还没有转换完成的文件：请先用下方的「文档打印 / 照片打印」上传，
            等状态变成可打印后再加入。
          </div>
          <button class="joinbtn" type="button" :disabled="joining" @click="joinGroup">
            {{ joining ? '加入中…' : '加入拼单' }}
          </button>
          <div class="gladder">
            加入后不会立刻扣款：由发起人统一付款、统一取件。
            你的文件会加一张姓名封皮，取件时按姓名分拣。
          </div>
        </div>
      </section>

      <!-- 首页：说明 / 设备状态 / 两个打印入口 -->
      <div class="desc">
        <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor"
             stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round">
          <path d="M4 9.5h3l5-3.5v12l-5-3.5H4z"/>
          <path d="M16.5 9a4 4 0 0 1 0 6"/>
          <path d="M19 6.5a7.5 7.5 0 0 1 0 11"/>
        </svg>
        文档打印，照片打印
      </div>

      <template v-if="!joinMode">
        <div class="status-row">
          <span class="dev">
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                 stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round">
              <path d="M7 8V4h10v4"/>
              <rect x="3.5" y="8" width="17" height="8" rx="2"/>
              <path d="M7 16h10v4H7z"/>
            </svg>
            {{ selectedPrinter || '默认设备' }}
          </span>
          <div class="status-right">
            <span class="ok" :class="{off: !printerOnline}"><i></i>{{ statusText }}</span>
            <button class="price-link" type="button" @click="showPrice = !showPrice">
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                   stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round">
                <path d="M20.5 13.5 13 21l-9-9 7.5-7.5H20a1 1 0 0 1 1 1z"/>
                <circle cx="16.5" cy="7.5" r="1.4"/>
              </svg>
              打印价格
            </button>
          </div>
        </div>

        <div class="price-pop" v-if="showPrice && priceRows.length">
          <div class="price-row" v-for="r in priceRows" :key="r.label">
            <span>{{ r.label }}</span><b>{{ r.value }}</b>
          </div>
          <div class="price-note">空白页不打印也不计费，彩色按每页覆盖率分档。</div>
        </div>
      </template>

      <!-- 两个上传入口：点击后直接选文件 -->
      <input type="file" multiple id="input-files" accept=".doc, .docx, .xlsx, .xls, .pdf"
             v-show="false" @change="handleFormFilesInput">
      <input type="file" multiple id="input-image" accept=".jpg,.jpeg,.png"
             v-show="false" @change="handleFormFilesInput">

      <section class="grid">
        <button class="svc" type="button" @click="chooseFile()">
          <div class="svc-text"><h3>文档打印</h3><p>PDF、Word、Excel</p></div>
          <span class="svc-ico">
            <svg viewBox="0 0 64 64" fill="none">
              <rect x="13" y="7" width="34" height="46" rx="5" fill="#fff" stroke="#DCE4F5"/>
              <path d="M38 7v9h9" fill="#EAF0FF"/>
              <rect x="20" y="16" width="20" height="3" rx="1.5" fill="#DCE6FF"/>
              <rect x="20" y="24" width="16" height="3" rx="1.5" fill="#EAF0FF"/>
              <rect x="20" y="32" width="20" height="3" rx="1.5" fill="#EAF0FF"/>
              <rect x="34" y="37" width="23" height="19" rx="4" fill="#F25C5C"/>
              <text x="45.5" y="50.5" text-anchor="middle" font-family="Arial,Helvetica,sans-serif"
                    font-size="9" font-weight="700" fill="#fff">PDF</text>
            </svg>
          </span>
        </button>

        <button class="svc" type="button" @click="chooseImage()">
          <div class="svc-text"><h3>照片打印</h3><p>JPG、PNG 打印到 A4</p></div>
          <span class="svc-ico">
            <svg viewBox="0 0 64 64" fill="none">
              <defs>
                <linearGradient id="pg1" x1="10" y1="14" x2="54" y2="50"
                                gradientUnits="userSpaceOnUse">
                  <stop stop-color="#FFE0A8"/><stop offset="1" stop-color="#FFB04D"/>
                </linearGradient>
              </defs>
              <g transform="rotate(-8 30 32)">
                <rect x="9" y="13" width="34" height="38" rx="4" fill="#FFF6E6" stroke="#F3DFC0"/>
              </g>
              <g transform="rotate(6 36 34)">
                <rect x="20" y="12" width="36" height="40" rx="5" fill="#fff" stroke="#F0DCB8"/>
                <rect x="25" y="17" width="26" height="22" rx="3" fill="url(#pg1)"/>
                <circle cx="31" cy="24" r="3" fill="#FFF8E8"/>
                <path d="M25 39l7-7 5 4 4-3 6 6v3H25z" fill="#F59E42" opacity=".75"/>
                <rect x="25" y="45" width="18" height="2.6" rx="1.3" fill="#F3E3C6"/>
              </g>
            </svg>
          </span>
        </button>
      </section>

      <div class="sub-links" v-if="!joinMode">
        <button class="linkbtn" type="button" @click="hrefTutorial()">使用教程</button>
        <button class="linkbtn" type="button" @click="showPrinterPicker = !showPrinterPicker">
          {{ showPrinterPicker ? '收起打印机' : '切换打印机' }}
        </button>
        <button class="linkbtn" type="button" @click="router.push('/library')">共享资料库</button>
        <span class="linknote" v-if="selectedPrinter">{{ printerName() || selectedPrinter }}<template v-if="printerStore()"> · {{ printerStore() }}</template></span>
      </div>

      <!-- 打印机列表：默认收起，只保留上面的状态条 -->
      <section class="card printer-card" v-if="!joinMode && (showPrinterPicker || !printers.length)">
        <div class="ch">
          <h3>选择打印机</h3>
          <span class="c">{{ printers.length }} 台可选<span v-if="tenantInfo"> · {{ tenantInfo.name }}</span></span>
          <button class="mini" type="button" @click="locateAndRecommend">
            {{ locating ? '定位中…' : '按距离推荐' }}
          </button>
          <button class="mini" type="button" v-if="!autoMode" @click="enableAutoSelect">
            恢复自动选择
          </button>
        </div>
        <div class="printers">
          <button v-for="p in printers" :key="p.device_id" type="button"
                  class="printer"
                  :class="{ sel: p.device_id === selectedPrinter, off: p.status !== 'online' }"
                  @click="selectPrinter(p)">
            <span class="pid">{{ p.device_id }}</span>
            <span class="pname">{{ p.name || '未命名设备' }}</span>
            <span class="pill" :class="p.status === 'online' ? 'on' : 'off'">
              {{ p.status === 'online' ? '在线' : '离线' }}
            </span>
            <span class="ups" :class="upsClass(p)">{{ upsText(p) }}</span>
            <span class="pmeta" v-if="printerMeta(p)">{{ printerMeta(p) }}</span>
            <span class="pqueue">{{ printerQueue(p) }}</span>
            <span class="pnav" v-if="navUrl(p)" @click.stop="openNav(p)">导航</span>
            <span class="rec" v-if="recommendedId && p.device_id === recommendedId">推荐</span>
            <span class="tick" v-if="p.device_id === selectedPrinter">已选</span>
          </button>
          <div class="empty" v-if="serviceMessage">{{ serviceMessage }}</div>
          <div class="empty" v-else-if="printerLoaded && !printers.length">
            暂无已注册的打印机 —— 不选择时按默认设备排队
          </div>
          <div class="empty" v-if="!printerLoaded">正在读取打印机列表…</div>
        </div>
        <div class="note" v-if="selectedPrinter">
          当前已选：{{ printerName() }}（{{ selectedPrinter }}）
          <span class="ups" :class="selectedUpsClass()">{{ selectedUpsText() }}</span>
          <span class="punit" v-if="priceText()">{{ priceText() }}</span>
        </div>
        <div class="recnote" v-if="recommendedId">
          智能推荐：{{ recommendationName() }}
          <span v-if="recommendationReasons()"> · {{ recommendationReasons() }}</span>
          <span v-if="recommendation && !recommendation.distance_used"> · 距离未启用（需 HTTPS + 定位授权）</span>
          <span v-if="recommendationPenalties()" class="recwarn"> · 注意：{{ recommendationPenalties() }}</span>
          <span v-if="!autoMode && recommendedId !== selectedPrinter">（你已手动选择）</span>
        </div>
      </section>

      <!-- 交付承诺（第三个创新）：不只承诺出纸，敢承诺结果 -->
      <section class="card promise-card">
        <div class="ch">
          <h3>交付承诺</h3>
          <span class="c">共享打印机只敢承诺出纸，我们敢承诺结果</span>
        </div>
        <div class="promise-body">
          <div class="pm"><b>超时免单</b>
            <span>下单即显示预计完成时间；超过承诺时间完成，自动免单。</span></div>
          <div class="pm"><b>格式不对免费重印</b>
            <span>格式 / 打印质量问题，在订单详情申请，审核通过免费重印。</span></div>
          <div class="pm"><b>完成后 5 分钟删文件</b>
            <span>打印完成 5 分钟后自动删除源文件，不用怕文件留在服务器。</span></div>
        </div>
      </section>

      <FileList :fileList="fileList" :getFilesAttributes="getFilesAttributes"
                @check="onCheck" @fixed="onFixed"/>

      <!-- 共享资料库：自愿勾选，95 折；审核通过后才公开 -->
      <section class="card share-card"
               v-if="!joinMode && (fileList.value || []).length && !batchOn">
        <div class="share-contrib" v-if="libraryStats.print_count > 0">
          你的共享资料已被打印 <b>{{ libraryStats.print_count }}</b> 次
          （{{ libraryStats.print_pages }} 页），本单自动享
          <b>{{ contributionPercent }}%</b> 贡献折扣。
        </div>
        <label class="share-toggle">
          <input type="checkbox" v-model="shareLibrary">
          <div class="share-text">
            <b>共享到公开资料库</b>
            <span v-if="contributionPercent > 0">
              再共享本单可享 {{ Math.max(5, contributionPercent) }}% 优惠（与贡献折扣取较高值）。
            </span>
            <span v-else>审核通过后其他同学可下载打印，本单立减 5%。</span>
          </div>
          <span class="share-tag">{{ shareTag }}</span>
        </label>
        <div class="share-extra" v-if="shareLibrary">
          <label class="share-cat">分类
            <select v-model="shareCategory">
              <option v-for="c in libraryCategories" :key="c" :value="c">{{ c }}</option>
            </select>
          </label>
          <div class="share-stat">
            <template v-if="libraryStats.next_need">
              资料再被打印
              {{ Math.max(libraryStats.next_need - libraryStats.print_count, 1) }}
              次，贡献折扣可升到 {{ Math.round(libraryStats.next_rate * 100) }}%。
            </template>
            <template v-else-if="libraryStats.print_count > 0">
              已达最高贡献折扣（{{ contributionPercent }}%）。
            </template>
            <template v-else>
              资料通过审核并被同学打印后，后续下单折扣会随打印量提升。
            </template>
          </div>
          <div class="share-warn">
            只共享本次打印范围的成品 PDF。需 agent 初审 + 管理员复审，未通过不会公开；
            请确认文件不含身份证号、手机号等个人信息。
          </div>
        </div>
      </section>

      <!-- 拼单打印（主打创新）：一人发起，链接/二维码发群里，人越多越便宜 -->
      <section class="card group-card" v-if="!joinMode">
        <div class="ch">
          <h3>拼单打印</h3>
          <span class="c">一人发起 · 扫码加入 · 人越多越便宜</span>
        </div>
        <div class="group-body">
          <template v-if="!groupInfo">
            <div class="brow">
              <span>发起人姓名</span>
              <el-input v-model="groupOwnerName" maxlength="20" placeholder="你的姓名"/>
            </div>
            <div class="brow">
              <span>付款方式</span>
              <el-radio-group v-model="groupPayMode" size="small">
                <el-radio-button label="aa">AA 各付各的</el-radio-button>
                <el-radio-button label="unified">发起人统一付</el-radio-button>
              </el-radio-group>
            </div>
            <div class="gladder">
              规则：3 人 9 折 · 5 人 8 折 · 10 人 7 折。
              发起后把链接/二维码发到宿舍群、课程群，大家各自上传自己的文件。
              <template v-if="groupPayMode === 'aa'">
                发起人锁定人数后，每人支付自己的份额、各自取件，每人一份姓名封皮。
              </template>
              <template v-else>
                发起人一次付清、统一取件，每人一份姓名封皮，按姓名分拣。
              </template>
            </div>
            <button class="joinbtn" type="button" :disabled="groupCreating"
                    @click="createGroup">
              {{ groupCreating ? '发起中…' : '发起拼单' }}
            </button>
          </template>

          <template v-else>
            <div class="gshare">
              <img v-if="groupQrData" :src="groupQrData" class="gqr" alt="拼单二维码"/>
              <div class="gshare-right">
                <div class="glabel">用微信扫一扫加入</div>
                <input class="glink" :value="groupLink" readonly
                       @focus="$event.target.select()"/>
                <div class="gbtns">
                  <button class="mini" type="button" @click="copyGroupLink">复制链接</button>
                  <button class="mini" type="button" @click="fetchMyGroup">刷新进度</button>
                </div>
                <div class="gladder">短码 {{ groupInfo.group_no }}</div>
              </div>
            </div>

            <div class="gstatus">
              <span class="gtag">{{ discountText(groupInfo) }}</span>
              <span class="ging">{{ nextTierText(groupInfo) }}</span>
            </div>

            <div class="gmembers">
              <div v-for="m in groupInfo.members" :key="m.id" class="gmrow">
                <span class="gmn">{{ m.name }}</span>
                <span class="gmp">
                  {{ m.pages }} 页 · {{ m.copies }} 份 ·
                  {{ m.color === 'color' ? '彩色' : '黑白' }} ·
                  {{ m.side === 'one-sided' ? '单面' : '双面' }}
                </span>
                <span class="gma">¥{{ moneyOf(m.payable != null ? m.payable : m.amount) }}</span>
                <span class="gpaid" v-if="groupInfo.pay_mode === 'aa'">
                  {{ m.paid ? '已付' : '待付' }}
                </span>
                <button class="mini" type="button" v-if="groupInfo.status === 'open'"
                        @click="removeMember(m.id)">移除</button>
              </div>
              <div v-if="!groupInfo.members.length" class="gwarn">
                还没有人加入：把上面的链接发到宿舍群 / 课程群，大家扫码上传自己的文件。
              </div>
            </div>

            <div class="gprice">
              当前 {{ groupInfo.member_count }} 人 · 合计
              <b>¥{{ moneyOf(groupInfo.total_price) }}</b>
              <span class="gsmall">原价 ¥{{ moneyOf(groupInfo.raw_total) }}</span>
              <span v-if="groupInfo.saved > 0" class="gsaved">
                已省 ¥{{ moneyOf(groupInfo.saved) }}
              </span>
            </div>

            <div v-if="groupInfo.status === 'open'">
              <button v-if="groupInfo.pay_mode === 'aa'" class="joinbtn" type="button"
                      :disabled="!groupInfo.member_count" @click="collectGroup">
                锁定人数，开始 AA 收款
              </button>
              <pay-button v-else-if="groupInfo.member_count"
                          :groupNo="groupInfo.group_no"
                          :text="'统一结算 ¥' + moneyOf(groupInfo.total_price)"/>
              <div class="gladder" v-if="groupInfo.pay_mode === 'aa'">
                锁定后不能再加人；系统按当前人数算好折扣，每人支付自己的份额、各自取件。
              </div>
            </div>
            <div v-else-if="groupInfo.status === 'collecting'" class="gsettled">
              <div class="gok">
                已锁定 {{ groupInfo.member_count }} 人，已付 {{ groupInfo.paid_count }} 人。
                每人支付自己的份额，付完即可各自取件。
              </div>
              <template v-if="groupInfo.my_bill">
                <div class="gok" v-if="groupInfo.my_bill.paid">
                  你已支付自己的份额<span v-if="groupInfo.my_bill.pickup_code">
                    ，取件码 {{ groupInfo.my_bill.pickup_code }}</span>
                </div>
                <div class="gwarn" v-else-if="groupInfo.my_bill.expired">
                  你的账单已过期，请取消锁定后重新收款。
                </div>
                <pay-button v-else :directBill="groupInfo.my_bill"
                            :text="'支付我的份额 ¥' + moneyOf(groupInfo.my_bill.total_price)"/>
              </template>
              <div class="gladder" v-else>你没有上传文件，无需付款；把页面发给成员让他们各自支付。</div>
              <button class="mini" type="button" v-if="!groupInfo.paid_count"
                      @click="reopenGroup">
                取消锁定，继续拉人
              </button>
            </div>
            <div v-else-if="groupInfo.status === 'paid'" class="gsettled">
              <div class="gok">
                全部 {{ groupInfo.member_count }} 人已付款，各自凭取件码到机旁取件。
              </div>
            </div>
            <div v-else class="gsettled">
              <div class="gok">
                已生成账单（订单尾号 {{ (groupInfo.order_no || '').slice(-6) }} ·
                取件码 {{ groupInfo.pickup_code }}）。
                未付款前可取消结算继续拉人。
              </div>
              <pay-button :groupNo="groupInfo.group_no"
                          :text="'打开收款码 ¥' + moneyOf(groupInfo.total_price)"/>
              <button class="mini" type="button" @click="reopenGroup">
                取消结算，继续拉人
              </button>
            </div>
          </template>
        </div>
      </section>

      <section class="card batch-card" v-if="!joinMode">
        <div class="ch">
          <h3>班级 / 社团批量</h3>
          <span class="c">一次上传，多份统一取件</span>
        </div>
        <div class="batch-body">
          <label class="batch-toggle">
            <input type="checkbox" v-model="batchOn"> 启用批量模式
          </label>
          <div v-if="batchOn" class="batch-form">
            <div class="brow">
              <span>份数</span>
              <el-input-number v-model="batchCopies" :min="1" :max="100"/>
            </div>
            <div class="brow">
              <span>名单（选填，仅备注）</span>
              <el-input type="textarea" v-model="batchNamesText" :rows="3"
                        placeholder="张三&#10;李四&#10;王五"/>
            </div>
            <div class="brow">
              <span>组织者</span>
              <el-input v-model="batchOwner" placeholder="班长 / 社团负责人"/>
            </div>
            <div class="batch-summary">
              统一取件：支付后一个取件码，一次取走 {{ batchCopies }} 份。
              打印 2 份及以上时，每份前面会加一张姓名封皮，按封皮分装即可。
              <template v-if="batchNames.length">
                名单 {{ batchNames.length }} 人{{ batchNames.length < batchCopies ? '，未填姓名的份会写「未填姓名」' : '' }}。
              </template>
            </div>
            <div class="batch-warn" v-if="batchNames.length > batchCopies">
              名单人数多于份数，只会给前 {{ batchCopies }} 个人印封皮。
            </div>
            <div class="batch-warn" v-if="(fileList.value || []).length > 1">
              批量模式目前只支持一个文件。
            </div>
          </div>
        </div>
      </section>

      <section class="card orders-card" id="orders-anchor" v-if="orders.length && !joinMode">
        <div class="ch">
          <h3>最近订单</h3>
          <span class="c">{{ orders.length }} 单 · 每 8 秒刷新</span>
        </div>
        <div class="orders">
          <button v-for="o in recentOrders" :key="o.id" type="button" class="order-row"
                  @click="openOrder(o)">
            <span class="ofn" :title="o.filename">{{ o.filename }}</span>
            <span class="ost" :class="statusInfo(o).cls">{{ statusInfo(o).text }}</span>
            <span class="oamt">¥{{ money(o.amount) }}</span>
            <span class="otime">{{ o.created_at }}</span>
          </button>
        </div>
        <div class="note">只显示当前浏览器会话的订单；换浏览器或清缓存后历史不可见。</div>
      </section>
    </main>

    <div class="odmask" v-if="orderDetail" @click.self="closeOrder">
      <div class="odbox">
        <div class="odh">
          <span>订单详情</span>
          <button class="odx" type="button" @click="closeOrder">×</button>
        </div>
        <div class="odsteps">
          <div v-for="(s, i) in ['已支付', '打印中', '待取件']" :key="s"
               class="odstep"
               :class="{on: statusInfo(orderDetail).step >= i, fail: statusInfo(orderDetail).step < 0}">
            <i></i><span>{{ s }}</span>
          </div>
        </div>
        <div class="odprogress">
          <template v-if="orderDetail.status === 'scheduled'">
            已预约 · {{ orderDetail.scheduled_at }} 到点后自动排队打印 · 共 {{ orderDetail.pages }} 页
          </template>
          <template v-else-if="orderDetail.status === 'pending'">
            已支付，正在排队 · 共 {{ orderDetail.pages }} 页
          </template>
          <template v-else-if="orderDetail.status === 'printing'">
            打印中 · 共 {{ orderDetail.pages }} 页
          </template>
          <template v-else-if="orderDetail.status === 'completed'">
            打印完成 · {{ orderDetail.pages }}/{{ orderDetail.pages }} 页，请到出纸口取纸
          </template>
          <template v-else-if="orderDetail.status === 'failed'">
            打印失败，请联系现场工作人员
          </template>
          <template v-else-if="orderDetail.status === 'canceled'">
            预约已取消，不会再打印
          </template>
          <template v-else-if="orderDetail.status === 'expired'">
            预约已过期（到点后长时间未打印），如需打印请重新下单
          </template>
        </div>
        <section class="odevidence">
          <div class="odevidence-head">出纸照片</div>
          <div v-if="ejectEvidenceLoading" class="odevidence-state">正在加载出纸记录…</div>
          <template v-else-if="ejectEvidence.some(event => event.has_photo)">
            <figure v-for="event in ejectEvidence.filter(item => item.has_photo)" :key="event.event_id" class="odevidence-item">
              <img v-if="photoUrls[event.event_id]" :src="photoUrls[event.event_id]" alt="出纸现场抓拍">
              <figcaption>{{ event.source === 'vision' ? '视觉检测抓拍' : '现场记录' }} · {{ event.created_at }}</figcaption>
            </figure>
          </template>
          <div v-else class="odevidence-state">暂无出纸照片证据</div>
        </section>
        <div class="odrows">
          <div><span>订单号</span><b>{{ orderNo(orderDetail) }}</b></div>
          <div><span>文件</span><b>{{ orderDetail.filename }}</b></div>
          <div><span>规格</span>
            <b>{{ orderDetail.paper || 'A4' }} · {{ colorText(orderDetail.print_color) }} ·
              {{ sideText(orderDetail.print_side) }} ·
              {{ orderDetail.print_copies }} 份 · {{ orderDetail.pages }} 页</b></div>
          <div><span>金额</span><b>¥{{ money(orderDetail.amount) }}</b></div>
          <div v-if="Number(orderDetail.saved_amount) > 0"><span>本单已省</span>
            <b class="saved">¥{{ money(orderDetail.saved_amount) }}</b></div>
          <div v-if="orderDetail.pickup_code"><span>取件码</span>
            <b>{{ orderDetail.pickup_code }}</b></div>
          <div v-if="orderDetail.batch_mode"><span>批量</span>
            <b>{{ orderDetail.print_copies }} 份 · 统一取件<template
              v-if="orderDetail.batch_owner"> · {{ orderDetail.batch_owner }}</template><template
              v-if="orderDetail.batch_bundle"> · 含姓名封皮</template></b></div>
          <div v-if="(orderDetail.batch_names || []).length"><span>名单（印在封皮）</span>
            <b>{{ batchNamesTextOf(orderDetail) }}</b></div>
          <div><span>打印机</span><b>{{ orderDetail.printer_id || '未指定' }}</b></div>
          <div v-if="showProgress(orderDetail)"><span>打印进度</span>
            <b>{{ progressText(orderDetail) }}</b></div>
          <div><span>下单时间</span><b>{{ orderDetail.created_at }}</b></div>
          <div v-if="orderDetail.scheduled_at"><span>预约出纸</span>
            <b>{{ orderDetail.scheduled_at }}{{ orderDetail.status === 'scheduled' ? '（未到点）' : '' }}</b></div>
          <div v-if="orderDetail.eta_deadline"><span>承诺完成</span>
            <b>{{ etaDeadlineText(orderDetail) }} 前 · 超时免单</b></div>
          <div v-if="orderDetail.waived"><span>交付承诺</span>
            <b class="waived">已超时免单</b></div>
          <div v-else-if="orderDetail.overtime"><span>交付承诺</span>
            <b class="waived">已超时，按承诺免单</b></div>
          <div v-if="orderDetail.reprint_of"><span>重印</span>
            <b>本单为 #{{ orderDetail.reprint_of }} 的免费重印件</b></div>
          <div v-if="orderDetail.payment_status"><span>收款</span>
            <b>{{ payStatusText(orderDetail.payment_status) }}</b></div>
        </div>
        <div class="odreprint" v-if="orderDetail.status === 'completed'">
          <template v-if="orderDetail.reprint_status === 'approved'">
            <div class="odr-ok">免费重印已通过，已重新排队打印，请稍候取件。</div>
          </template>
          <template v-else-if="orderDetail.reprint_status === 'requested'">
            <div class="odr-ok">重印申请处理中，管理员审核通过后免费重印。</div>
          </template>
          <template v-else>
            <div class="odr-row">
              <input v-model="reprintReason" maxlength="60"
                     placeholder="格式不对？说明问题（选填）"/>
              <button class="odr-btn" type="button" :disabled="reprinting" @click="applyReprint">
                {{ reprinting ? '提交中…' : '申请免费重印' }}
              </button>
            </div>
            <div class="odr-note">格式不对 / 打印质量有问题？申请后管理员审核，通过即免费重印（不收费）。</div>
          </template>
        </div>
        <div class="odcancel" v-if="orderDetail.status === 'scheduled'">
          <button class="odc-btn" type="button" :disabled="canceling" @click="cancelScheduled">
            {{ canceling ? '取消中…' : '取消预约' }}
          </button>
          <div class="odc-note">
            还没到预约时间，可以取消。若已付款，收款状态会记为「待退款」，
            由现场工作人员人工退回（个人收款码没有自动退款通道）。
          </div>
        </div>
        <div class="odnote">
          进度由打印机端真实上报：已支付 → 打印中 → 待取件。
          取件码只在订单里核对，不印在纸上；文件在打印完成 5 分钟后自动删除。
          系统没有「已取件」传感器，取走纸张不会自动记录。
        </div>
      </div>
    </div>

    <footer class="paybar" v-if="isShowPayArea && !joinMode">
      <div class="price">
        <PriceCounter :fileList="fileList.value" :price="priceCfg" :coverage="coverageMap"
                      :discountRate="effectiveDiscountRate"
                      @total="totalPrice = $event"></PriceCounter>
      </div>
      <div class="pay-right">
        <div class="sched" v-if="!batchOn">
          <label class="sched-sw">
            <input type="checkbox" v-model="scheduleOn">
            <span>定时出纸</span>
          </label>
          <input v-if="scheduleOn" type="datetime-local" class="sched-time"
                 v-model="scheduleAt" :min="scheduleMin">
        </div>
        <div class="pay-printer">{{ selectedPrinter || '未选打印机' }}</div>
        <pay-button :fileList="fileList.value" :printerId="selectedPrinter"
                    :batch="batchPayload" :scheduledAt="schedulePayload"
                    :shareLibrary="shareLibrary"></pay-button>
      </div>
    </footer>

    <!-- 底部导航：只有不显示支付条时出现，避免两条固定栏叠在一起 -->
    <nav class="tabbar" v-if="!isShowPayArea || joinMode">
      <button class="tab active" type="button" @click="scrollTop">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"
             stroke-linecap="round" stroke-linejoin="round">
          <path d="M3.5 10.8 12 3.8l8.5 7"/>
          <path d="M5.5 9.7V20h13V9.7"/>
          <path d="M9.7 20v-5.6h4.6V20"/>
        </svg>
        首页
      </button>
      <button class="tab" type="button" @click="scrollToOrders">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"
             stroke-linecap="round" stroke-linejoin="round">
          <path d="M6 3h12a1 1 0 0 1 1 1v17l-3-2-3 2-3-2-3 2V4a1 1 0 0 1 1-1z"/>
          <path d="M9 8h6M9 12h6"/>
        </svg>
        订单
      </button>
      <button class="tab" type="button" @click="showMe = true">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"
             stroke-linecap="round" stroke-linejoin="round">
          <circle cx="12" cy="8" r="3.5"/>
          <path d="M4.8 20c.8-3.5 3.8-5.4 7.2-5.4s6.4 1.9 7.2 5.4"/>
        </svg>
        我的
      </button>
    </nav>

    <!-- 我的账号 -->
    <div class="sheet-mask" v-if="showMe" @click.self="showMe = false">
      <div class="sheet">
        <div class="sheet-h">
          <span>我的账号</span>
          <button class="sheet-x" type="button" @click="showMe = false">×</button>
        </div>
        <div class="sheet-row"><span>昵称</span><b>{{ meName }}</b></div>
        <div class="sheet-row"><span>手机号</span><b>{{ mePhone }}</b></div>
        <button class="sheet-logout" type="button" @click="logout">退出登录</button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.pp{max-width:720px;margin:0 auto;padding-bottom:150px}

/* 顶栏：白底 + 底部细线，蓝色只留在 logo 方块上 */
.topbar{position:sticky;top:0;z-index:20;display:flex;align-items:center;gap:12px;
  padding:14px 18px;background:var(--white);border-bottom:1px solid var(--line);color:var(--ink)}
.topbar .mark{width:34px;height:34px;border-radius:9px;display:grid;place-items:center;flex:0 0 34px;
  background:var(--blue);color:#fff;font-weight:700;font-size:16px;
  box-shadow:0 2px 8px rgba(31,111,235,.28)}
.topbar .bt{font-size:16px;font-weight:700;line-height:1.2;letter-spacing:.02em;color:var(--ink)}
.topbar .bs{font-size:11px;color:var(--ink-3);letter-spacing:.06em;text-transform:uppercase}
.topbar .sp{flex:1}
.userchip{display:flex;align-items:center;gap:8px;font-size:12.5px;color:var(--ink-2)}
.userchip .uname{max-width:120px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;
  font-weight:600;color:var(--ink)}
.userchip .mini{padding:4px 10px;border:1px solid var(--line);border-radius:7px;background:#fff;
  color:var(--ink-2);font:inherit;font-size:12px;cursor:pointer}
.userchip .mini:hover{border-color:var(--blue);color:var(--blue)}
.body{padding:16px}
.body > * + *{margin-top:16px}

/* 合规公告：浅红底 + 红色左边线，白底页面里保持高可读性 */
.notice{display:flex;gap:10px;align-items:flex-start;padding:12px 14px;
  background:#fff5f5;border:1px solid #ffd7d7;border-left:4px solid #e5484d;
  border-radius:10px;color:#a12b2f;font-size:13.5px;line-height:1.65}
.notice b{flex:0 0 auto;font-weight:700;color:#c62828;white-space:nowrap}

/* 卡片 */
.card{background:var(--white);border:1px solid var(--line);border-radius:var(--r);box-shadow:var(--sh);
  overflow:hidden}
.card .ch{display:flex;align-items:center;gap:10px;padding:14px 16px;border-bottom:1px solid var(--line)}
.card .ch h3{margin:0;font-size:15.5px;font-weight:700}
.card .ch .c{margin-left:auto;font-size:12.5px;color:var(--ink-2)}

/* 打印机列表 */
.printers{padding:8px 12px}
.printer{width:100%;display:flex;align-items:center;gap:10px;flex-wrap:wrap;
  padding:13px 14px;margin:6px 0;border-radius:var(--r);cursor:pointer;text-align:left;
  border:1px solid var(--line);background:var(--white);color:var(--ink);transition:.15s;font-size:15px}
.printer:hover{border-color:var(--line-strong);background:var(--line-2)}
.printer.sel{border-color:var(--blue);background:var(--blue-50);box-shadow:0 0 0 3px rgba(31,111,235,.12)}
.printer .pid{font-family:var(--num);font-weight:700;font-size:15px}
.printer .pname{color:var(--ink-2);font-size:14px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;
  flex:1;min-width:0}
.printer.off .pid,.printer.off .pname{color:var(--ink-3)}
.printer .tick{margin-left:auto;font-size:12px;font-weight:700;color:var(--blue);
  background:var(--white);border:1px solid var(--blue-100);border-radius:20px;padding:2px 9px}
.pill{font-size:12px;font-weight:600;padding:3px 10px;border-radius:20px;white-space:nowrap}
.pill.on{background:var(--ok-bg);color:var(--ok)}
.pill.off{background:#EEF1F6;color:var(--ink-3)}
/* UPS 供电状态：市电正常=绿、市电断（UPS 供电中）=橙、设备未上报=灰（不编造） */
.ups{font-size:11.5px;font-weight:600;padding:3px 9px;border-radius:20px;white-space:nowrap}
.ups-ok{background:var(--ok-bg);color:var(--ok)}
.ups-warn{background:var(--warn-bg);color:var(--warn)}
.ups-none{background:#F1F4F9;color:var(--ink-3)}
/* 点位信息：地址/营业时间/排队/导航 */
.printer .pmeta{flex-basis:100%;font-size:12px;color:var(--ink-3);line-height:1.5}
.printer .pqueue{font-size:11.5px;color:var(--ink-2);background:var(--line-2);
  border-radius:20px;padding:3px 9px;white-space:nowrap}
.printer .pnav{font-size:11.5px;font-weight:700;color:var(--blue);cursor:pointer;
  border:1px solid var(--blue-100);border-radius:20px;padding:3px 10px;white-space:nowrap}
.printer .pnav:hover{background:var(--blue-50)}
.note .ups{margin-left:8px}
/* 所选机器的生效单价（每台机器可单独定价） */
.note .punit{margin-left:8px;color:var(--ink-3)}
/* 智能选机：推荐徽标、顶部小按钮与推荐理由 */
.card .ch .mini{margin-left:6px;padding:4px 10px;font-size:12px;border:1px solid var(--line-strong);
  background:var(--white);color:var(--ink-2);border-radius:999px;cursor:pointer;font-family:inherit}
.card .ch .mini:hover{border-color:var(--blue);color:var(--blue)}
.printer .rec{font-size:11px;font-weight:700;color:var(--blue);background:var(--blue-50);
  border:1px solid var(--blue-100);border-radius:20px;padding:2px 8px;white-space:nowrap}
.recnote{padding:0 16px 14px;font-size:12.5px;color:var(--ink-2);line-height:1.7}
.recnote .recwarn{color:var(--warn)}
/* 班级/社团批量 */
.batch-body{padding:12px 16px 16px}
.batch-toggle{display:flex;align-items:center;gap:8px;font-size:14px;color:var(--ink)}
.batch-form{margin-top:12px;display:flex;flex-direction:column;gap:10px}
.brow{display:flex;flex-direction:column;gap:6px;font-size:13px;color:var(--ink-2)}
.batch-summary{font-size:12.5px;color:var(--ok);line-height:1.7}
.batch-warn{font-size:12.5px;color:var(--warn);line-height:1.7}
.empty{padding:20px 14px;text-align:center;color:var(--ink-3);font-size:13.5px}
.note{padding:10px 16px 14px;font-size:12.5px;color:var(--ink-2);font-family:var(--num)}

/* ---------------- 拼单打印（主打创新） ---------------- */
.group-body{padding:12px 16px 16px;display:flex;flex-direction:column;gap:12px}
.ghead{font-size:14px;color:var(--ink-2)}
.ghead b{color:var(--ink);font-weight:700}
.gstatus{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.gtag{font-size:12.5px;font-weight:700;color:var(--blue-700);background:var(--blue-50);
  border:1px solid var(--blue-100);border-radius:999px;padding:4px 12px}
.ging{font-size:12.5px;color:var(--warn);background:var(--warn-bg);border-radius:999px;padding:4px 12px}
.gmembers{display:flex;flex-wrap:wrap;gap:8px}
.gm{font-size:13px;font-weight:600;background:var(--blue-50);color:var(--blue-700);
  border:1px solid var(--blue-100);border-radius:999px;padding:5px 12px}
.gm small{color:var(--ink-3);font-weight:400}
.gm-empty{background:var(--line-2);color:var(--ink-3);border-color:var(--line);font-weight:400}
.gmrow{display:flex;align-items:center;gap:10px;width:100%;padding:9px 12px;border:1px solid var(--line);
  border-radius:var(--r-sm);background:var(--line-2)}
.gmn{font-weight:700;font-size:13.5px;flex:0 0 auto}
.gmp{font-size:12px;color:var(--ink-2);flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.gma{font-family:var(--num);font-weight:700;font-size:13.5px}
.gpaid{font-size:11.5px;font-weight:700;padding:2px 8px;border-radius:999px;
  background:var(--line-2);color:var(--ink-2);white-space:nowrap}
.gpay{display:flex;flex-direction:column;gap:8px;margin-top:8px}
.gprice{font-size:14px;color:var(--ink-2)}
.gprice b{font-size:19px;color:var(--ink);font-family:var(--num)}
.gsmall{margin-left:8px;font-size:12.5px;color:var(--ink-3);text-decoration:line-through}
.gsaved{margin-left:8px;font-size:12.5px;font-weight:700;color:var(--ok)}
.gladder{font-size:12.5px;color:var(--ink-2);line-height:1.7}
.gwarn{font-size:12.5px;color:var(--warn);line-height:1.7;background:var(--warn-bg);
  border-radius:var(--r-sm);padding:9px 12px}
.gok{font-size:12.5px;color:var(--ok);line-height:1.7;background:var(--ok-bg);
  border-radius:var(--r-sm);padding:9px 12px;display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.gsettled{display:flex;flex-direction:column;gap:10px}
.gshare{display:flex;gap:14px;align-items:flex-start;flex-wrap:wrap}
.gqr{width:132px;height:132px;border:1px solid var(--line);border-radius:var(--r-sm);background:#fff;padding:6px}
.gshare-right{flex:1;min-width:180px;display:flex;flex-direction:column;gap:8px}
.glabel{font-size:12.5px;color:var(--ink-2)}
.glink{width:100%;padding:9px 11px;border:1px solid var(--line);border-radius:var(--r-sm);
  background:var(--white);color:var(--ink-2);font-family:var(--num);font-size:12px}
.gbtns{display:flex;gap:8px;flex-wrap:wrap}
.joinbtn{margin-top:2px;width:100%;padding:12px;border:0;border-radius:var(--r);cursor:pointer;font-family:inherit;
  background:var(--blue);color:#fff;font-size:15.5px;font-weight:700;box-shadow:var(--sh)}
.joinbtn:hover{background:var(--blue-600)}
.joinbtn:disabled{opacity:.6;cursor:default}
.mini{padding:5px 12px;font-size:12px;border:1px solid var(--line-strong);background:var(--white);
  color:var(--ink-2);border-radius:999px;cursor:pointer;font-family:inherit}
.mini:hover{border-color:var(--blue);color:var(--blue)}

/* 上传按钮 */
.uploader{display:flex;flex-direction:column;gap:12px}
.ub-row{display:flex;gap:12px}
.ub{display:flex;align-items:center;justify-content:center;gap:10px;
  padding:22px 18px;border:0;border-radius:var(--r);cursor:pointer;font-size:19px;font-weight:600;
  font-family:inherit;transition:.15s;box-shadow:var(--sh)}
.ub .ubi{width:26px;height:26px}
.ub-doc{width:100%;background:var(--blue);color:#fff}
.ub-doc:hover{background:var(--blue-600)}
.ub-img,.ub-tut{flex:1}
.ub-img{background:var(--white);color:var(--blue);border:1px solid var(--blue-100)}
.ub-img:hover{background:var(--blue-50);border-color:var(--blue)}
.ub-tut{background:var(--white);color:var(--ink);border:1px solid var(--line)}
.ub-tut:hover{background:var(--line-2)}

/* 底部支付条 */
.paybar{position:fixed;left:0;right:0;bottom:0;z-index:30;display:flex;align-items:center;gap:14px;
  max-width:720px;margin:0 auto;padding:12px 16px;
  background:var(--white);border-top:1px solid var(--line);box-shadow:0 -4px 16px rgba(15,23,42,.06)}
.price{flex:1;min-width:0;height:64px;border-radius:var(--r);overflow:hidden}
.pay-right{display:flex;flex-direction:column;align-items:center;gap:6px}
/* 定时出纸：勾选后才出现时间选择器，避免下单界面默认变复杂 */
.sched{display:flex;align-items:center;gap:8px;flex-wrap:wrap;justify-content:center}
.sched-sw{display:flex;align-items:center;gap:5px;font-size:12px;color:var(--ink-2);cursor:pointer}
.sched-sw input{width:14px;height:14px;accent-color:var(--blue);cursor:pointer}
.sched-time{height:30px;padding:0 6px;border:1px solid var(--line);border-radius:8px;
  font:inherit;font-size:12px;color:var(--ink);background:#fff;outline:none}
.sched-time:focus{border-color:var(--blue)}
.pay-printer{font-size:12px;color:var(--ink-3);font-family:var(--num);max-width:190px;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}

/* ---------------- 最近订单 ---------------- */
.orders-card .ch .c{font-size:12px;color:var(--ink-3)}
.orders{display:flex;flex-direction:column}
.order-row{display:flex;align-items:center;gap:10px;width:100%;padding:12px 16px;border:0;
  border-bottom:1px solid var(--line);background:transparent;font:inherit;text-align:left;cursor:pointer}
.order-row:last-child{border-bottom:0}
.order-row:hover{background:var(--line-2)}
.ofn{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;
  font-weight:600;font-size:14px}
.ost{font-size:11.5px;font-weight:700;padding:3px 9px;border-radius:20px;white-space:nowrap}
.st-paid{background:#EEF1F6;color:var(--ink-2)}
.st-printing{background:var(--blue-50);color:var(--blue-700)}
.st-done{background:var(--ok-bg);color:var(--ok)}
.st-failed{background:var(--crit-bg);color:var(--crit)}
.st-sched{background:#FFF4E5;color:#B26A00}
.st-mute{background:#EEF1F6;color:var(--ink-3)}
.oamt{font-family:var(--num);font-weight:700;font-size:13.5px}
.otime{font-size:11.5px;color:var(--ink-3);font-family:var(--num)}

/* ---------------- 订单详情 ---------------- */
.odmask{position:fixed;inset:0;z-index:70;background:rgba(15,23,42,.45);
  display:flex;align-items:center;justify-content:center;padding:16px}
.odbox{width:100%;max-width:420px;max-height:92vh;overflow:auto;background:var(--bg);
  border:1px solid var(--line);border-radius:var(--r);box-shadow:0 12px 32px rgba(15,23,42,.18)}
.odh{display:flex;align-items:center;justify-content:space-between;padding:14px 16px;
  border-bottom:1px solid var(--line);font-weight:700;font-size:15px}
.odx{border:0;background:none;font-size:22px;line-height:1;color:var(--ink-3);cursor:pointer}
.odsteps{display:flex;gap:8px;padding:16px}
.odstep{flex:1;text-align:center;font-size:11.5px;color:var(--ink-3)}
.odstep i{display:block;height:4px;border-radius:4px;background:#E6EAF0;margin-bottom:6px}
.odstep.on i{background:var(--blue)}
.odstep.on span{color:var(--blue-700);font-weight:700}
.odstep.fail i{background:var(--crit)}
.odprogress{padding:0 16px 14px;font-size:13px;color:var(--ink-2)}
.odevidence{padding:12px 16px;border-top:1px solid var(--line)}
.odevidence-head{font-size:13px;font-weight:700;color:var(--ink);margin-bottom:8px}
.odevidence-state{font-size:12px;color:var(--ink-3);padding:8px 0}
.odevidence-item{margin:0 0 10px}
.odevidence-item:last-child{margin-bottom:0}
.odevidence-item img{display:block;width:100%;max-height:260px;object-fit:contain;background:#f3f5f7;border:1px solid var(--line);border-radius:6px}
.odevidence-item figcaption{font-size:11px;color:var(--ink-3);margin-top:5px}
.odrows{padding:0 16px 12px}
.odrows > div{display:flex;gap:12px;padding:8px 0;border-bottom:1px solid var(--line);font-size:13px}
.odrows > div:last-child{border-bottom:0}
.odrows span{flex:0 0 68px;color:var(--ink-3);font-size:12.5px}
.odrows b{font-weight:600;color:var(--ink);word-break:break-all}
.odrows b.saved{color:var(--ok)}
.odnote{padding:12px 16px 16px;font-size:11.5px;line-height:1.6;color:var(--ink-3);
  border-top:1px solid var(--line)}

/* 交付承诺：卡片 + 详情里的免单/重印 */
.promise-body{padding:6px 16px 14px}
.pm{display:flex;gap:10px;padding:8px 0;font-size:13px;border-bottom:1px solid var(--line)}
.pm:last-child{border-bottom:0}
.pm b{flex:0 0 auto;color:var(--blue-700);font-weight:700}
.pm span{color:var(--ink-2);line-height:1.5}
.odrows b.waived{color:var(--ok)}
.odreprint{padding:12px 16px;border-top:1px solid var(--line)}
.odr-row{display:flex;gap:8px}
.odr-row input{flex:1;height:38px;padding:0 10px;border:1px solid var(--line);border-radius:10px;
  font-size:13px;font-family:inherit;color:var(--ink);background:var(--bg);outline:none}
.odr-row input:focus{border-color:var(--blue)}
.odr-btn{flex:0 0 auto;height:38px;padding:0 14px;border:0;border-radius:10px;background:var(--blue);
  color:#fff;font-size:13px;font-weight:700;font-family:inherit;cursor:pointer}
.odr-btn:hover{background:var(--blue-600)}
.odr-btn:disabled{opacity:.6;cursor:not-allowed}
.odr-note{font-size:11.5px;color:var(--ink-3);margin-top:8px;line-height:1.5}
.odr-ok{font-size:13px;font-weight:600;color:var(--ok)}
/* 定时打印：取消预约（仅未到点的预约单显示） */
.odcancel{padding:12px 16px;border-top:1px solid var(--line)}
.odc-btn{width:100%;height:40px;border:1px solid var(--line);border-radius:10px;background:#fff;
  color:var(--ink-2);font:inherit;font-size:14px;font-weight:600;cursor:pointer}
.odc-btn:hover{border-color:var(--crit);color:var(--crit)}
.odc-btn:disabled{opacity:.6;cursor:not-allowed}
.odc-note{font-size:11.5px;color:var(--ink-3);margin-top:8px;line-height:1.5}

/* ---------------- 首页新 UI：顶栏 / 状态条 / 打印入口 / 底部导航 ---------------- */
.topbar .pagetitle{
  position:absolute;left:50%;transform:translateX(-50%);
  font-size:16px;font-weight:600;letter-spacing:.4px;color:var(--ink);
}
.topbar .toplink{
  border:0;background:none;padding:6px 2px;font:inherit;font-size:12.5px;color:var(--ink-2);
  cursor:pointer;white-space:nowrap;
}
.topbar .toplink:hover{color:var(--blue)}
.topbar .avatar-btn{
  width:30px;height:30px;border-radius:50%;border:0;cursor:pointer;
  display:grid;place-items:center;flex:0 0 30px;
  background:linear-gradient(140deg,#DDE5FF,#C3D0FF);color:var(--blue-700);
  font:inherit;font-size:11.5px;font-weight:600;
}

.desc{
  display:flex;align-items:center;gap:8px;
  padding:6px 2px 2px;font-size:13px;color:#4E5874;
}
.desc svg{color:#6B7694;flex:none}

.status-row{
  display:flex;align-items:center;justify-content:space-between;gap:10px;
  padding:10px 12px;border-radius:10px;background:#F2F6FF;border:1px solid #E7EEFF;
}
.status-row .dev{
  display:inline-flex;align-items:center;gap:7px;font-size:12.5px;color:#3C4A78;
  font-weight:500;font-family:var(--num);min-width:0;overflow:hidden;
  text-overflow:ellipsis;white-space:nowrap;
}
.status-row .dev svg{color:var(--blue);flex:none}
.status-right{display:flex;align-items:center;gap:14px;flex:none}
.status-right .ok{
  display:inline-flex;align-items:center;gap:6px;font-size:12px;color:#4E5874;white-space:nowrap;
}
.status-right .ok i{
  width:6px;height:6px;border-radius:50%;background:var(--ok);
  box-shadow:0 0 0 3px rgba(22,179,100,.13);
}
.status-right .ok.off{color:var(--ink-3)}
.status-right .ok.off i{background:var(--ink-3);box-shadow:0 0 0 3px rgba(139,148,172,.14)}
.price-link{
  display:inline-flex;align-items:center;gap:5px;border:0;background:none;padding:0;
  font:inherit;font-size:12px;color:#7A5CE0;cursor:pointer;white-space:nowrap;
}
.price-link svg{color:#8B5CF6}

.price-pop{
  background:#fff;border:1px solid var(--line);border-radius:12px;
  padding:10px 14px;box-shadow:var(--sh);
}
.price-row{display:flex;align-items:center;justify-content:space-between;padding:6px 0;
  font-size:12.5px;color:var(--ink-2);border-bottom:1px solid #F4F6FB}
.price-row:last-of-type{border-bottom:0}
.price-row b{color:var(--ink);font-weight:600;font-family:var(--num)}
.price-note{font-size:11px;color:var(--ink-3);line-height:1.6;padding-top:8px}

.grid{display:grid;grid-template-columns:1fr 1fr;gap:13px}
.svc{
  display:flex;align-items:center;justify-content:space-between;gap:14px;
  min-height:124px;padding:22px 24px;border-radius:16px;cursor:pointer;text-align:left;
  background:#fff;border:1px solid #F5F7FC;box-shadow:0 3px 14px rgba(30,45,100,.045);
  font:inherit;color:var(--ink);transition:transform .18s,box-shadow .18s,border-color .18s;
}
.svc:hover{transform:translateY(-2px);box-shadow:0 12px 26px rgba(30,45,100,.09);
  border-color:#E3EAFB}
.svc-text{min-width:0}
.svc h3{margin:0;font-size:18px;font-weight:600;letter-spacing:-.2px}
.svc p{margin:8px 0 0;font-size:12.5px;color:var(--ink-3);line-height:1.55}
.svc-ico{width:64px;height:64px;flex:none}
.svc-ico svg{width:100%;height:100%;display:block}

.sub-links{display:flex;align-items:center;gap:16px;flex-wrap:wrap;padding:0 2px}
.linkbtn{border:0;background:none;padding:0;font:inherit;font-size:12.5px;
  color:var(--blue);cursor:pointer}
.linkbtn:hover{color:var(--blue-600)}
.linknote{font-size:11.5px;color:var(--ink-3);font-family:var(--num)}

.printer-card{box-shadow:none}

/* 共享资料库：自愿勾选 + 95 折 */
.share-card{padding:0}
.share-contrib{padding:14px 16px 0;font-size:12px;color:var(--ink-2);line-height:1.65}
.share-contrib b{color:var(--blue-700)}
.share-stat{font-size:11.5px;color:var(--ink-3);line-height:1.65}
.share-toggle{display:flex;align-items:center;gap:12px;padding:16px;cursor:pointer}
.share-toggle input{width:18px;height:18px;accent-color:var(--blue);flex:none;cursor:pointer}
.share-text{flex:1;min-width:0}
.share-text b{display:block;font-size:14px;font-weight:600}
.share-text span{display:block;font-size:12px;color:var(--ink-3);margin-top:4px;line-height:1.55}
.share-tag{font-size:11.5px;font-weight:700;color:#12734A;background:#E7F7EE;
  border:1px solid #CBEBD8;border-radius:999px;padding:4px 10px;white-space:nowrap;flex:none}
.share-extra{padding:0 16px 14px;display:flex;flex-direction:column;gap:10px}
.share-cat{display:flex;align-items:center;gap:8px;font-size:12.5px;color:var(--ink-2)}
.share-cat select{height:32px;padding:0 9px;border:1px solid var(--line);border-radius:8px;
  background:#fff;color:var(--ink);font:inherit;font-size:12.5px;outline:none;cursor:pointer}
.share-cat select:focus{border-color:var(--blue)}
.share-warn{font-size:11px;color:var(--warn);background:var(--warn-bg);border-radius:8px;
  padding:9px 11px;line-height:1.65}

.tabbar{
  position:fixed;left:0;right:0;bottom:0;z-index:35;height:62px;
  display:flex;justify-content:center;
  background:rgba(255,255,255,.96);backdrop-filter:blur(12px);
  border-top:1px solid #EEF1F8;
}
.tabbar .tab{
  flex:0 0 33.33%;max-width:240px;height:100%;
  display:flex;flex-direction:column;align-items:center;justify-content:center;gap:3px;
  border:0;background:none;font:inherit;font-size:10.5px;color:#8B94AC;cursor:pointer;
  transition:.16s;
}
.tabbar .tab svg{width:22px;height:22px}
.tabbar .tab.active{color:var(--blue);font-weight:600}
.tabbar .tab:hover{color:var(--blue)}

.sheet-mask{
  position:fixed;inset:0;z-index:90;background:rgba(20,27,52,.38);
  display:flex;align-items:flex-end;justify-content:center;
}
.sheet{
  width:100%;max-width:720px;background:#fff;border-radius:18px 18px 0 0;
  padding:18px 20px calc(22px + env(safe-area-inset-bottom));
  box-shadow:0 -12px 32px rgba(20,27,52,.16);
}
.sheet-h{display:flex;align-items:center;justify-content:space-between;
  font-size:15px;font-weight:600;margin-bottom:12px}
.sheet-x{border:0;background:none;font-size:22px;line-height:1;color:var(--ink-3);
  cursor:pointer;padding:0 4px}
.sheet-row{display:flex;align-items:center;justify-content:space-between;
  padding:10px 0;border-bottom:1px solid var(--line);font-size:13px;color:var(--ink-2)}
.sheet-row b{color:var(--ink);font-weight:600}
.sheet-logout{
  width:100%;height:44px;margin-top:16px;border:1px solid #F3C9C9;border-radius:11px;
  background:#FFF6F6;color:#D9483B;font:inherit;font-size:14px;font-weight:600;cursor:pointer;
}
.sheet-logout:hover{background:#FDECEC}

@media (max-width:760px){
  .topbar .brand .bs{display:none}
  .svc{min-height:106px;padding:18px;border-radius:15px;gap:10px}
  .svc h3{font-size:16px}
  .svc p{font-size:12px}
  .svc-ico{width:54px;height:54px}
  .grid{gap:11px}
}
@media (max-width:520px){
  .topbar .brand{display:none}
  .status-right{gap:10px}
  .status-right .ok{font-size:11px}
  .svc{min-height:96px;padding:15px 14px;gap:9px}
  .svc h3{font-size:15px}
  .svc p{font-size:11px}
  .svc-ico{width:46px;height:46px}
  .price-link{font-size:11px}
}
</style>
