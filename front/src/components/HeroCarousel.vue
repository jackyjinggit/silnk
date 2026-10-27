<script setup>
import {onMounted, onUnmounted, ref} from 'vue'

/* 首页创新点轮播：4 屏，自动播放 5 秒，圆点带进度，悬停/切后台暂停 */
const DURATION = 5000
const dots = ['打印前体检', '透明计价', 'A3 拆 A4', '交付承诺']

const index = ref(0)
const paused = ref(false)
let timer = null
let startedAt = 0
let remaining = DURATION
let hovering = false
let pageHidden = false

function show(n) {
  index.value = (n + dots.length) % dots.length
  remaining = DURATION
  if (!paused.value) schedule(DURATION)
}

function schedule(ms) {
  clearTimeout(timer)
  startedAt = Date.now()
  timer = setTimeout(() => {
    index.value = (index.value + 1) % dots.length
    remaining = DURATION
    schedule(DURATION)
  }, ms)
}

function pause() {
  if (paused.value) return
  paused.value = true
  clearTimeout(timer)
  remaining -= Date.now() - startedAt
  if (remaining < 800) remaining = 800
}

function resume() {
  if (!paused.value) return
  paused.value = false
  schedule(remaining || DURATION)
}

function onEnter() {
  hovering = true
  pause()
}

function onLeave() {
  hovering = false
  if (!pageHidden) resume()
}

function onVisibility() {
  pageHidden = document.hidden
  if (pageHidden) pause()
  else if (!hovering) resume()
}

onMounted(() => {
  schedule(DURATION)
  document.addEventListener('visibilitychange', onVisibility)
})

onUnmounted(() => {
  clearTimeout(timer)
  document.removeEventListener('visibilitychange', onVisibility)
})
</script>

<template>
  <section class="hero" :class="{paused}"
           @mouseenter="onEnter" @mouseleave="onLeave"
           @focusin="onEnter" @focusout="onLeave">
    <div class="hero-inner">
      <div class="hero-viewport">
        <div class="hero-track" :style="{transform: `translateX(-${index * 100}%)`}">

          <!-- 01 打印前体检 -->
          <article class="slide" :class="{active: index === 0}">
            <div class="hero-copy">
              <span class="eyebrow">01 · 打印前体检</span>
              <h1>打印之前，<br>先把问题<em>检查掉</em></h1>
              <p>页数、尺寸、分辨率，上传后先看一遍，避免打废重来。</p>
              <span class="hero-pill">只提示，不阻断下单</span>
            </div>
            <div class="hero-art" aria-hidden="true">
              <svg viewBox="0 0 200 150" fill="none">
                <ellipse cx="100" cy="136" rx="66" ry="9" fill="#C6D6FF" opacity=".5"/>
                <rect x="38" y="12" width="124" height="118" rx="9" fill="#fff" stroke="#DCE6FF"/>
                <rect x="50" y="24" width="42" height="5" rx="2.5" fill="#DCE6FF"/>
                <rect x="50" y="34" width="28" height="4" rx="2" fill="#EEF3FF"/>
                <circle cx="72" cy="70" r="19" stroke="#E7ECF9" stroke-width="6"/>
                <circle cx="72" cy="70" r="19" stroke="#4C63F0" stroke-width="6"
                        pathLength="100" stroke-dasharray="97 3" stroke-linecap="round"
                        transform="rotate(-90 72 70)"/>
                <text x="72" y="76" text-anchor="middle" font-family="Arial,Helvetica,sans-serif"
                      font-size="14" font-weight="700" fill="#1B2340">97</text>
                <g>
                  <circle cx="106" cy="52" r="6" fill="#E7F7EE"/>
                  <path d="M103.5 52l1.8 1.8 3-3.4" stroke="#12A05A" stroke-width="1.6"
                        stroke-linecap="round" stroke-linejoin="round"/>
                  <rect x="117" y="49" width="36" height="3" rx="1.5" fill="#E6EDF9"/>
                  <rect x="117" y="55" width="24" height="3" rx="1.5" fill="#F2F5FB"/>

                  <circle cx="106" cy="72" r="6" fill="#E7F7EE"/>
                  <path d="M103.5 72l1.8 1.8 3-3.4" stroke="#12A05A" stroke-width="1.6"
                        stroke-linecap="round" stroke-linejoin="round"/>
                  <rect x="117" y="69" width="30" height="3" rx="1.5" fill="#E6EDF9"/>
                  <rect x="117" y="75" width="20" height="3" rx="1.5" fill="#F2F5FB"/>

                  <circle cx="106" cy="92" r="6" fill="#FFF4DE"/>
                  <path d="M106 89v4" stroke="#C9861A" stroke-width="1.8" stroke-linecap="round"/>
                  <circle cx="106" cy="95.5" r="1" fill="#C9861A"/>
                  <rect x="117" y="89" width="34" height="3" rx="1.5" fill="#E6EDF9"/>
                  <rect x="117" y="95" width="26" height="3" rx="1.5" fill="#F2F5FB"/>
                </g>
                <rect x="50" y="108" width="54" height="12" rx="6" fill="#E9EEFF"/>
                <rect x="56" y="112.5" width="34" height="3" rx="1.5" fill="#7C96FF"/>
                <rect x="140" y="18" width="16" height="16" rx="4" fill="#9FE8D5" opacity=".85"/>
              </svg>
            </div>
          </article>

          <!-- 02 透明计价 -->
          <article class="slide" :class="{active: index === 1}">
            <div class="hero-copy">
              <span class="eyebrow">02 · 透明计价</span>
              <h1>按墨量计价，<br>空白页<em>不收钱</em></h1>
              <p>逐页看色彩覆盖率分档，账单里写明这一单省了多少。</p>
              <span class="hero-pill">空白页不打印也不计费</span>
            </div>
            <div class="hero-art" aria-hidden="true">
              <svg viewBox="0 0 200 150" fill="none">
                <ellipse cx="100" cy="136" rx="66" ry="9" fill="#C6D6FF" opacity=".5"/>
                <rect x="34" y="14" width="132" height="114" rx="9" fill="#fff" stroke="#DCE6FF"/>
                <rect x="46" y="26" width="36" height="5" rx="2.5" fill="#DCE6FF"/>
                <rect x="46" y="36" width="22" height="4" rx="2" fill="#EEF3FF"/>
                <g>
                  <rect x="46" y="52" width="9" height="9" rx="2" fill="#C9D0DE"/>
                  <rect x="59" y="54" width="44" height="5" rx="2.5" fill="#E9EDF7"/>
                  <rect x="111" y="54" width="42" height="5" rx="2.5" fill="#C9D0DE" opacity=".8"/>
                </g>
                <g>
                  <rect x="46" y="68" width="9" height="9" rx="2" fill="#8FA5FF"/>
                  <rect x="59" y="70" width="52" height="5" rx="2.5" fill="#E9EDF7"/>
                  <rect x="119" y="70" width="34" height="5" rx="2.5" fill="#8FA5FF" opacity=".8"/>
                </g>
                <g>
                  <rect x="46" y="84" width="9" height="9" rx="2" fill="#4C63F0"/>
                  <rect x="59" y="86" width="60" height="5" rx="2.5" fill="#E9EDF7"/>
                  <rect x="127" y="86" width="26" height="5" rx="2.5" fill="#4C63F0" opacity=".8"/>
                </g>
                <path d="M46 104h108" stroke="#E4EAF6" stroke-dasharray="3 3"/>
                <text x="46" y="120" font-family="Arial,Helvetica,sans-serif" font-size="9"
                      fill="#8B94AC">28 页 · 双面</text>
                <text x="154" y="120" text-anchor="end" font-family="Arial,Helvetica,sans-serif"
                      font-size="13" font-weight="700" fill="#1B2340">¥7.00</text>
                <rect x="96" y="108" width="46" height="14" rx="7" fill="#E7F7EE"/>
                <text x="119" y="118" text-anchor="middle" font-family="Arial,Helvetica,sans-serif"
                      font-size="8" font-weight="700" fill="#128A56">已省 ¥1.40</text>
              </svg>
            </div>
          </article>

          <!-- 03 A3 拆 A4 -->
          <article class="slide" :class="{active: index === 2}">
            <div class="hero-copy">
              <span class="eyebrow">03 · 免排版</span>
              <h1>A3 自动拆 A4，<br>不用自己<em>切</em></h1>
              <p>横向左右切、纵向上下切，矢量切分，文字依然可以搜索。</p>
              <span class="hero-pill">混排文档也不会乱</span>
            </div>
            <div class="hero-art" aria-hidden="true">
              <svg viewBox="0 0 200 150" fill="none">
                <ellipse cx="100" cy="136" rx="66" ry="9" fill="#C6D6FF" opacity=".5"/>
                <g transform="rotate(-3 60 74)">
                  <rect x="24" y="22" width="62" height="100" rx="6" fill="#fff" stroke="#DCE6FF"/>
                  <rect x="34" y="36" width="42" height="5" rx="2.5" fill="#DCE6FF"/>
                  <rect x="34" y="48" width="34" height="4" rx="2" fill="#EEF3FF"/>
                  <rect x="34" y="58" width="42" height="4" rx="2" fill="#EEF3FF"/>
                  <rect x="34" y="74" width="42" height="26" rx="4" fill="#E4ECFF"/>
                </g>
                <g transform="rotate(3 138 74)">
                  <rect x="112" y="22" width="62" height="100" rx="6" fill="#fff" stroke="#DCE6FF"/>
                  <rect x="122" y="36" width="42" height="5" rx="2.5" fill="#DCE6FF"/>
                  <rect x="122" y="48" width="34" height="4" rx="2" fill="#EEF3FF"/>
                  <rect x="122" y="58" width="42" height="4" rx="2" fill="#EEF3FF"/>
                  <rect x="122" y="74" width="42" height="26" rx="4" fill="#FFE7CC"/>
                </g>
                <path d="M100 14v118" stroke="#FF9F43" stroke-width="2.4"
                      stroke-dasharray="5 5" stroke-linecap="round"/>
                <circle cx="100" cy="73" r="13" fill="#fff" stroke="#FFD3A8"/>
                <path d="M95 73h10M100 68v10" stroke="#FF9F43" stroke-width="2" stroke-linecap="round"/>
              </svg>
            </div>
          </article>

          <!-- 04 交付承诺 -->
          <article class="slide" :class="{active: index === 3}">
            <div class="hero-copy">
              <span class="eyebrow">04 · 交付承诺</span>
              <h1>说几分钟，<br>就<em>几分钟</em></h1>
              <p>下单给出承诺完成时间；超时免单，格式不对免费重印。</p>
              <span class="hero-pill">超时免单 · 免费重印</span>
            </div>
            <div class="hero-art" aria-hidden="true">
              <svg viewBox="0 0 200 150" fill="none">
                <ellipse cx="100" cy="136" rx="66" ry="9" fill="#C6D6FF" opacity=".5"/>
                <rect x="40" y="16" width="120" height="110" rx="9" fill="#fff" stroke="#DCE6FF"/>
                <circle cx="68" cy="52" r="17" fill="#EEF2FF"/>
                <circle cx="68" cy="52" r="17" stroke="#4C63F0" stroke-width="4"/>
                <path d="M68 43v9l6 3.5" stroke="#4C63F0" stroke-width="2.6"
                      stroke-linecap="round" stroke-linejoin="round"/>
                <rect x="96" y="40" width="48" height="5" rx="2.5" fill="#DCE6FF"/>
                <rect x="96" y="50" width="34" height="4" rx="2" fill="#EEF3FF"/>
                <rect x="96" y="58" width="40" height="4" rx="2" fill="#EEF3FF"/>
                <g>
                  <circle cx="54" cy="92" r="4" fill="#4C63F0"/>
                  <rect x="64" y="88" width="52" height="4" rx="2" fill="#E6EDF9"/>
                  <rect x="64" y="95" width="34" height="3" rx="1.5" fill="#F2F5FB"/>
                  <circle cx="54" cy="110" r="4" fill="#16B364"/>
                  <rect x="64" y="106" width="44" height="4" rx="2" fill="#E6EDF9"/>
                  <rect x="64" y="113" width="28" height="3" rx="1.5" fill="#F2F5FB"/>
                </g>
                <g transform="rotate(-12 152 104)">
                  <circle cx="152" cy="104" r="21" fill="#FFF7F3" stroke="#E05343" opacity=".95"/>
                  <circle cx="152" cy="104" r="16" fill="none" stroke="#E05343" opacity=".5"/>
                  <text x="152" y="102" text-anchor="middle" font-family="Arial,Helvetica,sans-serif"
                        font-size="8" font-weight="700" fill="#C74A3A">超时</text>
                  <text x="152" y="112" text-anchor="middle" font-family="Arial,Helvetica,sans-serif"
                        font-size="8" font-weight="700" fill="#C74A3A">免单</text>
                </g>
              </svg>
            </div>
          </article>

        </div>
      </div>

      <div class="hero-dots" role="tablist" aria-label="创新点轮播">
        <button v-for="(label, i) in dots" :key="label" type="button"
                class="dot-btn" :class="{on: i === index}"
                :aria-label="label" @click="show(i)"></button>
      </div>
    </div>
  </section>
</template>

<style scoped>
.hero{
  background:linear-gradient(103deg,#DCE8FF 0%,#E9F1FF 46%,#E2ECFF 100%);
  overflow:hidden;
}
.hero-inner{position:relative;overflow:hidden;width:100%;}
.hero-viewport{overflow:hidden;}
.hero-track{
  display:flex;
  transition:transform .68s cubic-bezier(.22,.61,.36,1);
  will-change:transform;
}
.slide{
  flex:0 0 100%;min-width:100%;
  display:flex;align-items:center;gap:22px;
  padding:26px 22px 42px;
}
.slide .hero-copy,.slide .hero-art{opacity:1;}
.slide.active .hero-copy{animation:copyIn .55s cubic-bezier(.22,.61,.36,1) both;}
.slide.active .hero-art{animation:artIn .62s cubic-bezier(.22,.61,.36,1) both;}
@keyframes copyIn{from{opacity:0;transform:translateY(10px);}to{opacity:1;transform:none;}}
@keyframes artIn{from{opacity:0;transform:translateX(16px) scale(.97);}to{opacity:1;transform:none;}}

.hero-copy{flex:1;min-width:0;}
.eyebrow{
  display:inline-flex;align-items:center;gap:7px;
  font-size:11px;font-weight:600;letter-spacing:1px;color:#3A4ECB;
}
.eyebrow::before{content:"";width:16px;height:1px;background:#4C63F0;opacity:.7;}
.hero h1{
  font-size:24px;line-height:1.42;letter-spacing:-.3px;
  color:#3346B8;font-weight:700;margin:8px 0 0;
}
.hero h1 em{
  font-style:normal;position:relative;color:#2F3FCB;
  background:linear-gradient(transparent 62%,rgba(76,99,240,.18) 62%);
  padding:0 2px;
}
.hero p{font-size:12.5px;color:#5A6890;line-height:1.7;margin:8px 0 0;max-width:330px;}
.hero-pill{
  display:inline-flex;align-items:center;gap:6px;margin-top:12px;
  background:#4C63F0;color:#fff;font-size:11.5px;
  padding:6px 12px;border-radius:99px;
  box-shadow:0 8px 18px rgba(76,99,240,.24);
}
.hero-art{width:182px;flex:none;}
.hero-art svg{display:block;width:100%;height:auto;}

.hero-dots{
  position:absolute;left:22px;bottom:16px;display:flex;align-items:center;gap:6px;z-index:3;
}
.dot-btn{
  width:6px;height:6px;border-radius:99px;background:#B7C5E8;
  transition:width .3s ease,background .3s ease;
  padding:0;position:relative;overflow:hidden;border:0;cursor:pointer;
}
.dot-btn.on{width:30px;background:#D9E2F8;}
.dot-btn.on::after{
  content:"";position:absolute;left:0;top:0;bottom:0;width:0;
  border-radius:99px;background:#4C63F0;
  animation:dotfill 5s linear forwards;
}
.hero.paused .dot-btn.on::after{animation-play-state:paused;}
.dot-btn:hover{background:#9DAFDB;}
@keyframes dotfill{from{width:0;}to{width:100%;}}

@media (max-width:760px){
  .slide{padding:22px 18px 38px;}
  .hero h1{font-size:21px;}
  .hero p{font-size:11.5px;max-width:280px;}
  .hero-pill{font-size:11px;padding:5px 11px;}
  .hero-art{width:140px;}
  .hero-dots{left:18px;bottom:13px;}
}
@media (max-width:520px){
  .slide{padding:20px 14px 36px;gap:10px;}
  .hero h1{font-size:19px;}
  .hero p{font-size:11px;}
  .hero-art{width:112px;}
  .hero-dots{left:14px;bottom:12px;}
}
@media (prefers-reduced-motion: reduce){
  .hero-track{transition:none;}
  .slide.active .hero-copy,.slide.active .hero-art{animation:none;}
  .dot-btn.on::after{animation:none;width:100%;}
}
</style>
