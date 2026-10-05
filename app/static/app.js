"use strict";

const $ = (id) => document.getElementById(id);

const audioEl = new Audio();
audioEl.preload = "metadata";
const videoEl = $("video");
let media = audioEl;
let mediaType = "audio";

const state = {
  tracks: [],
  byId: new Map(),
  favorites: new Set(),
  view: "audio",
  query: "",
  queue: [],
  queueIndex: -1,
  shuffle: false,
  repeat: "off", // off | all | one
  skip: 10,
  scrubbing: false,
  currentId: null,
  prep: {},
  watching: new Set(),
  scanning: false,
  progress: { done: 0, total: 0 },
};

const els = {
  playlist: $("playlist"),
  empty: $("empty"),
  listHead: $("listHead"),
  search: $("search"),
  title: $("title"),
  subtitle: $("subtitle"),
  art: $("art"),
  visual: $("visual"),
  backdrop: $("backdrop"),
  seek: $("seek"),
  cur: $("cur"),
  dur: $("dur"),
  play: $("play"),
  playIcon: $("playIcon"),
  pauseIcon: $("pauseIcon"),
  shuffle: $("shuffle"),
  repeat: $("repeat"),
  favBig: $("favBig"),
  pip: $("pip"),
  fullscreen: $("fullscreen"),
  fsEnter: $("fsEnter"),
  fsExit: $("fsExit"),
  prep: $("prep"),
  prepText: $("prepText"),
  volume: $("volume"),
  toast: $("toast"),
};

/* ---------------- helpers ---------------- */
function fmt(sec) {
  if (!isFinite(sec) || sec < 0) sec = 0;
  sec = Math.floor(sec);
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = sec % 60;
  const pad = (n) => String(n).padStart(2, "0");
  return h > 0 ? `${h}:${pad(m)}:${pad(s)}` : `${m}:${pad(s)}`;
}

let toastTimer;
function toast(msg) {
  els.toast.textContent = msg;
  els.toast.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => els.toast.classList.remove("show"), 1800);
}

function setFill(el) {
  const max = Number(el.max) || 100;
  const pct = max ? (Number(el.value) / max) * 100 : 0;
  el.style.setProperty("--fill", pct + "%");
}

function visible() {
  const q = state.query.trim().toLowerCase();
  return state.tracks.filter((t) => {
    if (state.view === "favorites" && !state.favorites.has(t.id)) return false;
    if (state.view === "audio" && t.type !== "audio") return false;
    if (state.view === "video" && t.type !== "video") return false;
    if (!q) return true;
    return (
      t.title.toLowerCase().includes(q) ||
      t.artist.toLowerCase().includes(q) ||
      t.album.toLowerCase().includes(q)
    );
  });
}

/* ---------------- rendering ---------------- */
const rowMap = new Map();

function renderList() {
  const list = visible();
  els.playlist.innerHTML = "";
  rowMap.clear();

  const heads = { audio: "Audio", video: "Video", favorites: "Favorites" };
  if (state.query) els.listHead.textContent = `Search · ${list.length}`;
  else if (state.scanning && !list.length) els.listHead.textContent = "Scanning…";
  else els.listHead.textContent = `${heads[state.view] || "Library"} · ${list.length}`;

  els.empty.hidden = list.length > 0;
  const emptyText = {
    favorites: "No favorites yet. Tap the heart on a track.",
    audio: "No audio files found.",
    video: "No videos found.",
  };
  els.empty.textContent = state.scanning
    ? state.progress.total
      ? `Scanning your library… ${state.progress.done}/${state.progress.total}`
      : "Scanning your library…"
    : emptyText[state.view] || "Nothing here yet.";

  const frag = document.createDocumentFragment();
  list.forEach((t, i) => {
    const li = document.createElement("li");
    li.className = "track";
    li.dataset.id = t.id;

    const num = document.createElement("span");
    num.className = "num";
    num.textContent = i + 1;

    const info = document.createElement("div");
    info.className = "t-info";
    const title = document.createElement("div");
    title.className = "t-title";
    title.textContent = t.title;
    if (t.type === "video") {
      const b = document.createElement("span");
      b.className = "badge";
      b.textContent = "VIDEO";
      title.appendChild(b);
    }
    const sub = document.createElement("div");
    sub.className = "t-sub";
    sub.textContent = [t.artist, t.album].filter(Boolean).join(" · ") || "Unknown artist";
    info.append(title, sub);

    const heart = document.createElement("button");
    heart.className = "heart" + (state.favorites.has(t.id) ? " is-fav" : "");
    heart.dataset.act = "fav";
    heart.innerHTML =
      '<svg viewBox="0 0 24 24"><path d="M12 21s-7.5-4.6-9.8-8.4C.6 9.9 2 6.5 5.2 5.6 7 5.1 8.8 5.8 12 8.9c3.2-3.1 5-3.8 6.8-3.3C22 6.5 23.4 9.9 21.8 12.6 19.5 16.4 12 21 12 21z"/></svg>';

    li.append(num, info, heart);
    frag.appendChild(li);
    rowMap.set(t.id, li);
  });
  els.playlist.appendChild(frag);
  highlightCurrent();
}

function highlightCurrent() {
  rowMap.forEach((li, id) => li.classList.toggle("is-current", id === state.currentId));
}

function updateFavUI(id) {
  const on = state.favorites.has(id);
  const li = rowMap.get(id);
  if (li) li.querySelector(".heart").classList.toggle("is-fav", on);
  if (id === state.currentId) els.favBig.classList.toggle("is-fav", on);
}

/* ---------------- playback ---------------- */
function buildQueue(startId) {
  const list = visible();
  let ids = list.map((t) => t.id);
  if (!ids.length) return;
  if (state.shuffle) {
    ids = ids.filter((x) => x !== startId);
    for (let i = ids.length - 1; i > 0; i--) {
      const j = Math.floor(Math.random() * (i + 1));
      [ids[i], ids[j]] = [ids[j], ids[i]];
    }
    ids.unshift(startId);
  }
  state.queue = ids;
  state.queueIndex = ids.indexOf(startId);
}

function setMediaSource(track) {
  const el = track.type === "video" ? videoEl : audioEl;
  if (media !== el) {
    media.pause();
    media.removeAttribute("src");
    media = el;
  }
  mediaType = track.type;
  media.src = track.src;
  media.load();
  els.volume.value = localStorage.getItem("vol") ?? "100";
  media.volume = Number(els.volume.value) / 100;
  setFill(els.volume);
}

function setPlayingIcon(playing) {
  els.playIcon.style.display = playing ? "none" : "";
  els.pauseIcon.style.display = playing ? "" : "none";
}

function attachMediaHandlers() {
  [audioEl, videoEl].forEach((el) => {
    el.addEventListener("timeupdate", () => {
      if (media !== el || state.scrubbing) return;
      const d = el.duration || 0;
      els.seek.value = d ? String(Math.round((el.currentTime / d) * 1000)) : "0";
      setFill(els.seek);
      els.cur.textContent = fmt(el.currentTime);
    });
    el.addEventListener("loadedmetadata", () => {
      if (media !== el) return;
      els.dur.textContent = fmt(el.duration);
    });
    el.addEventListener("play", () => {
      if (media !== el) return;
      setPlayingIcon(true);
      els.art.classList.add("playing");
    });
    el.addEventListener("pause", () => {
      if (media !== el) return;
      setPlayingIcon(false);
      els.art.classList.remove("playing");
    });
    el.addEventListener("ended", () => {
      if (media !== el) return;
      onEnded();
    });
  });
}

function showTrack(track) {
  hidePrep();
  state.currentId = track.id;
  highlightCurrent();
  els.title.textContent = track.title;
  els.subtitle.textContent =
    [track.artist, track.album].filter(Boolean).join(" · ") || "Unknown artist";
  els.favBig.classList.toggle("is-fav", state.favorites.has(track.id));
  els.cur.textContent = "0:00";
  els.dur.textContent = track.duration ? fmt(track.duration) : "0:00";
  els.seek.value = "0";
  setFill(els.seek);

  const isVideo = track.type === "video";
  if (!isVideo) {
    exitFullscreenIfAny();
    exitPiPIfAny();
  }
  els.visual.classList.toggle("video-mode", isVideo);
  els.visual.style.removeProperty("--video-aspect");
  els.art.hidden = isVideo;
  videoEl.hidden = !isVideo;
  updateVideoButtons(isVideo);

  if (track.cover) {
    els.art.innerHTML = `<img alt="" src="${track.cover}" />`;
    els.backdrop.style.backgroundImage = `url("${track.cover}")`;
    els.backdrop.classList.add("has-art");
  } else {
    els.art.innerHTML = '<div class="art-placeholder">◍</div>';
    els.backdrop.style.backgroundImage = "";
    els.backdrop.classList.remove("has-art");
  }

  navigator.mediaSession.metadata = new MediaMetadata({
    title: track.title,
    artist: track.artist || "Unknown artist",
    album: track.album || "",
    artwork: track.cover ? [{ src: track.cover, sizes: "512x512" }] : [],
  });
}

function playTrack(id, { rebuild = true } = {}) {
  const track = state.byId.get(id);
  if (!track) return;
  if (rebuild || !state.queue.length) buildQueue(id);
  else state.queueIndex = state.queue.indexOf(id);
  showTrack(track);
  if (track.needs_transcode && state.prep[track.id] !== "ready") {
    prepareAndPlay(track);
    return;
  }
  setMediaSource(track);
  media.play().catch(() => {});
}

function showPrep(text) {
  els.prepText.textContent = text;
  els.prep.hidden = false;
}

function hidePrep() {
  els.prep.hidden = true;
}

async function fetchTranscode(id) {
  const res = await fetch(`/api/transcode/${id}`);
  const status = await res.json();
  state.prep[id] = status.state;
  return status;
}

async function ensureTranscode(track) {
  try {
    let status = await fetchTranscode(track.id);
    while (status.state === "queued" || status.state === "processing") {
      showPrep(status.progress ? `Optimizing video… ${status.progress}%` : "Optimizing video…");
      await new Promise((r) => setTimeout(r, 2000));
      if (state.currentId !== track.id) return false;
      status = await fetchTranscode(track.id);
    }
    return status.state === "ready" || status.state === "not_needed";
  } catch (_) {
    return false;
  }
}

async function prepareAndPlay(track) {
  showPrep("Optimizing video…");
  const ok = await ensureTranscode(track);
  if (state.currentId !== track.id) return;
  hidePrep();
  if (!ok) {
    toast("Could not prepare this video");
    return;
  }
  setMediaSource(track);
  media.play().catch(() => {});
}

function warmTranscodes() {
  state.tracks.filter((t) => t.needs_transcode).forEach(watchTranscode);
}

function watchTranscode(track) {
  if (state.watching.has(track.id)) return;
  state.watching.add(track.id);
  const poll = async () => {
    try {
      const status = await fetchTranscode(track.id);
      if (status.state === "queued" || status.state === "processing") {
        setTimeout(poll, 3000);
        return;
      }
    } catch (_) {}
    state.watching.delete(track.id);
  };
  poll();
}

function togglePlay() {
  if (!state.currentId) {
    const list = visible();
    if (list.length) playTrack(list[0].id);
    return;
  }
  if (media.paused) media.play().catch(() => {});
  else media.pause();
}

function next(auto = false) {
  if (!state.queue.length) return;
  if (state.repeat === "one" && auto) {
    media.currentTime = 0;
    media.play().catch(() => {});
    return;
  }
  let idx = state.queueIndex + 1;
  if (idx >= state.queue.length) {
    if (state.repeat === "all") idx = 0;
    else {
      media.pause();
      return;
    }
  }
  state.queueIndex = idx;
  playTrack(state.queue[idx], { rebuild: false });
}

function prev() {
  if (!state.queue.length) return;
  if (media.currentTime > 3) {
    media.currentTime = 0;
    return;
  }
  let idx = state.queueIndex - 1;
  if (idx < 0) idx = state.repeat === "all" ? state.queue.length - 1 : 0;
  state.queueIndex = idx;
  playTrack(state.queue[idx], { rebuild: false });
}

function onEnded() {
  next(true);
}

function seekBy(delta) {
  if (!state.currentId) return;
  media.currentTime = Math.max(0, Math.min((media.duration || 0), media.currentTime + delta));
}

function toggleFav(id) {
  const track = state.byId.get(id);
  if (!track) return;
  const now = !state.favorites.has(id);
  if (now) state.favorites.add(id);
  else state.favorites.delete(id);
  updateFavUI(id);
  if (state.view === "favorites") renderList();
  toast(now ? "Added to favorites" : "Removed from favorites");
  fetch(`/api/favorites/${id}`, { method: "POST" }).catch(() => {});
}

/* ---------------- events ---------------- */
els.playlist.addEventListener("click", (e) => {
  const li = e.target.closest(".track");
  if (!li) return;
  const id = li.dataset.id;
  if (e.target.closest(".heart")) {
    toggleFav(id);
    return;
  }
  playTrack(id);
  if (window.innerWidth <= 860) document.body.classList.remove("nav-open");
});

els.play.addEventListener("click", togglePlay);
$("next").addEventListener("click", () => next());
$("prev").addEventListener("click", prev);
$("fwd").addEventListener("click", () => seekBy(state.skip));
$("back").addEventListener("click", () => seekBy(-state.skip));

els.shuffle.addEventListener("click", () => {
  state.shuffle = !state.shuffle;
  els.shuffle.classList.toggle("is-active", state.shuffle);
  localStorage.setItem("shuffle", state.shuffle ? "1" : "0");
  toast(state.shuffle ? "Shuffle on" : "Shuffle off");
  if (state.currentId && state.shuffle) buildQueue(state.currentId);
});

els.repeat.addEventListener("click", () => {
  state.repeat = state.repeat === "off" ? "all" : state.repeat === "all" ? "one" : "off";
  els.repeat.classList.toggle("is-active", state.repeat !== "off");
  els.repeat.style.opacity = state.repeat === "one" ? "0.65" : "1";
  localStorage.setItem("repeat", state.repeat);
  toast("Repeat: " + state.repeat);
});

$("shuffleAll").addEventListener("click", () => {
  const list = visible();
  if (!list.length) return;
  state.shuffle = true;
  els.shuffle.classList.add("is-active");
  localStorage.setItem("shuffle", "1");
  playTrack(list[Math.floor(Math.random() * list.length)].id);
});

els.favBig.addEventListener("click", () => {
  if (state.currentId) toggleFav(state.currentId);
});

els.volume.addEventListener("input", () => {
  media.volume = Number(els.volume.value) / 100;
  localStorage.setItem("vol", els.volume.value);
  setFill(els.volume);
});

els.seek.addEventListener("input", () => {
  state.scrubbing = true;
  const d = media.duration || 0;
  const t = (Number(els.seek.value) / 1000) * d;
  els.cur.textContent = fmt(t);
  setFill(els.seek);
  if (d) media.currentTime = t;
});
els.seek.addEventListener("change", () => {
  state.scrubbing = false;
});

els.search.addEventListener("input", () => {
  state.query = els.search.value;
  renderList();
});

document.querySelectorAll(".tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((t) => t.classList.remove("is-active"));
    tab.classList.add("is-active");
    state.view = tab.dataset.view;
    renderList();
  });
});

$("menuBtn").addEventListener("click", () => document.body.classList.toggle("nav-open"));

document.addEventListener("keydown", (e) => {
  if (e.target.tagName === "INPUT") return;
  if (e.code === "Space") { e.preventDefault(); togglePlay(); }
  else if (e.code === "ArrowRight") seekBy(5);
  else if (e.code === "ArrowLeft") seekBy(-5);
  else if (e.code === "ArrowUp") { els.volume.value = Math.min(100, +els.volume.value + 5); els.volume.dispatchEvent(new Event("input")); }
  else if (e.code === "ArrowDown") { els.volume.value = Math.max(0, +els.volume.value - 5); els.volume.dispatchEvent(new Event("input")); }
  else if (e.key === "n") next();
  else if (e.key === "p") prev();
});

/* ---------------- media session ---------------- */
function setupMediaSession() {
  if (!("mediaSession" in navigator)) return;
  const H = {
    play: () => media.play(),
    pause: () => media.pause(),
    previoustrack: prev,
    nexttrack: () => next(),
    seekbackward: (d) => seekBy(-(d.seekOffset || state.skip)),
    seekforward: (d) => seekBy(d.seekOffset || state.skip),
    seekto: (d) => { if (d.seekTime != null) media.currentTime = d.seekTime; },
  };
  for (const [k, fn] of Object.entries(H)) {
    try { navigator.mediaSession.setActionHandler(k, fn); } catch (_) {}
  }
}

/* ---------------- fullscreen & picture-in-picture ---------------- */
function canFullscreen() {
  return !!(
    document.fullscreenEnabled ||
    document.webkitFullscreenEnabled ||
    videoEl.webkitEnterFullscreen
  );
}

function canPiP() {
  return "requestPictureInPicture" in videoEl || "webkitSetPresentationMode" in videoEl;
}

function updateVideoButtons(isVideo) {
  els.pip.style.display = isVideo && els.pip.dataset.supported === "1" ? "" : "none";
  els.fullscreen.style.display =
    isVideo && els.fullscreen.dataset.supported === "1" ? "" : "none";
}

async function toggleFullscreen() {
  const active =
    document.fullscreenElement || document.webkitFullscreenElement || videoEl.webkitDisplayingFullscreen;
  if (active) {
    if (document.exitFullscreen) await document.exitFullscreen().catch(() => {});
    else if (document.webkitExitFullscreen) document.webkitExitFullscreen();
    else if (videoEl.webkitExitFullscreen) videoEl.webkitExitFullscreen();
    return;
  }
  const el = document.querySelector(".stage");
  if (el.requestFullscreen) {
    try {
      await el.requestFullscreen();
      return;
    } catch (_) {}
  }
  if (el.webkitRequestFullscreen) {
    try { el.webkitRequestFullscreen(); return; } catch (_) {}
  }
  if (videoEl.webkitEnterFullscreen) {
    try { videoEl.webkitEnterFullscreen(); } catch (_) {}
  }
}

function exitFullscreenIfAny() {
  if (document.fullscreenElement && document.exitFullscreen) document.exitFullscreen().catch(() => {});
  else if (document.webkitFullscreenElement && document.webkitExitFullscreen) document.webkitExitFullscreen();
  if (videoEl.webkitDisplayingFullscreen && videoEl.webkitExitFullscreen) videoEl.webkitExitFullscreen();
}

async function togglePiP() {
  if (document.pictureInPictureElement) {
    try { await document.exitPictureInPicture(); } catch (_) {}
    return;
  }
  if (videoEl.paused) {
    try { await videoEl.play(); } catch (_) {}
  }
  if (videoEl.requestPictureInPicture) {
    try { await videoEl.requestPictureInPicture(); } catch (_) {}
    return;
  }
  if (videoEl.webkitSetPresentationMode) {
    const mode =
      videoEl.webkitPresentationMode === "picture-in-picture" ? "inline" : "picture-in-picture";
    try { videoEl.webkitSetPresentationMode(mode); } catch (_) {}
  }
}

function exitPiPIfAny() {
  if (document.pictureInPictureElement && document.exitPictureInPicture) {
    document.exitPictureInPicture().catch(() => {});
  }
  if (videoEl.webkitPresentationMode === "picture-in-picture" && videoEl.webkitSetPresentationMode) {
    try { videoEl.webkitSetPresentationMode("inline"); } catch (_) {}
  }
}

let fsHideTimer;
function scheduleFsHide() {
  const stage = document.querySelector(".stage");
  if (!stage || !(document.fullscreenElement || document.webkitFullscreenElement)) return;
  stage.classList.remove("controls-hidden");
  clearTimeout(fsHideTimer);
  fsHideTimer = setTimeout(() => stage.classList.add("controls-hidden"), 3000);
}

function updateFsIcon() {
  const on =
    !!(document.fullscreenElement || document.webkitFullscreenElement || videoEl.webkitDisplayingFullscreen);
  els.fsEnter.style.display = on ? "none" : "";
  els.fsExit.style.display = on ? "" : "none";

  const stage = document.querySelector(".stage");
  if (!stage) return;
  if (document.fullscreenElement || document.webkitFullscreenElement) {
    scheduleFsHide();
  } else {
    stage.classList.remove("controls-hidden");
    clearTimeout(fsHideTimer);
  }
}

function updatePipIcon() {
  const on =
    !!document.pictureInPictureElement || videoEl.webkitPresentationMode === "picture-in-picture";
  els.pip.classList.toggle("is-active", on);
}

function setupVideoFeatures() {
  els.pip.dataset.supported = canPiP() ? "1" : "0";
  els.fullscreen.dataset.supported = canFullscreen() ? "1" : "0";
  els.pip.addEventListener("click", togglePiP);
  els.fullscreen.addEventListener("click", toggleFullscreen);

  document.addEventListener("fullscreenchange", updateFsIcon);
  document.addEventListener("webkitfullscreenchange", updateFsIcon);
  ["pointermove", "pointerdown", "touchstart", "keydown"].forEach((ev) =>
    document.addEventListener(ev, scheduleFsHide, { passive: true })
  );
  videoEl.addEventListener("enterpictureinpicture", updatePipIcon);
  videoEl.addEventListener("leavepictureinpicture", updatePipIcon);
  videoEl.addEventListener("webkitpresentationmodechanged", updatePipIcon);
  videoEl.addEventListener("webkitbeginfullscreen", updateFsIcon);
  videoEl.addEventListener("webkitendfullscreen", updateFsIcon);

  videoEl.addEventListener("loadedmetadata", () => {
    if (videoEl.videoWidth && videoEl.videoHeight) {
      els.visual.style.setProperty("--video-aspect", `${videoEl.videoWidth} / ${videoEl.videoHeight}`);
    }
  });

  updateFsIcon();
  updatePipIcon();
}

/* ---------------- resizable sidebar ---------------- */
function setupResizer() {
  const handle = $("resizer");
  const sidebar = $("sidebar");
  const MIN = 220;
  const maxWidth = () => Math.min(640, window.innerWidth * 0.72);

  const applyWidth = (w) =>
    document.documentElement.style.setProperty("--sidebar-w", Math.round(w) + "px");

  const saved = parseInt(localStorage.getItem("sidebarW") || "", 10);
  if (saved) applyWidth(Math.max(MIN, Math.min(saved, maxWidth())));

  let startX = 0;
  let startW = 0;
  let dragging = false;

  handle.addEventListener("pointerdown", (e) => {
    dragging = true;
    startX = e.clientX;
    startW = sidebar.getBoundingClientRect().width;
    handle.classList.add("is-dragging");
    handle.setPointerCapture(e.pointerId);
    e.preventDefault();
  });

  handle.addEventListener("pointermove", (e) => {
    if (!dragging) return;
    const w = Math.max(MIN, Math.min(startW + (e.clientX - startX), maxWidth()));
    applyWidth(w);
  });

  const end = (e) => {
    if (!dragging) return;
    dragging = false;
    handle.classList.remove("is-dragging");
    try {
      handle.releasePointerCapture(e.pointerId);
    } catch (_) {}
    localStorage.setItem("sidebarW", String(Math.round(sidebar.getBoundingClientRect().width)));
  };
  handle.addEventListener("pointerup", end);
  handle.addEventListener("pointercancel", end);

  window.addEventListener("resize", () => {
    if (sidebar.getBoundingClientRect().width > maxWidth()) applyWidth(maxWidth());
  });
}

/* ---------------- boot ---------------- */
function applySkipLabels() {
  document.querySelectorAll("#back text, #fwd text").forEach((t) => {
    t.textContent = String(state.skip);
  });
}

async function boot() {
  attachMediaHandlers();
  setPlayingIcon(false);
  setupMediaSession();
  setupResizer();
  setupVideoFeatures();

  let cfg = { player: {} };
  try {
    cfg = await (await fetch("/api/config")).json();
  } catch (_) {}
  const p = cfg.player || {};
  document.body.classList.toggle("theme-light", p.theme === "light");

  const lsShuffle = localStorage.getItem("shuffle");
  state.shuffle = lsShuffle === null ? !!p.shuffle : lsShuffle === "1";
  state.repeat = localStorage.getItem("repeat") || p.repeat || "off";
  state.skip = p.skip_seconds || 10;

  els.shuffle.classList.toggle("is-active", state.shuffle);
  els.repeat.classList.toggle("is-active", state.repeat !== "off");
  els.volume.value =
    localStorage.getItem("vol") ?? String(Math.round((p.volume ?? 0.8) * 100));
  setFill(els.volume);
  applySkipLabels();

  try {
    const res = await fetch("/api/tracks");
    const data = await res.json();
    applyLibrary(data);
  } catch (_) {
    toast("Could not load library");
  }
  renderList();
  if (state.scanning) pollLibrary();
  else warmTranscodes();
}

function applyLibrary(data) {
  state.tracks = data.tracks || [];
  state.byId = new Map(state.tracks.map((t) => [t.id, t]));
  state.favorites = new Set(state.tracks.filter((t) => t.favorite).map((t) => t.id));
  state.scanning = !!data.scanning;
  state.progress = data.progress || { done: 0, total: 0 };
}

function pollLibrary() {
  clearTimeout(pollLibrary._timer);
  pollLibrary._timer = setTimeout(async () => {
    try {
      const data = await (await fetch("/api/tracks")).json();
      applyLibrary(data);
      renderList();
      if (state.scanning) {
        pollLibrary();
      } else {
        toast(`Library loaded · ${state.tracks.length} items`);
        warmTranscodes();
      }
    } catch (_) {
      pollLibrary();
    }
  }, 2000);
}

boot();
