/* Mode picker and imports on the "새 기록" screen. */
const T = window.I18N;

let source = 'mic';
let kind = 'lecture';

const systemHelp = document.getElementById('system-help');
const deviceField = document.getElementById('device-field');
const deviceSelect = document.getElementById('device');

function reflectSource() {
  document.querySelectorAll('.mode').forEach((btn) => {
    btn.setAttribute('aria-pressed', String(btn.dataset.source === source));
  });
  // Tab-sharing guidance only matters when system audio is involved.
  systemHelp.style.display = source === 'system' || source === 'both' ? '' : 'none';
  deviceField.style.display = source === 'mic' || source === 'both' ? '' : 'none';
}

document.querySelectorAll('.mode').forEach((btn) => {
  btn.addEventListener('click', () => {
    source = btn.dataset.source;
    reflectSource();
  });
});

const diarNote = document.getElementById('meeting-diar-note');
document.querySelectorAll('.seg-btn[data-kind]').forEach((btn) => {
  btn.addEventListener('click', () => {
    kind = btn.dataset.kind;
    document.querySelectorAll('.seg-btn[data-kind]').forEach((b) => {
      b.setAttribute('aria-pressed', String(b.dataset.kind === kind));
    });
    // Speaker separation only happens on uploaded meetings, so say so the
    // moment someone picks "미팅" for a live source.
    if (diarNote) diarNote.style.display = kind === 'meeting' ? '' : 'none';
  });
});

// Populate the input picker so BlackHole users can select it directly.
// Wrapped so a device-enumeration failure cannot take the rest of the page
// down with it — the picker is a convenience, the start button is not.
(async () => {
  deviceSelect.innerHTML = `<option value="">${T.defaultMic}</option>`;
  try {
    const devices = await listAudioInputs();
    deviceSelect.innerHTML =
      `<option value="">${T.defaultMic}</option>` +
      devices
        .map(
          (d, i) =>
            `<option value="${d.deviceId}">${escapeHtml(d.label || `${T.inputDevice} ${i + 1}`)}</option>`
        )
        .join('');
  } catch (err) {
    console.warn('Could not list audio inputs:', err);
  }
})();

reflectSource();

// ---------------------------------------------------------------- live start

document.getElementById('start-session').addEventListener('click', async (event) => {
  const btn = event.currentTarget;
  btn.disabled = true;
  try {
    const data = await api('/api/sessions', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ source, kind }),
    });
    const deviceId = deviceSelect.value;
    const query = deviceId ? `?device=${encodeURIComponent(deviceId)}` : '';
    window.location.href = data.url + query + (query ? '&' : '?') + 'autostart=1';
  } catch (err) {
    toast(err.message, 'bad');
    btn.disabled = false;
  }
});

// -------------------------------------------------------------------- import

const progressPanel = document.getElementById('import-progress');
const progressMessage = document.getElementById('import-message');
const progressBar = document.getElementById('import-bar');

function showProgress(message) {
  progressPanel.style.display = '';
  progressMessage.textContent = message;
}

/** Poll an import until it finishes, then open the session. */
function followImport(sessionId, targetUrl) {
  const timer = setInterval(async () => {
    try {
      const job = await api(`/api/sessions/${sessionId}/progress`);
      progressMessage.textContent = job.message || job.state || T.processing;
      if (job.total) {
        progressBar.style.width = `${Math.round((job.done / job.total) * 100)}%`;
      }
      if (job.state === 'done') {
        clearInterval(timer);
        progressBar.style.width = '100%';
        window.location.href = targetUrl;
      }
      if (job.state === 'error') {
        clearInterval(timer);
        progressPanel.style.display = 'none';
        toast(job.message || T.processFailed, 'bad');
      }
    } catch (err) {
      clearInterval(timer);
      progressPanel.style.display = 'none';
      toast(err.message, 'bad');
    }
  }, 1500);
}

document.getElementById('import-link').addEventListener('click', async (event) => {
  const url = document.getElementById('link-url').value.trim();
  if (!url) {
    toast(T.pasteLink, 'bad');
    return;
  }
  const btn = event.currentTarget;
  btn.disabled = true;
  showProgress(T.checkingLink);
  try {
    const data = await api('/api/import', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url, kind }),
    });
    followImport(data.id, data.url);
  } catch (err) {
    progressPanel.style.display = 'none';
    toast(err.message, 'bad');
    btn.disabled = false;
  }
});

// ---------------------------------------------------------------- file upload

const dropzone = document.getElementById('dropzone');
const fileInput = document.getElementById('file-input');

document.getElementById('pick-file').addEventListener('click', () => fileInput.click());
fileInput.addEventListener('change', () => {
  if (fileInput.files.length) uploadFile(fileInput.files[0]);
});

['dragenter', 'dragover'].forEach((name) =>
  dropzone.addEventListener(name, (e) => {
    e.preventDefault();
    dropzone.classList.add('is-over');
  })
);
['dragleave', 'drop'].forEach((name) =>
  dropzone.addEventListener(name, (e) => {
    e.preventDefault();
    dropzone.classList.remove('is-over');
  })
);
dropzone.addEventListener('drop', (e) => {
  const file = e.dataTransfer.files[0];
  if (file) uploadFile(file);
});

async function uploadFile(file) {
  showProgress(`${file.name} ${T.uploading}`);
  const form = new FormData();
  form.append('file', file);
  form.append('kind', kind);
  try {
    const data = await api('/api/import/file', { method: 'POST', body: form });
    followImport(data.id, data.url);
  } catch (err) {
    progressPanel.style.display = 'none';
    toast(err.message, 'bad');
  }
}
