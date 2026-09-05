/* Folder page: rename, delete, and chat across every recording in it. */
const T = window.I18N;

const folderRoot = document.getElementById('folder-root');
const folderId = folderRoot.dataset.folderId;

// ------------------------------------------------------------------ rename

const nameInput = document.getElementById('folder-name');
let nameTimer;
nameInput.addEventListener('input', () => {
  clearTimeout(nameTimer);
  nameTimer = setTimeout(() => {
    const name = nameInput.value.trim();
    if (!name) return;
    api(`/api/folders/${folderId}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name }),
    }).catch((err) => toast(err.message, 'bad'));
  }, 700);
});

// ------------------------------------------------------------------ delete

document.getElementById('folder-delete').addEventListener('click', async () => {
  if (!confirm(T.folderDeleteConfirm)) return;
  try {
    await api(`/api/folders/${folderId}`, { method: 'DELETE' });
    window.location.href = '/review';
  } catch (err) {
    toast(err.message, 'bad');
  }
});

// -------------------------------------------------------------------- chat

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
    const data = await api(`/api/folders/${folderId}/chat`, {
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
