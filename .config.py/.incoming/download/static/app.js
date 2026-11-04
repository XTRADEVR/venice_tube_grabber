let deferredPrompt = null;
let currentProbed = null;

if ('serviceWorker' in navigator) {
  navigator.serviceWorker.register('/service-worker.js');
}

window.addEventListener('beforeinstallprompt', (e) => {
  e.preventDefault();
  deferredPrompt = e;
  const btn = document.getElementById('btnInstallPwa');
  if (btn) {
    btn.classList.remove('hidden');
    btn.onclick = async () => {
      if (deferredPrompt) {
        await deferredPrompt.prompt();
        deferredPrompt = null;
        btn.classList.add('hidden');
      }
    };
  }
});

function formatBytes(bytes) {
  if (!bytes || bytes <= 0) return '0 B';
  const u = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0, v = bytes;
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  return `${v.toFixed(2)} ${u[i]}`;
}

async function updateTasks() {
  try {
    const res = await fetch('/api/tasks');
    if (!res.ok) return;
    const { tasks } = await res.json();
    const container = document.getElementById('tasksContainer');
    if (!container) return;

    if (!tasks || tasks.length === 0) {
      container.innerHTML = '<div class="text-slate-500 text-xs text-center py-6">Belum ada unduhan aktif.</div>';
      return;
    }

    container.innerHTML = tasks.map(t => `
      <div class="p-4 rounded-xl bg-slate-900 border border-slate-800 space-y-2 text-xs">
        <div class="flex justify-between font-bold text-white">
          <span class="truncate">${t.filename}</span>
          <span class="text-cyan-400 font-mono">${t.percentage}%</span>
        </div>
        <div class="w-full h-2 rounded bg-slate-800 overflow-hidden">
          <div class="h-full bg-cyan-400 transition-all duration-300" style="width: ${t.percentage}%"></div>
        </div>
        <div class="flex justify-between text-slate-400">
          <span>${formatBytes(t.downloaded_bytes)} / ${formatBytes(t.total_bytes)}</span>
          <span class="text-cyan-300 font-mono">${t.speed_mbps} MB/s</span>
          <div class="flex gap-2">
            ${t.status === 'downloading' ? `<button onclick="pauseTask('${t.task_id}')" class="text-amber-400">Jeda</button>` : ''}
            ${t.status === 'paused' ? `<button onclick="resumeTask('${t.task_id}')" class="text-emerald-400">Lanjut</button>` : ''}
            <button onclick="cancelTask('${t.task_id}')" class="text-red-400">Batal</button>
          </div>
        </div>
      </div>
    `).join('');
  } catch {}
}

window.pauseTask = (id) => fetch(`/api/download/pause/${id}`, { method: 'POST' }).then(updateTasks);
window.resumeTask = (id) => fetch(`/api/download/resume/${id}`, { method: 'POST' }).then(updateTasks);
window.cancelTask = (id) => fetch(`/api/download/cancel/${id}`, { method: 'POST' }).then(updateTasks);

document.addEventListener('DOMContentLoaded', () => {
  updateTasks();
  setInterval(updateTasks, 700);

  document.getElementById('btnProbe')?.addEventListener('click', async () => {
    const url = document.getElementById('inputUrl').value.trim();
    if (!url) return alert('Masukkan URL terlebih dahulu');
    const res = await fetch('/api/probe', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url })
    });
    const data = await res.json();
    if (res.ok && data.success) {
      currentProbed = data;
      document.getElementById('probeResultCard').classList.remove('hidden');
      document.getElementById('probeFilename').textContent = data.filename;
      document.getElementById('probeDetails').textContent = `Ukuran: ${formatBytes(data.total_bytes)} | IP 1.1.1.1: ${data.resolved_ip}`;
    } else {
      alert(data.error || 'Gagal memeriksa URL');
    }
  });

  document.getElementById('btnStartDownload')?.addEventListener('click', async () => {
    if (!currentProbed) return;
    await fetch('/api/download/start', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url: currentProbed.url, filename: currentProbed.filename })
    });
    document.getElementById('probeResultCard').classList.add('hidden');
    document.getElementById('inputUrl').value = '';
    currentProbed = null;
    updateTasks();
  });
});
