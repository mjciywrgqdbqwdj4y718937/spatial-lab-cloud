/* Spatial Lab · 前端（原生 JS + three.js r128） */
'use strict';

const S = {
  pipelines: [],
  models: [],
  status: null,
  jobs: [],
  picked: null,          // 工具箱/编辑器选中的模型 id
  selectedFile: null,    // 上传的 File
  timer: null,           // 任务轮询
  editor: null,          // three.js 上下文
};

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
const esc = (s) => String(s ?? '').replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

/* 按需加载脚本（three.js 589 KB 不该拖累首屏） */
const _scriptCache = {};
function loadScript(src) {
  if (_scriptCache[src]) return _scriptCache[src];
  _scriptCache[src] = new Promise((resolve, reject) => {
    const s = document.createElement('script');
    s.src = src;
    s.onload = resolve;
    s.onerror = () => reject(new Error('脚本加载失败：' + src));
    document.head.appendChild(s);
  });
  return _scriptCache[src];
}
async function ensureThree() {
  if (window.__threeReady) return true;
  $('.hud') && ($('.hud').textContent = '加载 3D 引擎…');
  await loadScript('/static/vendor/three.min.js');
  await Promise.all([
    loadScript('/static/vendor/OrbitControls.js'),
    loadScript('/static/vendor/GLTFLoader.js'),
  ]);
  window.__threeReady = true;
  return true;
}

let _slowHinted = false;
function api(path, opts) {
  const t0 = performance.now();
  return fetch(path, opts).then(async (r) => {
    const t = await r.text();
    let j = null;
    try { j = t ? JSON.parse(t) : null; } catch (e) { j = { raw: t }; }
    if (!r.ok) throw new Error((j && (j.detail || j.error)) || ('HTTP ' + r.status));
    return j;
  }).finally(() => {
    const dt = (performance.now() - t0) / 1000;
    if (dt > 3 && !_slowHinted) {
      _slowHinted = true;
      toast(`这条请求花了 ${dt.toFixed(1)}s（公网隧道慢），本机打开更快：127.0.0.1:8791`, true);
    }
  });
}

function toast(msg, bad) {
  const box = $('#toast');
  const d = document.createElement('div');
  if (bad) d.className = 'bad';
  d.textContent = msg;
  box.appendChild(d);
  setTimeout(() => d.remove(), bad ? 5200 : 3200);
}

const fmtInt = (n) => (n == null ? '—' : Number(n).toLocaleString('zh-CN'));
const fmtSize = (arr) => (Array.isArray(arr) ? arr.map((x) => (+x).toFixed(2)).join(' × ') + ' m' : '—');
const fmtKB = (kb) => (kb == null ? '—' : kb > 1024 ? (kb / 1024).toFixed(1) + ' MB' : kb + ' KB');

/* ───────────────── 路由 ───────────────── */
const TITLES = {
  '/': ['工作台', '总览与快捷入口'],
  '/library': ['模型库', '所有生成的模型'],
  '/depth': ['单图重建', '深度 → 点云 → 泊松网格'],
  '/triposr': ['AI 生成', 'TripoSR 前馈重建'],
  '/multiview': ['多视角融合', '多张照片 → 配准 → 泊松重建'],
  '/text': ['文生 3D', '文本 → SD-Turbo 出图 → 网格'],
  '/editor': ['3D 编辑器', '查看 · 材质 · 出图'],
  '/toolbox': ['工具箱', '简化 · 平滑 · 修复 · 转换 · 渲染 · 高模 · UV'],
  '/system': ['系统状态', '引擎 · 权重 · 任务历史'],
};

const VIEWS = {
  '/': viewHome,
  '/library': viewLibrary,
  '/depth': () => viewPipeline('depth'),
  '/triposr': () => viewPipeline('triposr'),
  '/multiview': () => viewPipeline('multiview'),
  '/text': viewText,
  '/editor': viewEditor,
  '/toolbox': viewToolbox,
  '/system': viewSystem,
};

function markNavAvailability() {
  const map = { '/depth': 'depth', '/triposr': 'triposr', '/multiview': 'multiview', '/text': 'text3d' };
  $$('#nav .nav-item').forEach((a) => {
    const pid = map[a.dataset.route];
    if (!pid) return;
    const pipe = S.pipelines.find((x) => x.id === pid);
    if (pipe && pipe.available === false && !a.querySelector('.nav-warn')) {
      const tag = document.createElement('span');
      tag.className = 'nav-warn';
      tag.textContent = '需本地';
      tag.style.cssText = 'margin-left:auto;font-size:9.5px;padding:1px 6px;border-radius:8px;'
        + 'background:rgba(255,182,72,.15);border:1px solid rgba(255,182,72,.4);color:#ffb648';
      a.appendChild(tag);
    }
  });
}

function route() {
  markNavAvailability();
  const r = (location.hash.replace(/^#/, '') || '/');
  const [title, sub] = TITLES[r] || TITLES['/'];
  $('#crumb').innerHTML = esc(title) + (sub ? ` <em>${esc(sub)}</em>` : '');
  $$('#nav .nav-item').forEach((a) => a.classList.toggle('on', a.dataset.route === r));
  if (S.editor) { S.editor.dispose(); S.editor = null; }
  clearInterval(S.timer); S.timer = null;
  const v = VIEWS[r] || viewHome;
  v();
}

window.addEventListener('hashchange', route);

/* ───────────────── 全局状态轮询（防重入 + 页面隐藏时停） ───────────────── */
let _statusBusy = false;
async function refreshStatus() {
  if (_statusBusy) return S.status;
  _statusBusy = true;
  try {
    S.status = await api('/api/status');
    const e = S.status.engine;
    const pe = $('#pillEngine');
    if (!e.alive) { pe.className = 'pill bad'; pe.innerHTML = '<span class="dot"></span>引擎未启动'; }
    else if (!e.ready) { pe.className = 'pill warn'; pe.innerHTML = '<span class="dot"></span>引擎加载中'; }
    else { pe.className = 'pill ok'; pe.innerHTML = '<span class="dot"></span>引擎就绪'; }
    $('#pillQueue').textContent = `队列 ${S.status.jobs.running} · 任务 ${S.status.jobs.total}`;
    const gpu = e.gpu || {};
    $('#sbEngine').textContent = e.alive ? (e.ready ? '就绪' : '加载中') : '未启动';
    $('#sbGpu').textContent = gpu.name ? gpu.name.replace(/NVIDIA |GeForce /g, '') : 'CPU';
    $('#sbModels').textContent = S.status.models;
    $('#sbGpu').title = gpu.name || '';
    return S.status;
  } catch (e) {
    const pe = $('#pillEngine');
    if (pe) {
      pe.className = 'pill bad';
      pe.innerHTML = '<span class="dot"></span>服务不可达';
    }
  } finally {
    _statusBusy = false;
  }
}

setInterval(() => { const c = $('#clock'); if (c) c.textContent = new Date().toLocaleTimeString('zh-CN', { hour12: false }); }, 1000);
setInterval(() => { if (!document.hidden) refreshStatus(); }, 15000);
document.addEventListener('visibilitychange', () => { if (!document.hidden) refreshStatus(); });

/* ───────────────── 工作台 ───────────────── */
async function viewHome() {
  const view = $('#view');
  view.innerHTML = `<div class="empty">载入中…</div>`;
  await refreshStatus();
  const st = S.status || { engine: {}, jobs: {}, models: 0, storage: {}, triposr: {} };
  try { S.models = (await api('/api/models')).models; } catch (e) { S.models = []; }
  try { S.jobs = (await api('/api/jobs?limit=8')).jobs; } catch (e) { S.jobs = []; }

  const gpu = (st.engine && st.engine.gpu) || {};
  const recent = S.models.slice(0, 6);

  view.innerHTML = `
  <div class="hero">
    <div>
      <h1>本机 3D 生产线</h1>
      <p>${S.pipelines.length || 8} 条管线全部在你自己的机器上跑：单图重建、TripoSR 生成、网格加工、格式转换、软件渲染出图。模型、图片、产物不出本机。</p>
    </div>
    <div class="acts">
      <a class="btn primary" href="#/triposr">✦ 开始生成</a>
      <a class="btn" href="#/library">▦ 模型库</a>
    </div>
  </div>

  <div class="grid g4">
    <div class="card kpi"><div class="v">${st.engine.alive ? (st.engine.ready ? '在线' : '加载') : '离线'}</div><div class="k">推理引擎 · PID ${st.engine.pid || '—'}</div></div>
    <div class="card kpi"><div class="v sm">${gpu.name ? esc(gpu.name.replace('NVIDIA GeForce ', '').replace(' Laptop GPU', '')) : 'CPU'}<span class="u">${gpu.mem_gb ? ' · ' + gpu.mem_gb + ' GB' : ''}</span></div><div class="k">显卡 · ${gpu.cuda ? 'CUDA 可用' : '仅 CPU'}</div></div>
    <div class="card kpi"><div class="v">${st.models}<span class="u">个</span></div><div class="k">已生成模型</div></div>
    <div class="card kpi"><div class="v">${st.storage.work_mb ?? 0}<span class="u">MB</span></div><div class="k">产物占用</div></div>
  </div>

  <div class="quick">
    <a class="qcard" href="#/depth"><i>◍</i><b>单图重建</b><span>一张照片 → 带顶点色网格，可调面数与平滑</span></a>
    <a class="qcard" href="#/triposr"><i>✦</i><b>AI 生成</b><span>TripoSR 前馈重建，128–320 分辨率</span></a>
    <a class="qcard" href="#/multiview"><i>⧉</i><b>多视角融合</b><span>2–8 张不同角度 → ICP 配准 → 泊松重建</span></a>
    <a class="qcard" href="#/text"><i>✎</i><b>文生 3D</b><span>一句话 → SD-Turbo 出图 → TripoSR 出模</span></a>
  </div>

  <div class="quick" style="grid-template-columns:repeat(4,1fr)">
    <a class="qcard" href="#/editor"><i>◈</i><b>3D 编辑器</b><span>材质、线框、视角、截图、尺寸</span></a>
    <a class="qcard" href="#/toolbox"><i>⚙</i><b>高模细分</b><span>Loop 细分翻 4 倍面数，保留顶点色</span></a>
    <a class="qcard" href="#/toolbox"><i>◫</i><b>UV + 贴图</b><span>xatlas 展开 + 顶点色烘 2048 贴图</span></a>
    <a class="qcard" href="#/library"><i>▦</i><b>模型库</b><span>查看、下载、删除、二次加工</span></a>
  </div>

  <div class="grid g2" style="align-items:start">
    <div class="card">
      <h3>最近任务</h3>
      ${S.jobs.length ? `<table><thead><tr><th>任务</th><th>管线</th><th>状态</th><th>耗时</th></tr></thead><tbody>
        ${S.jobs.map((j) => `<tr>
          <td class="mono">${esc(j.job_id)}</td>
          <td>${esc(j.pipeline_name || j.pipeline || '')}</td>
          <td>${jobTag(j)}</td>
          <td class="mono">${j.ms ? (j.ms / 1000).toFixed(1) + 's' : (j.status === 'running' ? (j.progress || 0) + '%' : '—')}</td>
        </tr>`).join('')}
      </tbody></table>` : `<div class="empty">还没有任务，去「AI 生成」跑第一张图</div>`}
    </div>
    <div class="card">
      <h3>最近模型</h3>
      ${recent.length ? `<div class="lib" style="grid-template-columns:repeat(3,1fr);gap:10px">
        ${recent.map((m) => `<div class="mcard" onclick="openInEditor('${m.job_id}')">
          <div class="thumb"><img loading="lazy" decoding="async" src="${m.thumb}?t=${Date.now()}" onerror="this.parentNode.innerHTML='<span class=noimg>◈</span>'"></div>
          <div class="meta"><b>${esc(m.job_id)}</b><span>${fmtInt(m.tris)} 面 · ${fmtKB(m.kb)}</span></div>
        </div>`).join('')}
      </div>` : `<div class="empty">模型库是空的</div>`}
    </div>
  </div>`;
}

function jobTag(j) {
  if (j.status === 'done') return '<span class="tag ok">完成</span>';
  if (j.status === 'failed') return '<span class="tag bad">失败</span>';
  return `<span class="tag run">${j.progress || 0}%</span>`;
}

function openInEditor(jid) {
  S.picked = jid;
  location.hash = '#/editor';
}

/* ───────────────── 参数表单 ───────────────── */
function paramForm(pipeId, skip = []) {
  const p = S.pipelines.find((x) => x.id === pipeId);
  if (!p) return '';
  const fields = p.params.filter((f) => !skip.includes(f.k));
  if (!fields.length) return '<div class="note">该管线没有可调参数。</div>';
  return fields.map((f) => {
    const k = esc(f.k);
    if (f.type === 'range') {
      return `<div class="field" data-k="${k}">
        <label>${esc(f.label)}<b data-out="${k}">${f.default}</b></label>
        <input type="range" data-k="${k}" min="${f.min}" max="${f.max}" step="${f.step}" value="${f.default}"
               oninput="this.closest('.field').querySelector('[data-out=${k}]').textContent=this.value">
      </div>`;
    }
    if (f.type === 'select') {
      return `<div class="field"><label>${esc(f.label)}</label>
        <select data-k="${k}">${f.options.map((o) => `<option value="${esc(o)}" ${o === f.default ? 'selected' : ''}>${esc(o)}</option>`).join('')}</select>
      </div>`;
    }
    if (f.type === 'switch') {
      const on = !!f.default;
      return `<div class="field"><div class="switch ${on ? 'on' : ''}" data-k="${k}" data-val="${on ? 1 : 0}"
        onclick="this.classList.toggle('on');this.dataset.val=this.classList.contains('on')?1:0">
        <span>${esc(f.label)}</span><div class="knob"></div></div></div>`;
    }
    if (f.type === 'text') {
      return `<div class="field"><label>${esc(f.label)}</label>
        <input type="text" data-k="${k}" value="${esc(f.default || '')}" placeholder="${esc(f.placeholder || '')}">
      </div>`;
    }
    return '';
  }).join('');
}

function collectParams(root) {
  const out = {};
  $$('[data-k]', root).forEach((el) => {
    const k = el.dataset.k;
    if (el.classList.contains('switch')) { out[k] = el.dataset.val === '1'; return; }
    if (el.tagName === 'SELECT' || el.tagName === 'INPUT') {
      const v = el.value;
      out[k] = v !== '' && !isNaN(+v) ? +v : v;
    }
  });
  return out;
}

/* ───────────────── 生成页（depth / triposr） ───────────────── */
async function viewPipeline(pipeId) {
  if (!S.pipelines.length) S.pipelines = (await api('/api/pipelines')).pipelines;
  const p = S.pipelines.find((x) => x.id === pipeId);
  const view = $('#view');
  const multi = p.needs === 'images';
  S.selectedFile = null;
  S.selectedFiles = [];

  const blocked = p.available === false;
  view.innerHTML = `
  ${blocked ? `<div class="notice" style="margin-bottom:14px;border-color:rgba(255,182,72,.35);background:rgba(255,182,72,.06)">⚠️ <b>${esc(p.name)}</b> 需要 GPU，当前云端模式无法运行。请在本地平台打开 <code>http://127.0.0.1:8791/</code>。</div>` : ''}
  <div class="grid g2" style="grid-template-columns: 1fr 380px; align-items:start">
    <div class="card">
      <h3>输入</h3>
      <div class="drop" id="drop">
        <i>⬆</i>
        <b>${multi ? '拖入多张照片，或点击选择（2–8 张不同角度）' : '拖入图片，或点击选择'}</b>
        <span>${multi ? '正面 / 侧面 / 背面 / 斜 45°，主体居中、背景干净效果最好' : '支持 JPG / PNG / WEBP / BMP · 主体居中、背景干净效果最好'}</span>
      </div>
      <input type="file" id="file" accept="image/*" ${multi ? 'multiple' : ''} hidden>
      <div id="prev" style="margin-top:12px"></div>
      <div class="hr"></div>
      <div class="note">${esc(p.desc)}</div>
    </div>

    <div class="card">
      <h3>参数</h3>
      <div id="params">${paramForm(pipeId)}</div>
      <div class="hr"></div>
      <button class="btn primary" id="run" style="width:100%;justify-content:center">▶ 开始生成</button>
      <div id="prog" style="margin-top:12px"></div>
    </div>
  </div>
  <div id="result" style="margin-top:14px"></div>`;

  const drop = $('#drop'), file = $('#file');
  drop.onclick = () => file.click();
  drop.ondragover = (e) => { e.preventDefault(); drop.classList.add('over'); };
  drop.ondragleave = () => drop.classList.remove('over');
  drop.ondrop = (e) => {
    e.preventDefault(); drop.classList.remove('over');
    if (e.dataTransfer.files.length) setFiles(e.dataTransfer.files);
  };
  file.onchange = () => { if (file.files.length) setFiles(file.files); };

  function setFiles(list) {
    const arr = Array.from(list).filter((f) => f.type.startsWith('image/'));
    if (!arr.length) return toast('请选择图片文件', true);
    if (multi) {
      S.selectedFiles = arr.slice(0, 8);
      S.selectedFile = S.selectedFiles[0];
      $('#prev').innerHTML = `<div class="row" style="gap:8px;flex-wrap:wrap">
        ${S.selectedFiles.map((f, i) => `<div style="position:relative">
          <img src="${URL.createObjectURL(f)}" style="width:76px;height:76px;object-fit:cover;border-radius:9px;border:1px solid var(--line)">
          <span class="tag" style="position:absolute;left:3px;top:3px;font-size:9px">${i + 1}</span>
        </div>`).join('')}
      </div>
      <div class="row sm mut" style="margin-top:8px"><span>已选 ${S.selectedFiles.length} 张不同视角</span></div>`;
    } else {
      const f = arr[0];
      S.selectedFile = f;
      S.selectedFiles = [f];
      $('#prev').innerHTML = `<img class="preview" src="${URL.createObjectURL(f)}">
        <div class="row sm mut" style="margin-top:8px"><span>${esc(f.name)}</span><span>${(f.size / 1024).toFixed(0)} KB</span></div>`;
    }
  }

  if (p.available === false) {
    // 云模式：允许提交到混合云队列，由本地 GPU 代跑
    const btn = $('#run');
    if (btn) btn.textContent = '提交到云端队列（本地 GPU 代跑）';
    $('#run').onclick = async () => {
      if (!S.selectedFile) return toast(multi ? '先选至少一张图片' : '先选一张图片', true);
      const params = collectParams($('#params'));
      const fd = new FormData();
      fd.append('pipeline', pipeId);
      fd.append('params', JSON.stringify(params));
      if (multi) S.selectedFiles.forEach((f) => fd.append('image', f));
      else fd.append('image', S.selectedFile);
      $('#run').disabled = true;
      $('#prog').innerHTML = `<div class="jobline"><span class="st">上传中…</span></div><div class="bar"><i style="width:2%"></i></div>`;
      try {
        const r = await submitQueue(fd, (pct) => {
          $('#prog').innerHTML = `<div class="jobline"><span class="st">上传中</span><span>${Math.round(pct * 100)}%</span></div>
            <div class="bar"><i style="width:${Math.max(2, pct * 100)}%"></i></div>`;
        });
        toast('已进入云端队列：' + r.task_id);
        pollQueue(r.task_id, $('#prog'), (t) => {
          $('#run').disabled = false;
          if (t.status === 'done') {
            const res = t.result || {};
            $('#result').innerHTML = `<div class="card"><h3>完成 · 由本地 GPU 代跑</h3>
              <div class="grid g4" style="margin-top:12px">
                <div class="card kpi" style="background:#0a1017"><div class="v">${fmtInt(res.tris)}</div><div class="k">三角面</div></div>
                <div class="card kpi" style="background:#0a1017"><div class="v">${fmtInt(res.verts)}</div><div class="k">顶点</div></div>
                <div class="card kpi" style="background:#0a1017"><div class="v">${t.glb_url ? '可下载' : '—'}</div><div class="k">产物</div></div>
                <div class="card kpi" style="background:#0a1017"><div class="v">${res.kb || '—'}</div><div class="k">KB</div></div>
              </div>
              ${t.glb_url ? `<div class="row" style="margin-top:12px">
                <a class="btn sm primary" href="${t.glb_url}" download>⬇ 下载 GLB</a>
                ${t.thumb_url ? `<img src="${t.thumb_url}" style="width:120px;border-radius:8px;border:1px solid var(--line)">` : ''}
              </div>` : ''}
            </div>`;
            toast('任务完成');
          } else {
            $('#result').innerHTML = `<div class="card"><h3>失败</h3><div class="note" style="color:var(--bad)">${esc(t.error || '')}</div></div>`;
            toast('任务失败', true);
          }
        });
      } catch (e) {
        $('#run').disabled = false;
        toast(e.message, true);
      }
    };
    return;
  }
  $('#run').onclick = async () => {
    if (!S.selectedFile) return toast(multi ? '先选至少一张图片' : '先选一张图片', true);
    if (multi && S.selectedFiles.length < 2) return toast('多视角融合至少需要 2 张不同角度的照片', true);
    const params = collectParams($('#params'));
    const fd = new FormData();
    fd.append('pipeline', pipeId);
    fd.append('params', JSON.stringify(params));
    if (multi) {
      S.selectedFiles.forEach((f) => fd.append('images', f));
    } else {
      fd.append('image', S.selectedFile);
    }
    $('#run').disabled = true;
    $('#prog').innerHTML = `<div class="jobline"><span class="st">准备上传…</span></div><div class="bar"><i style="width:2%"></i></div>`;
    try {
      const r = await submitJob(fd, (pct) => {
        $('#prog').innerHTML = `<div class="jobline"><span class="st">上传中</span><span>${Math.round(pct * 100)}%</span></div>
          <div class="bar"><i style="width:${Math.max(2, pct * 100)}%"></i></div>`;
      });
      toast('任务已提交：' + r.job_id);
      pollJob(r.job_id, $('#prog'), (job) => {
        $('#run').disabled = false;
        if (job.status === 'done') {
          const res = job.result || {};
          $('#result').innerHTML = resultCard(job.job_id, res, job);
          S.models = []; refreshStatus();
          toast('完成：' + fmtInt(res.tris) + ' 面');
        } else if (job.status === 'failed') {
          $('#result').innerHTML = `<div class="card"><h3>失败</h3><div class="note" style="color:var(--bad)">${esc(job.error || '')}</div></div>`;
          toast('任务失败', true);
        }
      });
    } catch (e) {
      $('#run').disabled = false;
      toast(e.message, true);
      $('#prog').innerHTML = '';
    }
  };
}

/* ───────────────── 文生 3D ───────────────── */
async function viewText() {
  if (!S.pipelines.length) S.pipelines = (await api('/api/pipelines')).pipelines;
  const p3d = S.pipelines.find((x) => x.id === 'text3d');
  const def = (p3d || { params: [] }).params.find((x) => x.k === 'prompt');
  const view = $('#view');
  const examples = [
    'a single red vintage teapot, product photo, plain white background',
    'a single wooden chair, studio photo, white background',
    'a single blue robot toy, product photo, white background',
    'a single ceramic vase with flowers, product photo, white background',
  ];

  view.innerHTML = `
  <div class="grid g2" style="grid-template-columns: 1fr 380px; align-items:start">
    <div class="card">
      <h3>描述</h3>
      <textarea id="prompt" rows="4" style="width:100%;background:#0a1018;border:1px solid var(--line2);color:var(--fg);padding:11px 13px;border-radius:10px;font-size:13px;line-height:1.6;outline:none;resize:vertical"
        placeholder="a single white ceramic teapot, product photo, plain white background">${esc((def && def.default) || '')}</textarea>
      <div class="note sm" style="margin-top:8px">英文更准。写清「单一物体 + 背景干净」——TripoSR 对整栋建筑、场景、线条海报无效，会出一团结块。</div>
      <div class="hr"></div>
      <h3>示例</h3>
      <div class="row" style="gap:6px;flex-wrap:wrap">
        ${examples.map((s) => `<button class="btn sm" data-ex="${esc(s)}">${esc(s.split(',')[0].replace('a single ', ''))}</button>`).join('')}
      </div>
      <div class="hr"></div>
      <div class="note">流程：文本 → <b>SD-Turbo 本地出图（1–2 步）</b> → <b>TripoSR 前馈重建</b> → 带顶点色网格。全在本机，不联网。</div>
    </div>
    <div class="card">
      <h3>参数</h3>
      <div id="params">${paramForm('text3d', ['prompt'])}</div>
      <div class="hr"></div>
      <button class="btn primary" id="run3d" style="width:100%;justify-content:center">✦ 生成 3D 模型</button>
      <div style="height:8px"></div>
      <button class="btn" id="run2d" style="width:100%;justify-content:center">▧ 只出图看看</button>
      <div id="prog" style="margin-top:12px"></div>
    </div>
  </div>
  <div id="result" style="margin-top:14px"></div>`;

  $$('[data-ex]').forEach((b) => {
    b.onclick = () => { $('#prompt').value = b.dataset.ex; toast('已填入示例'); };
  });

  async function submit(pipeline) {
    const prompt = $('#prompt').value.trim();
    if (!prompt) return toast('先写一段描述', true);
    const params = collectParams($('#params'));
    params.prompt = prompt;
    const fd = new FormData();
    fd.append('pipeline', pipeline);
    fd.append('params', JSON.stringify(params));
    $('#run3d').disabled = true; $('#run2d').disabled = true;
    $('#prog').innerHTML = `<div class="jobline"><span class="st">提交中…</span></div><div class="bar"><i style="width:2%"></i></div>`;
    try {
      const r = await submitJob(fd);
      toast('任务已提交：' + r.job_id);
      pollJob(r.job_id, $('#prog'), (job) => {
        $('#run3d').disabled = false; $('#run2d').disabled = false;
        if (job.status === 'done') {
          const res = job.result || {};
          const img = res.png ? `<div class="card" style="margin-bottom:14px"><h3>中间图（SD-Turbo）</h3>
              <img src="/files/${job.job_id}/${res.png}?t=${Date.now()}" style="max-width:420px;width:100%;border-radius:10px;border:1px solid var(--line)">
              <div class="note sm" style="margin-top:8px;word-break:break-all">${esc(res.prompt || '')}</div></div>` : '';
          $('#result').innerHTML = img + (res.glb ? resultCard(job.job_id, res, job) : '');
          S.models = []; refreshStatus();
          toast(res.glb ? ('模型完成：' + fmtInt(res.tris) + ' 面') : '出图完成');
        } else if (job.status === 'failed') {
          $('#result').innerHTML = `<div class="card"><h3>失败</h3><div class="note" style="color:var(--bad)">${esc(job.error || '')}</div></div>`;
          toast('任务失败', true);
        }
      });
    } catch (e) {
      $('#run3d').disabled = false; $('#run2d').disabled = false;
      toast(e.message, true);
    }
  }

  $('#run3d').onclick = () => submit('text3d');
  $('#run2d').onclick = () => submit('text2img');
}

function resultCard(jid, res, job) {
  return `<div class="card">
    <div class="row" style="justify-content:space-between">
      <h3 style="margin:0">结果 · ${esc(jid)}</h3>
      <div class="row">
        <a class="btn sm" href="#/editor" onclick="S.picked='${jid}'">◈ 在编辑器打开</a>
        ${res.glb ? `<a class="btn sm primary" href="/files/${jid}/${res.glb}" download>⬇ 下载 GLB</a>` : ''}
        ${res.ply ? `<a class="btn sm" href="/files/${jid}/${res.ply}" download>PLY</a>` : ''}
        ${res.texture ? `<a class="btn sm" href="/files/${jid}/${res.texture}" download>贴图 PNG</a>` : ''}
        ${res.file && !res.glb ? `<a class="btn sm" href="/files/${jid}/${res.file}" download>⬇ ${esc(res.file)}</a>` : ''}
      </div>
    </div>
    <div class="grid g4" style="margin-top:14px">
      <div class="card kpi" style="background:#0a1017"><div class="v">${fmtInt(res.tris)}</div><div class="k">三角面</div></div>
      <div class="card kpi" style="background:#0a1017"><div class="v">${fmtInt(res.verts)}</div><div class="k">顶点</div></div>
      <div class="card kpi" style="background:#0a1017"><div class="v" style="font-size:15px">${fmtSize(res.size)}</div><div class="k">包围盒</div></div>
      <div class="card kpi" style="background:#0a1017"><div class="v" style="font-size:15px">${job.ms ? (job.ms / 1000).toFixed(1) + 's' : '—'}</div><div class="k">耗时</div></div>
    </div>
    <div class="row" style="margin-top:12px">
      <span class="tag ${res.watertight ? 'ok' : ''}">${res.watertight ? '水密' : '非水密'}</span>
      ${res.views ? `<span class="tag local">${res.views} 视角融合</span>` : ''}
      ${res.faces_before ? `<span class="tag">${fmtInt(res.faces_before)} → ${fmtInt(res.tris)} 面</span>` : ''}
      ${res.volume != null ? `<span class="tag">体积 ${res.volume}</span>` : ''}
      ${res.area != null ? `<span class="tag">表面积 ${res.area}</span>` : ''}
      ${res.kb ? `<span class="tag">${res.kb} KB</span>` : ''}
      ${res.tex_kb ? `<span class="tag">贴图 ${res.tex_kb} KB</span>` : ''}
    </div>
  </div>`;
}

/* 带上传进度的提交（fetch 拿不到上传进度，公网大图必须给反馈） */
function submitJob(fd, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open('POST', '/api/run');
    xhr.timeout = 600000;
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable && onProgress) onProgress(e.loaded / e.total);
    };
    xhr.onload = () => {
      let j = null;
      try { j = JSON.parse(xhr.responseText); } catch (e) { j = null; }
      if (xhr.status >= 200 && xhr.status < 300 && j && j.job_id) resolve(j);
      else reject(new Error((j && (j.detail || j.error)) || ('HTTP ' + xhr.status)));
    };
    xhr.onerror = () => reject(new Error('上传失败：网络中断'));
    xhr.ontimeout = () => reject(new Error('上传超时'));
    xhr.send(fd);
  });
}

/* ── 混合云队列：云端提交、本地 GPU 代跑 ── */
function submitQueue(fd, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open('POST', '/api/queue/submit');
    xhr.timeout = 600000;
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable && onProgress) onProgress(e.loaded / e.total);
    };
    xhr.onload = () => {
      let j = null;
      try { j = JSON.parse(xhr.responseText); } catch (e) { j = null; }
      if (xhr.status >= 200 && xhr.status < 300 && j && j.task_id) resolve(j);
      else reject(new Error((j && (j.detail || j.error)) || ('HTTP ' + xhr.status)));
    };
    xhr.onerror = () => reject(new Error('上传失败：网络中断'));
    xhr.ontimeout = () => reject(new Error('上传超时'));
    xhr.send(fd);
  });
}

function pollQueue(tid, box, done) {
  let busy = false;
  const tick = async () => {
    if (busy) return;
    busy = true;
    try {
      const t = await api('/api/queue/status/' + tid);
      const label = t.status === 'pending' ? '排队中（等待本地 GPU 上线）'
        : t.status === 'claimed' ? '本地 GPU 正在处理…'
        : t.status === 'done' ? '完成' : '失败';
      if (box) {
        const pct = t.status === 'done' ? 100 : (t.status === 'claimed' ? 60 : 10);
        box.innerHTML = `<div class="jobline"><span class="st">${esc(label)}</span>
          <span class="spacer"></span><span class="mono">${esc(tid)}</span></div>
          <div class="bar"><i style="width:${pct}%"></i></div>
          ${t.error ? `<div class="note" style="color:var(--bad);margin-top:8px">${esc(t.error)}</div>` : ''}`;
      }
      if (t.status === 'done' || t.status === 'failed') {
        clearInterval(S.qtimer); S.qtimer = null;
        if (done) done(t);
      }
    } catch (e) {
      // 忽略单次失败
    } finally {
      busy = false;
    }
  };
  tick();
  S.qtimer = setInterval(() => { if (!document.hidden) tick(); }, 3000);
}

function pollJob(jid, box, done) {
  clearInterval(S.timer);
  let misses = 0;
  const tick = async () => {
    try {
      const j = await api('/api/job/' + jid);
      misses = 0;
      if (box) {
        box.innerHTML = `<div class="jobline"><span class="st">${esc(j.stage || j.status)}</span>
          <span>${j.progress || 0}%</span><span class="spacer"></span><span class="mono">${esc(jid)}</span></div>
          <div class="bar"><i style="width:${j.progress || 1}%"></i></div>`;
      }
      if (j.status === 'done' || j.status === 'failed') {
        clearInterval(S.timer); S.timer = null;
        if (done) done(j);
      }
    } catch (e) {
      misses += 1;
      if (misses >= 5) {
        clearInterval(S.timer); S.timer = null;
        toast('查询任务连续失败：' + e.message, true);
      }
    }
  };
  tick();
  // 公网隧道下 800ms 一次会把请求堆死：降到 2.2 秒，且页面隐藏时更慢
  S.timer = setInterval(() => { if (!document.hidden) tick(); }, 2200);
}

/* ───────────────── 模型库 ───────────────── */
let libFilter = 'hi';

async function viewLibrary() {
  const view = $('#view');
  view.innerHTML = `<div class="empty">载入中…</div>`;
  await loadModels();
  renderLib('');
}

async function loadModels() {
  try { S.models = (await api('/api/models')).models; } catch (e) { S.models = []; }
}

function libFiltered(q) {
  let list = S.models;
  if (libFilter === 'hi') list = list.filter((m) => (m.tris || 0) > 5000);
  else if (libFilter === 'mid') list = list.filter((m) => (m.tris || 0) > 1000 && (m.tris || 0) <= 5000);
  else if (libFilter === 'low') list = list.filter((m) => (m.tris || 0) <= 1000);
  else if (libFilter === 'lib') list = list.filter((m) => m.pipeline === 'library');
  else if (libFilter === 'mine') list = list.filter((m) => m.pipeline !== 'library');
  else if (['人物', '车辆', '武器', '屋子'].includes(libFilter)) {
    list = list.filter((m) => (m.asset_name || '').startsWith(libFilter + '·'));
  }
  if (q) {
    const k = q.toLowerCase();
    list = list.filter((m) => (m.asset_name || '').toLowerCase().includes(k)
      || m.job_id.includes(k) || (m.pipeline_name || '').toLowerCase().includes(k));
  }
  return list.slice().sort((a, b) => (b.tris || 0) - (a.tris || 0));
}

function renderLib(q) {
  const view = $('#view');
  const list = libFiltered(q);
  const hiN = S.models.filter((m) => (m.tris || 0) > 5000).length;
  const catCount = (g) => S.models.filter((m) => (m.asset_name || '').startsWith(g + '·')).length;
  const tabs = [
    ['全部', `全部 ${S.models.length}`],
    ['hi', `高模 ${hiN}`],
    ['人物', `人物 ${catCount('人物')}`],
    ['车辆', `车辆 ${catCount('车辆')}`],
    ['武器', `武器 ${catCount('武器')}`],
    ['屋子', `屋子 ${catCount('屋子')}`],
    ['lib', '官方资产'],
    ['mine', '我生成的'],
  ];
  view.innerHTML = `
    <div class="lib-head">
      <input type="text" id="libSearch" placeholder="搜索模型名 / ID / 管线…" value="${esc(q)}" style="max-width:260px">
      <button class="btn sm" id="libReload">↻ 刷新</button>
      <span class="spacer"></span>
      <a class="btn sm primary" href="#/triposr">＋ 新建</a>
    </div>
    <div class="row" style="gap:6px;margin-bottom:14px;flex-wrap:wrap">
      ${tabs.map(([k, label]) => `<button class="btn sm ${libFilter === k ? 'primary' : ''}" data-filter="${k}">${label}</button>`).join('')}
      <span class="tag" style="margin-left:auto">当前显示 ${list.length} 个</span>
    </div>
    ${list.length ? `<div class="lib">${list.map(card).join('')}</div>` : `<div class="empty">没有匹配的模型</div>`}`;

  $$('[data-filter]').forEach((b) => {
    b.onclick = () => { libFilter = b.dataset.filter; renderLib($('#libSearch').value.trim()); };
  });
  $('#libSearch').oninput = (e) => { const v = e.target.value.trim(); renderLibKeep(v); };
  $('#libReload').onclick = async () => { await loadModels(); renderLib(q); toast('已刷新'); };
  bindLibCards();   // ← 之前漏了这一行，卡片渲染出来但没绑点击
}

function renderLibKeep(q) {
  const list = libFiltered(q);
  const wrap = $('.lib');
  const holder = document.createElement('div');
  holder.innerHTML = list.length ? `<div class="lib">${list.map(card).join('')}</div>` : `<div class="empty">无匹配</div>`;
  if (wrap) wrap.replaceWith(holder.firstElementChild); else $('#view').appendChild(holder.firstElementChild);
  bindLibCards();
}

function card(m) {
  const title = m.asset_name || m.job_id;
  const sub = m.asset_name ? (m.pipeline === 'library' ? '官方资产' : (m.pipeline_name || '')) : (m.pipeline_name || '');
  return `<div class="mcard" data-jid="${m.job_id}">
    <div class="thumb"><img loading="lazy" decoding="async" src="${m.thumb}?t=${encodeURIComponent(m.mtime || '')}" onerror="this.parentNode.innerHTML='<span class=noimg>◈</span>'"></div>
    <div class="meta">
      <b>${esc(title)}</b>
      <span>${m.tris ? fmtInt(m.tris) + ' 面' : ''} · ${fmtKB(m.kb)}</span>
      <span>${esc(sub)} · ${esc(m.mtime || '')}</span>
    </div>
    <div class="ops">
      <button class="btn sm" data-act="edit">◈ 查看</button>
      <a class="btn sm" href="/files/${m.job_id}/${esc(m.file || 'model.glb')}" download>⬇</a>
      <button class="btn sm danger" data-act="del">✕</button>
    </div>
  </div>`;
}

function bindLibCards() {
  $$('.mcard').forEach((el) => {
    const jid = el.dataset.jid;
    el.onclick = (e) => {
      const act = e.target.closest('[data-act]');
      if (act) {
        e.stopPropagation();
        if (act.dataset.act === 'del') delModel(jid);
        if (act.dataset.act === 'edit') openInEditor(jid);
        return;
      }
      openInEditor(jid);
    };
  });
}

async function delModel(jid) {
  if (!confirm(`删除模型 ${jid}？产物目录会被清掉。`)) return;
  try {
    await api('/api/model/' + jid, { method: 'DELETE' });
    S.models = S.models.filter((m) => m.job_id !== jid);
    toast('已删除 ' + jid);
    renderLib($('#libSearch') ? $('#libSearch').value.trim() : '');
  } catch (e) { toast(e.message, true); }
}

/* ───────────────── 3D 编辑器 ───────────────── */
async function viewEditor() {
  const view = $('#view');
  if (!S.models.length) await loadModels();
  const cur = S.picked && S.models.find((m) => m.job_id === S.picked) ? S.picked : (S.models[0] && S.models[0].job_id);
  S.picked = cur;

  view.innerHTML = `
  <div class="editor">
    <div class="canvas-wrap" id="wrap">
      <div class="empty-canvas" id="canvasHint">${cur ? '加载中…' : '模型库是空的，去生成一个'}</div>
      <div class="hud" id="hud"><span>拖拽旋转</span><span>滚轮缩放</span><span>右键平移</span></div>
    </div>
    <div class="panel">
      <div class="card">
        <h3>模型</h3>
        <select id="selModel">${S.models.map((m) => `<option value="${m.job_id}" ${m.job_id === cur ? 'selected' : ''}>${m.job_id} · ${m.pipeline_name || ''}</option>`).join('') || '<option>无</option>'}</select>
        <div class="hr"></div>
        <div class="row sm"><a class="btn sm" id="btnDl">⬇ 下载</a><button class="btn sm" id="btnShot">▣ 截图</button></div>
      </div>
      <div class="card">
        <h3>材质</h3>
        <div class="field"><label>金属感<b id="vMetal">0.08</b></label><input type="range" id="metal" min="0" max="1" step="0.02" value="0.08"></div>
        <div class="field"><label>粗糙度<b id="vRough">0.72</b></label><input type="range" id="rough" min="0" max="1" step="0.02" value="0.72"></div>
        <div class="field"><label>发光<b id="vEmis">0.00</b></label><input type="range" id="emis" min="0" max="1" step="0.02" value="0"></div>
        <div class="switch" id="swWire"><span>线框</span><div class="knob"></div></div>
        <div style="height:8px"></div>
        <div class="switch on" id="swVColor"><span>顶点色</span><div class="knob"></div></div>
      </div>
      <div class="card">
        <h3>场景</h3>
        <div class="switch on" id="swGrid"><span>地面网格</span><div class="knob"></div></div>
        <div style="height:8px"></div>
        <div class="switch" id="swSpin"><span>自动旋转</span><div class="knob"></div></div>
        <div style="height:8px"></div>
        <div class="row">
          <button class="btn sm" data-cam="1">正视</button>
          <button class="btn sm" data-cam="2">侧视</button>
          <button class="btn sm" data-cam="3">俯视</button>
        </div>
      </div>
      <div class="card">
        <h3>几何</h3>
        <div id="geoInfo" class="note">—</div>
      </div>
    </div>
  </div>`;

  if (!cur) return;
  try {
    await ensureThree();
  } catch (e) {
    const hint = $('#canvasHint');
    if (hint) {
      hint.style.display = 'grid';
      hint.innerHTML = '3D 引擎加载失败（公网隧道较慢）<br><span class="sm mut">本地打开 127.0.0.1:8791 秒开</span>';
    }
    return;
  }
  mountEditor(cur);
  bindEditorControls();
}

function mountEditor(jid) {
  const wrap = $('#wrap');
  const hint = $('#canvasHint');
  const m = S.models.find((x) => x.job_id === jid) || {};
  const file = m.file || 'model.glb';

  const scene = new THREE.Scene();
  const cam = new THREE.PerspectiveCamera(42, wrap.clientWidth / wrap.clientHeight, 0.01, 4000);
  const renderer = new THREE.WebGLRenderer({ antialias: true, preserveDrawingBuffer: true, alpha: false });
  renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
  renderer.setSize(wrap.clientWidth, wrap.clientHeight);
  renderer.setClearColor(0x070b11, 1);
  renderer.outputEncoding = THREE.sRGBEncoding;
  wrap.insertBefore(renderer.domElement, hint);

  scene.add(new THREE.HemisphereLight(0xcfe6ff, 0x24303c, 1.15));
  const key = new THREE.DirectionalLight(0xffffff, 1.5); key.position.set(3, 5, 4); scene.add(key);
  const rim = new THREE.DirectionalLight(0x7b6cff, 0.75); rim.position.set(-4, 2, -3); scene.add(rim);
  const fill = new THREE.DirectionalLight(0x38e0c8, 0.45); fill.position.set(0, -3, 2); scene.add(fill);

  const grid = new THREE.GridHelper(6, 24, 0x1d2b3a, 0x141d28);
  grid.position.y = -1.001;
  scene.add(grid);

  const controls = new THREE.OrbitControls(cam, renderer.domElement);
  controls.enableDamping = true; controls.dampingFactor = 0.08;
  controls.minDistance = 0.4; controls.maxDistance = 40;

  let root = null, spin = false;
  const api_ = {
    scene, cam, renderer, controls, grid,
    dispose() {
      try { controls.dispose(); } catch (e) {}
      try { renderer.dispose(); renderer.domElement.remove(); } catch (e) {}
    },
  };
  S.editor = api_;

  const loader = new THREE.GLTFLoader();
  hint.textContent = '解析模型中…';
  loader.load(`/files/${jid}/${file}`, (gltf) => {
    root = gltf.scene;
    scene.add(root);
    // 统一材质 & 顶点色标记
    root.traverse((o) => {
      if (!o.isMesh) return;
      o.geometry.computeBoundingBox();
      const mats = Array.isArray(o.material) ? o.material : [o.material];
      o.userData.mats = mats;
      o.userData.hasColor = !!(o.geometry.attributes.color);
      mats.forEach((mt) => {
        mt.side = THREE.DoubleSide;
        mt.vertexColors = !!o.userData.hasColor;
        mt.needsUpdate = true;
      });
    });
    fitCamera();
    hint.style.display = 'none';
    $('#hud').style.display = 'flex';
  }, (p) => {
    if (p.total) hint.textContent = `解析模型… ${Math.round(p.loaded / p.total * 100)}%`;
  }, (err) => {
    hint.textContent = '模型载入失败：' + (err && err.message ? err.message : err);
    hint.style.display = 'grid';
  });

  function fitCamera() {
    const box = new THREE.Box3().setFromObject(root);
    const size = box.getSize(new THREE.Vector3());
    const center = box.getCenter(new THREE.Vector3());
    const maxDim = Math.max(size.x, size.y, size.z) || 1;
    const dist = maxDim * 2.1;
    cam.position.set(center.x + dist * 0.75, center.y + dist * 0.5, center.z + dist * 0.85);
    cam.near = maxDim / 1000; cam.far = maxDim * 100; cam.updateProjectionMatrix();
    controls.target.copy(center);
    controls.update();
    const g = Math.max(maxDim * 3, 2);
    grid.scale.set(g / 6, 1, g / 6);
    grid.position.y = box.min.y - maxDim * 0.02;
    $('#geoInfo').innerHTML = `
      <div>面数 <b>${fmtInt(m.tris)}</b> · 顶点 <b>${fmtInt(m.verts)}</b></div>
      <div>尺寸 <b>${fmtSize(m.size)}</b></div>
      <div>水密 <b>${m.watertight ? '是' : '否'}</b>${m.volume != null ? ` · 体积 <b>${m.volume}</b>` : ''}</div>
      <div class="mut">文件 ${esc(file)} · ${fmtKB(m.kb)}</div>`;
  }

  let lastCall = 0;
  function loop(t) {
    api_.raf = requestAnimationFrame(loop);
    if (spin && root) root.rotation.y += 0.006;
    controls.update();
    renderer.render(scene, cam);
  }
  loop(0);

  function onResize() {
    if (!wrap.clientWidth) return;
    cam.aspect = wrap.clientWidth / wrap.clientHeight;
    cam.updateProjectionMatrix();
    renderer.setSize(wrap.clientWidth, wrap.clientHeight);
  }
  window.addEventListener('resize', onResize);
  api_.onResize = onResize;

  api_.setMat = (k, v) => {
    if (!root) return;
    root.traverse((o) => {
      if (!o.isMesh) return;
      (o.userData.mats || []).forEach((mt) => {
        if (k === 'metal') mt.metalness = v;
        if (k === 'rough') mt.roughness = v;
        if (k === 'emis') { mt.emissive = new THREE.Color(0x38e0c8); mt.emissiveIntensity = v; }
        if (k === 'wire') mt.wireframe = v;
        if (k === 'vcolor') { mt.vertexColors = v && o.userData.hasColor; }
        mt.needsUpdate = true;
      });
    });
  };
  api_.setSpin = (v) => { spin = v; };
  api_.setGrid = (v) => { grid.visible = v; };
  api_.shot = () => {
    renderer.render(scene, cam);
    const a = document.createElement('a');
    a.href = renderer.domElement.toDataURL('image/png');
    a.download = `render_${jid}.png`;
    a.click();
  };
  api_.camera = (n) => {
    if (!root) return;
    const box = new THREE.Box3().setFromObject(root);
    const c = box.getCenter(new THREE.Vector3());
    const s = box.getSize(new THREE.Vector3());
    const d = Math.max(s.x, s.y, s.z) * 2.1;
    if (n === 1) cam.position.set(c.x, c.y, c.z + d);
    if (n === 2) cam.position.set(c.x + d, c.y, c.z);
    if (n === 3) cam.position.set(c.x, c.y + d, c.z + 0.001);
    controls.target.copy(c); controls.update();
  };
  // 卸载时清动画
  const oldDispose = api_.dispose;
  api_.dispose = () => {
    cancelAnimationFrame(api_.raf);
    window.removeEventListener('resize', onResize);
    oldDispose();
  };
}

function bindEditorControls() {
  const E = () => S.editor;
  $('#selModel').onchange = (e) => {
    S.picked = e.target.value;
    if (S.editor) S.editor.dispose();
    $('#canvasHint').textContent = '加载中…';
    $('#canvasHint').style.display = 'grid';
    mountEditor(S.picked);
    bindEditorControls();
  };
  $('#metal').oninput = (e) => { $('#vMetal').textContent = (+e.target.value).toFixed(2); E().setMat('metal', +e.target.value); };
  $('#rough').oninput = (e) => { $('#vRough').textContent = (+e.target.value).toFixed(2); E().setMat('rough', +e.target.value); };
  $('#emis').oninput = (e) => { $('#vEmis').textContent = (+e.target.value).toFixed(2); E().setMat('emis', +e.target.value); };
  $('#swWire').onclick = (e) => { e.currentTarget.classList.toggle('on'); E().setMat('wire', e.currentTarget.classList.contains('on')); };
  $('#swVColor').onclick = (e) => { e.currentTarget.classList.toggle('on'); E().setMat('vcolor', e.currentTarget.classList.contains('on')); };
  $('#swGrid').onclick = (e) => { e.currentTarget.classList.toggle('on'); E().setGrid(e.currentTarget.classList.contains('on')); };
  $('#swSpin').onclick = (e) => { e.currentTarget.classList.toggle('on'); E().setSpin(e.currentTarget.classList.contains('on')); };
  $$('[data-cam]').forEach((b) => { b.onclick = () => E().camera(+b.dataset.cam); });
  $('#btnShot').onclick = () => E().shot();
  const m = S.models.find((x) => x.job_id === S.picked);
  $('#btnDl').onclick = () => { if (m) location.href = `/files/${m.job_id}/${m.file || 'model.glb'}`; };
}

/* ───────────────── 工具箱 ───────────────── */
async function viewToolbox() {
  const view = $('#view');
  if (!S.models.length) await loadModels();
  if (!S.models.length) {
    view.innerHTML = `<div class="empty">模型库是空的，先去生成一个模型。</div>`;
    return;
  }
  const tools = S.pipelines.filter((p) => ['simplify', 'smooth', 'repair', 'convert', 'render', 'measure', 'highpoly', 'uvbake', 'normalize', 'assetcheck', 'lod'].includes(p.id));
  const curModel = S.picked && S.models.some((m) => m.job_id === S.picked) ? S.picked : S.models[0].job_id;
  const curTool = S.tool || 'simplify';

  view.innerHTML = `
  <div class="grid g2" style="grid-template-columns: 300px 1fr; align-items:start">
    <div class="card">
      <h3>选择模型</h3>
      <select id="toolModel">${S.models.map((m) => `<option value="${m.job_id}" ${m.job_id === curModel ? 'selected' : ''}>${m.job_id} · ${fmtInt(m.tris)} 面</option>`).join('')}</select>
      <div class="hr"></div>
      <div class="row"><span class="tag" id="toolModelInfo">—</span></div>
      <div class="hr"></div>
      <h3>操作</h3>
      <div style="display:flex;flex-direction:column;gap:6px">
        ${tools.map((t) => `<button class="btn sm" data-tool="${t.id}" style="justify-content:flex-start">${esc(t.name)}</button>`).join('')}
      </div>
    </div>
    <div class="card">
      <h3 id="toolTitle">${esc((tools.find((t) => t.id === curTool) || tools[0]).name)}</h3>
      <div class="note" id="toolDesc">${esc((tools.find((t) => t.id === curTool) || tools[0]).desc)}</div>
      <div class="hr"></div>
      <div id="toolParams">${paramForm(curTool)}</div>
      <div class="hr"></div>
      <button class="btn primary" id="toolRun" style="width:100%;justify-content:center">▶ 运行</button>
      <div id="toolProg" style="margin-top:12px"></div>
    </div>
  </div>
  <div id="toolResult" style="margin-top:14px"></div>`;

  const info = S.models.find((m) => m.job_id === curModel);
  $('#toolModelInfo').textContent = `${fmtInt(info.tris)} 面 · ${fmtKB(info.kb)}`;
  $('#toolModel').onchange = (e) => {
    S.picked = e.target.value;
    const m = S.models.find((x) => x.job_id === S.picked);
    $('#toolModelInfo').textContent = `${fmtInt(m.tris)} 面 · ${fmtKB(m.kb)}`;
  };
  $$('[data-tool]').forEach((b) => {
    b.onclick = () => {
      S.tool = b.dataset.tool;
      const t = tools.find((x) => x.id === S.tool);
      $('#toolTitle').textContent = t.name;
      $('#toolDesc').textContent = t.desc;
      $('#toolParams').innerHTML = paramForm(t.id);
      $$('[data-tool]').forEach((x) => x.style.borderColor = '');
      b.style.borderColor = 'rgba(56,224,200,.5)';
      $('#toolResult').innerHTML = '';
    };
  });
  $('#toolRun').onclick = async () => {
    const params = collectParams($('#toolParams'));
    const fd = new FormData();
    fd.append('pipeline', S.tool || 'simplify');
    fd.append('params', JSON.stringify(params));
    fd.append('source', $('#toolModel').value);
    $('#toolRun').disabled = true;
    try {
      const r = await submitJob(fd);
      pollJob(r.job_id, $('#toolProg'), async (job) => {
        $('#toolRun').disabled = false;
        if (job.status === 'done') {
          await loadModels();
          const res = job.result || {};
          const m = res.measured;
          if (m && m.grade) {
            const badges = { ok: '✅', warn: '⚠️', bad: '❌' };
            $('#toolResult').innerHTML = `<div class="card">
              <div class="row"><h3 style="margin:0">游戏就绪度体检</h3><span class="spacer"></span>
                <span class="tag ${m.score >= 85 ? 'ok' : m.score >= 65 ? '' : 'bad'}">${m.score} 分 · ${esc(m.grade)}</span></div>
              <table style="margin-top:12px"><tbody>
                ${m.items.map((it) => `<tr>
                  <td style="width:26px">${badges[it.级别] || ''}</td>
                  <td style="width:90px">${esc(it.名)}</td>
                  <td style="width:150px"><b>${esc(String(it.值))}</b></td>
                  <td class="sm mut">${esc(it.建议)}</td></tr>`).join('')}
              </tbody></table></div>`;
          } else if (m && m.lod) {
            $('#toolResult').innerHTML = `<div class="card"><h3>LOD 输出</h3>
              <table><thead><tr><th>级别</th><th>面数</th><th>大小</th><th>文件</th></tr></thead><tbody>
                ${m.lod.map((L) => `<tr><td><b>${esc(L.level)}</b></td><td>${fmtInt(L.tris)}</td><td>${L.kb} KB</td>
                  <td><a class="btn sm" href="/files/${job.job_id}/${esc(L.file)}" download>⬇ ${esc(L.file)}</a></td></tr>`).join('')}
              </tbody></table>
              <div class="note sm" style="margin-top:10px">原始 ${fmtInt(m.lod[0].tris)} 面，共 ${m.lod.length} 级。附 lod_manifest.json 供引擎导入。</div>
            </div>`;
          } else if (m) {
            $('#toolResult').innerHTML = `<div class="card"><h3>测量结果</h3>
              <table><tbody>
                ${Object.entries(m).map(([k, v]) => `<tr><td>${esc(k)}</td><td><b>${esc(Array.isArray(v) ? v.join(' × ') : v)}</b></td></tr>`).join('')}
              </tbody></table></div>`;
          } else if (res.png) {
            $('#toolResult').innerHTML = `<div class="card"><h3>渲染结果</h3>
              <img src="/files/${job.job_id}/${res.png}?t=${Date.now()}" style="max-width:100%;border-radius:10px;border:1px solid var(--line)">
              <div class="row" style="margin-top:10px"><a class="btn sm primary" href="/files/${job.job_id}/${res.png}" download>⬇ 下载 PNG</a></div></div>`;
          } else {
            $('#toolResult').innerHTML = resultCard(job.job_id, res, job);
          }
          toast('完成：' + (res.file || res.glb || res.png || ''));
        } else if (job.status === 'failed') {
          $('#toolResult').innerHTML = `<div class="card"><h3>失败</h3><div class="note" style="color:var(--bad)">${esc(job.error || '')}</div></div>`;
          toast('任务失败', true);
        }
      });
    } catch (e) {
      $('#toolRun').disabled = false;
      toast(e.message, true);
    }
  };
}

/* ───────────────── 系统状态 ───────────────── */
async function viewSystem() {
  const view = $('#view');
  view.innerHTML = `<div class="empty">载入中…</div>`;
  await refreshStatus();
  let jobs = [];
  try { jobs = (await api('/api/jobs?limit=40')).jobs; } catch (e) {}
  const st = S.status || {};
  const g = (st.engine && st.engine.gpu) || {};
  const tp = st.triposr || {};

  view.innerHTML = `
  <div class="grid g2" style="align-items:start">
    <div class="card">
      <h3>服务</h3>
      <table><tbody>
        <tr><td>监听端口</td><td><b>${st.service ? st.service.port : '—'}</b></td></tr>
        <tr><td>启动时刻</td><td><b>${esc(st.service && st.service.uptime_since || '—')}</b></td></tr>
        <tr><td>引擎进程</td><td><b>${st.engine && st.engine.alive ? '运行中 PID ' + st.engine.pid : '未启动'}</b></td></tr>
        <tr><td>引擎就绪</td><td>${st.engine && st.engine.ready ? '<span class="tag ok">就绪</span>' : '<span class="tag run">加载中</span>'}</td></tr>
        <tr><td>累计任务</td><td><b>${st.engine ? st.engine.tasks : 0}</b></td></tr>
        <tr><td>产物占用</td><td><b>${st.storage ? st.storage.work_mb : 0} MB</b></td></tr>
        <tr><td>上传占用</td><td><b>${st.storage ? st.storage.uploads_mb : 0} MB</b></td></tr>
      </tbody></table>
    </div>
    <div class="card">
      <h3>算力与权重</h3>
      <table><tbody>
        <tr><td>GPU</td><td><b>${esc(g.name || 'CPU')}</b></td></tr>
        <tr><td>显存</td><td><b>${g.mem_gb ? g.mem_gb + ' GB' : '—'}</b></td></tr>
        <tr><td>CUDA</td><td>${g.cuda ? '<span class="tag ok">可用</span>' : '<span class="tag bad">不可用</span>'}</td></tr>
        <tr><td>Torch</td><td><b>${esc(g.torch || '—')}</b></td></tr>
        <tr><td>TripoSR 权重</td><td><b>${tp.weights_mb ? tp.weights_mb + ' MB' : '缺失'}</b></td></tr>
        <tr><td>权重路径</td><td class="mono sm">${esc(tp.weights || '—')}</td></tr>
        <tr><td>配置</td><td>${tp.config ? '<span class="tag ok">就绪</span>' : '<span class="tag bad">缺失</span>'}</td></tr>
      </tbody></table>
    </div>
  </div>

  <div class="card" style="margin-top:14px">
    <h3>管线（${S.pipelines.length}）</h3>
    <table><thead><tr><th>ID</th><th>名称</th><th>分组</th><th>输入</th></tr></thead><tbody>
      ${S.pipelines.map((p) => `<tr><td class="mono">${esc(p.id)}</td><td>${esc(p.name)}</td><td>${esc(p.group)}</td><td>${p.needs === 'image' ? '图片' : p.needs === 'model' ? '已有模型' : '无'}</td></tr>`).join('')}
    </tbody></table>
  </div>

  <div class="card" style="margin-top:14px">
    <h3>任务历史</h3>
    ${jobs.length ? `<table><thead><tr><th>ID</th><th>管线</th><th>状态</th><th>进度</th><th>耗时</th><th>开始</th><th>错误</th></tr></thead><tbody>
      ${jobs.map((j) => `<tr>
        <td class="mono">${esc(j.job_id)}</td>
        <td>${esc(j.pipeline_name || j.pipeline || '')}</td>
        <td>${jobTag(j)}</td>
        <td class="mono">${j.progress || 0}%</td>
        <td class="mono">${j.ms ? (j.ms / 1000).toFixed(1) + 's' : '—'}</td>
        <td class="mono">${esc(j.started_at || '')}</td>
        <td class="sm" style="color:var(--bad)">${esc((j.error || '').slice(0, 80))}</td>
      </tr>`).join('')}
    </tbody></table>` : '<div class="empty">暂无任务</div>'}
  </div>`;
}

/* ───────────────── 启动 ───────────────── */
(async function boot() {
  // Service Worker：公网隧道下第二次访问近乎秒开
  if ('serviceWorker' in navigator) {
    navigator.serviceWorker.register('/sw.js').catch(() => {});
  }
  // 侧栏点击兜底：即使 href 被某些环境拦截，也强制切路由
  $$('#nav .nav-item').forEach((a) => {
    a.addEventListener('click', (e) => {
      const r = a.dataset.route;
      if (!r) return;
      e.preventDefault();
      if (location.hash === '#' + r) route();
      else location.hash = '#' + r;
    });
  });
  try { S.pipelines = (await api('/api/pipelines')).pipelines; } catch (e) { S.pipelines = []; }
  await refreshStatus();
  route();
  window.S = S;
  window.openInEditor = openInEditor;
  // 自检：?selftest 时把滚动/布局实测写进 title，便于外部抓取
  if (location.search.includes('selftest')) {
    setTimeout(() => {
      const v = $('#view');
      const before = v.scrollTop;
      v.scrollTop = 400;
      const after1 = v.scrollTop;
      v.scrollTo(0, 300);
      const after2 = v.scrollTop;
      document.title = `SELFTEST SH=${v.scrollHeight} CH=${v.clientHeight} `
        + `ST0=${before} ST1=${after1} ST2=${after2} `
        + `OY=${getComputedStyle(v).overflowY} `
        + `MAIN=${$('main').clientHeight} SIDE=${$('.side').clientHeight} `
        + `NAV=${document.querySelectorAll('#nav .nav-item').length} `
        + `HREF=${document.querySelector('#nav .nav-item').getAttribute('href')}`;
    }, 2600);
  }
  // 自检：?libtest 验证模型库卡片能否点开编辑器
  if (location.search.includes('libtest')) {
    setTimeout(async () => {
      location.hash = '#/library';
      await new Promise((r) => setTimeout(r, 2200));
      const card = document.querySelector('.mcard');
      if (!card) { document.title = 'LIBTEST no-card'; return; }
      const jid = card.dataset.jid;
      card.click();
      setTimeout(() => {
        const hasCanvas = !!document.querySelector('.canvas-wrap');
        const crumb = (document.querySelector('#crumb') || {}).textContent || '';
        document.title = `LIBTEST card=${jid} hash=${location.hash} canvas=${hasCanvas} crumb=${crumb.trim()}`;
      }, 3000);
    }, 2000);
  }
})();
