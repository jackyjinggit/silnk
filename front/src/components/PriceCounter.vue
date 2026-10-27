<script setup>
import {computed, watchEffect} from "vue";
import {Decimal} from 'decimal.js'

const emit = defineEmits(['total'])

const props = defineProps({
  fileList: {
    type: Object
  },
  // 当前所选机器的生效单价 {bw, color, bw_duplex, color_duplex, coverage}
  price: {
    type: Object
  },
  // FileList 检查完成后回传的数据：{filename: {coverage_pages, blank_pages}}
  coverage: {
    type: Object
  },
  // 资料库优惠率（0~0.10）：本单共享 5% 与历史贡献折扣取较高值
  discountRate: {
    type: Number,
    default: 0
  }
})

const COV_DEFAULT = {
  no_color_percent: 0.5,
  low_max_percent: 5,
  mid_max_percent: 30,
  low_factor: 0.5,
  mid_factor: 0.8
}

const covCfg = () => (props.price && props.price.coverage) || COV_DEFAULT

const priceOf = (key) => {
  const v = props.price && props.price[key]
  return (v === null || v === undefined) ? window.config[key] : v
}

/* 按文件名取每页覆盖率（0~1）：{页码: 覆盖率} */
const coverageMap = (filename) => {
  const data = props.coverage && props.coverage[filename]
  const map = {}
  for (const page of (data && data.coverage_pages) || []) {
    map[page.page] = page.coverage / 100
  }
  return map
}

/* 空白页集合：用户勾选「跳过空白页」时才生效 */
const blankSet = (filename, skip) => {
  if (!skip) return new Set()
  const data = props.coverage && props.coverage[filename]
  return new Set((data && data.blank_pages) || [])
}

/* 单页价格：<0.5% 按黑白价；0.5~5% 五折；5~30% 八折；>30% 全价 */
const pagePrice = (coverage, bwPrice, colorPrice) => {
  const cfg = covCfg()
  if (coverage === undefined || coverage === null) return new Decimal(colorPrice)
  if (coverage < cfg.no_color_percent / 100) return new Decimal(bwPrice)
  if (coverage <= cfg.low_max_percent / 100) {
    return new Decimal(colorPrice).mul(cfg.low_factor)
  }
  if (coverage <= cfg.mid_max_percent / 100) {
    return new Decimal(colorPrice).mul(cfg.mid_factor)
  }
  return new Decimal(colorPrice)
}

/* 双面一张纸：两面都无彩色按黑白双面价；否则两面单页价之和 × 双面折扣比例 */
const duplexPrice = (cov1, cov2, bwPrice, colorPrice, duplexPriceValue) => {
  const cfg = covCfg()
  const no1 = cov1 === undefined || cov1 === null || cov1 < cfg.no_color_percent / 100
  const no2 = cov2 === undefined || cov2 === null || cov2 < cfg.no_color_percent / 100
  if (no1 && no2) return new Decimal(duplexPriceValue)
  if (!colorPrice) return new Decimal(duplexPriceValue)
  const ratio = new Decimal(duplexPriceValue).div(new Decimal(colorPrice).mul(2))
  return pagePrice(cov1, bwPrice, colorPrice)
    .add(pagePrice(cov2, bwPrice, colorPrice))
    .mul(ratio)
}

const calc = computed(() => {
  let total = new Decimal(0)      // 实付：跳过空白页 + 覆盖率折扣 + 资料库折扣
  let noSkip = new Decimal(0)     // 只打覆盖率折扣、不跳空白页
  let full = new Decimal(0)       // 原价：不跳空白页、不打覆盖率折扣
  let shareBase = new Decimal(0)  // 资料库折扣前的金额（用于展示优惠）

  for (let file of props.fileList || []) {
    const startRaw = parseInt(file.print_range_start, 10)
    const endRaw = parseInt(file.print_range_end, 10)
    if (!Number.isFinite(startRaw) || !Number.isFinite(endRaw) || endRaw < startRaw) continue
    const copies = Math.max(parseInt(file.print_copies, 10) || 1, 1)
    const colorMode = file.print_color === 'color'
    const single = priceOf(colorMode ? 'color' : 'bw')
    const duplex = priceOf(colorMode ? 'color_duplex' : 'bw_duplex')
    const bwSingle = priceOf('bw')
    const covs = colorMode ? coverageMap(file.filename) : {}
    const blanks = blankSet(file.filename, file.skip_blank !== false)

    const allPages = []
    for (let page = startRaw; page <= endRaw; page++) allPages.push(page)
    const pages = allPages.filter(page => !blanks.has(page))
    if (!pages.length) continue

    /* 按给定页码序列算钱（含覆盖率折扣） */
    const amountOf = (list) => {
      if (file.print_side === 'one-sided') {
        if (!colorMode) return new Decimal(list.length).mul(single)
        let sum = new Decimal(0)
        for (const page of list) sum = sum.add(pagePrice(covs[page], bwSingle, single))
        return sum
      }
      if (colorMode) {
        let sum = new Decimal(0)
        for (let i = 0; i < list.length; i += 2) {
          if (i + 1 < list.length) {
            sum = sum.add(duplexPrice(covs[list[i]], covs[list[i + 1]], bwSingle, single, duplex))
          } else {
            sum = sum.add(pagePrice(covs[list[i]], bwSingle, single))
          }
        }
        return sum
      }
      const sheets = Math.floor(list.length / 2)
      let sum = new Decimal(sheets).mul(duplex)
      if (list.length % 2) sum = sum.add(single)
      return sum
    }

    /* 原价：每页都按全价，不享受覆盖率折扣 */
    const fullOf = (list) => {
      if (file.print_side === 'one-sided') return new Decimal(list.length).mul(single)
      let sum = new Decimal(Math.floor(list.length / 2)).mul(duplex)
      if (list.length % 2) sum = sum.add(single)
      return sum
    }

    const rawAmount = amountOf(pages).mul(copies).toDecimalPlaces(2)
    shareBase = shareBase.add(rawAmount)
    // 与后端一致：按文件逐个四舍五入，再相加
    const rate = Number(props.discountRate || 0)
    total = total.add(rate > 0
      ? rawAmount.mul(new Decimal(1).minus(rate)).toDecimalPlaces(2)
      : rawAmount)
    noSkip = noSkip.add(amountOf(allPages).mul(copies).toDecimalPlaces(2))
    full = full.add(fullOf(allPages).mul(copies).toDecimalPlaces(2))
  }
  return {total, noSkip, full, shareBase}
})

const totalPrice = computed(() => calc.value.total)
const blankSaving = computed(() => calc.value.noSkip.minus(calc.value.total))
const coverageSaving = computed(() => calc.value.full.minus(calc.value.noSkip))
const totalSaving = computed(() => calc.value.full.minus(calc.value.total))
const librarySaving = computed(() =>
  props.discountRate > 0 ? calc.value.shareBase.minus(calc.value.total) : new Decimal(0))

// 把总价交给 Home，班级批量用来算人均
watchEffect(() => {
  emit('total', calc.value.total.toNumber())
})
</script>

<template>
  <div class="wrapper">
    <div class="line">
      <span class="k">预估费用</span>
      <span class="v"><b>{{ totalPrice }}</b><small>元</small></span>
    </div>
    <div class="line" v-if="totalSaving.gt(0)">
      <span class="k">本单已省</span>
      <span class="v discount">-{{ totalSaving.toFixed(2) }} 元</span>
    </div>
    <div class="saving-detail" v-if="blankSaving.gt(0) || coverageSaving.gt(0) || librarySaving.gt(0)">
      <span v-if="blankSaving.gt(0)">空白页 {{ blankSaving.toFixed(2) }}</span>
      <span v-if="coverageSaving.gt(0)">覆盖率 {{ coverageSaving.toFixed(2) }}</span>
      <span v-if="librarySaving.gt(0)">资料库优惠 {{ librarySaving.toFixed(2) }}</span>
    </div>
  </div>
</template>

<style scoped>
.wrapper{width:100%;height:100%;display:flex;flex-direction:column;justify-content:center;gap:2px;
  padding:0 16px;border-radius:var(--r);background:var(--bg);border:1px solid var(--line)}
.line{display:flex;align-items:baseline;justify-content:space-between;gap:10px}
.line .k{font-size:12.5px;color:var(--ink-3)}
.line .v{font-family:var(--num);font-variant-numeric:tabular-nums}
.line .v b{font-size:23px;font-weight:800;color:var(--blue-700);letter-spacing:-.02em}
.line .v small{font-size:12px;color:var(--ink-2);margin-left:3px;font-weight:600}
.line .v.discount{font-size:13px;color:var(--ok);font-weight:600}
.saving-detail{display:flex;gap:10px;font-size:11px;color:var(--ink-3);line-height:1.3}
</style>
