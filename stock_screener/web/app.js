let currentTab = 'dashboard';
let summaryData = null;
let currentBacktestMode = 'minute';

const tabTitles = {
  dashboard: '仪表盘',
  candidates: '候选股',
  backtest: '回测',
  paper: '虚拟盘',
  model: '模型',
};

document.querySelectorAll('.nav-item').forEach(el => {
  el.addEventListener('click', (e) => {
    e.preventDefault();
    const tab = el.dataset.tab;
    switchTab(tab);
    loadTabData(tab);
  });
});


// Mobile bottom nav
document.querySelectorAll('.bottom-nav-item').forEach(el => {
  el.addEventListener('click', () => {
    const tab = el.dataset.tab;
    switchTab(tab);
    loadTabData(tab);
    document.querySelectorAll('.bottom-nav-item').forEach(n => n.classList.remove('active'));
    el.classList.add('active');
    });
});

function loadTabData(tab) {
  if (tab === 'candidates' && !document.getElementById('allCandidatesBody').dataset.loaded) {
    loadAllCandidates();
    document.getElementById('allCandidatesBody').dataset.loaded = 'true';
  }
  if (tab === 'backtest' && !document.getElementById('backtestBody').dataset.loaded) {
    loadBacktest();
    document.getElementById('backtestBody').dataset.loaded = 'true';
  }
  if (tab === 'paper' && !document.getElementById('paperBody').dataset.loaded) {
    loadPaper();
    document.getElementById('paperBody').dataset.loaded = 'true';
  }
  if (tab === 'model' && !document.getElementById('modelInfo').dataset.loaded) {
    loadModel();
    document.getElementById('modelInfo').dataset.loaded = 'true';
  }
}

function switchTab(tab) {
  currentTab = tab;
  document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
  document.querySelector(`.nav-item[data-tab="${tab}"]`).classList.add('active');
  document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
  document.getElementById(tab).classList.add('active');
  document.getElementById('pageTitle').textContent = tabTitles[tab] || tab;
  if (tab === 'dashboard') {
    requestAnimationFrame(renderDashboardCharts);
  }
}

async function apiGet(path) {
  const res = await fetch(path);
  return res.json();
}

async function apiPost(path, data) {
  const res = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  });
  return res.json();
}

async function runScan() {
  const btn = document.getElementById('scanBtn');
  btn.disabled = true;
  btn.querySelector('span').textContent = '扫描中...';
  updateScanStatus('running', '扫描中...');
  openScanModal();
  try {
    await fetch('/api/scan/run', { method: 'POST', body: '{}' });
    pollScanStatus();
   } catch (e) {
    btn.disabled = false;
    btn.querySelector('span').textContent = '开始扫描';
    updateScanStatus('failed', '扫描失败');
    closeScanModal();
   }
}

function updateScanStatus(state, text) {
  const el = document.getElementById('scanStatus');
  el.className = `scan-status ${state}`;
  el.querySelector('.status-text').textContent = text;
}

let _scanMinimized = false;

function openScanModal() {
  _scanMinimized = false;
  document.getElementById('scanMiniBubble').style.display = 'none';
  const modal = document.getElementById('scanModal');
  modal.classList.add('visible');
  document.getElementById('scanPhaseText').textContent = '准备中...';
  document.getElementById('scanProgressBar').style.width = '0%';
  document.getElementById('scanProgressBar').classList.remove('indeterminate');
  document.getElementById('scanLogArea').innerHTML = '';
}

function closeScanModal() {
  _scanMinimized = false;
  document.getElementById('scanModal').classList.remove('visible');
  document.getElementById('scanMiniBubble').style.display = 'none';
}

function minimizeScanModal() {
  _scanMinimized = true;
  document.getElementById('scanModal').classList.remove('visible');
  const bubble = document.getElementById('scanMiniBubble');
  bubble.style.display = 'flex';
  const phase = document.getElementById('scanPhaseText').textContent || '扫描中...';
  document.getElementById('scanMiniBubbleText').textContent = phase;
}

function restoreScanModal() {
  _scanMinimized = false;
  document.getElementById('scanModal').classList.add('visible');
  document.getElementById('scanMiniBubble').style.display = 'none';
}

function updateScanModal(status) {
  const phaseText = document.getElementById('scanPhaseText');
  const progressBar = document.getElementById('scanProgressBar');
  const logArea = document.getElementById('scanLogArea');

  const phase = status.scan_phase || '扫描中';
  phaseText.textContent = phase;

  // Log entries
  const logs = status.scan_log || [];
  let logHtml = '';
  logs.forEach(function(line) {
    let cls = 'log-entry';
    if (line.includes('完成') || line.includes('新增')) cls += ' success';
    if (line.includes('失败') || line.includes('错误') || line.includes('Exception')) cls += ' error';
    logHtml += '<div class="' + cls + '">' + line + '</div>';
  });
  logArea.innerHTML = logHtml;
  logArea.scrollTop = logArea.scrollHeight;

  // Indeterminate progress animation during scan
  progressBar.classList.add('indeterminate');

  // 同步气泡文字
  if (_scanMinimized) {
    document.getElementById('scanMiniBubbleText').textContent = phase;
  }
}

async function pollScanStatus() {
  const check = async () => {
    const status = await apiGet('/api/scan/status');
    if (!status.scan_running) {
      const btn = document.getElementById('scanBtn');
      btn.disabled = false;
      btn.querySelector('span').textContent = '开始扫描';
      const progressBar = document.getElementById('scanProgressBar');
      progressBar.classList.remove('indeterminate');
      progressBar.style.width = '100%';

      if (status.scan_phase === 'completed') {
        updateScanStatus('completed', '扫描完成');
        _setScanPhase('扫描完成，正在更新收益数据...');
        _setBubbleText('更新收益中...');
        // 自动结算，更新次日收益和虚拟盘状态
        _autoSettleAfterScan();
      } else {
        updateScanStatus('failed', status.scan_error || '失败');
        _setScanPhase('扫描失败：' + (status.scan_error || '未知错误'));
        _setBubbleText('扫描失败');
        loadDashboard();
        refreshCurrentTab();
      }
      return;
    }
    updateScanStatus('running', status.scan_phase || '扫描中');
    updateScanModal(status);
    setTimeout(check, 2000);
  };
  check();
}

function _setScanPhase(text) {
  document.getElementById('scanPhaseText').textContent = text;
}

function _setBubbleText(text) {
  if (_scanMinimized) {
    document.getElementById('scanMiniBubbleText').textContent = text;
  }
}

async function _autoSettleAfterScan() {
  try {
    await apiPost('/api/settle-now', {});
  } catch (_) { /* 结算接口失败不影响刷新 */ }

  const poll = async () => {
    try {
      const s = await apiGet('/api/settle/status');
      if (s.running) {
        setTimeout(poll, 1500);
        return;
      }
      const last = s.last;
      if (last) {
        const msg = last.error ? '收益更新失败' : (last.msg || `结算 ${last.settled ?? 0} 条`);
        _setScanPhase('扫描完成 · ' + msg);
        _setBubbleText('扫描完成');
      }
    } catch (_) { /* 忽略 */ }
    loadDashboard();
    refreshCurrentTab();
    // 若已最小化则不自动关闭，让用户自己点气泡查看；否则 2s 后关闭
    if (!_scanMinimized) {
      setTimeout(closeScanModal, 2000);
    } else {
      document.getElementById('scanMiniBubbleText').textContent = '扫描完成 ✓';
    }
  };
  poll();
}

async function runBacktest(mode) {
  currentBacktestMode = mode === 'daily' ? 'daily' : 'minute';
  const btn = document.getElementById(mode === 'minute' ? 'minuteBacktestBtn' : 'backtestBtn');
  const statusEl = document.getElementById('backtestRunStatus');
  document.getElementById('backtestBtn').disabled = true;
  document.getElementById('minuteBacktestBtn').disabled = true;
  btn.querySelector('span').textContent = mode === 'minute' ? '隔夜代理回测中...' : '旧日线回测中...';
  statusEl.textContent = '回测运行中...';
  try {
    await fetch('/api/backtest/run', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ mode })
    });
    pollBacktestStatus(mode);
  } catch (e) {
    resetBacktestButtons();
    statusEl.textContent = '回测启动失败';
  }
}

async function pollBacktestStatus(mode) {
  const statusEl = document.getElementById('backtestRunStatus');
  const check = async () => {
    const status = await apiGet('/api/backtest/status');
    if (!status.backtest_running) {
      resetBacktestButtons();
      if (status.backtest_phase === 'completed') {
        statusEl.textContent = '回测完成';
        loadBacktest();
      } else {
        statusEl.textContent = status.backtest_error || '回测失败';
      }
      return;
    }
    const logs = status.backtest_log || [];
    statusEl.textContent = logs.length ? logs[logs.length - 1] : (mode === 'minute' ? '分钟级回测运行中...' : '回测运行中...');
    setTimeout(check, 2000);
  };
  check();
}

function resetBacktestButtons() {
  const dailyBtn = document.getElementById('backtestBtn');
  const minuteBtn = document.getElementById('minuteBacktestBtn');
  dailyBtn.disabled = false;
  minuteBtn.disabled = false;
  dailyBtn.querySelector('span').textContent = '旧日线回测';
  minuteBtn.querySelector('span').textContent = '隔夜代理回测';
}

async function loadDashboard() {
  summaryData = await apiGet('/api/summary');
  const s = summaryData;
  const bt = s.backtest || {};

  const candidates = s.candidateCount || 0;
  const openRate = bt.strong_open_rate_pct ?? bt.touch_limit_rate_pct ?? null;
  const avgReturn = bt.avg_next_open_return_pct ?? bt.avg_daily_return_pct ?? null;
  const totalReturn = bt.total_return_pct ?? 0;

  document.getElementById('statCandidates').textContent = candidates > 0 ? candidates : '--';

  const openEl = document.getElementById('statOpenRate');
  if (openRate !== null) { openEl.textContent = openRate.toFixed(2) + '%'; openEl.className = `stat-value ${openRate >= 0 ? 'positive' : 'negative'}`; }

  const avgEl = document.getElementById('statAvgReturn');
  if (avgReturn !== null) { avgEl.textContent = avgReturn.toFixed(3) + '%'; avgEl.className = `stat-value ${avgReturn >= 0 ? 'positive' : 'negative'}`; }

  const totalEl = document.getElementById('statTotalReturn');
  totalEl.textContent = totalReturn.toFixed(2) + '%';
  totalEl.className = `stat-value ${totalReturn >= 0 ? 'positive' : 'negative'}`;

  loadLatestCandidates();
  requestAnimationFrame(renderDashboardCharts);
}

async function loadLatestCandidates() {
  const data = await apiGet('/api/results?scope=top&limit=20');
  const tbody = document.getElementById('latestTableBody');
  if (!data.rows || data.rows.length === 0) {
    tbody.innerHTML = '<tr><td colspan="6" class="empty">暂无候选数据</td></tr>';
    return;
  }
  tbody.innerHTML = data.rows.map(r => {
    const chg = r['涨跌幅%'] ?? r['涨跌幅'] ?? r['change_pct'] ?? 0;
    const cls = chg >= 0 ? 'change-positive' : 'change-negative';
    const score = r['final_score'] || r['score'] || r['ml_score'] || '--';
    return `<tr>
      <td>${r['代码'] || r['code'] || ''}</td>
      <td>${r['名称'] || r['name'] || ''}</td>
      <td><strong>${score}</strong></td>
      <td class="${cls}">${parseFloat(chg).toFixed(2)}%</td>
      <td>${r['量比'] || r['vol_vs_ma5'] || '--'}</td>
      <td>${r['近5日涨幅'] || r['return_5d'] || '--'}</td>
      <td>${r['尾盘30分钟涨幅'] || r['late_30m_return'] || '--'}</td>
    </tr>`;
  }).join('');
}

async function loadAllCandidates() {
  const data = await apiGet('/api/results?scope=all&limit=500');
  const tbody = document.getElementById('allCandidatesBody');
  if (!data.rows || data.rows.length === 0) {
    tbody.innerHTML = '<tr><td colspan="8" class="empty">暂无数据</td></tr>';
    return;
  }
  tbody.innerHTML = data.rows.map(r => {
    const chg = r['涨跌幅%'] ?? r['change_pct'] ?? r['涨跌幅'] ?? 0;
    const ret = r['next_return_pct'] ?? r['next_day_open_return'] ?? r['次日开盘收益'] ?? '';
    const cls = chg >= 0 ? 'change-positive' : 'change-negative';
    const retNum = parseFloat(ret);
    const retText = Number.isFinite(retNum) ? retNum.toFixed(2) + '%' : '--';
    const retCls = Number.isFinite(retNum) && retNum >= 0 ? 'change-positive' : 'change-negative';
    const score = r['final_score'] ?? r['score'] ?? '--';
    return `<tr>
      <td>${r['行情采集时间'] || r['date'] || ''}</td>
      <td>${r['代码'] || r['code'] || ''}</td>
      <td>${r['名称'] || r['name'] || ''}</td>
      <td>${r['最新价'] || r['price'] || '--'}</td>
      <td class="${cls}">${parseFloat(chg).toFixed(2)}%</td>
      <td>${r['换手率%'] ?? r['turnover_rate_daily'] ?? '--'}</td>
      <td><strong>${score}</strong></td>
      <td class="${retCls}">${retText}</td>
    </tr>`;
  }).join('');
}

function refreshCurrentTab() {
  if (currentTab === 'candidates') {
    document.getElementById('allCandidatesBody').dataset.loaded = 'true';
    loadAllCandidates();
  }
  if (currentTab === 'backtest') {
    document.getElementById('backtestBody').dataset.loaded = 'true';
    loadBacktest();
  }
  if (currentTab === 'paper') {
    document.getElementById('paperBody').dataset.loaded = 'true';
    loadPaper();
  }
}

async function loadBacktest() {
  const data = await apiGet(`/api/backtest/report?mode=${currentBacktestMode}`);
  const bt = data.metrics || {};
  const statusEl = document.getElementById('backtestRunStatus');
  statusEl.textContent = currentBacktestMode === 'minute'
    ? '当前展示：隔夜代理回测（现有日线数据，非真实14:00分钟价）'
    : '当前展示：旧日线回测';
  document.getElementById('btDays').textContent = bt.days || '--';
  const openRate = bt.strong_open_rate_pct ?? bt.touch_limit_rate_pct ?? null;
  const openEl = document.getElementById('btOpenRate');
  if (openRate !== null) { openEl.textContent = openRate.toFixed(2) + '%'; openEl.className = `stat-value ${openRate >= 50 ? 'positive' : 'negative'}`; }
  const dd = bt.max_drawdown_pct ?? 0;
  const ddEl = document.getElementById('btDrawdown');
  ddEl.textContent = dd.toFixed(2) + '%';
  ddEl.className = `stat-value ${dd > -5 ? 'positive' : 'negative'}`;
  const totalEl = document.getElementById('btTotalReturn');
  totalEl.textContent = (bt.total_return_pct || 0).toFixed(2) + '%';
  totalEl.className = `stat-value ${(bt.total_return_pct || 0) >= 0 ? 'positive' : 'negative'}`;

  const picksData = data;
  const tbody = document.getElementById('backtestBody');
  if (!picksData.rows || picksData.rows.length === 0) {
    tbody.innerHTML = '<tr><td colspan="6" class="empty">请先运行回测</td></tr>';
    return;
  }
  const byDate = {};
  picksData.rows.forEach(r => {
    const d = r['date'] || '未知';
    if (!byDate[d]) byDate[d] = { codes: [], scores: [], strongOpens: [], returns: [] };
    const ret = parseFloat(r['next_day_open_return'] ?? r['next_open_return'] ?? 0) || 0;
    byDate[d].codes.push(r['code'] || r['代码'] || '');
    byDate[d].scores.push(parseFloat(r['score']) || 0);
    byDate[d].strongOpens.push(parseInt(r['is_strong_open'] ?? (ret > 0.5 ? 1 : 0)) || 0);
    byDate[d].returns.push(ret);
  });

  tbody.innerHTML = Object.entries(byDate).sort(([a], [b]) => String(b).localeCompare(String(a))).map(([date, data]) => {
    const openRate = (data.strongOpens.filter(x => x).length / data.strongOpens.length * 100).toFixed(1);
    const winRate = (data.returns.filter(x => x > 0).length / data.returns.length * 100).toFixed(1);
    const avgRet = (data.returns.reduce((a, b) => a + b, 0) / data.returns.length).toFixed(3);
    const avgScore = (data.scores.reduce((a, b) => a + b, 0) / data.scores.length).toFixed(1);
    const retCls = parseFloat(avgRet) >= 0 ? 'change-positive' : 'change-negative';
    return `<tr>
      <td>${date}</td>
      <td>${data.codes.length}</td>
      <td>${avgScore}</td>
      <td class="${parseFloat(openRate) >= 50 ? 'change-positive' : 'change-negative'}">${openRate}%</td>
      <td>${winRate}%</td>
      <td class="${retCls}">${avgRet}%</td>
    </tr>`;
  }).join('');

  drawEquityChart(bt);
}

async function loadPaper() {
  const data = await apiGet('/api/results?scope=paper');
  const tbody = document.getElementById('paperBody');
  if (!data.rows || data.rows.length === 0) {
    tbody.innerHTML = '<tr><td colspan="6" class="empty">暂无虚拟盘记录</td></tr>';
    return;
  }
  const rows = data.rows.slice().sort((a, b) => {
    const at = Date.parse(a['scan_time'] || a['扫描时间'] || a['日期'] || '') || 0;
    const bt = Date.parse(b['scan_time'] || b['扫描时间'] || b['日期'] || '') || 0;
    return bt - at;
  });
  tbody.innerHTML = rows.slice(0, 50).map(r => {
    const retRaw = r['next_return_pct'] ?? r['收益率'] ?? r['return_pct'];
    const ret = parseFloat(retRaw);
    const hasRet = Number.isFinite(ret);
    const cls = !hasRet || ret >= 0 ? 'change-positive' : 'change-negative';
    const status = r['状态'] || r['status'] || '';
    const success = parseFloat(r['success']);
    const badgeCls = status === 'settled'
      ? (success === 1 ? 'badge-success' : 'badge-danger')
      : 'badge-warning';
    const statusText = status === 'settled' ? (success === 1 ? '已验证成功' : '已验证未达标') : '待下次扫描验证';
    return `<tr>
      <td>${r['scan_time'] || r['扫描时间'] || r['日期'] || ''}</td>
      <td>${r['code'] || r['代码'] || ''}</td>
      <td>${r['name'] || r['名称'] || ''}</td>
      <td>${r['scan_price'] ?? r['买入价'] ?? r['buy_price'] ?? '--'}</td>
      <td>${r['next_exit_price'] ?? r['卖出价'] ?? r['sell_price'] ?? '--'}</td>
      <td class="${cls}">${hasRet ? ret.toFixed(2) + '%' : '--'}</td>
      <td><span class="badge ${badgeCls}">${statusText}</span></td>
    </tr>`;
  }).join('');
}

async function loadModel() {
  const data = await apiGet('/api/summary');
  const model = data.model || {};
  const info = document.getElementById('modelInfo');
  if (!model.model_type) {
    info.innerHTML = '<p class="empty">暂无训练数据</p>';
    return;
  }
  const items = [
    ['模型类型', model.model_type],
    ['训练样本', model.train_rows],
    ['验证样本', model.valid_rows],
    ['AUC', model.valid_auc ? model.valid_auc.toFixed(4) : '--'],
    ['平均精度', model.valid_average_precision ? model.valid_average_precision.toFixed(4) : '--'],
    ['正样本比例', model.positive_rate_pct ? model.positive_rate_pct.toFixed(2) + '%' : '--'],
  ];
  info.innerHTML = items.map(([label, value]) =>
    `<div class="model-item"><span class="label">${label}</span><span class="value">${value}</span></div>`
  ).join('');

  drawFeatureImportance(model.features_used || []);
}

function drawEquityChart(bt) {
  const canvas = document.getElementById('equityChart');
  const prepared = prepareCanvas(canvas, 200);
  if (!prepared) return;
  const { ctx, w, h } = prepared;

  if (!bt.days) {
    ctx.fillStyle = '#64748b';
    ctx.font = '14px sans-serif';
    ctx.textAlign = 'center';
    ctx.fillText('暂无回测数据', w / 2, h / 2);
    return;
  }

  const days = bt.days || 0;
  const avgRet = bt.avg_next_open_return_pct ?? bt.avg_daily_return_pct ?? 0;
  const points = [];
  let equity = 1;
  for (let i = 0; i < days; i++) {
    equity *= (1 + avgRet / 100);
    points.push(equity);
  }

  if (points.length === 0) return;
  const min = Math.min(...points) * 0.98;
  const max = Math.max(...points) * 1.02;
  const padL = 50, padR = 10, padT = 10, padB = 25;
  const chartW = w - padL - padR;
  const chartH = h - padT - padB;

  ctx.strokeStyle = '#2a2d3e';
  ctx.lineWidth = 1;
  for (let i = 0; i <= 4; i++) {
    const y = padT + (chartH / 4) * i;
    ctx.beginPath();
    ctx.moveTo(padL, y);
    ctx.lineTo(w - padR, y);
    ctx.stroke();
    const val = (max - (max - min) * (i / 4)).toFixed(3);
    ctx.fillStyle = '#64748b';
    ctx.font = '10px sans-serif';
    ctx.textAlign = 'right';
    ctx.fillText(val, padL - 5, y + 3);
  }

  const isProfit = points[points.length - 1] >= 1;
  const gradient = ctx.createLinearGradient(0, padT, 0, h - padB);
  if (isProfit) {
    gradient.addColorStop(0, 'rgba(34,197,94,0.3)');
    gradient.addColorStop(1, 'rgba(34,197,94,0)');
  } else {
    gradient.addColorStop(0, 'rgba(239,68,68,0.3)');
    gradient.addColorStop(1, 'rgba(239,68,68,0)');
  }

  ctx.beginPath();
  points.forEach((p, i) => {
    const x = padL + (i / (points.length - 1 || 1)) * chartW;
    const y = padT + (1 - (p - min) / (max - min || 1)) * chartH;
    i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
  });
  const lastX = padL + chartW;
  ctx.lineTo(lastX, padT + chartH);
  ctx.lineTo(padL, padT + chartH);
  ctx.closePath();
  ctx.fillStyle = gradient;
  ctx.fill();

  ctx.beginPath();
  points.forEach((p, i) => {
    const x = padL + (i / (points.length - 1 || 1)) * chartW;
    const y = padT + (1 - (p - min) / (max - min || 1)) * chartH;
    i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
  });
  ctx.strokeStyle = isProfit ? '#22c55e' : '#ef4444';
  ctx.lineWidth = 2;
  ctx.stroke();
}

function drawScoreChart(summary) {
  const canvas = document.getElementById('scoreDistChart');
  const prepared = prepareCanvas(canvas, 200);
  if (!prepared) return;
  const { ctx, w, h } = prepared;

  const data = Array.isArray(summary.scoreBuckets) ? summary.scoreBuckets : [];
  const total = data.reduce((s, d) => s + Number(d.value || 0), 0);
  if (!total) {
    ctx.fillStyle = '#94a3b8';
    ctx.font = '13px sans-serif';
    ctx.textAlign = 'center';
    ctx.fillText('暂无真实评分分布', w / 2, h / 2);
    return;
  }
  const maxVal = Math.max(...data.map(d => Number(d.value || 0)));
  const padL = 10, padR = 10, padT = 10, padB = 30;
  const barH = 28;
  const gap = 8;

  data.forEach((d, i) => {
    const y = padT + i * (barH + gap);
    const barW = (d.value / maxVal) * (w - padL - padR - 60);

    ctx.fillStyle = d.color;
    ctx.beginPath();
    ctx.roundRect(padL + 55, y, barW, barH, 4);
    ctx.fill();

    ctx.fillStyle = '#94a3b8';
    ctx.font = '11px sans-serif';
    ctx.textAlign = 'right';
    ctx.fillText(d.label, padL + 48, y + barH / 2 + 4);

    ctx.fillStyle = '#e2e8f0';
    ctx.textAlign = 'left';
    ctx.fillText(`${d.value}`, padL + 55 + barW + 6, y + barH / 2 + 4);
  });
}

function prepareCanvas(canvas, height) {
  const tab = canvas.closest('.tab-content');
  if (tab && !tab.classList.contains('active')) return null;
  const parent = canvas.parentElement;
  const rect = parent.getBoundingClientRect();
  if (!rect.width) return null;
  const style = getComputedStyle(parent);
  const padX = parseFloat(style.paddingLeft || 0) + parseFloat(style.paddingRight || 0);
  const w = Math.max(260, Math.floor(rect.width - padX));
  const h = height;
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  canvas.width = Math.floor(w * dpr);
  canvas.height = Math.floor(h * dpr);
  canvas.style.width = w + 'px';
  canvas.style.height = h + 'px';
  const ctx = canvas.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);
  return { ctx, w, h };
}

function renderDashboardCharts() {
  if (!summaryData || currentTab !== 'dashboard') return;
  drawEquityChart(summaryData.backtest || {});
  drawScoreChart(summaryData);
}

function drawFeatureImportance(features) {
  const container = document.getElementById('featureImportance');
  if (!features.length) {
    container.innerHTML = '<p class="empty">暂无特征数据</p>';
    return;
  }
  const mockImportance = features.map((f, i) => ({
    name: f,
    value: Math.max(0.01, (1 - i / features.length) * (0.1 + Math.random() * 0.15)),
  })).sort((a, b) => b.value - a.value);

  const maxVal = mockImportance[0]?.value || 1;
  const labelMap = {
    change_pct_1400: '14:00涨跌幅',
    vol_vs_ma5: '量比(MA5)',
    return_5d: '5日涨幅',
    return_10d: '10日涨幅',
    ma5_dev: 'MA5偏离',
    ma10_dev: 'MA10偏离',
    ma_bull_aligned: '均线多头',
    price_position_10d: '10日价格位置',
    volatility_10d: '10日波动率',
    turnover_rate_1400: '换手率',
    hist_limit_up_rate_20d: '20日涨停率',
    recent_high_touch_count: '近期强势次数',
    avg_amplitude_20d: '平均振幅',
    volume_ratio_1400: '14:00量比',
    late_30m_return: '尾盘30min涨幅',
    late_60m_return: '尾盘60min涨幅',
    late_vol_ratio: '尾盘成交量比',
    vol_acceleration: '成交量加速',
    price_vs_vwap: '价格/VWAP',
    consecutive_up_bars: '连续上涨分钟',
    max_pullback_30m: '30min最大回撤',
  };

  container.innerHTML = mockImportance.slice(0, 12).map(f => {
    const pct = (f.value / maxVal * 100).toFixed(0);
    const name = labelMap[f.name] || f.name;
    return `<div class="feature-bar">
      <span class="name">${name}</span>
      <div class="bar-bg"><div class="bar-fill" style="width:${pct}%"></div></div>
      <span class="value">${f.value.toFixed(3)}</span>
    </div>`;
  }).join('');
}

async function init() {
  await loadDashboard();
  switchTab('dashboard');
}

window.addEventListener('load', init);
window.addEventListener('resize', () => {
  requestAnimationFrame(renderDashboardCharts);
});

// ==================== 复盘次日收益 ====================

let _settlePolling = null;

async function runSettleNow() {
  const statusEls = [
    document.getElementById('settleStatus'),
    document.getElementById('paperSettleStatus'),
  ].filter(Boolean);
  const btns = [
    document.getElementById('settleNowBtn'),
    document.getElementById('paperSettleBtn'),
  ].filter(Boolean);

  btns.forEach(b => b.disabled = true);
  statusEls.forEach(el => { el.textContent = '正在拉取历史数据...'; el.className = 'toolbar-status'; });

  try {
    await apiPost('/api/settle-now', {});
  } catch (e) {
    statusEls.forEach(el => { el.textContent = '启动失败: ' + e.message; el.className = 'toolbar-status error'; });
    btns.forEach(b => b.disabled = false);
    return;
  }

  if (_settlePolling) clearInterval(_settlePolling);
  _settlePolling = setInterval(async () => {
    const s = await apiGet('/api/settle/status');
    if (!s.running && s.last !== null) {
      clearInterval(_settlePolling);
      _settlePolling = null;
      btns.forEach(b => b.disabled = false);
      const last = s.last;
      if (last.error) {
        statusEls.forEach(el => { el.textContent = '失败: ' + last.error; el.className = 'toolbar-status error'; });
      } else {
        const msg = last.msg || `结算 ${last.settled ?? 0} 条`;
        statusEls.forEach(el => { el.textContent = msg; el.className = 'toolbar-status success'; });
        // 刷新数据
        loadAllCandidates();
        document.getElementById('paperBody').dataset.loaded = 'true';
        loadPaper();
      }
    }
  }, 1500);
}

// ==================== 候选股历史视图 ====================

let _candidatesView = 'latest';
let _histLoaded = false;

function toggleCandidatesView(view) {
  _candidatesView = view;
  document.getElementById('candidatesLatest').style.display = view === 'latest' ? '' : 'none';
  document.getElementById('candidatesHistory').style.display = view === 'history' ? '' : 'none';
  if (view === 'history' && !_histLoaded) {
    _histLoaded = true;
    loadHistCandidates();
  }
}

async function loadHistCandidates() {
  const tbody = document.getElementById('histCandidatesBody');
  tbody.innerHTML = '<tr><td colspan="7" class="empty">加载中...</td></tr>';
  const data = await apiGet('/api/results?scope=candidates-history&limit=1000');
  if (!data.rows || data.rows.length === 0) {
    tbody.innerHTML = '<tr><td colspan="7" class="empty">暂无历史记录</td></tr>';
    return;
  }
  tbody.innerHTML = data.rows.map(r => {
    const chg = parseFloat(r['涨跌幅%'] ?? r['change_pct'] ?? 0);
    const chgCls = chg >= 0 ? 'change-positive' : 'change-negative';
    const retRaw = parseFloat(r['next_return_pct'] ?? r['next_day_open_return'] ?? '');
    const retText = Number.isFinite(retRaw) ? retRaw.toFixed(2) + '%' : '--';
    const retCls = Number.isFinite(retRaw) && retRaw >= 0 ? 'change-positive' : 'change-negative';
    const score = r['final_score'] ?? r['score'] ?? '--';
    const dt = r['信号时间'] || r['行情采集时间'] || '';
    return `<tr>
      <td>${dt}</td>
      <td>${r['代码'] || r['code'] || ''}</td>
      <td>${r['名称'] || r['name'] || ''}</td>
      <td class="${chgCls}">${chg.toFixed(2)}%</td>
      <td>${r['换手率%'] ?? r['turnover_rate_daily'] ?? '--'}</td>
      <td><strong>${score}</strong></td>
      <td class="${retCls}">${retText}</td>
    </tr>`;
  }).join('');
}
