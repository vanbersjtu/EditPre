const state = {
  jobId: null,
  pollTimer: null,
  svgBlobUrls: [],
  lastSvgPagesKey: null,
};

const JOB_HISTORY_KEY = 'slide-app-job-history';
const JOB_HISTORY_MAX = 50;

const elements = {
  apiBase: document.getElementById('apiBase'),
  uploadForm: document.getElementById('uploadForm'),
  pdfFile: document.getElementById('pdfFile'),
  refillMode: document.getElementById('refillMode'),
  submitBtn: document.getElementById('submitBtn'),
  statusPill: document.getElementById('statusPill'),
  statusText: document.getElementById('statusText'),
  pageCount: document.getElementById('pageCount'),
  svgCount: document.getElementById('svgCount'),
  refillModeValue: document.getElementById('refillModeValue'),
  pptxLink: document.getElementById('pptxLink'),
  logTail: document.getElementById('logTail'),
  svgGrid: document.getElementById('svgGrid'),
  svgMeta: document.getElementById('svgMeta'),
  clearJobBtn: document.getElementById('clearJobBtn'),
  jobHistoryList: document.getElementById('jobHistoryList'),
  clearHistoryBtn: document.getElementById('clearHistoryBtn'),
};

const configBase = (window.SLIDE_APP_CONFIG && window.SLIDE_APP_CONFIG.apiBase) || '';
const storedBase = localStorage.getItem('slide-app-api-base') || '';
const storedRefillMode = localStorage.getItem('slide-app-refill-mode') || 'source-crop';
// 从 Vercel 打开时强制用 config 里的隧道地址，避免 localStorage 里存的本机地址导致 Failed to fetch
const isVercel = window.location.hostname.endsWith('vercel.app');
elements.apiBase.value = (isVercel && configBase) ? configBase : (storedBase || configBase);
elements.refillMode.value = storedRefillMode;

function getApiBase() {
  const raw = elements.apiBase.value.trim();
  localStorage.setItem('slide-app-api-base', raw);
  return raw.replace(/\/$/, '');
}

function getRefillMode() {
  const mode = elements.refillMode.value || 'source-crop';
  localStorage.setItem('slide-app-refill-mode', mode);
  return mode;
}

function getJobHistory() {
  try {
    const raw = localStorage.getItem(JOB_HISTORY_KEY);
    return raw ? JSON.parse(raw) : [];
  } catch {
    return [];
  }
}

function saveJobHistory(list) {
  localStorage.setItem(JOB_HISTORY_KEY, JSON.stringify(list.slice(0, JOB_HISTORY_MAX)));
}

function addToJobHistory(entry) {
  const list = getJobHistory();
  const newEntry = {
    jobId: entry.jobId,
    filename: entry.filename || entry.originalFilename || 'PDF',
    createdAt: entry.createdAt || new Date().toISOString(),
    status: entry.status || 'queued',
  };
  const filtered = list.filter((e) => e.jobId !== newEntry.jobId);
  saveJobHistory([newEntry, ...filtered]);
  renderJobHistory();
}

function updateJobInHistory(jobId, patch) {
  const list = getJobHistory();
  const idx = list.findIndex((e) => e.jobId === jobId);
  if (idx === -1) return;
  list[idx] = { ...list[idx], ...patch };
  saveJobHistory(list);
  renderJobHistory();
}

function renderJobHistory() {
  if (!elements.jobHistoryList) return;
  const list = getJobHistory();
  elements.jobHistoryList.innerHTML = list
    .map(
      (e) =>
        `<li>
          <span class="job-history-filename" title="${(e.filename || e.jobId).replace(/"/g, '&quot;')}">${e.filename || e.jobId}</span>
          <span class="job-history-status ${(e.status || '').toLowerCase()}">${e.status || '?'}</span>
          <button type="button" class="view-job-btn" data-job-id="${e.jobId}">查看</button>
        </li>`
    )
    .join('');
  elements.jobHistoryList.querySelectorAll('.view-job-btn').forEach((btn) => {
    btn.addEventListener('click', () => loadJobFromHistory(btn.dataset.jobId));
  });
}

async function loadJobFromHistory(jobId) {
  if (!jobId) return;
  try {
    setBusy(true);
    state.jobId = jobId;
    const response = await apiFetch(apiUrl(`/api/jobs/${jobId}`));
    if (!response.ok) {
      setStatus('error', `任务不存在或已过期 (${response.status})`);
      setBusy(false);
      return;
    }
    const job = await response.json();
    renderJob(job);
    updateJobInHistory(jobId, { status: job.status, filename: job.originalFilename || job.artifacts?.sourcePdf });
    if (job.status !== 'succeeded' && job.status !== 'failed') {
      startPolling();
    } else {
      setBusy(false);
    }
  } catch (err) {
    setBusy(false);
    setStatus('error', err.message || '加载任务失败');
  }
}

function apiUrl(path) {
  const base = getApiBase();
  if (!base) {
    return path;
  }
  return `${base}${path}`;
}

function apiFetch(url, options) {
  const headers = new Headers(options && options.headers);
  if (url.includes('loca.lt')) {
    headers.set('Bypass-Tunnel-Reminder', 'true');
  }
  return fetch(url, { ...options, headers, credentials: 'omit' });
}

function setBusy(isBusy) {
  elements.submitBtn.disabled = isBusy;
  elements.submitBtn.textContent = isBusy ? 'Running...' : 'Run Pipeline';
}

function setStatus(status, text) {
  elements.statusPill.textContent = status;
  elements.statusText.textContent = text;
}

function renderLog(lines) {
  elements.logTail.textContent = lines && lines.length ? lines.join('\n') : 'Pipeline output will appear here.';
}

function isTunnelUrl() {
  const base = getApiBase();
  return base && base.includes('loca.lt');
}

function loadSvgPreview(imgEl, url) {
  apiFetch(url)
    .then((r) => r.blob())
    .then((blob) => {
      const objUrl = URL.createObjectURL(blob);
      state.svgBlobUrls.push(objUrl);
      imgEl.src = objUrl;
    })
    .catch(() => {
      imgEl.alt = 'Preview load failed';
    });
}

function renderSvgCards(job) {
  state.svgBlobUrls.forEach((u) => URL.revokeObjectURL(u));
  state.svgBlobUrls = [];
  const pages = job.svgPages || [];
  elements.svgMeta.textContent = `${pages.length} page${pages.length === 1 ? '' : 's'}`;
  elements.svgGrid.innerHTML = '';
  const useTunnel = isTunnelUrl();
  for (const page of pages) {
    const fullUrl = apiUrl(page.url);
    const card = document.createElement('article');
    card.className = 'svg-card';
    card.innerHTML = `
      <header>
        <span>Page ${page.pageNumber}</span>
        <span>${page.name}</span>
      </header>
      <img alt="SVG preview for page ${page.pageNumber}" loading="lazy" />
      <footer>
        <a href="${useTunnel ? '#' : fullUrl}" target="_blank" rel="noreferrer" ${useTunnel ? `data-svg-url="${fullUrl}"` : ''}>Open SVG</a>
      </footer>
    `;
    const img = card.querySelector('img');
    if (useTunnel) {
      loadSvgPreview(img, fullUrl);
    } else {
      img.src = fullUrl;
    }
    const openLink = card.querySelector('a[data-svg-url]');
    if (openLink && useTunnel) {
      openLink.addEventListener('click', (e) => {
        e.preventDefault();
        apiFetch(openLink.getAttribute('data-svg-url'))
          .then((r) => r.blob())
          .then((blob) => {
            const u = URL.createObjectURL(blob);
            window.open(u, '_blank');
          });
      });
    }
    elements.svgGrid.appendChild(card);
  }
}

function svgPagesKey(pages) {
  if (!pages || !pages.length) return '';
  return pages.map((p) => p.url || p.name).join(',');
}

function renderJob(job) {
  state.jobId = job.jobId;
  setStatus(job.status, `${job.stage || 'queued'}${job.error ? `: ${job.error}` : ''}`);
  elements.pageCount.textContent = String(job.pageCount || 0);
  elements.svgCount.textContent = String(job.svgCount || 0);
  elements.refillModeValue.textContent = (job.settings && job.settings.refillMode) || 'source-crop';
  renderLog(job.logTail || []);
  const newKey = svgPagesKey(job.svgPages);
  if (newKey !== state.lastSvgPagesKey) {
    state.lastSvgPagesKey = newKey;
    renderSvgCards(job);
  } else {
    elements.svgMeta.textContent = `${(job.svgPages || []).length} page${(job.svgPages || []).length === 1 ? '' : 's'}`;
  }
  if (job.pptxUrl) {
    const pptxFullUrl = apiUrl(job.pptxUrl);
    if (isTunnelUrl()) {
      elements.pptxLink.href = '#';
      elements.pptxLink.dataset.pptxUrl = pptxFullUrl;
    } else {
      elements.pptxLink.href = pptxFullUrl;
      delete elements.pptxLink.dataset.pptxUrl;
    }
    elements.pptxLink.classList.remove('hidden');
  } else {
    elements.pptxLink.classList.add('hidden');
    elements.pptxLink.removeAttribute('href');
    delete elements.pptxLink.dataset.pptxUrl;
  }
}

async function fetchJob() {
  if (!state.jobId) return;
  const response = await apiFetch(apiUrl(`/api/jobs/${state.jobId}`));
  if (!response.ok) {
    throw new Error(`Failed to poll job (${response.status})`);
  }
  const job = await response.json();
  renderJob(job);
  updateJobInHistory(job.jobId, { status: job.status, filename: job.originalFilename });
  if (job.status === 'succeeded' || job.status === 'failed') {
    stopPolling();
    setBusy(false);
  }
}

function startPolling() {
  stopPolling();
  const pollIntervalMs = 1000;
  state.pollTimer = window.setInterval(() => {
    fetchJob().catch((error) => {
      stopPolling();
      setBusy(false);
      setStatus('error', error.message);
    });
  }, pollIntervalMs);
}

function stopPolling() {
  if (state.pollTimer) {
    window.clearInterval(state.pollTimer);
    state.pollTimer = null;
  }
}

const CHUNK_SIZE = 3 * 1024 * 1024;
const CHUNK_THRESHOLD = 5 * 1024 * 1024;
const TUNNEL_MAX_MB = 35;

function parseJsonResponse(response, fallbackMsg) {
  return response.json().catch(() => {
    throw new Error(
      response.ok ? '服务器返回格式异常' : (fallbackMsg || `上传失败 (${response.status})`)
    );
  });
}

async function createJob(file) {
  const formData = new FormData();
  formData.append('file', file);
  formData.append('refill_mode', getRefillMode());
  let response;
  try {
    response = await apiFetch(apiUrl('/api/jobs'), {
      method: 'POST',
      body: formData,
    });
  } catch (err) {
    const msg = err && err.message ? err.message : String(err);
    if (/fetch|network|failed/i.test(msg)) {
      throw new Error(
        '上传请求失败（网络/隧道超时）。大文件请使用分片上传（约 30MB 内支持），或在本机直连后端。'
      );
    }
    throw err;
  }
  const payload = await parseJsonResponse(response, `上传失败 (${response.status})，请检查后端再试`);
  if (!response.ok) {
    const detail = Array.isArray(payload.detail) ? payload.detail.map((d) => d.msg || d).join('; ') : payload.detail;
    throw new Error(detail || `Upload failed (${response.status})`);
  }
  addToJobHistory({ jobId: payload.jobId, filename: payload.originalFilename, status: payload.status || 'queued' });
  return payload;
}

async function createJobChunked(file) {
  const totalChunks = Math.ceil(file.size / CHUNK_SIZE);
  const initForm = new FormData();
  initForm.append('filename', file.name);
  initForm.append('refill_mode', getRefillMode());
  let response = await apiFetch(apiUrl('/api/jobs/init'), { method: 'POST', body: initForm });
  let payload = await parseJsonResponse(response, '初始化分片任务失败');
  if (!response.ok) {
    const detail = Array.isArray(payload.detail) ? payload.detail.map((d) => d.msg || d).join('; ') : payload.detail;
    throw new Error(detail || `Init failed (${response.status})`);
  }
  const jobId = payload.jobId;
  for (let i = 0; i < totalChunks; i++) {
    setStatus('uploading', `上传分片 ${i + 1}/${totalChunks} …`);
    const start = i * CHUNK_SIZE;
    const end = Math.min(start + CHUNK_SIZE, file.size);
    const chunk = file.slice(start, end);
    const chunkForm = new FormData();
    chunkForm.append('chunk_index', i);
    chunkForm.append('total_chunks', totalChunks);
    chunkForm.append('file', chunk, file.name);
    response = await apiFetch(apiUrl(`/api/jobs/${jobId}/chunk`), { method: 'POST', body: chunkForm });
    payload = await parseJsonResponse(response, `分片 ${i + 1} 上传失败`);
    if (!response.ok) {
    const detail = Array.isArray(payload.detail) ? payload.detail.map((d) => d.msg || d).join('; ') : payload.detail;
    throw new Error(detail || `Chunk ${i + 1} failed (${response.status})`);
    }
  }
  addToJobHistory({ jobId: payload.jobId, filename: file.name, status: payload.status || 'queued' });
  return payload;
}

elements.uploadForm.addEventListener('submit', async (event) => {
  event.preventDefault();
  const file = elements.pdfFile.files && elements.pdfFile.files[0];
  if (!file) {
    setStatus('idle', 'Choose one PDF first.');
    return;
  }
  if (isTunnelUrl() && file.size > TUNNEL_MAX_MB * 1024 * 1024) {
    setStatus(
      'error',
      `当前通过隧道上传，单文件不超过 ${TUNNEL_MAX_MB}MB（当前约 ${(file.size / 1024 / 1024).toFixed(1)}MB）。请压缩或分页后重试，或在本机直连后端。`
    );
    return;
  }
  try {
    setBusy(true);
    const useChunked = isTunnelUrl() && file.size > CHUNK_THRESHOLD;
    setStatus('uploading', useChunked ? `准备分片上传 ${file.name} …` : `Submitting ${file.name}`);
    const job = useChunked ? await createJobChunked(file) : await createJob(file);
    renderJob(job);
    startPolling();
  } catch (error) {
    setBusy(false);
    setStatus('error', error.message);
  }
});

elements.pptxLink.addEventListener('click', (e) => {
  const url = elements.pptxLink.dataset.pptxUrl;
  if (url) {
    e.preventDefault();
    apiFetch(url).then((r) => r.blob()).then((blob) => {
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = 'slides.pptx';
      a.click();
      URL.revokeObjectURL(a.href);
    });
  }
});

elements.clearJobBtn.addEventListener('click', () => {
  stopPolling();
  state.jobId = null;
  state.lastSvgPagesKey = null;
  elements.uploadForm.reset();
  elements.refillMode.value = localStorage.getItem('slide-app-refill-mode') || 'source-crop';
  setBusy(false);
  setStatus('idle', 'No job running.');
  elements.pageCount.textContent = '0';
  elements.svgCount.textContent = '0';
  elements.refillModeValue.textContent = elements.refillMode.value;
  elements.svgGrid.innerHTML = '';
  elements.svgMeta.textContent = '0 pages';
  elements.pptxLink.classList.add('hidden');
  renderLog([]);
});

window.addEventListener('beforeunload', stopPolling);
elements.refillMode.addEventListener('change', getRefillMode);
elements.refillModeValue.textContent = elements.refillMode.value;

if (elements.clearHistoryBtn) {
  elements.clearHistoryBtn.addEventListener('click', () => {
    saveJobHistory([]);
    renderJobHistory();
  });
}
renderJobHistory();
