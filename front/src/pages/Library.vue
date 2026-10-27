<script setup>
import {onMounted, ref} from 'vue'
import axios from 'axios'
import {useRouter} from 'vue-router'
import {ElMessage} from 'element-plus'

const router = useRouter()
const docs = ref([])
const total = ref(0)
const categories = ref([])
const activeCat = ref('')
const q = ref('')
const loading = ref(false)
const usingId = ref(null)

const fetchList = async () => {
  loading.value = true
  try {
    const res = await axios.get(window.config.baseURL + '/api/library/list', {
      params: {q: q.value.trim(), category: activeCat.value}
    })
    const data = (res.data && res.data.data) || {}
    docs.value = data.docs || []
    total.value = data.total || 0
    categories.value = data.categories || []
  } catch (e) {
    ElMessage.error('资料库加载失败，请稍后重试')
  } finally {
    loading.value = false
  }
}

const chooseCat = (cat) => {
  activeCat.value = activeCat.value === cat ? '' : cat
  fetchList()
}

const sizeText = (bytes) => {
  const n = Number(bytes || 0)
  if (n >= 1024 * 1024) return (n / 1024 / 1024).toFixed(1) + ' MB'
  return Math.max(1, Math.round(n / 1024)) + ' KB'
}

const useDoc = async (doc) => {
  const token = window.localStorage.getItem('token')
  if (!token || token === 'none') {
    router.push('/login')
    return
  }
  usingId.value = doc.id
  try {
    const res = await axios.post(
      window.config.baseURL + '/api/library/' + doc.id + '/use', {},
      {headers: {Authorization: 'Bearer ' + token}}
    )
    if (res.data && res.data.message === 'success') {
      ElMessage.success('已加入上传列表，选好规格即可下单')
      router.push('/')
    } else {
      ElMessage.error((res.data && res.data.message) || '取用失败')
    }
  } catch (e) {
    ElMessage.error('取用失败，请稍后重试')
  } finally {
    usingId.value = null
  }
}

const download = (doc) => {
  window.open(window.config.baseURL + '/api/library/' + doc.id + '/file', '_blank')
}

onMounted(fetchList)
</script>

<template>
  <div class="lib">
    <header class="libbar">
      <button class="back" type="button" @click="router.push('/')" aria-label="返回">‹</button>
      <div class="libtitle">共享资料库</div>
      <span class="sp"></span>
    </header>

    <main class="libbody">
      <section class="intro">
        <span class="eyebrow">SHARED LIBRARY</span>
        <h1>同学共享的资料，<br>可以直接拿去打印。</h1>
        <p>每份都要经过 agent 初审和商家管理员复审才会公开。取用后加入本次上传列表，按正常流程下单。</p>
        <div class="facts">
          <div><b>{{ total }}</b><span>份已公开资料</span></div>
          <div><b>2 次</b><span>审核后才公开</span></div>
          <div><b>95 折</b><span>共享者下单优惠</span></div>
        </div>
      </section>

      <div class="search">
        <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor"
             stroke-width="1.9" stroke-linecap="round">
          <circle cx="11" cy="11" r="7"/><path d="m20 20-3.6-3.6"/>
        </svg>
        <input v-model="q" type="search" placeholder="搜索资料标题，例如：高数、实验报告"
               @keyup.enter="fetchList">
        <button class="searchbtn" type="button" @click="fetchList">搜索</button>
      </div>

      <div class="chips" v-if="categories.length">
        <button v-for="c in categories" :key="c" type="button"
                class="chip" :class="{on: activeCat === c}" @click="chooseCat(c)">{{ c }}</button>
      </div>

      <div v-if="loading" class="empty">正在读取资料库…</div>
      <div v-else-if="!docs.length" class="empty">
        还没有公开资料。下单时勾选「共享到公开资料库」，审核通过后就会出现在这里。
      </div>

      <div v-else class="list">
        <article v-for="d in docs" :key="d.id" class="doc">
          <div class="doc-ico">
            <svg viewBox="0 0 64 64" fill="none">
              <rect x="13" y="7" width="34" height="46" rx="5" fill="#fff" stroke="#DCE4F5"/>
              <path d="M38 7v9h9" fill="#EAF0FF"/>
              <rect x="20" y="17" width="20" height="3" rx="1.5" fill="#DCE6FF"/>
              <rect x="20" y="25" width="16" height="3" rx="1.5" fill="#EAF0FF"/>
              <rect x="20" y="33" width="20" height="3" rx="1.5" fill="#EAF0FF"/>
              <rect x="34" y="38" width="22" height="17" rx="4" fill="#4C63F0"/>
              <path d="M41 46h8m-4-4v8" stroke="#fff" stroke-width="2" stroke-linecap="round"/>
            </svg>
          </div>
          <div class="doc-main">
            <div class="doc-t">
              <h3 :title="d.title">{{ d.title }}</h3>
              <span class="cat">{{ d.category }}</span>
            </div>
            <div class="doc-meta">
              {{ d.pages }} 页 · {{ sizeText(d.size_bytes) }} ·
              已打印 {{ d.print_count }} 次 · 已取用 {{ d.downloads }} 次
            </div>
          </div>
          <div class="doc-act">
            <button class="use" type="button" :disabled="usingId === d.id" @click="useDoc(d)">
              {{ usingId === d.id ? '加入中…' : '拿去打印' }}
            </button>
            <button class="dl" type="button" @click="download(d)">下载</button>
          </div>
        </article>
      </div>

      <div class="policy">
        资料由同学自愿共享，审核通过后才公开；如发现侵权或隐私内容，请联系现场工作人员下架。
        取用不会改变原文件的公开状态。
      </div>
    </main>
  </div>
</template>

<style scoped>
.lib{max-width:860px;margin:0 auto;min-height:100vh;background:var(--bg)}
.libbar{position:sticky;top:0;z-index:20;height:58px;display:flex;align-items:center;
  padding:0 16px;background:rgba(255,255,255,.94);backdrop-filter:blur(12px);
  border-bottom:1px solid var(--line)}
.back{width:34px;height:34px;border:0;background:none;font-size:26px;line-height:1;
  color:var(--ink);cursor:pointer;border-radius:9px}
.back:hover{background:var(--line-2)}
.libtitle{position:absolute;left:50%;transform:translateX(-50%);font-size:16px;font-weight:600}
.sp{flex:1}
.libbody{padding:18px 18px 40px;display:flex;flex-direction:column;gap:16px}
.intro{background:linear-gradient(103deg,#DCE8FF 0%,#E9F1FF 50%,#E2ECFF 100%);
  border-radius:18px;padding:24px 22px}
.eyebrow{font-size:10.5px;letter-spacing:1.6px;color:#3A4ECB;font-weight:600}
.intro h1{margin:10px 0 0;font-size:24px;line-height:1.42;color:#3346B8;letter-spacing:-.4px}
.intro p{margin:10px 0 0;font-size:12.5px;line-height:1.75;color:#5A6890;max-width:520px}
.facts{display:flex;gap:26px;margin-top:18px}
.facts b{display:block;font-size:18px;color:#2F3FCB;font-family:var(--num)}
.facts span{display:block;font-size:10.5px;color:#5A6890;margin-top:2px}
.search{display:flex;align-items:center;gap:9px;height:46px;padding:0 8px 0 14px;
  background:#fff;border:1px solid var(--line);border-radius:12px;color:var(--ink-3)}
.search:focus-within{border-color:var(--blue);box-shadow:0 0 0 4px rgba(76,99,240,.08)}
.search input{flex:1;border:0;outline:0;background:none;font:inherit;font-size:13.5px;color:var(--ink)}
.searchbtn{height:34px;padding:0 16px;border:0;border-radius:9px;background:var(--blue);
  color:#fff;font:inherit;font-size:12.5px;font-weight:600;cursor:pointer}
.chips{display:flex;gap:8px;flex-wrap:wrap}
.chip{height:30px;padding:0 13px;border:1px solid var(--line);border-radius:99px;
  background:#fff;color:var(--ink-2);font:inherit;font-size:12px;cursor:pointer}
.chip.on{background:var(--blue-50);border-color:var(--blue-100);color:var(--blue-700);font-weight:600}
.empty{padding:40px 18px;text-align:center;color:var(--ink-3);font-size:13px;line-height:1.8;
  background:#fff;border:1px dashed var(--line-strong);border-radius:14px}
.list{display:flex;flex-direction:column;gap:10px}
.doc{display:flex;align-items:center;gap:13px;padding:14px 16px;background:#fff;
  border:1px solid var(--line);border-radius:14px;transition:.18s}
.doc:hover{border-color:#DCE4FB;box-shadow:0 8px 22px rgba(30,45,100,.07)}
.doc-ico{width:44px;height:44px;flex:none}
.doc-ico svg{width:100%;height:100%;display:block}
.doc-main{flex:1;min-width:0}
.doc-t{display:flex;align-items:center;gap:8px}
.doc-t h3{margin:0;font-size:14.5px;font-weight:600;white-space:nowrap;overflow:hidden;
  text-overflow:ellipsis}
.cat{font-size:9.5px;color:var(--blue-700);background:var(--blue-50);
  border:1px solid var(--blue-100);border-radius:5px;padding:2px 6px;flex:none}
.doc-meta{font-size:11px;color:var(--ink-3);margin-top:5px}
.doc-act{display:flex;align-items:center;gap:8px;flex:none}
.use{height:34px;padding:0 14px;border:0;border-radius:9px;background:var(--blue);
  color:#fff;font:inherit;font-size:12.5px;font-weight:600;cursor:pointer}
.use:hover{background:var(--blue-600)}
.use:disabled{opacity:.6;cursor:default}
.dl{height:34px;padding:0 11px;border:1px solid var(--line);border-radius:9px;background:#fff;
  color:var(--ink-2);font:inherit;font-size:12px;cursor:pointer}
.dl:hover{border-color:var(--blue);color:var(--blue)}
.policy{font-size:11px;color:var(--ink-3);line-height:1.75;padding:4px 2px}
@media (max-width:560px){
  .libbody{padding:14px 14px 32px}
  .intro{padding:20px 16px;border-radius:16px}
  .intro h1{font-size:20px}
  .facts{gap:18px}
  .facts b{font-size:16px}
  .doc{align-items:flex-start;flex-wrap:wrap;padding:13px 14px}
  .doc-main{flex:1;min-width:150px}
  .doc-act{width:100%;justify-content:flex-end}
}
</style>
