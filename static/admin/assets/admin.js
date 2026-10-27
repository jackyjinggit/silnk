/* 思印客 Silnk · 运营管理台 · 公共脚本
   密钥一律从 localStorage 的 "pp_key" 读取，页面源码不含任何默认密钥。 */
window.PP = (function () {
  "use strict";

  const OFFLINE_SECONDS = 60;
  let serverNow = null;
  let KEY = localStorage.getItem("pp_key") || "";
  let ROLE = localStorage.getItem("pp_role") || "";
  let refreshFn = null;
  /* 运维不能进的商家页面：订单金额、账单、价格配置 */
  const MERCHANT_PAGES = ["orders", "billing", "price", "users", "library", "reports"];

  const PAPER_LABEL = { full: "满纸", half: "半纸", low: "少纸", empty: "缺纸" };
  const PAPER_LEVEL = { full: 4, half: 3, low: 2, empty: 1 };

  /* ---------------- 工具 ---------------- */
  function fmtAge(sec) {
    if (sec === null || sec === undefined) return "—";
    if (sec < 0) sec = 0;
    if (sec < 60) return sec + " 秒前";
    if (sec < 3600) return Math.floor(sec / 60) + " 分钟前";
    if (sec < 86400) return Math.floor(sec / 3600) + " 小时前";
    return Math.floor(sec / 86400) + " 天前";
  }
  function ageOf(ts) {
    if (!ts || !serverNow) return null;
    const t = typeof ts === "number" ? ts * 1000 : Date.parse(String(ts).replace(" ", "T"));
    if (isNaN(t)) return null;
    return Math.round((serverNow - t) / 1000);
  }
  const esc = s => String(s === null || s === undefined ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");

  function onlineInfo(d) {
    const age = ageOf(d.last_heartbeat);
    const stale = d.status === "online" && age !== null && age > OFFLINE_SECONDS;
    const online = d.status === "online" && !stale;
    return { age, stale, online };
  }

  /* ---------------- 接口 ---------------- */
  async function api(path, opts) {
    const o = opts || {};
    const headers = Object.assign({ Authorization: "Bearer " + KEY }, o.headers || {});
    const res = await fetch(path, Object.assign({}, o, { headers }));
    const d = res.headers.get("date");
    if (d) serverNow = new Date(d).getTime();
    if (res.status === 401) throw new Error("密钥无效（401）");
    if (!res.ok) throw new Error(path + " 返回 " + res.status);
    const body = await res.json();
    return body.data;
  }
  async function post(path, payload) {
    const opts = { method: "POST" };
    if (payload !== undefined) {
      opts.headers = { "Content-Type": "application/json" };
      opts.body = JSON.stringify(payload);
    }
    return api(path, opts);
  }
  async function del(path) { return api(path, { method: "DELETE" }); }

  /* 同 post，但返回完整 {code,message,data}，便于页面检查业务 message */
  async function postFull(path, payload) {
    const opts = { method: "POST" };
    if (payload !== undefined) {
      opts.headers = { "Content-Type": "application/json" };
      opts.body = JSON.stringify(payload);
    }
    const headers = Object.assign({ Authorization: "Bearer " + KEY }, opts.headers || {});
    const res = await fetch(path, Object.assign({}, opts, { headers }));
    if (res.status === 401) throw new Error("密钥无效（401）");
    if (!res.ok) throw new Error(path + " 返回 " + res.status);
    return await res.json();
  }

  function getKey() { return KEY; }
  function setKey(v) { KEY = v; localStorage.setItem("pp_key", KEY); }
  function getRole() { return ROLE; }

  /* ---------------- 角色（商家 / 运维） ---------------- */
  function roleLabel() {
    return ROLE === "merchant" ? "商家" : (ROLE === "ops" ? "运维" : "未登录");
  }
  function applyRole() {
    document.body.setAttribute("data-role", ROLE || "none");
    // 侧栏：运维隐藏订单/账单/价格
    document.querySelectorAll(".snav .sitem").forEach(a => {
      const p = a.getAttribute("data-page");
      a.style.display = (ROLE === "ops" && MERCHANT_PAGES.indexOf(p) >= 0) ? "none" : "";
    });
    // 顶栏角色徽标
    const tb = document.querySelector(".tb-right");
    if (tb) {
      let badge = document.getElementById("role-badge");
      if (!badge) {
        badge = document.createElement("span");
        badge.id = "role-badge";
        const keyBtn = document.getElementById("btn-key");
        if (keyBtn) tb.insertBefore(badge, keyBtn); else tb.appendChild(badge);
      }
      badge.className = "pill " + (ROLE === "merchant" ? "on" : (ROLE === "ops" ? "warn" : "crit"));
      badge.textContent = roleLabel();
    }
    // 商家专属按钮/区块由 CSS（body[data-role="ops"]）隐藏，切换角色后自动恢复
    if (ROLE === "ops") {
      // 运维直接打开商家页面时退回概览
      if (MERCHANT_PAGES.indexOf(document.body.getAttribute("data-page")) >= 0) {
        location.href = "index.html";
        return;
      }
    }
  }
  async function refreshRole() {
    if (!KEY) {
      ROLE = "";
      localStorage.removeItem("pp_role");
      applyRole();
      return;
    }
    try {
      const d = await api("/api/whoami");
      ROLE = (d && d.role) || "";
    } catch (e) {
      ROLE = "";
    }
    if (ROLE) localStorage.setItem("pp_role", ROLE);
    else localStorage.removeItem("pp_role");
    applyRole();
  }

  /* ---------------- 反馈 ---------------- */
  function showErr(msg) {
    const el = document.getElementById("err");
    if (!el) return;
    if (!msg) { el.classList.remove("on"); return; }
    const hint = KEY
      ? "（当前密钥前 8 位：" + esc(KEY.slice(0, 8)) + "…）"
      : "（点右上角「密钥」输入与后端一致的 SECRET_KEY）";
    el.innerHTML = "<b>接口异常</b> — " + esc(msg) + "　" + hint;
    el.classList.add("on");
  }
  function setLive(ok, text) {
    const live = document.getElementById("live");
    if (!live) return;
    live.classList.toggle("bad", !ok);
    const t = document.getElementById("livetxt");
    if (t) t.textContent = text;
  }

  /* ---------------- 侧栏 / 顶栏 ---------------- */
  function highlightNav() {
    const cur = document.body.getAttribute("data-page");
    document.querySelectorAll(".snav .sitem").forEach(a => {
      a.classList.toggle("active", a.getAttribute("data-page") === cur);
    });
  }
  function setCounts(o) {
    o = o || {};
    if (o.devices !== undefined) { const n = document.getElementById("nav-dev"); if (n) n.textContent = o.devices; }
    if (o.orders !== undefined) { const n = document.getElementById("nav-ord"); if (n) n.textContent = o.orders; }
    if (o.payments !== undefined) { const n = document.getElementById("nav-pay"); if (n) n.textContent = o.payments; }
    if (o.alerts !== undefined) { const n = document.getElementById("nav-al"); if (n) n.textContent = o.alerts; }
    if (o.library !== undefined) { const n = document.getElementById("nav-lib"); if (n) n.textContent = o.library; }
  }

  function initClock() {
    const tick = () => {
      const n = new Date();
      const el = document.getElementById("clock");
      if (el) el.textContent =
        String(n.getHours()).padStart(2, "0") + ":" + String(n.getMinutes()).padStart(2, "0") + ":" + String(n.getSeconds()).padStart(2, "0");
    };
    tick();
    setInterval(tick, 1000);
  }

  function initModal() {
    const mask = document.getElementById("mask");
    if (!mask) return;
    const keyHint = () => {
      const h = document.getElementById("key-hint");
      if (h) h.textContent = KEY ? (KEY.slice(0, 8) + "…") : "未配置";
    };
    const tip = document.querySelector("#mask .mb p");
    if (tip) tip.textContent =
      "输入商家密钥或运维密钥（两者都由后端环境变量配置）。切换角色：把密钥替换成另一个再保存。运维只能查看设备、告警和执行测试页/重启，不能改价格、看账单。";
    const btnKey = document.getElementById("btn-key");
    if (btnKey) btnKey.onclick = () => {
      document.getElementById("key-input").value = KEY;
      keyHint();
      mask.classList.add("on");
      setTimeout(() => document.getElementById("key-input").focus(), 30);
    };
    const cancel = document.getElementById("key-cancel");
    if (cancel) cancel.onclick = () => mask.classList.remove("on");
    const save = document.getElementById("key-save");
    if (save) save.onclick = () => {
      const v = document.getElementById("key-input").value.trim();
      if (v) setKey(v);
      mask.classList.remove("on");
      refreshRole().finally(() => { if (refreshFn) refreshFn(); });
    };
    mask.addEventListener("click", ev => { if (ev.target === mask) mask.classList.remove("on"); });
  }

  /* ---------------- 启动 ---------------- */
  function boot(loadFn) {
    refreshFn = loadFn;
    highlightNav();
    initClock();
    initModal();
    const host = document.getElementById("f-host");
    if (host) host.textContent = location.host || "本地文件";
    const btn = document.getElementById("btn-refresh");
    if (btn) btn.onclick = () => loadFn();
    refreshRole().finally(() => loadFn());
    setInterval(loadFn, 5000);
  }

  return {
    OFFLINE_SECONDS, PAPER_LABEL, PAPER_LEVEL,
    fmtAge, ageOf, esc, onlineInfo,
    api, post, del, postFull,
    getKey, setKey, getRole, refreshRole,
    showErr, setLive, setCounts, boot,
  };
})();
