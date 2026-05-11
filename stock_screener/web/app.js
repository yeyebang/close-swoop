let currentTab = 'dashboard';
let summaryData = null;

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
  });
});


// Mobile bottom nav
document.querySelectorAll('.bottom-nav-item').forEach(el => {
  el.addEventListener('click', () => {
    const tab = el.dataset.tab;
    switchTab(tab);
    document.querySelectorAll('.bottom-nav-item').forEach(n => n.classList.remove('active'));
    el.classList.add('active');
    });
});

function switchTab(tab) {
  currentTab = tab;
  document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
  document.querySelector(`.nav-item[data-tab="${tab}"]`).classList.add('active');
  document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
  document.getElementById(tab).classList.add('active');
  document.getElementById('pageTitle').textContent = tabTitles[tab] || tab;
}

async function apiGet(path) {
  const res = await fetch(path);
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

function openScanModal() {
  const modal = document.getElementById('scanModal');
  modal.classList.add('visible');
  document.getElementById('scanPhaseText').textContent = '准备中...';
  document.getElementById('scanProgressBar').style.width = '0%';
  document.getElementById('scanProgressBar').classList.remove('indeterminate');
  document.getElementById('scanLogArea').innerHTML = '';
}

function closeScanModal() {
  const modal = document.getElementById('scanModal');
  modal.classList.remove('visible');
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
      if (status.scan_phase === 'completed') {
        updateScanStatus('completed', '扫描完成');
        document.getElementById('scanPhaseText').textContent = '扫描完成';
        progressBar.style.width = '100%';
        setTimeout(closeScanModal, 1500);
       } else {
        updateScanStatus('failed', status.scan_error || '失败');
        document.getElementById('scanPhaseText').textContent = '扫描失败';
        progressBar.style.width = '100%';
       }
      loadDashboard();
      return;
     }
    updateScanStatus('running', status.scan_phase || '扫描中');
    updateScanModal(status);
    setTimeout(check, 2000);
   };
  check();
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
  drawEquityChart(bt);
  drawScoreChart(s);
}

async function loadLatestCandidates() {
  const data = await apiGet('/api/results?scope=top&limit=20');
  const tbody = document.getElementById('latestTableBody');
  if (!data.rows || data.rows.length === 0) {
    tbody.innerHTML = '<tr><td colspan="6" class="empty">暂无候选数据</td></tr>';
    return;
  }
  tbody.innerHTML = data.rows.map(r => {
    const chg = r['涨跌幅'] || r['change_pct'] || 0;
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
  const data = await apiGet('/api/results?scope=backtest&limit=200');
  const tbody = document.getElementById('allCandidatesBody');
  if (!data.rows || data.rows.length === 0) {
    tbody.innerHTML = '<tr><td colspan="7" class="empty">暂无数据</td></tr>';
    return;
  }
  tbody.innerHTML = data.rows.map(r => {
    const chg = r['change_pct'] || r['涨跌幅'] || 0;
    const ret = r['next_day_open_return'] || 0;
    const cls = chg >= 0 ? 'change-positive' : 'change-negative';
    const retCls = ret >= 0 ? 'change-positive' : 'change-negative';
    return `<tr>
      <td>${r['date'] || ''}</td>
      <td>${r['code'] || ''}</td>
      <td>${r['name'] || ''}</td>
      <td>${r['price'] || '--'}</td>
      <td class="${cls}">${parseFloat(chg).toFixed(2)}%</td>
      <td>${r['turnover_rate_daily'] || '--'}</td>
      <td><strong>${r['score'] || '--'}</strong></td>
      <td class="${retCls}">${parseFloat(ret).toFixed(2)}%</td>
    </tr>`;
  }).join('');
}

async function loadBacktest() {
  const data = await apiGet('/api/summary');
  const bt = data.backtest || {};
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

  const picksData = await apiGet('/api/results?scope=backtest&limit=500');
  const tbody = document.getElementById('backtestBody');
  if (!picksData.rows || picksData.rows.length === 0) {
    tbody.innerHTML = '<tr><td colspan="6" class="empty">请先运行回测</td></tr>';
    return;
  }
  const byDate = {};
  picksData.rows.forEach(r => {
    const d = r['date'] || '未知';
    if (!byDate[d]) byDate[d] = { codes: [], scores: [], strongOpens: [], returns: [] };
    byDate[d].codes.push(r['code']);
    byDate[d].scores.push(parseFloat(r['score']) || 0);
    byDate[d].strongOpens.push(parseInt(r['is_strong_open']) || 0);
    byDate[d].returns.push(parseFloat(r['next_day_open_return']) || 0);
  });

  tbody.innerHTML = Object.entries(byDate).map(([date, data]) => {
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
  tbody.innerHTML = data.rows.slice(0, 50).map(r => {
    const ret = parseFloat(r['收益率'] || r['return_pct'] || 0);
    const cls = ret >= 0 ? 'change-positive' : 'change-negative';
    const status = r['状态'] || r['status'] || '';
    const badgeCls = status.includes('成功') || status.includes('盈利') ? 'badge-success' : status.includes('亏损') ? 'badge-danger' : 'badge-warning';
    return `<tr>
      <td>${r['scan_time'] || r['扫描时间'] || r['日期'] || ''}</td>
      <td>${r['code'] || r['代码'] || ''}</td>
      <td>${r['name'] || r['名称'] || ''}</td>
      <td>${r['买入价'] || r['buy_price'] || '--'}</td>
      <td>${r['卖出价'] || r['sell_price'] || '--'}</td>
      <td class="${cls}">${ret.toFixed(2)}%</td>
      <td><span class="badge ${badgeCls}">${status}</span></td>
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
  const ctx = canvas.getContext('2d');
  const w = canvas.parentElement.clientWidth - 40;
  const h = 200;
  canvas.width = w * 2;
  canvas.height = h * 2;
  canvas.style.width = w + 'px';
  canvas.style.height = h + 'px';
  ctx.scale(2, 2);
  ctx.clearRect(0, 0, w, h);

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
  const ctx = canvas.getContext('2d');
  const w = canvas.parentElement.clientWidth - 40;
  const h = 200;
  canvas.width = w * 2;
  canvas.height = h * 2;
  canvas.style.width = w + 'px';
  canvas.style.height = h + 'px';
  ctx.scale(2, 2);
  ctx.clearRect(0, 0, w, h);

  const mockData = [
    { label: '高分(>80)', value: 15, color: '#22c55e' },
    { label: '中上(60-80)', value: 35, color: '#10b981' },
    { label: '中等(40-60)', value: 55, color: '#3b82f6' },
    { label: '中下(20-40)', value: 25, color: '#f59e0b' },
    { label: '低分(<20)', value: 10, color: '#ef4444' },
  ];

  const total = mockData.reduce((s, d) => s + d.value, 0);
  const maxVal = Math.max(...mockData.map(d => d.value));
  const padL = 10, padR = 10, padT = 10, padB = 30;
  const barH = 28;
  const gap = 8;

  mockData.forEach((d, i) => {
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
  if (summaryData) {
    drawEquityChart(summaryData.backtest || {});
    drawScoreChart(summaryData);
  }
});
