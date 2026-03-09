const state = {
  jobId: null,
  pollTimer: null,
};

const elements = {
  apiBase: document.getElementById('apiBase'),
  uploadForm: document.getElementById('uploadForm'),
  pdfFile: document.getElementById('pdfFile'),
  requestProvider: document.getElementById('requestProvider'),
  requestApiBase: document.getElementById('requestApiBase'),
  requestApiKey: document.getElementById('requestApiKey'),
  defaultModel: document.getElementById('defaultModel'),
  imageModel: document.getElementById('imageModel'),
  refillMode: document.getElementById('refillMode'),
  submitBtn: document.getElementById('submitBtn'),
  statusPill: document.getElementById('statusPill'),
  statusText: document.getElementById('statusText'),
  pageCount: document.getElementById('pageCount'),
  svgCount: document.getElementById('svgCount'),
  refillModeValue: document.getElementById('refillModeValue'),
  requestProviderValue: document.getElementById('requestProviderValue'),
  requestApiBaseValue: document.getElementById('requestApiBaseValue'),
  defaultModelValue: document.getElementById('defaultModelValue'),
  imageModelValue: document.getElementById('imageModelValue'),
  pptxLink: document.getElementById('pptxLink'),
  logTail: document.getElementById('logTail'),
  svgGrid: document.getElementById('svgGrid'),
  svgMeta: document.getElementById('svgMeta'),
  clearJobBtn: document.getElementById('clearJobBtn'),
};

const uiConfig = window.SLIDE_APP_CONFIG || {};
const runtimeDefaults = {
  requestProvider: uiConfig.requestProvider || 'openai-compatible',
  openaiCompatibleApiBase: uiConfig.openaiCompatibleApiBase || 'https://cdn.12ai.org/v1',
  geminiNativeApiBase: uiConfig.geminiNativeApiBase || 'https://generativelanguage.googleapis.com/v1beta',
  defaultModel: uiConfig.defaultModel || 'gemini-3.1-pro-preview',
  imageModel: uiConfig.imageModel || 'gemini-3.1-flash-image-preview',
  refillMode: uiConfig.refillMode || 'source-crop',
};

const storageKeys = {
  apiBase: 'slide-app-api-base',
  requestProvider: 'slide-app-request-provider',
  requestApiBase: 'slide-app-request-api-base',
  defaultModel: 'slide-app-default-model',
  imageModel: 'slide-app-image-model',
  refillMode: 'slide-app-refill-mode',
};

function defaultRequestApiBase(provider) {
  return provider === 'gemini-native'
    ? runtimeDefaults.geminiNativeApiBase
    : runtimeDefaults.openaiCompatibleApiBase;
}

function knownApiBases() {
  return new Set([
    runtimeDefaults.openaiCompatibleApiBase,
    runtimeDefaults.geminiNativeApiBase,
  ]);
}

function readStored(key) {
  return localStorage.getItem(key) || '';
}

function persist(key, value) {
  localStorage.setItem(key, value);
  return value;
}

function getApiBase() {
  const raw = elements.apiBase.value.trim();
  persist(storageKeys.apiBase, raw);
  return raw.replace(/\/$/, '');
}

function getRequestProvider() {
  const provider = elements.requestProvider.value || runtimeDefaults.requestProvider;
  persist(storageKeys.requestProvider, provider);
  return provider;
}

function getRequestApiBase() {
  const raw = elements.requestApiBase.value.trim() || defaultRequestApiBase(getRequestProvider());
  elements.requestApiBase.value = raw;
  persist(storageKeys.requestApiBase, raw);
  return raw.replace(/\/$/, '');
}

function getDefaultModel() {
  const value = elements.defaultModel.value.trim() || runtimeDefaults.defaultModel;
  elements.defaultModel.value = value;
  persist(storageKeys.defaultModel, value);
  return value;
}

function getImageModel() {
  const value = elements.imageModel.value.trim() || runtimeDefaults.imageModel;
  elements.imageModel.value = value;
  persist(storageKeys.imageModel, value);
  return value;
}

function getRefillMode() {
  const mode = elements.refillMode.value || runtimeDefaults.refillMode;
  persist(storageKeys.refillMode, mode);
  return mode;
}

function apiUrl(path) {
  const base = getApiBase();
  if (!base) {
    return path;
  }
  return `${base}${path}`;
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

function renderSvgCards(job) {
  const pages = job.svgPages || [];
  elements.svgMeta.textContent = `${pages.length} page${pages.length === 1 ? '' : 's'}`;
  elements.svgGrid.innerHTML = '';
  for (const page of pages) {
    const card = document.createElement('article');
    card.className = 'svg-card';
    card.innerHTML = `
      <header>
        <span>Page ${page.pageNumber}</span>
        <span>${page.name}</span>
      </header>
      <img src="${apiUrl(page.url)}" alt="SVG preview for page ${page.pageNumber}" loading="lazy" />
      <footer>
        <a href="${apiUrl(page.url)}" target="_blank" rel="noreferrer">Open SVG</a>
      </footer>
    `;
    elements.svgGrid.appendChild(card);
  }
}

function renderJob(job) {
  state.jobId = job.jobId;
  setStatus(job.status, `${job.stage || 'queued'}${job.error ? `: ${job.error}` : ''}`);
  elements.pageCount.textContent = String(job.pageCount || 0);
  elements.svgCount.textContent = String(job.svgCount || 0);
  const settings = job.settings || {};
  elements.refillModeValue.textContent = settings.refillMode || runtimeDefaults.refillMode;
  elements.requestProviderValue.textContent = settings.requestProvider || runtimeDefaults.requestProvider;
  elements.requestApiBaseValue.textContent = settings.requestApiBase || defaultRequestApiBase(settings.requestProvider || runtimeDefaults.requestProvider);
  elements.defaultModelValue.textContent = settings.defaultModel || runtimeDefaults.defaultModel;
  elements.imageModelValue.textContent = settings.imageModel || runtimeDefaults.imageModel;
  renderLog(job.logTail || []);
  renderSvgCards(job);
  if (job.pptxUrl) {
    elements.pptxLink.href = apiUrl(job.pptxUrl);
    elements.pptxLink.classList.remove('hidden');
  } else {
    elements.pptxLink.classList.add('hidden');
    elements.pptxLink.removeAttribute('href');
  }
}

async function fetchJob() {
  if (!state.jobId) return;
  const response = await fetch(apiUrl(`/api/jobs/${state.jobId}`));
  if (!response.ok) {
    throw new Error(`Failed to poll job (${response.status})`);
  }
  const job = await response.json();
  renderJob(job);
  if (job.status === 'succeeded' || job.status === 'failed') {
    stopPolling();
    setBusy(false);
  }
}

function startPolling() {
  stopPolling();
  state.pollTimer = window.setInterval(() => {
    fetchJob().catch((error) => {
      stopPolling();
      setBusy(false);
      setStatus('error', error.message);
    });
  }, 3000);
}

function stopPolling() {
  if (state.pollTimer) {
    window.clearInterval(state.pollTimer);
    state.pollTimer = null;
  }
}

async function createJob(file) {
  const formData = new FormData();
  formData.append('file', file);
  formData.append('request_provider', getRequestProvider());
  formData.append('request_api_base', getRequestApiBase());
  formData.append('request_api_key', elements.requestApiKey.value.trim());
  formData.append('default_model', getDefaultModel());
  formData.append('image_model', getImageModel());
  formData.append('refill_mode', getRefillMode());
  const response = await fetch(apiUrl('/api/jobs'), {
    method: 'POST',
    body: formData,
  });
  const payload = await response.json();
  if (!response.ok) {
    throw new Error(payload.detail || `Upload failed (${response.status})`);
  }
  return payload;
}

function loadInitialValues() {
  const configuredApiBase = uiConfig.apiBase || '';
  const storedApiBase = readStored(storageKeys.apiBase);
  elements.apiBase.value = storedApiBase || configuredApiBase;

  const storedProvider = readStored(storageKeys.requestProvider);
  elements.requestProvider.value = storedProvider || runtimeDefaults.requestProvider;

  const storedRequestApiBase = readStored(storageKeys.requestApiBase);
  elements.requestApiBase.value = storedRequestApiBase || defaultRequestApiBase(elements.requestProvider.value);

  const storedDefaultModel = readStored(storageKeys.defaultModel);
  elements.defaultModel.value = storedDefaultModel || runtimeDefaults.defaultModel;

  const storedImageModel = readStored(storageKeys.imageModel);
  elements.imageModel.value = storedImageModel || runtimeDefaults.imageModel;

  const storedRefillMode = readStored(storageKeys.refillMode);
  elements.refillMode.value = storedRefillMode || runtimeDefaults.refillMode;

  elements.requestApiKey.value = '';
  elements.refillModeValue.textContent = elements.refillMode.value;
  elements.requestProviderValue.textContent = elements.requestProvider.value;
  elements.requestApiBaseValue.textContent = elements.requestApiBase.value;
  elements.defaultModelValue.textContent = elements.defaultModel.value;
  elements.imageModelValue.textContent = elements.imageModel.value;
}

elements.uploadForm.addEventListener('submit', async (event) => {
  event.preventDefault();
  const file = elements.pdfFile.files && elements.pdfFile.files[0];
  if (!file) {
    setStatus('idle', 'Choose one PDF first.');
    return;
  }
  try {
    setBusy(true);
    setStatus('uploading', `Submitting ${file.name}`);
    const job = await createJob(file);
    renderJob(job);
    startPolling();
  } catch (error) {
    setBusy(false);
    setStatus('error', error.message);
  }
});

elements.clearJobBtn.addEventListener('click', () => {
  stopPolling();
  state.jobId = null;
  elements.uploadForm.reset();
  loadInitialValues();
  setBusy(false);
  setStatus('idle', 'No job running.');
  elements.pageCount.textContent = '0';
  elements.svgCount.textContent = '0';
  elements.svgGrid.innerHTML = '';
  elements.svgMeta.textContent = '0 pages';
  elements.pptxLink.classList.add('hidden');
  renderLog([]);
});

elements.refillMode.addEventListener('change', () => {
  elements.refillModeValue.textContent = getRefillMode();
});

elements.requestProvider.addEventListener('change', () => {
  const previousBase = elements.requestApiBase.value.trim();
  const provider = getRequestProvider();
  const defaults = knownApiBases();
  if (!previousBase || defaults.has(previousBase)) {
    elements.requestApiBase.value = defaultRequestApiBase(provider);
  }
  elements.requestProviderValue.textContent = provider;
  elements.requestApiBaseValue.textContent = getRequestApiBase();
});

elements.requestApiBase.addEventListener('change', () => {
  elements.requestApiBaseValue.textContent = getRequestApiBase();
});

elements.defaultModel.addEventListener('change', () => {
  elements.defaultModelValue.textContent = getDefaultModel();
});

elements.imageModel.addEventListener('change', () => {
  elements.imageModelValue.textContent = getImageModel();
});

window.addEventListener('beforeunload', stopPolling);

loadInitialValues();
