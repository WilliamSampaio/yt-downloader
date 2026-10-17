const urlInput = document.getElementById("url");
const fetchBtn = document.getElementById("fetchBtn");
const statusEl = document.getElementById("status");
const resultEl = document.getElementById("result");
const thumbEl = document.getElementById("thumb");
const titleEl = document.getElementById("title");
const uploaderEl = document.getElementById("uploader");
const playlistBadgeEl = document.getElementById("playlistBadge");
const liveBadgeEl = document.getElementById("liveBadge");
const qualityEl = document.getElementById("quality");
const downloadVideoBtn = document.getElementById("downloadVideoBtn");
const downloadPlaylistBtn = document.getElementById("downloadPlaylistBtn");
const downloadTracklistBtn = document.getElementById("downloadTracklistBtn");
const downloadZipBtn = document.getElementById("downloadZipBtn");
const progressEl = document.getElementById("progress");
const playlistProgressEl = document.getElementById("playlistProgress");
const playlistProgressLabel = document.getElementById("playlistProgressLabel");
const playlistItemsEl = document.getElementById("playlistItems");
const tabs = document.querySelectorAll(".tab");

const JOB_STORAGE_KEY = "ytdl_active_playlist_job";

let currentInfo = null;
let currentType = "video";
let activeUrl = "";
let pollTimer = null;

const STATUS_ICON = { pending: "⏳", downloading: "⬇️", done: "✅", error: "❌" };

function setStatus(msg, isError = true) {
  statusEl.textContent = msg || "";
  statusEl.classList.toggle("info", !isError);
}

function populateQualities() {
  qualityEl.innerHTML = "";
  const list = currentType === "video" ? currentInfo.video_qualities : currentInfo.audio_qualities;
  const unit = currentType === "video" ? "p" : " kbps";
  list.forEach((q) => {
    const opt = document.createElement("option");
    opt.value = q;
    opt.textContent = `${q}${unit}`;
    qualityEl.appendChild(opt);
  });
}

function updateActionButtons() {
  const hasVideo = !!currentInfo.video;
  const hasPlaylist = !!currentInfo.playlist;
  const liveMessage = (currentInfo.video && currentInfo.video.live_message) || null;

  downloadVideoBtn.classList.toggle("hidden", !hasVideo);
  downloadPlaylistBtn.classList.toggle("hidden", !hasPlaylist);
  downloadTracklistBtn.classList.toggle("hidden", !hasPlaylist);
  downloadVideoBtn.disabled = !!liveMessage;

  downloadVideoBtn.textContent = hasVideo && hasPlaylist ? "Baixar este vídeo" : "Baixar";
  downloadPlaylistBtn.textContent = hasPlaylist
    ? `Baixar Playlist (${currentInfo.playlist.count} ${playlistUnit(currentInfo.playlist)})`
    : "Baixar Playlist";
}

function playlistUnit(playlist) {
  return playlist.is_music ? "músicas" : "vídeos";
}

function selectType(type) {
  tabs.forEach((t) => t.classList.toggle("active", t.dataset.type === type));
  currentType = type;
  if (currentInfo) populateQualities();
}

tabs.forEach((tab) => {
  tab.addEventListener("click", () => selectType(tab.dataset.type));
});

// Tenta cada imagem em ordem; se nenhuma carregar, esconde em vez de mostrar o ícone quebrado.
function showThumbnail(candidates) {
  const queue = candidates.filter(Boolean);
  const next = () => {
    const src = queue.shift();
    thumbEl.classList.toggle("hidden", !src);
    if (src) thumbEl.src = src;
    else thumbEl.removeAttribute("src");
  };
  thumbEl.onerror = next;
  next();
}

fetchBtn.addEventListener("click", fetchInfo);
urlInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter") fetchInfo();
});

async function fetchInfo() {
  const url = urlInput.value.trim();
  if (!url) {
    setStatus("Cole um link do YouTube.");
    return;
  }

  stopPolling();
  setStatus("Buscando informações...", false);
  resultEl.classList.add("hidden");
  playlistProgressEl.classList.add("hidden");
  fetchBtn.disabled = true;

  try {
    const res = await fetch(`/api/info?url=${encodeURIComponent(url)}`);
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Erro ao buscar vídeo.");

    currentInfo = data;
    activeUrl = url;

    const video = data.video;
    const playlist = data.playlist;

    showThumbnail([
      video && video.thumbnail,
      playlist && playlist.thumbnail,
      playlist && playlist.fallback_thumbnail,
    ]);

    if (video) {
      titleEl.textContent = video.title || "Sem título";
      uploaderEl.textContent = video.uploader || "";
    } else {
      titleEl.textContent = playlist.title || "Playlist";
      uploaderEl.textContent = "";
    }

    if (video && video.live_message) {
      liveBadgeEl.textContent = `⏺ ${video.live_message}`;
      liveBadgeEl.classList.remove("hidden");
    } else {
      liveBadgeEl.classList.add("hidden");
    }

    if (playlist) {
      playlistBadgeEl.textContent = `Playlist: ${playlist.title} · ${playlist.count} ${playlistUnit(playlist)}`;
      playlistBadgeEl.classList.remove("hidden");
    } else {
      playlistBadgeEl.classList.add("hidden");
    }

    // Playlists do YouTube Music quase sempre são para ouvir: já abre em MP3.
    if (playlist && playlist.is_music && !video) currentType = "audio";
    selectType(currentType);
    updateActionButtons();

    resultEl.classList.remove("hidden");
    setStatus("");
  } catch (err) {
    setStatus(err.message);
  } finally {
    fetchBtn.disabled = false;
  }
}

downloadVideoBtn.addEventListener("click", downloadSingle);
downloadPlaylistBtn.addEventListener("click", startPlaylistDownload);
downloadTracklistBtn.addEventListener("click", downloadTracklist);
downloadZipBtn.addEventListener("click", () => {
  const jobId = downloadZipBtn.dataset.jobId;
  if (jobId) window.location.href = `/api/playlist/result/${jobId}`;
});

async function saveResponseAsFile(res, fallbackName) {
  const blob = await res.blob();
  const disposition = res.headers.get("Content-Disposition") || "";
  const utf8Match = disposition.match(/filename\*=UTF-8''([^;]+)/i);
  const plainMatch = disposition.match(/filename="?([^";]+)"?/i);
  let filename = fallbackName;
  if (utf8Match) filename = decodeURIComponent(utf8Match[1]);
  else if (plainMatch) filename = plainMatch[1];

  const link = document.createElement("a");
  link.href = URL.createObjectURL(blob);
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(link.href);
}

async function downloadTracklist() {
  const url = activeUrl;
  if (!url) return;

  downloadTracklistBtn.disabled = true;
  setStatus("Gerando lista de músicas...", false);

  try {
    const res = await fetch(`/api/playlist/tracklist?url=${encodeURIComponent(url)}`);
    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      throw new Error(data.detail || "Erro ao gerar a lista de músicas.");
    }
    await saveResponseAsFile(res, "playlist.txt");
    setStatus("");
  } catch (err) {
    setStatus(err.message);
  } finally {
    downloadTracklistBtn.disabled = false;
  }
}

async function downloadSingle() {
  const url = activeUrl;
  const quality = qualityEl.value;
  if (!url || !quality) return;

  downloadVideoBtn.disabled = true;
  progressEl.classList.remove("hidden");
  setStatus("");

  try {
    const params = new URLSearchParams({ url, type: currentType, quality });
    const res = await fetch(`/api/download?${params.toString()}`);

    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      throw new Error(data.detail || "Erro ao baixar.");
    }

    await saveResponseAsFile(res, `download.${currentType === "video" ? "mp4" : "mp3"}`);
  } catch (err) {
    setStatus(err.message);
  } finally {
    downloadVideoBtn.disabled = !!(currentInfo && currentInfo.video && currentInfo.video.live_message);
    progressEl.classList.add("hidden");
  }
}

function renderPlaylistItems(items) {
  playlistItemsEl.innerHTML = "";
  items.forEach((item) => {
    const li = document.createElement("li");
    if (item.status === "error") li.classList.add("error");
    const icon = document.createElement("span");
    icon.className = "status-icon";
    icon.textContent = STATUS_ICON[item.status] || "⏳";
    const title = document.createElement("span");
    title.className = "item-title";
    title.textContent = item.title;
    title.title = item.error ? `${item.title} — ${item.error}` : item.title;
    li.appendChild(icon);
    li.appendChild(title);
    playlistItemsEl.appendChild(li);
  });
}

function stopPolling() {
  if (pollTimer) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
}

function saveActiveJob(jobId) {
  localStorage.setItem(JOB_STORAGE_KEY, jobId);
}

function clearActiveJob() {
  localStorage.removeItem(JOB_STORAGE_KEY);
}

async function startPlaylistDownload() {
  const url = activeUrl;
  const quality = qualityEl.value;
  if (!url || !quality) return;

  downloadPlaylistBtn.disabled = true;
  downloadZipBtn.classList.add("hidden");
  setStatus("");
  playlistProgressEl.classList.remove("hidden");
  playlistItemsEl.innerHTML = "";
  playlistProgressLabel.textContent = "Iniciando...";

  try {
    const res = await fetch("/api/playlist/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url, type: currentType, quality: Number(quality) }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Erro ao iniciar download da playlist.");

    saveActiveJob(data.job_id);
    pollPlaylistStatus(data.job_id);
  } catch (err) {
    setStatus(err.message);
    downloadPlaylistBtn.disabled = false;
    playlistProgressEl.classList.add("hidden");
  }
}

function pollPlaylistStatus(jobId) {
  stopPolling();

  const tick = async () => {
    try {
      const res = await fetch(`/api/playlist/status/${jobId}`);

      if (res.status === 404) {
        stopPolling();
        clearActiveJob();
        setStatus("O job da playlist expirou ou o servidor foi reiniciado. Tente novamente.");
        downloadPlaylistBtn.disabled = false;
        return;
      }

      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "Erro ao consultar progresso.");

      renderPlaylistItems(data.items);
      playlistProgressLabel.textContent = `Baixando ${data.done} de ${data.total}`;

      if (data.status === "completed") {
        stopPolling();
        clearActiveJob();
        playlistProgressLabel.textContent = `Concluído! Clique para baixar o ZIP.`;
        downloadZipBtn.dataset.jobId = jobId;
        downloadZipBtn.classList.remove("hidden");
        downloadPlaylistBtn.disabled = false;
      } else if (data.status === "error") {
        stopPolling();
        clearActiveJob();
        setStatus(data.error || "Falha ao baixar a playlist.");
        downloadPlaylistBtn.disabled = false;
      }
    } catch (err) {
      stopPolling();
      setStatus(err.message);
      downloadPlaylistBtn.disabled = false;
    }
  };

  tick();
  pollTimer = setInterval(tick, 1500);
}

// If a playlist job was still running when the page was closed/reloaded,
// pick up where it left off instead of losing track of it silently.
(function resumeActiveJob() {
  const jobId = localStorage.getItem(JOB_STORAGE_KEY);
  if (!jobId) return;

  resultEl.classList.remove("hidden");
  playlistProgressEl.classList.remove("hidden");
  downloadPlaylistBtn.classList.remove("hidden");
  downloadVideoBtn.classList.add("hidden");
  downloadPlaylistBtn.disabled = true;
  setStatus("Retomando o acompanhamento de uma playlist em andamento...", false);
  pollPlaylistStatus(jobId);
})();
