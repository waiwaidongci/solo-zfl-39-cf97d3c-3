/* 纸浆原料验收入库与结算系统 —— 前端逻辑（原生 JS，零依赖）
 * 网页端做与后端一致的校验，最终以后端为准。
 */
"use strict";

const state = {
  me: null, users: [], suppliers: [], contracts: [], tiers: [],
  prepayments: [], receipts: [], settlements: [],
};

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

const STATUS_ZH = {
  weighed: "已验收", settled: "已结算",
  pending_review: "待复核", approved: "待付款", paid: "已付款", reversed: "已冲正",
};
const ROLE_ZH = { clerk: "过磅员", finance: "财务", manager: "主管" };

// --------------------------------------------------------------- 工具
function yuan(cents) {
  if (cents === null || cents === undefined) return "-";
  const neg = cents < 0;
  const v = Math.abs(Math.round(cents));
  return (neg ? "-" : "") + (v / 100).toFixed(2);
}
function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g,
    c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function toast(msg, isError) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast" + (isError ? " error" : "");
  setTimeout(() => t.classList.add("hidden"), 3500);
  t.classList.remove("hidden");
}
async function api(path, options = {}) {
  const opts = options.body
    ? { ...options, headers: { "Content-Type": "application/json" } }
    : options;
  const res = await fetch(path, opts);
  let data = {};
  try { data = await res.json(); } catch (_) { /* 非 JSON */ }
  if (!res.ok) {
    throw Object.assign(new Error(data.message || `请求失败（${res.status}）`),
      { code: data.error, status: res.status });
  }
  return data;
}
function markInvalid(input, bad) {
  if (input) input.classList.toggle("invalid", !!bad);
}
function decimalOk(raw, { min, max, maxDp = 3, positive = false } = {}) {
  const s = String(raw ?? "").trim();
  if (!/^\d+(\.\d+)?$/.test(s)) return false;
  const v = Number(s);
  if (!Number.isFinite(v)) return false;
  if ((s.split(".")[1] || "").length > maxDp) return false;
  if (positive && v <= 0) return false;
  if (min !== undefined && v < min) return false;
  if (max !== undefined && v > max) return false;
  return true;
}
function todayISO() {
  const d = new Date();
  return d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0")
    + "-" + String(d.getDate()).padStart(2, "0");
}
function nowLocalInput() {
  const d = new Date();
  const p = n => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}`;
}

// --------------------------------------------------------------- 登录 / 页签
async function refreshMe() {
  state.me = await api("/api/me").catch(() => null);
  renderLogin();
}
function renderLogin() {
  const box = $("#loginBox");
  if (state.me && state.me.id) {
    box.innerHTML = `<span>当前操作人：<b>${esc(state.me.display_name)}</b>
      （${ROLE_ZH[state.me.role] || state.me.role}）</span>
      <button class="secondary mini" id="logoutBtn">切换用户</button>`;
    $("#logoutBtn").onclick = async () => {
      await api("/api/logout", { method: "POST" });
      state.me = null;
      renderLogin();
      renderAll();
    };
  } else {
    box.innerHTML = `<select id="loginUser"><option value="">选择操作人…</option>
      ${state.users.map(u => `<option value="${u.username}">${esc(u.display_name)}
      （${ROLE_ZH[u.role] || u.role}）</option>`).join("")}</select>
      <button id="loginBtn">登录</button>`;
    $("#loginBtn").onclick = async () => {
      const username = $("#loginUser").value;
      if (!username) return toast("请选择操作人", true);
      state.me = await api("/api/login", {
        method: "POST", body: JSON.stringify({ username }),
      });
      renderLogin();
      renderAll();
      toast(`已登录：${state.me.display_name}`);
    };
  }
}
$$("#tabs button").forEach(btn => {
  btn.onclick = () => {
    $$("#tabs button").forEach(b => b.classList.toggle("active", b === btn));
    $$(".tab").forEach(t => t.classList.toggle("active", t.id === "tab-" + btn.dataset.tab));
  };
});

// --------------------------------------------------------------- 数据加载
async function loadAll() {
  const [users, suppliers, contracts, tiers, prepayments, receipts, settlements] =
    await Promise.all([
      api("/api/users"), api("/api/suppliers"), api("/api/contracts"),
      api("/api/tiers"), api("/api/prepayments"),
      api("/api/receipts"), api("/api/settlements"),
    ]);
  Object.assign(state, { users, suppliers, contracts, tiers, prepayments,
    receipts, settlements });
}
async function reload(part) {
  if (part === "receipts") state.receipts = await api("/api/receipts");
  else if (part === "settlements") state.settlements = await api("/api/settlements");
  else if (part === "prepayments") state.prepayments = await api("/api/prepayments");
  else if (part === "suppliers") {
    state.suppliers = await api("/api/suppliers");
    state.contracts = await api("/api/contracts");
  } else {
    await loadAll();
  }
  renderAll();
}
function supplierName(id) {
  const s = state.suppliers.find(x => x.id === id);
  return s ? s.name : "#" + id;
}

// --------------------------------------------------------------- 总览
function renderDash(summary) {
  $("#dashCards").innerHTML = `
    <div class="card-stat"><div class="k">验收车次数</div><div class="v">${summary.receipt_count}</div></div>
    <div class="card-stat"><div class="k">待复核结算单</div><div class="v">${summary.pending_review}</div></div>
    <div class="card-stat"><div class="k">待付款金额（元）</div><div class="v">${yuan(summary.payable_cents)}</div></div>
    <div class="card-stat"><div class="k">已付款金额（元）</div><div class="v">${yuan(summary.paid_cents)}</div></div>
    <div class="card-stat"><div class="k">预付款总余额（元）</div><div class="v">${yuan(summary.prepay_remaining_cents)}</div></div>`;
}

// --------------------------------------------------------------- 主数据
function fillSupplierSelects() {
  const opts = state.suppliers
    .map(s => `<option value="${s.id}">${esc(s.code)} · ${esc(s.name)}</option>`).join("");
  $$("select[name='supplier_id']").forEach(sel => {
    const cur = sel.value;
    sel.innerHTML = opts;
    if (cur) sel.value = cur;
  });
}
function renderMasters() {
  $("#supplierTable").innerHTML = `<tr><th>编码</th><th>名称</th><th>预付余额(元)</th></tr>` +
    state.suppliers.map(s => `<tr><td>${esc(s.code)}</td><td>${esc(s.name)}</td>
      <td>${yuan(s.remaining_cents)}</td></tr>`).join("");

  $("#contractTable").innerHTML =
    `<tr><th>供应商</th><th>等级</th><th>单价(元/kg)</th><th>生效日期</th><th>状态</th><th>备注</th></tr>` +
    state.contracts.map(c => {
      const future = c.effective_date > todayISO();
      return `<tr><td>${esc(c.supplier_name)}</td><td>${c.grade}</td>
        <td>${(c.price_cents_per_kg / 100).toFixed(2)}</td>
        <td>${c.effective_date}</td>
        <td>${future ? '<span class="pill reversed">未生效</span>'
          : '<span class="pill approved">已生效</span>'}</td>
        <td>${esc(c.note)}</td></tr>`;
    }).join("");

  const w = state.tiers.filter(t => t.measure === "water");
  const i = state.tiers.filter(t => t.measure === "impurity");
  const head = t => `<tr><th>区间(%)</th><th>扣点</th><th>说明</th></tr>${t}`;
  $("#waterTierTable").innerHTML = head(w.map(t =>
    `<tr><td>&gt; ${t.min_value} 且 ≤ ${t.max_value}</td><td>扣 <b>${t.deduction_pct}%</b></td>
     <td class="hint">${esc(t.note)}</td></tr>`).join(""));
  $("#impurityTierTable").innerHTML = head(i.map(t =>
    `<tr><td>&gt; ${t.min_value} 且 ≤ ${t.max_value}</td><td>扣 <b>${t.deduction_pct}%</b></td>
     <td class="hint">${esc(t.note)}</td></tr>`).join(""));
}

$("#supplierForm").onsubmit = async e => {
  e.preventDefault();
  const f = e.target;
  try {
    await api("/api/suppliers", { method: "POST",
      body: JSON.stringify(Object.fromEntries(new FormData(f))) });
    f.reset(); toast("供应商已新增"); await reload("suppliers");
  } catch (err) { toast(err.message, true); }
};
$("#contractForm").onsubmit = async e => {
  e.preventDefault();
  const f = e.target;
  const price = f.elements.price_yuan.value;
  if (!decimalOk(price, { positive: true, maxDp: 2 }))
    return toast("单价必须是正数且最多两位小数", true);
  try {
    await api("/api/contracts", { method: "POST",
      body: JSON.stringify(Object.fromEntries(new FormData(f))) });
    f.reset(); toast("合同段已登记（未来日期的价格在生效前不会被使用）");
    await reload("suppliers");
  } catch (err) { toast(err.message, true); }
};

// --------------------------------------------------------------- 预付款
function renderPrepay() {
  $("#prepayTable").innerHTML =
    `<tr><th>单号</th><th>供应商</th><th>到账时间</th><th>原额(元)</th><th>剩余(元)</th><th>备注</th></tr>` +
    state.prepayments.map(p => `<tr><td>${esc(p.code)}</td><td>${esc(p.supplier_name)}</td>
      <td>${esc(p.received_at)}</td><td>${yuan(p.amount_cents)}</td>
      <td><b>${yuan(p.remaining_cents)}</b></td><td>${esc(p.note)}</td></tr>`).join("");
}
$("#prepayForm").onsubmit = async e => {
  e.preventDefault();
  const f = e.target;
  const amount = f.elements.amount_yuan.value;
  if (!decimalOk(amount, { positive: true, maxDp: 2 }))
    return toast("金额必须是正数且最多两位小数", true);
  if (!f.elements.received_at.value) return toast("请选择到账时间", true);
  const body = Object.fromEntries(new FormData(f));
  body.received_at = body.received_at.replace("T", " ") + ":00";
  try {
    await api("/api/prepayments", { method: "POST", body: JSON.stringify(body) });
    f.reset(); f.elements.received_at.value = nowLocalInput();
    toast("预付款已登记"); await reload("prepayments");
  } catch (err) { toast(err.message, true); }
};

// --------------------------------------------------------------- 验收：前端试算
function pickTier(measure, value) {
  const v = Number(value);
  const ts = state.tiers.filter(t => t.measure === measure)
    .sort((a, b) => Number(a.max_value) - Number(b.max_value));
  return ts.find(t => v > Number(t.min_value) && v <= Number(t.max_value)) || ts[0];
}
function previewReceipt() {
  const f = $("#receiptForm");
  const gross = f.elements.gross_kg.value, tare = f.elements.tare_kg.value;
  const water = f.elements.water_pct.value, impurity = f.elements.impurity_pct.value;
  const errs = [];
  const checks = [
    [f.elements.gross_kg, decimalOk(gross, { positive: true }), "毛重须为正数（≤3位小数）"],
    [f.elements.tare_kg, decimalOk(tare, { min: 0, maxDp: 3 }), "皮重须为非负数"],
    [f.elements.water_pct, decimalOk(water, { min: 0, max: 100, maxDp: 2 }), "含水须在 0~100"],
    [f.elements.impurity_pct, decimalOk(impurity, { min: 0, max: 100, maxDp: 2 }), "杂质须在 0~100"],
  ];
  checks.forEach(([input, ok, msg]) => { markInvalid(input, !ok); if (!ok) errs.push(msg); });
  if (Number(tare) >= Number(gross)) errs.push("皮重必须小于毛重");
  const box = $("#receiptPreviewBox");
  if (errs.length) { box.classList.remove("hidden"); box.innerHTML = errs.map(esc).join("<br>"); return; }

  const net = Number(gross) - Number(tare);
  const wt = pickTier("water", water), it = pickTier("impurity", impurity);
  const pct = Number(wt.deduction_pct) + Number(it.deduction_pct);
  const deducted = net * pct / 100;
  const settled = net - deducted;
  const supplierId = Number(f.elements.supplier_id.value);
  const grade = f.elements.grade.value;
  const wd = f.elements.weigh_date.value;
  const c = state.contracts
    .filter(x => x.supplier_id === supplierId && x.grade === grade && x.effective_date <= wd)
    .sort((a, b) => (a.effective_date < b.effective_date ? 1 : -1))[0];
  const future = state.contracts
    .filter(x => x.supplier_id === supplierId && x.grade === grade && x.effective_date > wd)
    .sort((a, b) => a.effective_date < b.effective_date ? -1 : 1)[0];
  let priceLine = c
    ? `适用合同段：${c.effective_date} 起 <b>${(c.price_cents_per_kg / 100).toFixed(2)} 元/kg</b>
       （${esc(c.note) || "已生效"}），试算金额 <b>${yuan(Math.round(settled * c.price_cents_per_kg))} 元</b>`
    : `⚠ 过磅日 ${esc(wd)} 无已生效合同价，无法计价` +
      (future ? `；最早将于 ${future.effective_date} 生效，<b>未来价格不能提前使用</b>` : "");
  box.classList.remove("hidden");
  box.innerHTML = `
    净重 = ${gross} − ${tare} = <b>${net.toFixed(3)} kg</b><br>
    含水 ${water}% → ${esc(wt.note)}（扣 ${wt.deduction_pct}%）；
    杂质 ${impurity}% → ${esc(it.note)}（扣 ${it.deduction_pct}%）<br>
    扣量合计 = 净重 × ${pct}% = <b>${deducted.toFixed(3)} kg</b>；
    结算重量 = <b>${settled.toFixed(3)} kg</b><br>${priceLine}`;
}
function renderReceipts() {
  $("#receiptTable").innerHTML =
    `<tr><th>单号</th><th>供应商</th><th>过磅日</th><th>等级</th><th>毛重</th><th>皮重</th>
     <th>净重</th><th>含水/杂质</th><th>扣量</th><th>结算重</th><th>状态</th></tr>` +
    state.receipts.map(r => `<tr><td>${esc(r.code)}</td><td>${esc(r.supplier_name)}</td>
      <td>${r.weigh_date}</td><td>${r.grade}</td><td>${r.gross_kg}</td><td>${r.tare_kg}</td>
      <td><b>${r.net_kg}</b></td><td>${r.water_pct}% / ${r.impurity_pct}%</td>
      <td>${r.deducted_kg}</td><td><b>${r.settled_kg}</b></td>
      <td><span class="pill ${r.settlement_status || r.status}">
      ${STATUS_ZH[r.settlement_status] || STATUS_ZH[r.status] || r.status}</span></td></tr>`)
      .join("");
}
$("#receiptPreview").onclick = previewReceipt;
$("#receiptForm").onsubmit = async e => {
  e.preventDefault();
  const f = e.target;
  const ok = decimalOk(f.elements.gross_kg.value, { positive: true })
    && decimalOk(f.elements.tare_kg.value, { min: 0, maxDp: 3 })
    && Number(f.elements.tare_kg.value) < Number(f.elements.gross_kg.value)
    && decimalOk(f.elements.water_pct.value, { min: 0, max: 100, maxDp: 2 })
    && decimalOk(f.elements.impurity_pct.value, { min: 0, max: 100, maxDp: 2 })
    && f.elements.weigh_date.value && f.elements.weigh_date.value <= todayISO();
  if (!ok) { previewReceipt(); return toast("请检查录入项（皮重须小于毛重、日期不可晚于今天）", true); }
  try {
    await api("/api/receipts", { method: "POST",
      body: JSON.stringify(Object.fromEntries(new FormData(f))) });
    f.reset(); setReceiptDefaults();
    $("#receiptPreviewBox").classList.add("hidden");
    toast("验收登记成功，净重与扣量已计算");
    await reload("receipts");
  } catch (err) { toast(err.message, true); }
};
function setReceiptDefaults() {
  const f = $("#receiptForm");
  f.elements.weigh_date.value = todayISO();
}

// --------------------------------------------------------------- 结算 / 复核 / 付款 / 冲正
function canReview(st) {
  if (!state.me || st.status !== "pending_review") return false;
  if (state.me.id === st.created_by) return false;
  if ((st.reviews || []).some(r => r.reviewer_id === state.me.id)) return false;
  return (st.reviews || []).length < 2;
}
function settleCard(st) {
  const d = st.detail || {};
  const overThreshold = d.need_review;
  const finance = state.me && (state.me.role === "finance" || state.me.role === "manager");
  const reviews = (st.reviews || []).map(r =>
    `<li>第${r.seq}审：${esc(r.reviewer_name)} · ${r.created_at}${r.note ? " · " + esc(r.note) : ""}</li>`)
    .join("") || "<li class='hint'>暂无复核记录</li>";
  let actions = "";
  if (st.status === "pending_review") {
    actions = `<button class="mini" data-action="review" data-id="${st.id}"
        ${canReview(st) ? "" : "disabled"}
        title="${state.me && state.me.id === st.created_by ? "制单人不能复核本人单据" : "需两名不同人员"}">
        复核通过（当前人，第 ${(st.reviews || []).length + 1} 审）</button>`;
  } else if (st.status === "approved") {
    actions = `<button class="mini" data-action="pay" data-id="${st.id}"
        ${finance ? "" : "disabled"} title="仅财务/主管可付款">预付款抵扣付款</button>`;
  } else if (st.status === "paid") {
    actions = `<button class="mini danger" data-action="reverse" data-id="${st.id}"
        ${finance ? "" : "disabled"} title="付款不可改写，只能红冲">冲正此笔付款</button>`;
  }
  const allocs = (st.payment && st.payment.allocations || []).map(a =>
    `${esc(a.prepayment_code)} 抵扣 ${yuan(a.amount_cents)} 元`).join("；");
  return `<div class="settle-card">
    <div class="settle-head">
      <h3>${esc(st.code)} · ${esc(st.receipt_code)} · ${esc(st.supplier_name)}
        <span class="pill ${st.status}">${STATUS_ZH[st.status]}</span></h3>
      <div>金额 <b style="font-size:17px">${yuan(st.amount_cents)} 元</b>
        ${overThreshold ? '<span class="pill pending_review">超5万·须双人复核</span>'
          : '<span class="pill approved">未超限·免复核</span>'}</div>
    </div>
    <div class="kv">
      <div><span>净重 (kg)</span><b>${esc(d.net_kg)}</b></div>
      <div><span>含水 / 杂质</span><b>${esc(d.water_pct)}% / ${esc(d.impurity_pct)}%</b></div>
      <div><span>扣量 (kg)</span><b>${(Number(d.net_kg) - Number(d.settled_kg)).toFixed(3)}</b></div>
      <div><span>结算重量 (kg)</span><b>${esc(d.settled_kg)}</b></div>
      <div><span>适用单价 (元/kg)</span><b>${(st.price_cents_per_kg / 100).toFixed(2)}</b></div>
      <div><span>合同生效日</span><b>${esc(d.contract_effective_date)}</b></div>
      <div><span>制单人</span><b>${esc(st.creator_name)}</b></div>
      <div><span>创建时间</span><b>${esc(st.created_at)}</b></div>
    </div>
    <ul>${reviews}</ul>
    ${st.payment ? `<div class="alloc-list">付款单 ${esc(st.payment.code)} ·
      ${st.payment.status === "reversed"
        ? `已于 ${esc(st.payment.reversed_at)} 冲正（${esc(st.payment.reverse_reason)}），预付款余额已恢复`
        : "FIFO 抵扣：" + esc(allocs)}</div>` : ""}
    <div>${actions}</div>
  </div>`;
}
function renderSettlements() {
  const f = $("#settleFilter").value;
  const list = state.settlements.filter(s => !f || s.status === f);
  // 给「已验收未结算」的验收单提供计价入口
  const unsettled = state.receipts.filter(r => !r.settlement_code);
  $("#settleList").innerHTML =
    unsettled.map(r => `<div class="settle-card">
      <div class="settle-head"><h3>${esc(r.code)} · ${esc(r.supplier_name)} · ${r.grade}级
        <span class="pill weighed">已验收·待计价</span></h3></div>
      <div class="kv">
        <div><span>过磅日</span><b>${r.weigh_date}</b></div>
        <div><span>净重 (kg)</span><b>${r.net_kg}</b></div>
        <div><span>结算重量 (kg)</span><b>${r.settled_kg}</b></div>
      </div>
      <button class="mini" data-action="settle" data-id="${r.id}">按生效合同计价生成结算单</button>
    </div>`).join("") +
    list.map(settleCard).join("") || "<p class='hint'>暂无结算单</p>";

  $$("#settleList [data-action]").forEach(btn => {
    btn.onclick = async () => {
      const id = btn.dataset.id, action = btn.dataset.action;
      try {
        if (action === "settle") {
          await api(`/api/receipts/${id}/settle`, { method: "POST" });
          toast("已按过磅日生效合同段计价");
        } else if (action === "review") {
          const note = prompt("复核备注（可留空）：", "") ?? "";
          await api(`/api/settlements/${id}/review`, {
            method: "POST", body: JSON.stringify({ note }),
          });
          toast("复核已记录（须两名不同人员）");
        } else if (action === "pay") {
          await api(`/api/settlements/${id}/pay`, { method: "POST" });
          toast("付款成功：预付款已按到账先后 FIFO 全额抵扣");
        } else if (action === "reverse") {
          const reason = prompt("付款不可改写，请填写冲正原因：", "");
          if (!reason || !reason.trim()) return;
          await api(`/api/settlements/${id}/reverse`, {
            method: "POST", body: JSON.stringify({ reason }),
          });
          toast("已冲正，原付款保留留痕，预付款余额已恢复");
        }
        await reload();
      } catch (err) { toast(err.message, true); }
    };
  });
}
$("#settleFilter").onchange = renderSettlements;
$("#settleRefresh").onclick = () => reload();

// --------------------------------------------------------------- 汇总 + 全渲染
async function renderAll() {
  fillSupplierSelects();
  renderMasters();
  renderPrepay();
  renderReceipts();
  renderSettlements();
  try { renderDash(await api("/api/summary")); } catch (_) {}
}
async function init() {
  setReceiptDefaults();
  const pf = $("#prepayForm");
  pf.elements.received_at.value = nowLocalInput();
  await refreshMe();
  await loadAll();
  fillSupplierSelects();
  renderLogin();
  await renderAll();
}
init().catch(e => toast("初始化失败：" + e.message, true));
