/* The session screen: record, transcribe, summarise, explain, chat. */
const T = window.I18N;

const root = document.getElementById('session-root');
const sessionId = root.dataset.sessionId;
const params = new URLSearchParams(window.location.search);

let source = root.dataset.source === 'link' ? 'mic' : root.dataset.source;
const deviceId = params.get('device') || '';

const transcriptEl = document.getElementById('transcript');
const transcriptEmpty = document.getElementById('transcript-empty');
const statusEl = document.getElementById('capture-status');
const liveTag = document.getElementById('live-tag');

// ------------------------------------------------------------------ recording

const startBtn = document.getElementById('start-rec');
const stopBtn = document.getElementById('stop-rec');

if (startBtn) {
  const paint = makeWaveformPainter(document.getElementById('waveform'));
  let pending = 0;

  const capture = new Capture({
    onLevel: paint,
    onStatus: (state) => {
      const recording = state === 'recording';
      startBtn.disabled = recording;
      stopBtn.disabled = !recording;
      liveTag.style.display = recording ? '' : 'none';
      statusEl.textContent = recording ? T.listening : T.stopped;
    },
    onClip: async (blob) => {
      pending += 1;
      statusEl.textContent = `${T.transcribing} (${pending})`;
      const ext = (blob.type.split('/')[1] || 'webm').split(';')[0];
      const form = new FormData();
      form.append('file', blob, `clip.${ext}`);
      try {
        const data = await api(`/api/sessions/${sessionId}/transcribe`, {
          method: 'POST',
          body: form,
        });
        if (data.text) appendSegment(data.text);
      } catch (err) {
        toast(err.message, 'bad');
      } finally {
        pending -= 1;
        if (pending === 0) statusEl.textContent = capture.isRecording ? T.listening : T.stopped;
      }
    },
  });

  startBtn.addEventListener('click', async () => {
    startBtn.disabled = true;
    try {
      await capture.start(source, deviceId);
    } catch (err) {
      startBtn.disabled = false;
      toast(err.message || T.micFailed, 'bad');
    }
  });

  stopBtn.addEventListener('click', () => capture.stop());

  // Switching source mid-session restarts capture with the new one.
  document.querySelectorAll('#source-switch .seg-btn').forEach((btn) => {
    btn.addEventListener('click', async () => {
      const next = btn.dataset.source;
      if (next === source) return;
      const wasRecording = capture.isRecording;
      capture.stop();
      source = next;
      document.querySelectorAll('#source-switch .seg-btn').forEach((b) => {
        b.setAttribute('aria-pressed', String(b.dataset.source === source));
      });
      document.getElementById('source-tag').textContent =
        T.sourceNames[source];
      if (wasRecording) {
        try {
          await capture.start(source, deviceId);
        } catch (err) {
          toast(err.message, 'bad');
        }
      }
    });
  });

  if (params.get('autostart') === '1') {
    startBtn.click();
  }
}

function appendSegment(text) {
  if (transcriptEmpty) transcriptEmpty.remove();
  const line = document.createElement('div');
  line.className = 'seg-line is-new';
  line.innerHTML = `<span class="seg-time">${timeNow()}</span><span class="seg-text"></span>`;
  line.querySelector('.seg-text').textContent = text;
  transcriptEl.appendChild(line);
  transcriptEl.scrollTop = transcriptEl.scrollHeight;
}

// ------------------------------------------------------------------- importing

const importPanel = document.getElementById('import-panel');
if (importPanel) {
  const message = document.getElementById('import-message');
  const bar = document.getElementById('import-bar');
  const timer = setInterval(async () => {
    try {
      const job = await api(`/api/sessions/${sessionId}/progress`);
      if (job.state === 'idle' || job.state === 'done') {
        clearInterval(timer);
        importPanel.style.display = 'none';
        if (job.state === 'done') window.location.reload();
        return;
      }
      importPanel.style.display = '';
      message.textContent = job.message || T.processing;
      if (job.total) bar.style.width = `${Math.round((job.done / job.total) * 100)}%`;
      if (job.state === 'error') {
        clearInterval(timer);
        importPanel.style.display = 'none';
        toast(job.message, 'bad');
      }
    } catch {
      clearInterval(timer);
    }
  }, 1500);
}

// --------------------------------------------------------------------- title

const titleInput = document.getElementById('session-title');
let titleTimer;
titleInput.addEventListener('input', () => {
  clearTimeout(titleTimer);
  titleTimer = setTimeout(() => {
    api(`/api/sessions/${sessionId}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ title: titleInput.value.trim() || T.untitled }),
    }).catch(() => {});
  }, 700);
});

// ------------------------------------------------------------------- summary

const summaryCard = document.getElementById('summary-card');
const summaryBody = document.getElementById('summary-body');
const keywordsEl = document.getElementById('keywords');
const kwPanel = document.getElementById('kw-panel');
const summaryBtn = document.getElementById('make-summary');

if (summaryBody.textContent.trim()) {
  summaryBody.innerHTML = renderMarkdown(summaryBody.textContent);
}

summaryBtn.addEventListener('click', async () => {
  summaryBtn.disabled = true;
  const original = summaryBtn.textContent;
  summaryBtn.textContent = T.summarizing;
  try {
    const data = await api(`/api/sessions/${sessionId}/summary`, { method: 'POST' });
    summaryBody.innerHTML = renderMarkdown(data.summary);
    summaryCard.style.display = '';
    renderKeywords(data.keywords);
    if (data.title) titleInput.value = data.title;
    summaryCard.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    toast(T.summarized, 'ok');
  } catch (err) {
    toast(err.message, 'bad');
  } finally {
    summaryBtn.disabled = false;
    summaryBtn.textContent = original;
  }
});

function renderKeywords(list) {
  keywordsEl.innerHTML = '';
  (list || []).forEach((kw) => {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'kw';
    btn.dataset.kw = kw;
    btn.setAttribute('aria-expanded', 'false');
    btn.textContent = kw;
    keywordsEl.appendChild(btn);
  });
}

keywordsEl.addEventListener('click', async (event) => {
  const btn = event.target.closest('.kw');
  if (!btn) return;

  // Clicking the open keyword again closes the panel.
  if (btn.getAttribute('aria-expanded') === 'true') {
    btn.setAttribute('aria-expanded', 'false');
    kwPanel.style.display = 'none';
    return;
  }
  keywordsEl.querySelectorAll('.kw').forEach((b) => b.setAttribute('aria-expanded', 'false'));
  btn.setAttribute('aria-expanded', 'true');

  const kw = btn.dataset.kw;
  const notes = window.KEYWORD_NOTES || {};
  kwPanel.style.display = '';

  // Seen before — render the saved note without a round trip.
  if (notes[kw]) {
    kwPanel.innerHTML = renderMarkdown(notes[kw]);
    return;
  }

  kwPanel.textContent = T.loadingExplanation;
  try {
    const data = await api(`/api/sessions/${sessionId}/keyword?q=${encodeURIComponent(kw)}`);
    notes[kw] = data.explanation;
    kwPanel.innerHTML = renderMarkdown(data.explanation);
  } catch (err) {
    kwPanel.textContent = err.message;
  }
});

// ---------------------------------------------------------------------- chat

const chatForm = document.getElementById('chat-form');
const chatInput = document.getElementById('chat-input');
const chatLog = document.getElementById('chat-log');
const chatSend = document.getElementById('chat-send');

chatForm.addEventListener('submit', async (event) => {
  event.preventDefault();
  const question = chatInput.value.trim();
  if (!question) return;

  const empty = document.getElementById('chat-empty');
  if (empty) empty.remove();

  addMessage('user', question);
  chatInput.value = '';
  chatSend.disabled = true;

  const thinking = addMessage('assistant', '…');
  try {
    const data = await api(`/api/sessions/${sessionId}/chat`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message: question }),
    });
    thinking.querySelector('.msg-body').textContent = data.reply;
  } catch (err) {
    thinking.querySelector('.msg-body').textContent = err.message;
  } finally {
    chatSend.disabled = false;
    chatInput.focus();
  }
});

function addMessage(role, text) {
  const el = document.createElement('div');
  el.className = 'msg' + (role === 'user' ? ' msg-user' : '');
  el.innerHTML = `<span class="msg-role">${role === 'user' ? T.me : T.ai}</span><div class="msg-body"></div>`;
  el.querySelector('.msg-body').textContent = text;
  chatLog.appendChild(el);
  chatLog.scrollTop = chatLog.scrollHeight;
  return el;
}

// -------------------------------------------------------------------- export

document.getElementById('export').addEventListener('click', () => {
  const lines = [`# ${titleInput.value}`, ''];

  const summary = summaryBody.textContent.trim();
  if (summary) lines.push(`## ${T.exportSummary}`, '', summary, '');

  const keywords = [...keywordsEl.querySelectorAll('.kw')].map((b) => b.textContent);
  if (keywords.length) lines.push(`## ${T.exportKeywords}`, '', keywords.join(', '), '');

  lines.push(`## ${T.exportTranscript}`, '');
  transcriptEl.querySelectorAll('.seg-line').forEach((line) => {
    lines.push(`[${line.querySelector('.seg-time').textContent}] ${line.querySelector('.seg-text').textContent}`);
  });

  const blob = new Blob([lines.join('\n')], { type: 'text/markdown' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = `${titleInput.value.replace(/[^\w가-힣 -]/g, '') || 'session'}.md`;
  a.click();
  URL.revokeObjectURL(url);
});

// ------------------------------------------------------------------ speakers

// Renaming a speaker is a per-session label, so it saves as you type.
document.querySelectorAll('.speaker-name').forEach((input) => {
  let timer;
  input.addEventListener('input', () => {
    clearTimeout(timer);
    timer = setTimeout(() => {
      api(`/api/sessions/${sessionId}/speakers`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ label: input.dataset.label, name: input.value.trim() }),
      }).catch((err) => toast(err.message, 'bad'));
    }, 700);
  });
});

// -------------------------------------------------------------------- folder

// Filing this session into a folder saves immediately; the folder page then
// includes it in cross-recording chat.
const folderSelect = document.getElementById('folder-select');
if (folderSelect) {
  folderSelect.addEventListener('change', async () => {
    try {
      await api(`/api/sessions/${sessionId}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ folder_id: folderSelect.value || null }),
      });
      toast(folderSelect.value ? T.filed : T.unfiled, 'ok');
    } catch (err) {
      toast(err.message, 'bad');
    }
  });
}
