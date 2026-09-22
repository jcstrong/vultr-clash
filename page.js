
let busy = false;
let currentSub = "";
let logPollInterval = null;

function showModal() {
  document.getElementById('modal').classList.add('show');
  document.getElementById('keyInput').focus();
}
function hideModal() {
  document.getElementById('modal').classList.remove('show');
}

async function saveKey() {
  const key = document.getElementById('keyInput').value.trim();
  if (!key) return;
  const r = await fetch("/api", {method:"POST", headers:{"Content-Type":"application/json"},
    body: JSON.stringify({action:"savekey", key})});
  const j = await r.json();
  const box = document.getElementById('selfMsg');
  if (box) box.textContent = j.ok ? '✓ API Key 有效并已保存' : ('✗ ' + (j.output || '校验失败'));
  if (j.ok) {
    setTimeout(() => { hideModal(); loadStatus(); }, 600);
  }
}

async function runSelftest() {
  const box = document.getElementById('selfMsg');
  if (box) box.textContent = '自检中…';
  try {
    const r = await fetch("/api", {method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({action:"selftest"})});
    const j = await r.json();
    const lines = ['平台: ' + (j.platform||''), '机器: ' + (j.machine||'') + ' · Python ' + (j.python||'')];
    (j.checks||[]).forEach(c => lines.push((c.present?'✓ ':'✗ ') + c.name + (c.path?' ('+c.path+')':'') + ' — ' + (c.desc||'')));
    if (j.tailscale_ip) lines.push('Tailscale IP: ' + j.tailscale_ip);
    if (box) box.textContent = lines.join('\n');
  } catch(e) {
    if (box) box.textContent = '自检失败: ' + e.message;
  }
}

async function loadStatus() {
  try {
    const r = await fetch("/api", {method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({action:"status"})});
    const j = await r.json();
    if (!j.ok) throw new Error(j.output || '状态查询失败');
    if (j.instance) {
      const inst = j.instance;
      const running = inst.status === 'active' && inst.power_status === 'running';
      const readyInit = inst.initialized === true;    // 节点初始化完成才能 Update
      document.getElementById('nodeDot').className = running ? 'dot on' : 'dot off';
      document.getElementById('nodeLabel').textContent = inst.region + ' · ' + inst.status;
      document.getElementById('nodeCard').className = running ? 'node-card active' : 'node-card inactive';
      document.getElementById('nodeName').textContent = inst.region + ' (' + inst.main_ip + ')';
      document.getElementById('nodeStatus').className = running ? 'node-status on' : 'node-status off';
      document.getElementById('nodeStatus').textContent = running ? (readyInit ? '运行中' : '初始化中…') : inst.status;
      document.getElementById('niRegion').textContent = inst.region;
      document.getElementById('niPlan').textContent = inst.plan;
      document.getElementById('niIP').textContent = inst.main_ip;
      document.getElementById('niPass').textContent = inst.ss_password || '-';
      document.getElementById('niCreateTime').textContent = inst.create_duration != null ? inst.create_duration + ' 秒' : '-';
      // 仅节点已创建且初始化完成，才允许"更新订阅"
      document.getElementById('updateBtn').disabled = !(running && readyInit);
      document.getElementById('updateBtn').textContent = (running && readyInit) ? '更新订阅' : '等待初始化…';
      currentSub = j.subscription_url || '';
      return;
    }
    document.getElementById('nodeDot').className = 'dot off';
    document.getElementById('nodeLabel').textContent = '无节点';
    document.getElementById('nodeCard').className = 'node-card inactive';
    document.getElementById('nodeName').textContent = '无活跃节点';
    document.getElementById('nodeStatus').className = 'node-status off';
    document.getElementById('nodeStatus').textContent = '未激活';
    document.getElementById('niRegion').textContent = '-';
    document.getElementById('niPlan').textContent = '-';
    document.getElementById('niIP').textContent = '-';
    document.getElementById('niPass').textContent = '-';
    document.getElementById('updateBtn').disabled = true;
    document.getElementById('updateBtn').textContent = '更新订阅';
    currentSub = j.subscription_url || '';
  } catch (e) {
    document.getElementById('nodeLabel').textContent = '状态查询失败';
    document.getElementById('nodeName').textContent = e.message;
  }
}

async function loadLog() {
  try {
    const r = await fetch("/api", {method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({action:"getlog"})});
    const j = await r.json();
    const el = document.getElementById('logBody');
    el.textContent = j.log || '准备就绪';
    el.scrollTop = el.scrollHeight;
  } catch(e) {}
}

async function checkBusy() {
  try {
    const r = await fetch("/api", {method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({action:"isbusy"})});
    const j = await r.json();
    return j.busy || false;
  } catch(e) {
    return false;
  }
}

function setButtons(disabled) {
  document.getElementById('createBtn').disabled = disabled;
  document.getElementById('updateBtn').disabled = disabled;
  document.getElementById('destroyBtn').disabled = disabled;
}

async function doAction(action) {
  // 先给即时反馈：确认弹窗同步弹出，不被待网络卡住
  if (action === 'create' && !confirm('即将创建新实例，确认？')) return;
  if (action === 'destroy' && !confirm('确认销毁实例？将停止计费')) return;

  setButtons(true);
  document.getElementById('logBody').textContent = '正在执行…';

  // 检查是否已有操作在运行（仅提示，不阻塞）
  try {
    const isBusy = await checkBusy();
    if (isBusy) {
      setButtons(false);
      loadLog();
      return;
    }
  } catch(e) {}

  const region = document.getElementById('region').value;
  const plan = document.getElementById('plan').value;

  let resp;
  try {
    resp = await fetch("/api", {method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({action, region, plan})});
  } catch(e) {
    console.error(e);
    loadLog();
    loadStatus();
    setButtons(false);
    return;
  }

  // 立即刷新一次日志（服务端已写入"开始执行"）
  loadLog();

  // 若服务端未真正启动后台任务（如已有节点），立即结束
  try {
    const j = await resp.json();
    if (j.started === false) {
      loadLog();
      loadStatus();
      setButtons(false);
      return;
    }
  } catch(e) {}

  // 执行期间轮询日志；动作完成（busy=false）后结束并刷新状态
  if (logPollInterval) clearInterval(logPollInterval);
  logPollInterval = setInterval(async () => {
    loadLog();
    const stillBusy = await checkBusy();
    if (!stillBusy) {
      if (logPollInterval) { clearInterval(logPollInterval); logPollInterval = null; }
      loadLog();
      loadStatus();
      setButtons(false);
    }
  }, 2000);
}

function copySub() {
  const url = currentSub || 'http://100.90.80.9:8888/sub/clash.yaml';
  const showCopied = () => {
    const hint = document.getElementById('copyHint');
    hint.classList.add('show');
    setTimeout(() => hint.classList.remove('show'), 1500);
  };
  const fallbackCopy = () => {
    const t = document.createElement('textarea');
    t.value = url;
    t.style.position = 'fixed';
    t.style.opacity = '0';
    document.body.appendChild(t);
    t.focus();
    t.select();
    t.setSelectionRange(0, t.value.length);
    const copied = document.execCommand('copy');
    document.body.removeChild(t);
    if (copied) showCopied();
  };

  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(url).then(showCopied).catch(fallbackCopy);
  } else {
    fallbackCopy();
  }
}

// 套餐↔地区联动：新泽西↔$3.5，其他地区↔$5
function syncPlanRegion() {
  const region = document.getElementById('region');
  const plan = document.getElementById('plan');
  if (region.value === 'ewr') {
    plan.value = 'vc2-1c-0.5gb';          // 新泽西 → $3.5
  } else {
    plan.value = 'vc2-1c-1gb';            // 日本/其他 → $5
  }
  if (plan.value === 'vc2-1c-0.5gb') {
    region.value = 'ewr';                 // $3.5 → 新泽西
  }
}
document.getElementById('region').addEventListener('change', syncPlanRegion);
document.getElementById('plan').addEventListener('change', syncPlanRegion);

async function loadCost() {
  try {
    const r = await fetch("/api", {method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({action:"cost"})});
    const j = await r.json();
    if (!j.ok) throw new Error(j.output || '费用查询失败');
    const b = j.billing;
    // 余额 = -(充入金额 + 已花费)：-10 为充入，0.22 为本月已花费
    if (b.balance != null && b.pending != null) {
      const remaining = -1 * (b.balance + b.pending);
      document.getElementById('coBalance').textContent =
        '$' + remaining.toFixed(2) + (b.pending > 0 ? '（本月已花费 $' + b.pending + '）' : '');
    } else {
      document.getElementById('coBalance').textContent = '-';
    }
    document.getElementById('coCurrent').textContent =
      (b.current_cost != null ? '$' + b.current_cost.toFixed(4) : '-') + (b.current_plan_monthly ? '（月费 $' + b.current_plan_monthly + '）' : '');
    document.getElementById('coHistory').textContent =
      (b.history_total != null ? '$' + b.history_total.toFixed(4) : '-') + (b.total_all != null && b.total_all > 0 ? ' · 累计 $' + b.total_all.toFixed(4) : '');
  } catch(e) {
    document.getElementById('coBalance').textContent = '查询失败';
  }
}

// Enter 键保存 API Key
document.getElementById('keyInput').addEventListener('keydown', e => {
  if (e.key === 'Enter') saveKey();
});

// 初始加载（各页首次各请求一次，后续用户手动刷新）
loadStatus();
loadCost();
loadLog();

// 操作执行期间由 doAction 内部轮询日志；无操作时不再自动刷新
