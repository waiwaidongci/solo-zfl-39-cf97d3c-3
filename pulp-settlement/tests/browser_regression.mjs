// 浏览器回归：jsdom 通过 HTTP 加载真实页面并执行 static/app.js（非 mock 脚本）。
// 覆盖：无初始化错误 → 登录 → 网页校验 → 录入 → 计价 → 双人复核 → FIFO 付款
//       → 冲正 → 服务重启后页面与状态恢复。
// 运行：node tests/browser_regression.mjs
import { JSDOM, VirtualConsole } from "jsdom";
import { spawn } from "node:child_process";
import net from "node:net";
import os from "node:os";
import path from "node:path";
import fs from "node:fs";

const ROOT = path.resolve(path.dirname(new URL(import.meta.url).pathname), "..");
const PORT = Number(process.env.PULP_BROWSER_PORT || 18099);
const BASE = `http://127.0.0.1:${PORT}`;
const dbPath = path.join(os.tmpdir(), `pulp-browser-${process.pid}.db`);

let failures = 0;
function check(cond, msg) {
  if (cond) { console.log("  ✓", msg); }
  else { failures++; console.error("  ✗", msg); }
}
function waitFor(fn, { timeout = 10000, label = "condition" } = {}) {
  return new Promise((resolve, reject) => {
    const t0 = Date.now();
    const tick = () => {
      let v;
      try { v = fn(); } catch (e) { v = null; }
      if (v) return resolve(v);
      if (Date.now() - t0 > timeout) return reject(new Error("等待超时: " + label));
      setTimeout(tick, 80);
    };
    tick();
  });
}

function waitPort(port) {
  return new Promise((resolve, reject) => {
    const t0 = Date.now();
    const attempt = () => {
      const sock = net.connect(port, "127.0.0.1");
      sock.on("connect", () => { sock.end(); resolve(); });
      sock.on("error", () => {
        if (Date.now() - t0 > 15000) reject(new Error("服务启动超时"));
        else setTimeout(attempt, 150);
      });
    };
    attempt();
  });
}

function startServer() {
  const env = { ...process.env, PULP_DB_PATH: dbPath, PULP_PORT: String(PORT),
    PULP_HOST: "127.0.0.1" };
  // 独立进程组，脚本异常退出时可整组回收，不留孤儿服务
  const proc = spawn("python3", [path.join(ROOT, "run.py")],
    { cwd: ROOT, env, detached: true });
  liveServers.add(proc);
  proc.on("exit", () => liveServers.delete(proc));
  proc.stdout.on("data", d => process.env.PULP_VERBOSE && process.stdout.write(d));
  proc.stderr.on("data", d => process.stderr.write(d));
  return proc;
}
const liveServers = new Set();
function killAllServers() {
  for (const proc of liveServers) {
    try { process.kill(-proc.pid, "SIGKILL"); } catch {}
  }
  liveServers.clear();
}
process.on("exit", killAllServers);
process.on("SIGTERM", () => { killAllServers(); process.exit(143); });
process.on("SIGINT", () => { killAllServers(); process.exit(130); });
function stopServer(proc) {
  return new Promise(resolve => {
    if (proc.exitCode !== null) return resolve();
    proc.on("exit", resolve);
    try { process.kill(-proc.pid, "SIGINT"); } catch {}
    setTimeout(() => {
      try { process.kill(-proc.pid, "SIGKILL"); } catch {}
      resolve();
    }, 4000);
  });
}

// 每个页面对应一个真实 jsdom 窗口；fetch 桥接到 Node 网络栈并手工透传会话 Cookie。
async function openPage() {
  const consoleErrors = [];
  const vc = new VirtualConsole();
  vc.on("jsdomError", e => consoleErrors.push("jsdomError: " + (e.detail?.stack || e.message)));
  const dom = await JSDOM.fromURL(BASE + "/", {
    runScripts: "dangerously",
    resources: "usable",
    pretendToBeVisual: true,
    virtualConsole: vc,
    beforeParse(window) {
      window.console.error = (...a) => consoleErrors.push("console.error: " + a.join(" "));
      window.prompt = () => null;
      window.fetch = async (input, init = {}) => {
        const url = new URL(input, window.location.href);
        const headers = new Headers(init.headers || {});
        if (window.document.cookie) headers.set("cookie", window.document.cookie);
        const res = await globalThis.fetch(url, { ...init, headers });
        const sc = res.headers.get("set-cookie");
        if (sc) {
          const pair = sc.split(";")[0];
          const eq = pair.indexOf("=");
          const name = pair.slice(0, eq), val = pair.slice(eq + 1);
          window.document.cookie = val
            ? `${name}=${val}; path=/`
            : `${name}=; path=/; expires=Thu, 01 Jan 1970 00:00:00 GMT`;
        }
        return res;
      };
    },
  });
  const w = dom.window;
  const d = w.document;
  const clickTab = name => d.querySelector(`#tabs [data-tab="${name}"]`).click();
  const setVal = (el, val) => {
    el.value = val;
    el.dispatchEvent(new w.Event("input", { bubbles: true }));
    el.dispatchEvent(new w.Event("change", { bubbles: true }));
  };
  const fillForm = (form, vals) => {
    for (const [k, v] of Object.entries(vals)) setVal(form.elements[k], v);
  };
  const login = async username => {
    setVal(d.querySelector("#loginUser"), username);
    d.querySelector("#loginBtn").click();
    await waitFor(() => d.querySelector("#logoutBtn"), { label: `登录 ${username}` });
  };
  const logout = async () => {
    d.querySelector("#logoutBtn").click();
    await waitFor(() => d.querySelector("#loginUser"), { label: "退出登录" });
  };
  return { dom, window: w, document: d, consoleErrors, clickTab, setVal,
           fillForm, login, logout };
}

function isoDate(deltaDays) {
  const d = new Date();
  d.setDate(d.getDate() + deltaDays);
  return d.toISOString().slice(0, 10);
}

async function run() {
  for (const ext of ["", "-wal", "-shm"]) { try { fs.rmSync(dbPath + ext); } catch {} }
  let server = startServer();
  await waitPort(PORT);

  // ============ 1. 首屏初始化：无错误、数据已加载 ============
  console.log("[1] 页面初始化与数据加载");
  let page = await openPage();
  await waitFor(() => page.document.querySelectorAll("#loginUser option").length === 5,
    { label: "首屏用户下拉加载" });
  check(page.consoleErrors.length === 0,
    "控制台无初始化错误（实际: " + JSON.stringify(page.consoleErrors) + "）");
  const d = page.document;
  check(d.querySelectorAll("#loginUser option").length === 5, "登录下拉含全部 4 名用户 + 占位项");
  page.clickTab("receive");
  check(d.querySelectorAll('#receiptForm select[name="supplier_id"] option').length >= 2,
    "验收表单供应商选项已填充");
  page.clickTab("masters");
  check(d.querySelectorAll("#supplierTable tr").length >= 3, "供应商表格已渲染（含表头）");
  check(d.querySelectorAll("#contractTable tr").length >= 2, "合同分段价已渲染");
  page.clickTab("dash");
  const statVals = [...d.querySelectorAll("#dashCards .card-stat .v")].map(e => e.textContent);
  check(statVals[0] === "0" && statVals.includes("105000.00"),
    "总览卡片已渲染（车次数 0、预付总余额 105000.00 元），实际: " + statVals.join("|"));
  check(d.querySelector("#toast").classList.contains("hidden"), "没有弹出初始化失败提示");

  // ============ 2. 登录 + 网页端校验 ============
  console.log("[2] 登录与网页校验");
  await page.login("u1"); // 张三 过磅员
  check(d.querySelector("#loginBox").textContent.includes("张三"), "页面显示当前操作人张三");
  page.clickTab("receive");
  page.fillForm(d.querySelector("#receiptForm"), {
    code: "RC-BR-1", supplier_id: "1", weigh_date: isoDate(-5), grade: "A",
    gross_kg: "1000", tare_kg: "2000", water_pct: "13.5", impurity_pct: "2.2",
  });
  d.querySelector("#receiptForm").dispatchEvent(new page.window.Event("submit",
    { bubbles: true, cancelable: true }));
  await new Promise(r => setTimeout(r, 300));
  check(!d.querySelector("#toast").classList.contains("hidden")
    && d.querySelector("#toast").classList.contains("error"),
    "皮重大于毛重时网页拦截并提示错误");
  check(d.querySelectorAll("#receiptTable tr").length === 1, "非法录入没有产生任何记录");

  // ============ 3. 录入（净重/扣量由后端计算并回显） ============
  console.log("[3] 验收入库");
  page.fillForm(d.querySelector("#receiptForm"), {
    code: "RC-BR-1", supplier_id: "1", weigh_date: isoDate(-5), grade: "A",
    gross_kg: "26000", tare_kg: "9000", water_pct: "13.5", impurity_pct: "2.2",
  });
  d.querySelector("#receiptForm").dispatchEvent(new page.window.Event("submit",
    { bubbles: true, cancelable: true }));
  const row = await waitFor(() => {
    const tr = [...d.querySelectorAll("#receiptTable tr")]
      .find(t => t.textContent.includes("RC-BR-1"));
    return tr && tr.textContent.includes("17000.000") ? tr : null;
  }, { label: "验收记录回显" });
  check(row.textContent.includes("510.000") && row.textContent.includes("16490.000"),
    "回显净重 17000.000、扣量 510.000、结算重 16490.000");

  // ============ 4. 计价 ============
  console.log("[4] 按生效合同段计价");
  page.clickTab("settle");
  const settleBtn = await waitFor(() =>
    [...d.querySelectorAll('#settleList [data-action="settle"]')]
      .find(b => b.closest(".settle-card").textContent.includes("RC-BR-1")),
    { label: "计价按钮出现" });
  settleBtn.click();
  const card = await waitFor(() => {
    const c = [...d.querySelectorAll(".settle-card")]
      .find(x => x.textContent.includes("RC-BR-1") && x.textContent.includes("JS"));
    return c && c.textContent.includes("待复核") ? c : null;
  }, { label: "结算单生成并待复核" });
  check(card.textContent.includes("92344.00"), "结算金额 92344.00 元（16490 × 5.60）");
  check(card.textContent.includes("5.60"), "使用当前生效价 5.60 元/kg（未提前用未来价 6.20）");
  check(card.textContent.includes("超5万·须双人复核"), "超限单据标记须双人复核");

  // ============ 5. 双人复核（制单人被拒，两名不同人员依次复核） ============
  console.log("[5] 双人复核");
  const reviewAsU1 = d.querySelector('#settleList [data-action="review"]');
  check(reviewAsU1.disabled, "制单人张三的复核按钮禁用（不能自审）");
  await page.logout();
  await page.login("u2"); // 李四 财务，第一审
  page.window.prompt = () => "浏览器回归-一审";
  d.querySelector('#settleList [data-action="review"]').click();
  await waitFor(() => d.querySelector("#settleList").textContent.includes("第1审：李四"),
    { label: "第一审落账" });
  check(d.querySelector("#settleList").textContent.includes("待复核"), "一审后仍待第二审");
  let reviewBtn2;
  await waitFor(() => {
    reviewBtn2 = d.querySelector('#settleList [data-action="review"]');
    return reviewBtn2;
  }, { label: "李四一审后复核按钮重渲染" });
  check(reviewBtn2.disabled, "李四不能进行第二审（同人不可重复复核，按钮禁用）");

  await page.logout();
  await page.login("u3"); // 王五 主管，第二审
  page.window.prompt = () => "浏览器回归-二审";
  await waitFor(() => d.querySelector('#settleList [data-action="review"]'),
    { label: "王五可复核按钮出现" });
  d.querySelector('#settleList [data-action="review"]').click();
  await waitFor(() => d.querySelector("#settleList").textContent.includes("第2审：王五"),
    { label: "第二审落账" });
  check(d.querySelector("#settleList").textContent.includes("待付款"), "两名不同人员复核完成 → 待付款");

  // ============ 6. FIFO 付款 ============
  console.log("[6] 预付款 FIFO 抵扣付款");
  await page.logout();
  await page.login("u2"); // 财务执行付款
  await waitFor(() => d.querySelector('#settleList [data-action="pay"]'),
    { label: "付款按钮出现" });
  d.querySelector('#settleList [data-action="pay"]').click();
  await waitFor(() => {
    const c = [...d.querySelectorAll(".settle-card")].find(x => x.textContent.includes("RC-BR-1"));
    return c && c.textContent.includes("已付款") ? c : null;
  }, { label: "付款成功状态" });
  const paidCard = [...d.querySelectorAll(".settle-card")]
    .find(x => x.textContent.includes("RC-BR-1"));
  check(paidCard.textContent.includes("P001 抵扣 40000.00"),
    "FIFO 第一笔 P001 先抵 40000.00 元");
  check(paidCard.textContent.includes("P002 抵扣 52344.00"),
    "FIFO 第二笔 P002 续抵 52344.00 元");
  check(!d.querySelector('#settleList [data-action="pay"]'), "付款后不再显示付款按钮（不可重复支付）");
  page.clickTab("prepay");
  const preRow = code => [...d.querySelectorAll("#prepayTable tr")]
    .find(t => t.children[0]?.textContent === code);
  check(preRow("P001").children[4].textContent === "0.00", "P001 剩余 0.00");
  check(preRow("P002").children[4].textContent === "7656.00", "P002 剩余 7656.00");

  // ============ 7. 红冲 ============
  console.log("[7] 付款红冲");
  page.clickTab("settle");
  page.window.prompt = () => "浏览器回归-冲正原因";
  d.querySelector('#settleList [data-action="reverse"]').click();
  await waitFor(() => {
    const c = [...d.querySelectorAll(".settle-card")].find(x => x.textContent.includes("RC-BR-1"));
    return c && c.textContent.includes("已冲正") ? c : null;
  }, { label: "冲正完成状态" });
  const revCard = [...d.querySelectorAll(".settle-card")]
    .find(x => x.textContent.includes("RC-BR-1"));
  check(revCard.textContent.includes("浏览器回归-冲正原因"), "冲正原因留痕展示");
  check(revCard.textContent.includes("预付款余额已恢复"), "页面提示预付款余额已恢复");
  page.clickTab("prepay");
  check(preRow("P001").children[4].textContent === "40000.00", "冲正后 P001 恢复 40000.00");
  check(preRow("P002").children[4].textContent === "60000.00", "冲正后 P002 恢复 60000.00");

  // ============ 8. 服务重启：新页面无错、状态完整 ============
  console.log("[8] 服务重启后页面与状态恢复");
  check(page.consoleErrors.length === 0,
    "完整业务链过程控制台无错误（实际: " + JSON.stringify(page.consoleErrors) + "）");
  page.dom.window.close();
  await stopServer(server);
  server = startServer();
  await waitPort(PORT);

  page = await openPage();
  await waitFor(() => page.document.querySelectorAll("#loginUser option").length === 5,
    { label: "重启后用户下拉加载" });
  check(page.consoleErrors.length === 0,
    "重启后首屏控制台无错误（实际: " + JSON.stringify(page.consoleErrors) + "）");
  const d2 = page.document;
  check(d2.querySelectorAll("#loginUser option").length === 5, "重启后登录人员下拉正常");
  await page.login("u2");
  check(d2.querySelector("#loginBox").textContent.includes("李四"), "重启后可正常登录");
  const vals2 = [...d2.querySelectorAll("#dashCards .card-stat .v")].map(e => e.textContent);
  check(vals2[0] === "1", "重启后总览车次仍为 1");
  page.clickTab("prepay");
  const preRow2 = code => [...d2.querySelectorAll("#prepayTable tr")]
    .find(t => t.children[0]?.textContent === code);
  check(preRow2("P001").children[4].textContent === "40000.00"
    && preRow2("P002").children[4].textContent === "60000.00",
    "重启后预付款余额为冲正恢复值");
  page.clickTab("settle");
  await waitFor(() => d2.querySelector("#settleList").textContent.includes("已冲正"),
    { label: "重启后结算单仍为已冲正" });
  const card2 = [...d2.querySelectorAll(".settle-card")]
    .find(x => x.textContent.includes("RC-BR-1"));
  check(card2 && card2.textContent.includes("92344.00")
    && card2.textContent.includes("第1审：李四")
    && card2.textContent.includes("第2审：王五"),
    "重启后金额与两审记录完整保留");
  check(!d2.querySelector('#settleList [data-action="reverse"]')
    && !d2.querySelector('#settleList [data-action="pay"]'),
    "已冲正单据无再次付款/冲正入口");
  check(page.consoleErrors.length === 0, "收尾控制台仍无错误");

  await stopServer(server);
  for (const ext of ["", "-wal", "-shm"]) { try { fs.rmSync(dbPath + ext); } catch {} }
  console.log(failures === 0 ? "\n浏览器回归全部通过 ✅" : `\n${failures} 项失败 ❌`);
  process.exit(failures === 0 ? 0 : 1);
}

run().catch(e => {
  console.error("回归脚本异常:", e);
  killAllServers();
  process.exit(2);
});
