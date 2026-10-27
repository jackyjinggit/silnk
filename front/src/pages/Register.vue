<script setup>
import {computed, ref} from 'vue'
import {useRouter} from 'vue-router'
import axios from 'axios'
import {ElMessage} from 'element-plus'

const router = useRouter()
const phone = ref('')
const code = ref('')
const nickname = ref('')
const password = ref('')
const confirm = ref('')
const sending = ref(false)
const submitting = ref(false)
const countdown = ref(0)
const demoCode = ref('')
const demoNote = ref('')
let timer = null

const base = () => window.config.baseURL
const phoneOk = computed(() => /^1[3-9]\d{9}$/.test(phone.value.trim()))

const startCountdown = (seconds) => {
  countdown.value = seconds
  if (timer) clearInterval(timer)
  timer = setInterval(() => {
    countdown.value -= 1
    if (countdown.value <= 0) clearInterval(timer)
  }, 1000)
}

const sendCode = async () => {
  if (!phoneOk.value) { ElMessage.warning('请输入正确的 11 位手机号'); return }
  if (sending.value || countdown.value > 0) return
  sending.value = true
  try {
    const res = await axios.post(base() + '/api/auth/send-code', {phone: phone.value.trim()})
    const msg = res.data.message
    if (msg !== 'success') { ElMessage.error(msg || '发送失败'); return }
    const data = res.data.data || {}
    if (data.demo && data.demo_code) {
      demoCode.value = data.demo_code
      demoNote.value = data.note || '演示模式：验证码直接显示'
      ElMessage.success('演示验证码已生成')
    } else {
      demoCode.value = ''
      demoNote.value = ''
      ElMessage.success('验证码已发送')
    }
    startCountdown(data.interval || 60)
  } catch (e) {
    ElMessage.error('发送失败，请稍后再试')
  } finally {
    sending.value = false
  }
}

const submit = async () => {
  if (!phoneOk.value) { ElMessage.warning('请输入正确的 11 位手机号'); return }
  if (!code.value.trim()) { ElMessage.warning('请输入验证码'); return }
  if (!password.value) { ElMessage.warning('请设置登录密码'); return }
  if (password.value.length < 8) { ElMessage.warning('密码至少 8 位'); return }
  if (password.value !== confirm.value) { ElMessage.warning('两次输入的密码不一致'); return }
  if (submitting.value) return
  submitting.value = true
  try {
    const res = await axios.post(base() + '/api/auth/register', {
      phone: phone.value.trim(),
      code: code.value.trim(),
      nickname: nickname.value.trim(),
      password: password.value,
    })
    const msg = res.data.message
    if (msg !== 'success') {
      ElMessage.error(msg || '注册失败')
      if (msg && msg.indexOf('已注册') >= 0) {
        setTimeout(() => router.push('/login'), 900)
      }
      return
    }
    const data = res.data.data || {}
    window.localStorage.setItem('token', data.token)
    window.localStorage.setItem('pp_user', JSON.stringify(data.user || {}))
    ElMessage.success(data.is_new === false ? '密码设置成功' : '注册成功')
    router.replace('/')
  } catch (e) {
    ElMessage.error('注册失败，请稍后再试')
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <div class="lg">
    <div class="lgcard">
      <div class="lghead">
        <span class="mark">印</span>
        <div>
          <div class="bt">思印客</div>
          <div class="bs">Silnk · 注册</div>
        </div>
      </div>

      <div class="lgbody">
        <label class="fld">
          <span>手机号</span>
          <input v-model="phone" type="tel" maxlength="11" placeholder="请输入 11 位手机号"
                 autocomplete="tel">
        </label>

        <label class="fld">
          <span>验证码</span>
          <div class="coderow">
            <input v-model="code" type="text" maxlength="6" placeholder="6 位验证码"
                   inputmode="numeric" autocomplete="one-time-code">
            <button type="button" class="sendbtn"
                    :disabled="sending || countdown > 0 || !phoneOk"
                    @click="sendCode">
              {{ countdown > 0 ? countdown + 's' : (sending ? '发送中…' : '获取验证码') }}
            </button>
          </div>
        </label>

        <div class="demo" v-if="demoCode">
          <b>演示验证码 {{ demoCode }}</b>
          <span>{{ demoNote }}</span>
        </div>

        <label class="fld">
          <span>昵称（选填）</span>
          <input v-model="nickname" type="text" maxlength="20" placeholder="会显示在订单与拼单里">
        </label>

        <label class="fld">
          <span>登录密码</span>
          <input v-model="password" type="password" maxlength="64" placeholder="至少 8 位"
                 autocomplete="new-password">
        </label>

        <label class="fld">
          <span>确认密码</span>
          <input v-model="confirm" type="password" maxlength="64" placeholder="再输入一次密码"
                 autocomplete="new-password" @keyup.enter="submit">
        </label>

        <button type="button" class="submit" :disabled="submitting" @click="submit">
          {{ submitting ? '注册中…' : '注册并登录' }}
        </button>

        <div class="switch">
          已有账号？
          <button type="button" class="linkbtn" @click="router.push('/login')">去登录</button>
        </div>

        <div class="tip">
          老账号（之前只用验证码登录）也用这个页面补设密码：验证码通过后会直接写入新密码并登录。
        </div>

        <div class="notice">
          请勿上传打印涉黄、涉政、涉密、侵权等不合规的打印内容，一经查实，将同步公安并做永久封号处理！
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.lg{min-height:100vh;background:#f6f8fb;display:flex;align-items:center;justify-content:center;padding:24px}
.lgcard{width:100%;max-width:420px;background:#fff;border:1px solid var(--line,#e5e8ee);
  border-radius:14px;box-shadow:0 10px 30px rgba(20,40,80,.08);overflow:hidden}
.lghead{display:flex;align-items:center;gap:12px;padding:20px 20px 16px;border-bottom:1px solid var(--line,#e5e8ee)}
.lghead .mark{width:36px;height:36px;border-radius:10px;display:grid;place-items:center;
  background:var(--blue,#1f6feb);color:#fff;font-weight:700;font-size:17px}
.lghead .bt{font-size:17px;font-weight:700;color:#111827}
.lghead .bs{font-size:11.5px;color:#8a94a6;margin-top:2px}
.lgbody{padding:18px 20px 20px}
.fld{display:block;margin-bottom:14px}
.fld > span{display:block;font-size:12.5px;color:#5b6577;margin-bottom:6px}
.fld input{width:100%;box-sizing:border-box;padding:11px 12px;border:1px solid #dfe3ea;
  border-radius:9px;font:inherit;font-size:14.5px;color:#111827;background:#fff;outline:none}
.fld input:focus{border-color:var(--blue,#1f6feb);box-shadow:0 0 0 3px rgba(31,111,235,.12)}
.coderow{display:flex;gap:8px}
.coderow input{flex:1}
.sendbtn{flex:0 0 108px;border:1px solid var(--blue,#1f6feb);background:#fff;color:var(--blue,#1f6feb);
  border-radius:9px;font:inherit;font-size:13px;cursor:pointer}
.sendbtn:disabled{opacity:.5;cursor:not-allowed}
.submit{width:100%;margin-top:4px;padding:12px;border:0;border-radius:10px;
  background:var(--blue,#1f6feb);color:#fff;font:inherit;font-size:15px;font-weight:600;cursor:pointer}
.submit:disabled{opacity:.6;cursor:not-allowed}
.switch{margin-top:14px;text-align:center;font-size:13px;color:#8a94a6}
.linkbtn{border:0;background:none;color:var(--blue,#1f6feb);font:inherit;font-size:13px;
  cursor:pointer;padding:0 2px}
.tip{margin-top:12px;font-size:12.5px;color:#8a94a6;line-height:1.6}
.demo{margin:-6px 0 14px;padding:10px 12px;border-radius:9px;background:#fffbeb;
  border:1px solid #fde68a;color:#92400e;font-size:13px;line-height:1.6}
.demo b{display:block;font-weight:700;letter-spacing:.08em}
.notice{margin-top:16px;padding:10px 12px;border-radius:9px;background:#fff5f5;
  border:1px solid #ffd7d7;border-left:4px solid #e5484d;color:#a12b2f;font-size:12.5px;line-height:1.65}
</style>
