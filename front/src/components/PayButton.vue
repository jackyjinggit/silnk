<script setup>
import {computed, onMounted, ref} from 'vue';
import {IconBrandWechat} from '@tabler/icons-vue';
import axios from "axios";
import {ElMessage} from "element-plus";

const props = defineProps({
  fileList: {
    type: Object
  },
  printerId: {
    type: String
  },
  batch: {
    type: Object,
    default: null
  },
  // 拼单结算：给定时走 /api/group/settle/{短码}，而不是普通下单
  groupNo: {
    type: String,
    default: ''
  },
  // AA 拼单：后端已经生成好本人账单，直接展示收款码
  directBill: {
    type: Object,
    default: null
  },
  // 按钮文案（拼单结算按钮要显示金额）
  text: {
    type: String,
    default: '微信扫码支付'
  },
  // 定时打印：预约出纸时间（"YYYY-MM-DD HH:MM"，为空表示立即打印）
  scheduledAt: {
    type: String,
    default: ''
  },
  // 共享资料库：勾选后本单 95 折，成品在审核通过后公开
  shareLibrary: {
    type: Boolean,
    default: false
  }
})
const authHeader = () => ({
  'Accept': 'application/json',
  "Content-Type": "application/json",
  'Authorization': "Bearer " + window.localStorage.getItem("token")
})

/* 当前收款方式：个人微信收款码（无商户号）。
   下单后弹出收款码，用户扫码付款后自己点「我已支付」提交订单；
   因个人收款码没有微信到账回调，后端无法自动确认收款，
   提交后订单立即入队，同时记一条「待核验」流水，由管理员核对到账。 */
const payInfo = ref(null)       // GET /api/pay/info：收款码路径、收款人、图片是否存在
const bill = ref(null)          // 下单单据：订单号与应付金额
const showQr = ref(false)       // 收款码弹窗
const submitting = ref(false)
const payerNote = ref('')       // 付款备注（如微信昵称/尾号），方便管理员核对到账

const money = (v) => Number(v || 0).toFixed(2)

/* 交付承诺：下单即给出预计完成时间，超过即免单。 */
const etaMinutes = computed(() => {
  const s = bill.value && bill.value.eta_seconds
  return s ? Math.max(Math.round(s / 60), 1) : null
})
const etaTime = computed(() => {
  const d = bill.value && bill.value.eta_deadline
  return d ? String(d).slice(11, 19) : ''
})

const fetchPayInfo = async () => {
  try {
    const res = await axios.get(window.config.baseURL + '/api/pay/info')
    payInfo.value = res.data && res.data.data
  } catch (err) {
    console.log(err)
  }
}
onMounted(fetchPayInfo)

const pay = () => {
  // AA 拼单：账单已由后端按人生成，直接展示收款码
  if (props.directBill) {
    bill.value = props.directBill
    payerNote.value = ''
    showQr.value = true
    return
  }
  if (props.batch) {
    if ((props.fileList || []).length !== 1) {
      ElMessage.error('班级批量一次只能包含一个文件')
      return
    }
  }
  // 拼单：发起人统一结算，后端按人数打折后返回收款码
  const url = props.groupNo
    ? window.config.baseURL + '/api/group/settle/' + props.groupNo
    : window.config.baseURL + "/pay/bill/create"
  const body = props.groupNo ? {} : {
    "fileList": props.fileList,
    "printer_id": props.printerId || null,
    "batch": props.batch || null,
    "scheduled_at": props.scheduledAt || null,
    "share_library": !!props.shareLibrary
  }
  axios.post(url, body, {
    headers: authHeader()
  }).then(res => {
    const msg = res.data.message
    if (msg === 'success') {
      // 配好支付宝密钥时会走这条：跳转支付宝支付链接
      window.location.href = res.data.data.url
      return
    }
    if (msg === 'wechat payment required') {
      bill.value = res.data.data
      payerNote.value = ''
      showQr.value = true
      return
    }
    ElMessage.error(msg);
  }).catch(err => {
    ElMessage.error('支付请求失败')
    console.log(err)
  });
}

/* 用户点「我已支付」：订单立即入队，收款状态记为待核验 */
const submitPaid = () => {
  if (!bill.value) return
  submitting.value = true
  axios.post(window.config.baseURL + "/pay/wechat/submit", {
    "out_trade_no": bill.value.out_trade_no,
    "payer_note": payerNote.value || null
  }, {headers: authHeader()}).then(res => {
    if (res.data.message === 'success') {
      showQr.value = false
      const data = res.data.data || {}
      const code = data.pickup_code
      if (data.scheduled_at) {
        ElMessage.success(`已预约 ${data.scheduled_at} 出纸，到点后自动排队打印`
          + (code ? `，取件码 ${code}` : ''))
      } else {
        ElMessage.success(code
          ? `订单已提交，取件码 ${code}（在「最近订单」里查看）`
          : '订单已提交，请到打印机旁取件（收款由管理员核对到账）')
      }
      setTimeout(() => window.location.reload(), 2500)
      return
    }
    ElMessage.error('提交失败：' + res.data.message)
  }).catch(err => {
    ElMessage.error('提交请求失败')
    console.log(err)
  }).finally(() => {
    submitting.value = false
  })
}
</script>

<template>
  <button class="paybtn" type="button" @click="pay">
    <icon-brand-wechat class="icon"></icon-brand-wechat>
    <span class="txt">{{ text }}</span>
  </button>

  <div class="qrmask" v-if="showQr" @click.self="showQr = false">
    <div class="qrbox">
      <div class="qrh">
        <span>微信扫码支付</span>
        <button class="qrclose" type="button" @click="showQr = false">×</button>
      </div>
      <div class="qramount">{{ money(bill && bill.total_price) }}<small>元</small></div>
      <div class="qrpayee" v-if="bill && bill.pickup_code">
        取件码 {{ bill.pickup_code }} · 只在订单详情里核对，不印在纸上
      </div>
      <div class="qrpayee" v-if="payInfo && payInfo.payee">收款人：{{ payInfo.payee }}</div>

      <div class="qrsched" v-if="bill && bill.scheduled_at">
        定时出纸：预约 <b>{{ bill.scheduled_at }}</b> 到点后自动排队打印
      </div>

      <div class="qrsched" v-if="bill && bill.share_library">
        已选择共享到资料库，审核通过后才会公开
      </div>
      <div class="qrsched" v-if="bill && bill.library_discount_rate > 0">
        资料库优惠：已减 <b>{{ money(bill.library_discount) }}</b> 元
        （{{ Math.round(bill.library_discount_rate * 100) }}%，共享/贡献取较高值）
      </div>

      <div class="qrpromise" v-if="etaMinutes">
        <div class="qrp1">承诺 <b>{{ etaMinutes }}</b> 分钟内完成 · 预计 {{ etaTime }} 打完</div>
        <div class="qrp2">超时免单 · 格式不对免费重印 · 完成后 5 分钟自动删文件</div>
      </div>

      <div class="qrimg" v-if="payInfo && payInfo.qr_available">
        <img :src="payInfo.wechat_qr" alt="微信收款码">
        <div class="qrtip">请用微信扫一扫，按上方金额付款</div>
      </div>
      <div class="qrimg missing" v-else>
        收款码未配置：管理员需在管理端「账单流水」页设置收款码图片
      </div>

      <div class="qrf">
        <input v-model="payerNote" type="text" maxlength="40"
               placeholder="付款备注（选填，便于管理员核对到账）">
        <button class="qrbtn" type="button" :disabled="submitting" @click="submitPaid">
          {{ submitting ? '提交中…' : '我已支付完成' }}
        </button>
        <div class="qrn">
          个人收款码没有微信到账回调，点「我已支付完成」后订单会{{ bill && bill.scheduled_at ? '在预约时间到点后' : '立即' }}入队，
          收款状态记为「待核验」，由管理员核对到账后确认。
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.paybtn{width:180px;height:64px;display:flex;justify-content:center;align-items:center;gap:10px;
  border:0;border-radius:var(--r);background:var(--blue);color:#fff;font-size:18px;font-weight:600;
  font-family:inherit;cursor:pointer;transition:.15s;box-shadow:0 1px 2px rgba(31,111,235,.28)}
.paybtn:hover{background:var(--blue-600)}
.icon{width:26px;height:26px;color:#fff}

/* 收款码弹窗：白底 + 蓝色主按钮，与用户端清爽风格一致 */
.qrmask{position:fixed;inset:0;z-index:60;background:rgba(15,23,42,.45);
  display:flex;align-items:center;justify-content:center;padding:16px}
.qrbox{width:100%;max-width:360px;max-height:92vh;overflow:auto;background:var(--bg);
  border:1px solid var(--line);border-radius:var(--r);box-shadow:0 12px 32px rgba(15,23,42,.18)}
.qrh{display:flex;align-items:center;justify-content:space-between;
  padding:14px 16px;border-bottom:1px solid var(--line);font-size:15px;font-weight:700;color:var(--ink)}
.qrclose{border:0;background:none;font-size:22px;line-height:1;color:var(--ink-3);cursor:pointer;padding:0 4px}
.qramount{padding:18px 16px 6px;text-align:center;font-family:var(--num);font-variant-numeric:tabular-nums;
  font-size:34px;font-weight:800;color:var(--blue-700);letter-spacing:-.02em}
.qramount small{font-size:14px;font-weight:600;color:var(--ink-2);margin-left:4px}
.qrpayee{text-align:center;font-size:12.5px;color:var(--ink-3);padding-bottom:10px}
.qrpromise{margin:0 16px 12px;padding:10px 12px;background:#F1F6FF;border:1px solid #D6E4FF;
  border-radius:10px;text-align:center}
.qrsched{margin:0 16px 12px;padding:9px 12px;background:#F5F7FA;border:1px solid var(--line);
  border-radius:10px;text-align:center;font-size:12.5px;color:var(--ink-2)}
.qrsched b{color:var(--blue-700)}
.qrp1{font-size:13px;font-weight:700;color:var(--blue-700)}
.qrp1 b{font-size:16px}
.qrp2{font-size:11.5px;color:var(--ink-2);margin-top:4px;line-height:1.5}
.qrimg{padding:0 16px 14px;text-align:center}
.qrimg img{width:100%;max-width:240px;border:1px solid var(--line);border-radius:10px;display:block;margin:0 auto}
.qrimg .qrtip{font-size:12.5px;color:var(--ink-2);margin-top:8px}
.qrimg.missing{padding:22px 16px;font-size:12.5px;color:var(--ink-3);background:#F1F4F9;border-radius:10px;margin:0 16px 14px}
.qrf{padding:0 16px 16px;display:flex;flex-direction:column;gap:10px;border-top:1px solid var(--line);padding-top:14px}
.qrf input{height:40px;padding:0 12px;border:1px solid var(--line);border-radius:10px;
  font-size:13.5px;font-family:inherit;color:var(--ink);background:var(--bg);outline:none}
.qrf input:focus{border-color:var(--blue)}
.qrbtn{height:46px;border:0;border-radius:var(--r);background:var(--blue);color:#fff;
  font-size:15.5px;font-weight:700;font-family:inherit;cursor:pointer}
.qrbtn:hover{background:var(--blue-600)}
.qrbtn:disabled{opacity:.6;cursor:not-allowed}
.qrn{font-size:11.5px;line-height:1.6;color:var(--ink-3)}
</style>
