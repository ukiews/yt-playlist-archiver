const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

const themeKey = 'yt-playlist-archiver-theme';
function setTheme(theme) {
  document.documentElement.dataset.theme = theme;
  const button = $('#theme-toggle');
  const targetTheme = theme === 'dark' ? 'light' : 'dark';
  button.textContent = theme === 'dark' ? '☀︎' : '☾';
  button.setAttribute('aria-label', `Switch to ${targetTheme} mode`);
  button.title = `Switch to ${targetTheme} mode`;
}
setTheme(localStorage.getItem(themeKey) || (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'));
$('#theme-toggle').addEventListener('click', () => {
  const theme = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
  localStorage.setItem(themeKey, theme);
  setTheme(theme);
});

const appState = {
  data: null,
  filter: 'all',
  search: '',
  selected: null,
  editDirty: false,
  page: 'activity',
  log: 'scheduled',
  downloadFilter: 'all',
  progress: null,
  file: 'subscriptions.yaml',
  settings: null,
  settingsPage: 'presets',
  poll: null,
  progressPoll: null,
  progressLoading: false,
  recordedSignature: null,
  clockPoll: null,
  nextCheck: null,
  hadActiveJob: false,
};

function escapeHtml(value = '') {
  return String(value).replace(/[&<>'"]/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[ch]));
}

const fieldHelp = {
  'new-id': 'Choose a unique internal name, such as choir_audio. This becomes the key in subscriptions.yaml. Use lowercase letters, numbers, and underscores; it cannot be renamed after creation.',
  'new-output': 'Folder inside the container. Start with /media/videos/ or /media/music/ so downloads reach the mounted storage.',
  'new-schedule': 'Choose when this subscription is checked. You can change each group’s interval under Configuration → Scheduler.',
  'new-use-auth': 'Use the saved YouTube sign-in for private sources such as Watch Later.',
  'edit-output': 'Container path where downloads are stored. Moving it does not move files already downloaded.',
  'edit-interval': 'Minutes between checks for this schedule group. Changing it also affects other subscriptions in the same group.',
  'setting-working-dir': 'Temporary processing folder inside the container. Keep it on storage with enough space for a full download.',
  'video-sync-source': 'When enabled, files removed from the source playlist may also be removed locally.',
  'audio-sync-source': 'When enabled, files removed from the source playlist may also be removed locally.',
};

let activeHelp = null;
let helpPinned = false;
let helpPopover;

function hideHelp() {
  if (activeHelp) activeHelp.setAttribute('aria-expanded', 'false');
  activeHelp = null;
  helpPinned = false;
  if (helpPopover) helpPopover.classList.add('hidden');
}

function showHelp(tip, pinned = false) {
  if (activeHelp && activeHelp !== tip) activeHelp.setAttribute('aria-expanded', 'false');
  activeHelp = tip;
  helpPinned = pinned;
  tip.setAttribute('aria-expanded', 'true');
  const layer = tip.closest('dialog') || document.body;
  if (helpPopover.parentElement !== layer) layer.append(helpPopover);
  helpPopover.textContent = tip.dataset.help;
  helpPopover.classList.remove('hidden');
  const rect = tip.getBoundingClientRect();
  const width = Math.min(310, window.innerWidth - 24);
  helpPopover.style.width = `${width}px`;
  const height = helpPopover.getBoundingClientRect().height;
  helpPopover.style.left = `${Math.max(12, Math.min(rect.left - 6, window.innerWidth - width - 12))}px`;
  helpPopover.style.top = `${rect.bottom + height + 8 < window.innerHeight - 12 ? rect.bottom + 8 : Math.max(12, rect.top - height - 8)}px`;
}

function addFieldHelp(field, description) {
  const label = field?.closest('label');
  if (!label || label.querySelector('.help-tip')) return;
  const labelName = [...label.childNodes].filter(node => node.nodeType === Node.TEXT_NODE).map(node => node.textContent.trim()).filter(Boolean).join(' ');
  const tip = document.createElement('span');
  tip.className = 'help-tip';
  tip.textContent = '?';
  tip.tabIndex = 0;
  tip.setAttribute('role', 'button');
  tip.setAttribute('aria-label', `Help: ${labelName}`);
  tip.setAttribute('aria-expanded', 'false');
  tip.dataset.help = description;
  tip.addEventListener('mouseenter', () => { if (!helpPinned) showHelp(tip); });
  tip.addEventListener('mouseleave', () => { if (!helpPinned) hideHelp(); });
  tip.addEventListener('focus', () => { if (!helpPinned) showHelp(tip); });
  tip.addEventListener('blur', () => { if (!helpPinned) hideHelp(); });
  tip.addEventListener('click', event => {
    event.preventDefault();
    event.stopPropagation();
    if (activeHelp === tip && helpPinned) hideHelp();
    else showHelp(tip, true);
  });
  tip.addEventListener('keydown', event => {
    if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); tip.click(); }
  });
  field.setAttribute('aria-description', description);
  if (labelName) field.setAttribute('aria-label', labelName);
  if (field.type === 'checkbox') label.append(tip);
  else label.insertBefore(tip, field);
}

function addStaticHelp() {
  helpPopover = document.createElement('div');
  helpPopover.id = 'help-popover';
  helpPopover.className = 'hidden';
  helpPopover.setAttribute('role', 'tooltip');
  document.body.append(helpPopover);
  document.addEventListener('click', event => { if (activeHelp && !activeHelp.contains(event.target)) hideHelp(); });
  document.addEventListener('keydown', event => { if (event.key === 'Escape') hideHelp(); });
  window.addEventListener('scroll', hideHelp, true);
  for (const [id, description] of Object.entries(fieldHelp)) addFieldHelp(document.getElementById(id), description);
  for (const prefix of ['new', 'edit']) {
    addFieldHelp(document.getElementById(`${prefix}-tag-album`), 'Set an album only when you know its real name. Leave blank to avoid a subscription album override.');
  }
}

function addFormatHelp(host) {
  addFieldHelp($('[data-format-choice]', host), 'Use the preset, adjust common format options, or enter a custom yt-dlp expression.');
}

function friendlyTime(value) {
  if (!value) return 'Not yet';
  const date = new Date(value);
  const diff = date.getTime() - Date.now();
  const abs = Math.abs(diff);
  if (abs < 60_000) {
    const seconds = Math.max(1, Math.ceil(abs / 1000));
    return diff >= 0 ? `in ${seconds} sec` : `${seconds} sec ago`;
  }
  if (abs < 3_600_000) return `${diff >= 0 ? 'in ' : ''}${Math.round(abs / 60_000)} min${diff < 0 ? ' ago' : ''}`;
  return date.toLocaleString([], {month:'short', day:'numeric', hour:'numeric', minute:'2-digit'});
}

function nextCheckTime(value) {
  if (!value) return 'Not scheduled';
  const diff = new Date(value).getTime() - Date.now();
  if (diff <= 0) return 'Due now';
  if (diff < 60_000) return `in ${Math.max(1, Math.ceil(diff / 1000))} sec`;
  return friendlyTime(value);
}

function bytes(value) {
  if (!value) return '—';
  const units = ['B','KB','MB','GB','TB'];
  let n = value, i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i += 1; }
  return `${n.toFixed(i > 2 ? 1 : 0)} ${units[i]}`;
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: {'Content-Type':'application/json', ...(options.headers || {})},
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    if (response.status === 401 && path !== '/api/login') showLogin();
    throw new Error(payload.error || `Request failed (${response.status})`);
  }
  return payload;
}

function toast(message, type = '') {
  const item = document.createElement('div');
  item.className = `toast ${type}`;
  item.textContent = message;
  $('#toast-region').append(item);
  setTimeout(() => item.remove(), 4200);
}

function showLogin() {
  $('#app').classList.add('hidden');
  $('#login').classList.remove('hidden');
  clearInterval(appState.poll);
  clearTimeout(appState.progressPoll);
  clearInterval(appState.clockPoll);
  setTimeout(() => $('#login-password').focus(), 0);
}

function showApp() {
  $('#login').classList.add('hidden');
  $('#app').classList.remove('hidden');
  refresh();
  loadSettings();
  const requestedPage = location.hash.replace('#','');
  if (['subscriptions','activity','configuration'].includes(requestedPage)) setPage(requestedPage);
  clearInterval(appState.poll);
  appState.poll = setInterval(() => { if (!document.hidden) refresh(); }, 10000);
  clearTimeout(appState.progressPoll);
  refreshProgress();
  clearInterval(appState.clockPoll);
  appState.clockPoll = setInterval(() => {
    if (appState.nextCheck) $('#next-check').textContent = nextCheckTime(appState.nextCheck);
  }, 1000);
}

function statusClass() {
  if (!appState.data) return '';
  if (appState.data.allPaused) return 'paused';
  if (!appState.data.auth.ok) return 'error';
  if (appState.data.job) return 'busy';
  return 'ok';
}

function renderSubscriptions() {
  const data = appState.data;
  const query = appState.search.toLowerCase();
  const rows = data.subscriptions.filter(row => {
    const filterMatch = appState.filter === 'all' || row.mode === appState.filter;
    const queryMatch = !query || `${row.name} ${row.id} ${row.outputDir} ${row.genre || ''}`.toLowerCase().includes(query);
    return filterMatch && queryMatch;
  });
  $('#subscription-list').innerHTML = rows.map(row => `
    <button class="subscription-row ${row.id === appState.selected ? 'selected' : ''} ${row.paused ? 'paused' : ''}" data-id="${escapeHtml(row.id)}" type="button">
      <span class="subscription-main">
        <span class="mode-icon ${row.mode}">${row.mode === 'video' ? 'MP4' : 'M4A'}</span>
        <span class="subscription-copy"><strong>${escapeHtml(row.name)}</strong><span>${escapeHtml(row.genre || row.outputDir.replace('/media/',''))}</span></span>
      </span>
      <span class="schedule-cell"><strong>${row.paused ? 'Paused' : `${row.intervalMinutes} min`}</strong><span>${row.manuallyPaused ? 'Paused individually' : row.paused ? 'All paused' : friendlyTime(row.nextRun)}</span></span>
      <span class="count-cell"><strong>${row.archiveCount.toLocaleString()}</strong><span>archived</span></span>
      <span class="status-cell"><span class="state-dot ${row.paused ? '' : row.authenticated && !data.auth.ok ? 'error' : 'ok'}"></span><span>${row.paused ? 'Paused' : row.authenticated && !data.auth.ok ? 'Sign-in' : 'Watching'}</span></span>
      <span class="row-chevron">›</span>
    </button>`).join('');
  const emptyState = $('#empty-state');
  emptyState.textContent = data.subscriptions.length
    ? 'No subscriptions match this view.'
    : 'No subscriptions yet. Select + Add to create the first one.';
  emptyState.classList.toggle('hidden', rows.length > 0);
  $$('.subscription-row').forEach(button => button.addEventListener('click', () => selectSubscription(button.dataset.id)));
}

function selectSubscription(id) {
  if (id === appState.selected && appState.editDirty) return;
  appState.selected = id;
  appState.editDirty = false;
  const row = appState.data.subscriptions.find(item => item.id === id);
  if (!row) return;
  $('#inspector-empty').classList.add('hidden');
  $('#subscription-form').classList.remove('hidden');
  $('#edit-id').value = row.id;
  $('#edit-title').textContent = row.name;
  $('#edit-mode').textContent = row.mode;
  $('#edit-mode').className = `mode-pill ${row.mode}`;
  $('#edit-url').value = row.url;
  $('#edit-output').value = row.outputDir;
  FormatBuilder.mount($('#edit-format'), row.mode, row.format === 'Preset default' ? '' : row.format, true);
  addFormatHelp($('#edit-format'));
  $('#edit-interval').value = row.intervalMinutes;
  $('#edit-genre').value = row.genre || '';
  $('#edit-tag-title').value = row.metadata?.title || '';
  $('#edit-tag-artist').value = row.metadata?.artist || '';
  $('#edit-tag-album').value = row.metadata?.album || '';
  $('#edit-tag-album-artist').value = row.metadata?.albumArtist || '';
  $('#edit-tag-date').value = row.metadata?.date || '';
  $('#edit-embed-thumbnail').checked = Boolean(row.embedThumbnail);
  $('#edit-artwork-row').classList.toggle('hidden', row.mode !== 'audio');
  $('#pause-selected').textContent = row.manuallyPaused ? 'Resume' : 'Pause';
  $('#pause-selected').classList.toggle('resume', row.manuallyPaused);
  $('#run-selected').disabled = Boolean(row.paused || appState.data.job);
  const groupMembers = appState.data.subscriptions.filter(item => item.scheduleGroup === row.scheduleGroup).length;
  $('#schedule-note').textContent = groupMembers > 1 ? `This interval is shared by ${groupMembers} subscriptions in the same schedule group.` : 'This subscription has its own schedule group.';
  $('#edit-archive').textContent = `${row.archiveCount.toLocaleString()} items`;
  $('#edit-archive-date').textContent = row.archiveUpdated ? `Updated ${friendlyTime(row.archiveUpdated)}` : 'Archive not found';
  renderSubscriptions();
}

function closeInspector() {
  appState.selected = null;
  appState.editDirty = false;
  $('#subscription-form').classList.add('hidden');
  $('#inspector-empty').classList.remove('hidden');
  renderSubscriptions();
}

function renderDownloads() {
  const data = appState.data;
  if (!data) return;
  const subscriptions = new Map(data.subscriptions.map(item => [item.id, item]));
  const downloads = (data.latestDownloads || []).filter(item => appState.downloadFilter === 'all' || item.mode === appState.downloadFilter);
  const active = (appState.progress?.activeItems || []).map(item => ({...item, subscriptionRow: subscriptions.get(item.subscription)}))
    .filter(item => item.subscriptionRow && (appState.downloadFilter === 'all' || item.subscriptionRow.mode === appState.downloadFilter))
    .filter(item => item.phase !== 'Saving download history' || !downloads.some(saved => saved.subscriptionId === item.subscription
      && ((item.videoId && saved.videoId === item.videoId) || saved.title === item.title)
      && Date.parse(saved.downloadedAt) >= Date.parse(appState.progress.job.startedAt)))
    .reverse();
  $('#downloads-summary').textContent = `${active.length ? `${active.length} in this check · ` : ''}${downloads.length} recorded download${downloads.length === 1 ? '' : 's'}`;
  const liveContainer = $('#live-downloads');
  const activeIds = new Set(active.map(item => item.id));
  [...liveContainer.children].forEach(element => { if (!activeIds.has(element.dataset.activeId)) element.remove(); });
  active.forEach((item, index) => {
    const row = item.subscriptionRow;
    let element = [...liveContainer.children].find(child => child.dataset.activeId === item.id);
    if (!element) {
      element = document.createElement('div');
      element.className = 'download-row download-row-live';
      element.dataset.activeId = item.id;
      element.innerHTML = `<span class="download-thumb download-thumb-live"></span>
        <span class="download-copy"><strong>${escapeHtml(item.title)}</strong><span class="download-live-status"></span><span class="download-tags"><span>${row.mode === 'video' ? 'Video' : 'Audio'}</span>${row.genre ? `<span>${escapeHtml(row.genre)}</span>` : ''}<span class="download-live-badge"></span></span></span>
        <span class="download-playlist"><strong>${escapeHtml(row.name)}</strong><span>${escapeHtml(row.outputDir.replace('/media/',''))}</span></span>
        <div class="download-row-progress" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-label="${escapeHtml(item.title)} transfer"><span class="download-row-progress-fill"></span></div>`;
      element.querySelector('.download-thumb').textContent = '↓';
    }
    if (liveContainer.children[index] !== element) liveContainer.insertBefore(element, liveContainer.children[index] || null);
    const thumbnail = element.querySelector('.download-thumb');
    if (item.videoId && thumbnail.dataset.videoId !== item.videoId) {
      thumbnail.dataset.videoId = item.videoId;
      thumbnail.innerHTML = `<img src="https://i.ytimg.com/vi/${escapeHtml(item.videoId)}/mqdefault.jpg" alt="" loading="lazy">`;
      thumbnail.querySelector('img').addEventListener('error', () => { thumbnail.textContent = 'YT'; });
    }
    const transferring = item.phase === 'Downloading media' && Number.isFinite(item.percent) && item.percent < 100;
    const status = `${item.phase}${item.detail ? ` · ${item.detail}` : ''}`;
    const label = transferring ? `${Math.round(item.percent)}%` : item.phase;
    const statusNode = element.querySelector('.download-live-status');
    const badge = element.querySelector('.download-live-badge');
    if (statusNode.textContent !== status) statusNode.textContent = status;
    if (badge.textContent !== label) badge.textContent = label;
    const bar = element.querySelector('.download-row-progress');
    bar.classList.toggle('busy', !transferring);
    if (transferring) {
      const percent = Math.max(0, Math.min(100, item.percent));
      bar.setAttribute('aria-valuenow', String(percent));
      bar.removeAttribute('aria-valuetext');
      bar.querySelector('.download-row-progress-fill').style.width = `${percent}%`;
    } else {
      bar.removeAttribute('aria-valuenow');
      bar.setAttribute('aria-valuetext', item.phase);
      bar.querySelector('.download-row-progress-fill').style.removeProperty('width');
    }
  });
  const recordedSignature = JSON.stringify([appState.downloadFilter, downloads, active.length === 0]);
  if (recordedSignature !== appState.recordedSignature) {
    appState.recordedSignature = recordedSignature;
    const recordedRows = downloads.map(item => `
      <a class="download-row" href="${escapeHtml(item.sourceUrl || '#')}" ${item.sourceUrl ? 'target="_blank" rel="noreferrer"' : ''}>
        <span class="download-thumb">${item.thumbnail ? `<img src="${escapeHtml(item.thumbnail)}" alt="" loading="lazy">` : item.mode.toUpperCase()}</span>
        <span class="download-copy"><strong>${escapeHtml(item.title)}</strong><span title="${escapeHtml(item.fileName)}">${escapeHtml(item.channel || item.fileName)}</span><span class="download-tags"><span>${item.mode === 'video' ? 'MP4 video' : 'M4A audio'}</span>${item.genre ? `<span>${escapeHtml(item.genre)}</span>` : ''}</span></span>
        <span class="download-playlist"><strong>${escapeHtml(item.playlist)}</strong><span>${escapeHtml(item.folder.replace('/media/',''))}</span></span>
        <span class="download-meta"><strong>${bytes(item.size)}</strong><span>${friendlyTime(item.downloadedAt)}</span></span>
      </a>`);
    $('#recorded-downloads').innerHTML = recordedRows.join('') || (active.length ? '' : '<div class="download-empty">No recorded downloads yet. New downloads will appear here.</div>');
    $$('#recorded-downloads .download-thumb img').forEach(image => image.addEventListener('error', () => { image.parentElement.textContent = 'YT'; }));
  }
}

function renderActivity() {
  const data = appState.data;
  $('#scheduler-status').textContent = data.job ? data.job.label : (data.allPaused ? 'Paused' : data.scheduleActive ? 'Watching' : 'Unavailable');
  $('#auth-card-status').textContent = data.auth.ok ? 'Authenticated' : 'Action needed';
  $('#auth-card-detail').textContent = data.auth.status;
  const disk = data.storage.videos.total ? data.storage.videos : data.storage.music;
  const percent = disk.total ? Math.round(disk.used / disk.total * 100) : 0;
  $('#media-storage').textContent = disk.total ? `${percent}% used` : 'Unavailable';
  $('#media-storage-detail').textContent = disk.total ? `${bytes(disk.free)} free of ${bytes(disk.total)}` : 'Media path not found';
  $('#last-error').textContent = data.lastError ? 'Attention needed' : 'No errors';
  $('#last-error').classList.toggle('error-text', Boolean(data.lastError));
  $('#last-error-detail').textContent = data.lastError ? `${data.lastError.message} · ${friendlyTime(data.lastError.at)}` : 'Scheduled and manual checks are healthy';
  $('#activity-log').textContent = data.logs[appState.log] || 'No activity has been recorded yet.';
  $('#log-updated').textContent = `Refreshed ${new Date().toLocaleTimeString([], {hour:'numeric',minute:'2-digit',second:'2-digit'})}`;
  $('#activity-badge').classList.toggle('hidden', !data.job);
  renderDownloads();
}

function renderProgress(data) {
  appState.progress = data;
  renderDownloads();
  if (!data.job) {
    if (appState.hadActiveJob) refresh();
    appState.hadActiveJob = false;
    return;
  }
  appState.hadActiveJob = true;
}

function renderAuthDialog() {
  const auth = appState.data?.auth;
  if (!auth) return;
  $('#auth-dialog-dot').className = `state-dot ${auth.ok ? 'ok' : 'error'}`;
  $('#auth-dialog-status').textContent = auth.ok ? 'Watch Later access verified' : 'Watch Later sign-in needs attention';
  $('#auth-dialog-detail').textContent = auth.status;
  $('#auth-explainer').textContent = auth.ok
    ? 'Your current sign-in works. Use the steps below only when it needs refreshing; sign in inside the separate Firefox browser, not the browser showing this dashboard.'
    : 'Sign in inside the separate Firefox browser below. Signing in to YouTube in the browser showing this dashboard does not update the downloader.';
  const browserUrl = auth.firefoxBrowserUrl || (auth.firefoxBrowserPort ? `http://${location.hostname}:${auth.firefoxBrowserPort}/` : '');
  const browserReady = auth.firefoxBrowserAvailable;
  const containerControl = auth.firefoxContainerControlAvailable;
  const containerRunning = auth.firefoxContainerRunning;
  const browserStatus = $('#auth-browser-status');
  browserStatus.className = `auth-browser-status ${browserReady === true ? 'ready' : browserReady === false ? 'offline' : ''}`;
  browserStatus.textContent = containerControl && containerRunning === false ? 'Firefox is stopped and is not using active CPU or memory.'
    : browserReady === true ? `Sign-in browser responds at ${browserUrl}`
    : browserReady === false ? 'The sign-in browser is starting or cannot be reached yet.'
    : browserUrl ? 'Start the sign-in browser service before opening it. Connection status is not configured.'
    : 'No sign-in browser address is configured. An administrator can set one, or use the advanced file option below.';
  const toggle = $('#toggle-auth-browser');
  toggle.textContent = containerControl ? (containerRunning ? 'Stop browser' : 'Start browser') : 'Check connection';
  toggle.dataset.action = containerControl ? (containerRunning ? 'stop' : 'start') : 'check';
  toggle.title = containerControl ? '' : (auth.firefoxContainerControlError || 'Container control is not configured');
  $('#auth-start-detail').textContent = containerControl
    ? 'The dashboard can start Firefox only while you need to refresh the YouTube session.'
    : 'Start Firefox on its host, then use Check connection. Docker control is optional.';
  $('#auth-import-title').textContent = containerControl ? 'Import, test, and stop' : 'Import and test Watch Later';
  $('#auth-import-detail').textContent = containerControl
    ? 'Return here after signing in. The dashboard imports YouTube cookies and stops Firefox to release its resources.'
    : 'Return here after signing in. Only YouTube cookies are copied from the Firefox profile.';
  $('#import-firefox-auth').textContent = containerControl ? 'Import and stop' : 'Import and test';
  const browserLink = $('#open-auth-browser');
  if (browserUrl && containerRunning !== false) browserLink.href = browserUrl;
  else browserLink.removeAttribute('href');
  browserLink.classList.toggle('disabled', !browserLink.hasAttribute('href'));
  browserLink.setAttribute('aria-disabled', String(!browserLink.hasAttribute('href')));
  browserLink.tabIndex = browserLink.hasAttribute('href') ? 0 : -1;
  $('#import-firefox-auth').disabled = !auth.firefoxProfileAvailable;
  $('#import-firefox-auth').title = auth.firefoxProfileAvailable ? '' : 'Firefox profile is not connected to this installation';
}

async function refreshProgress() {
  if (appState.progressLoading) return;
  appState.progressLoading = true;
  try {
    renderProgress(await api('/api/progress'));
  } catch (error) {
    if (!$('#app').classList.contains('hidden')) console.error(error);
  } finally {
    appState.progressLoading = false;
    clearTimeout(appState.progressPoll);
    if (!$('#app').classList.contains('hidden')) {
      const delay = document.hidden ? 15000 : appState.progress?.job ? 2000 : 5000;
      appState.progressPoll = setTimeout(refreshProgress, delay);
    }
  }
}

document.addEventListener('visibilitychange', () => {
  if (!document.hidden && !$('#app').classList.contains('hidden')) {
    clearTimeout(appState.progressPoll);
    refreshProgress();
    refresh();
  }
});

function setField(id, value) {
  const field = $(`#${id}`);
  if (field.type === 'checkbox') field.checked = Boolean(value);
  else {
    if (field.tagName === 'SELECT' && value && ![...field.options].some(option => option.value === value)) {
      field.add(new Option(value, value));
    }
    field.value = value ?? '';
  }
}

function renderSettings() {
  const settings = appState.settings;
  if (!settings) return;
  setField('setting-working-dir', settings.workingDirectory);
  for (const [prefix, preset] of [['video', settings.video], ['audio', settings.audio]]) {
    for (const [key, suffix] of [
      ['fileName','file-name'], ['archiveName','archive-name'],
      ['titleTag','title-tag'], ['artistTag','artist-tag'], ['maintainArchive','maintain-archive'],
      ['syncWithSource','sync-source'], ['breakOnExisting','break-existing'],
    ]) setField(`${prefix}-${suffix}`, preset[key]);
  }
  FormatBuilder.mount($('#video-format'), 'video', settings.video.format);
  FormatBuilder.mount($('#audio-format'), 'audio', settings.audio.format);
  addFormatHelp($('#video-format'));
  addFormatHelp($('#audio-format'));
  setField('video-merge-format', settings.video.mergeFormat);
  setField('audio-codec', settings.audio.codec);
  setField('audio-embed-thumbnail', settings.audio.embedThumbnail);
  const names = Object.fromEntries((appState.data?.subscriptions || []).map(item => [item.id, `${item.name} · ${item.mode}`]));
  $('#schedule-cards').innerHTML = settings.schedules.map(schedule => `
    <section class="settings-card schedule-card">
      <span>Every ${schedule.intervalMinutes} minutes</span>
      <h3>${escapeHtml(schedule.label)}</h3>
      <label>Run every<input type="number" min="1" max="1440" value="${schedule.intervalMinutes}" data-schedule-id="${escapeHtml(schedule.id)}" required><span class="input-suffix">min</span></label>
      <div class="schedule-members">${schedule.subscriptions.map(id => `<span>${escapeHtml(names[id] || id)}</span>`).join('')}</div>
    </section>`).join('');
  $$('[data-schedule-id]').forEach(field => addFieldHelp(field, 'Minutes between checks for every subscription in this schedule group.'));
}

async function loadSettings() {
  try {
    appState.settings = await api('/api/settings');
    renderSettings();
  } catch (error) { toast(error.message, 'error'); }
}

function setSettingsPage(page) {
  appState.settingsPage = page;
  $$('[data-settings-page]').forEach(button => button.classList.toggle('active', button.dataset.settingsPage === page));
  $$('[data-settings-panel]').forEach(panel => panel.classList.toggle('active', panel.dataset.settingsPanel === page));
  if (page === 'advanced' && !$('#file-editor').value) loadFile(appState.file);
}

function render() {
  const data = appState.data;
  if (!data) return;
  const cls = statusClass();
  $('#live-dot').className = `state-dot ${cls}`;
  $('#system-label').textContent = data.job ? 'Check running' : data.allPaused ? 'Checks paused' : !data.auth.ok ? 'YouTube sign-in needed' : data.scheduleActive ? 'Scheduler active' : 'Scheduler unavailable';
  const lastDownload = data.latestDownloads?.[0];
  $('#footer-status').textContent = lastDownload?.downloadedAt ? `Last download ${friendlyTime(lastDownload.downloadedAt)}` : 'No downloads recorded yet';
  $('#footer-status').title = lastDownload?.fileName || '';
  $('#app-version').textContent = `YT Playlist Archiver v${data.version}`;
  $('#library-total').textContent = `${data.totals.archived.toLocaleString()} archived`;
  const next = data.subscriptions.filter(item => !item.paused).map(item => item.nextRun).filter(Boolean).sort()[0];
  appState.nextCheck = next || null;
  $('#next-check').textContent = nextCheckTime(appState.nextCheck);
  $('#subscription-count').textContent = data.totals.subscriptions;
  const disabled = Boolean(data.job);
  for (const id of ['run-all','check-auth','run-selected']) $(`#${id}`).disabled = disabled;
  $('#run-all').disabled = disabled || data.allPaused || data.subscriptions.every(item => item.paused);
  $('#pause-all').textContent = data.allPaused ? 'Resume all' : 'Pause all';
  renderSubscriptions();
  renderActivity();
  renderAuthDialog();
  if (appState.selected) selectSubscription(appState.selected);
}

async function refresh() {
  try {
    appState.data = await api('/api/state');
    render();
  } catch (error) {
    if (!$('#app').classList.contains('hidden')) {
      $('#system-label').textContent = 'Connection failed';
      $('#live-dot').className = 'state-dot error';
      $('#footer-status').textContent = error.message;
    }
  }
}

async function startRun(ids) {
  try {
    const result = await api('/api/run', {method:'POST', body: JSON.stringify({subscriptions: ids, dryRun: false})});
    toast(`${result.job.label} started`);
    appState.page = 'activity';
    setPage('activity');
    await refresh();
  } catch (error) { toast(error.message, 'error'); }
}

function setPage(page) {
  appState.page = page;
  $$('.page-tab').forEach(tab => tab.classList.toggle('active', tab.dataset.page === page));
  $$('.page').forEach(item => item.classList.toggle('active', item.id === `page-${page}`));
  if (page === 'configuration' && !appState.settings) loadSettings();
}

async function loadFile(name) {
  try {
    const result = await api(`/api/file/${encodeURIComponent(name)}`);
    appState.file = name;
    $('#editor-name').textContent = name;
    $('#file-editor').value = result.content;
    $$('.file-list button').forEach(button => button.classList.toggle('active', button.dataset.file === name));
  } catch (error) { toast(error.message, 'error'); }
}

$('#login-form').addEventListener('submit', async event => {
  event.preventDefault();
  $('#login-error').textContent = '';
  try {
    await api('/api/login', {method:'POST', body: JSON.stringify({password: $('#login-password').value})});
    $('#login-password').value = '';
    showApp();
  } catch (error) { $('#login-error').textContent = error.message; }
});

$$('.page-tab').forEach(tab => tab.addEventListener('click', () => {
  location.hash = tab.dataset.page;
  setPage(tab.dataset.page);
}));
window.addEventListener('hashchange', () => {
  const page = location.hash.replace('#','');
  if (['subscriptions','activity','configuration'].includes(page)) setPage(page);
});
$$('[data-filter]').forEach(button => button.addEventListener('click', () => {
  appState.filter = button.dataset.filter;
  $$('[data-filter]').forEach(item => item.classList.toggle('active', item === button));
  renderSubscriptions();
}));
$('#search').addEventListener('input', event => { appState.search = event.target.value; renderSubscriptions(); });
$('#close-inspector').addEventListener('click', closeInspector);
$('#run-all').addEventListener('click', () => startRun([]));
$('#pause-all').addEventListener('click', async () => {
  const paused = !appState.data.allPaused;
  try {
    await api('/api/subscriptions/pause-all', {method:'POST', body: JSON.stringify({paused})});
    toast(paused ? 'All subscriptions paused' : 'Automatic checks resumed; individual pauses are preserved');
    await refresh();
  } catch (error) { toast(error.message, 'error'); }
});
$('#add-subscription').addEventListener('click', () => {
  const schedules = appState.settings?.schedules || [];
  $('#new-schedule').innerHTML = schedules.map(item => `<option value="${escapeHtml(item.id)}">${escapeHtml(item.label)} · every ${item.intervalMinutes} min</option>`).join('');
  if (!schedules.length) { toast('Schedule groups are still loading', 'error'); return; }
  $('#add-form').reset();
  $('#new-schedule').value = schedules.find(item => item.id === 'frequent')?.id
    || schedules.find(item => item.id === 'standard')?.id
    || schedules.find(item => item.id !== 'watch-later')?.id
    || schedules[0].id;
  $('#new-output').value = '/media/videos/';
  FormatBuilder.mount($('#new-format'), 'video', '', true);
  addFormatHelp($('#new-format'));
  $('#add-dialog').showModal();
  $('#new-id').focus();
});
$$('[data-close-add]').forEach(button => button.addEventListener('click', () => $('#add-dialog').close()));
$('#new-mode').addEventListener('change', () => {
  $('#new-output').value = $('#new-mode').value === 'audio' ? '/media/music/' : '/media/videos/';
  FormatBuilder.mount($('#new-format'), $('#new-mode').value, '', true);
  addFormatHelp($('#new-format'));
});
$('#add-form').addEventListener('submit', async event => {
  event.preventDefault();
  const payload = {
    id: $('#new-id').value.trim(), mode: $('#new-mode').value,
    url: $('#new-url').value.trim(), outputDir: $('#new-output').value.trim(),
    scheduleGroup: $('#new-schedule').value, genre: $('#new-genre').value.trim(),
    format: FormatBuilder.value($('#new-format')), useAuth: $('#new-use-auth').checked,
    embedThumbnail: $('#new-embed-thumbnail').checked,
    metadata: {
      title: $('#new-tag-title').value.trim(), artist: $('#new-tag-artist').value.trim(),
      album: $('#new-tag-album').value.trim(), albumArtist: $('#new-tag-album-artist').value.trim(),
      date: $('#new-tag-date').value.trim(),
    },
  };
  try {
    const result = await api('/api/subscription/create', {method:'POST', body: JSON.stringify(payload)});
    $('#add-dialog').close();
    toast(`${result.subscription} added`);
    await refresh();
    await loadSettings();
    selectSubscription(result.subscription);
  } catch (error) { toast(error.message, 'error'); }
});
$('#run-selected').addEventListener('click', () => startRun([$('#edit-id').value]));
$('#check-auth').addEventListener('click', () => {
  renderAuthDialog();
  $('#auth-dialog').showModal();
});
$('#toggle-auth-browser').addEventListener('click', async () => {
  const button = $('#toggle-auth-browser');
  const action = button.dataset.action;
  if (action === 'check') { await refresh(); return; }
  button.disabled = true;
  button.textContent = action === 'start' ? 'Starting…' : 'Stopping…';
  try {
    await api(`/api/auth/browser/${action}`, {method:'POST', body:'{}'});
    toast(`Sign-in browser ${action === 'start' ? 'started' : 'stopped'}`);
    await refresh();
    if (action === 'start') {
      for (let attempt = 0; attempt < 12 && appState.data?.auth?.firefoxBrowserAvailable !== true; attempt += 1) {
        await new Promise(resolve => setTimeout(resolve, 750));
        await refresh();
      }
    }
  } catch (error) { toast(error.message, 'error'); }
  finally { button.disabled = false; renderAuthDialog(); }
});
$('#verify-auth').addEventListener('click', async () => {
  try {
    const result = await api('/api/auth/check', {method:'POST', body:'{}'});
    toast(`${result.job.label} started`);
    $('#auth-dialog').close();
    setPage('activity');
    refresh();
  } catch (error) { toast(error.message, 'error'); }
});
$('#import-firefox-auth').addEventListener('click', async () => {
  const button = $('#import-firefox-auth');
  button.disabled = true;
  button.textContent = 'Importing and verifying…';
  try {
    const result = await api('/api/auth/import-firefox', {method:'POST', body:'{}'});
    toast(`YouTube sign-in verified · ${result.cookieCount} cookies imported${result.browserStopped ? ' · browser stopped' : ''}`);
    await refresh();
  } catch (error) { toast(error.message, 'error'); }
  finally { renderAuthDialog(); }
});
$('#import-cookie-file').addEventListener('click', async () => {
  const file = $('#cookie-upload').files[0];
  if (!file) { toast('Select a cookies.txt file first', 'error'); return; }
  try {
    await api('/api/auth/import-file', {method:'POST', body: JSON.stringify({content: await file.text()})});
    toast('YouTube sign-in imported and verified');
    $('#cookie-upload').value = '';
    await refresh();
  } catch (error) { toast(error.message, 'error'); }
});
$('#pause-selected').addEventListener('click', async () => {
  const id = $('#edit-id').value;
  const row = appState.data.subscriptions.find(item => item.id === id);
  if (!row) return;
  try {
    await api('/api/subscription/pause', {method:'POST', body: JSON.stringify({id, paused: !row.manuallyPaused})});
    toast(`${row.name} ${row.manuallyPaused ? 'resumed individually' : 'paused individually'}`);
    await refresh();
  } catch (error) { toast(error.message, 'error'); }
});
$('#remove-selected').addEventListener('click', async () => {
  const id = $('#edit-id').value;
  const row = appState.data.subscriptions.find(item => item.id === id);
  if (!row || !window.confirm(`Remove ${row.name} (${id}) from subscriptions? Downloaded files and its archive will be kept.`)) return;
  try {
    await api('/api/subscription/remove', {method:'POST', body: JSON.stringify({id})});
    closeInspector();
    toast(`${row.name} removed; downloaded files kept`);
    await refresh();
    await loadSettings();
  } catch (error) { toast(error.message, 'error'); }
});
$('#subscription-form').addEventListener('submit', async event => {
  event.preventDefault();
  const payload = {
    id: $('#edit-id').value,
    url: $('#edit-url').value,
    outputDir: $('#edit-output').value,
    intervalMinutes: Number($('#edit-interval').value),
    genre: $('#edit-genre').value,
    format: FormatBuilder.value($('#edit-format')),
    metadata: {
      title: $('#edit-tag-title').value,
      artist: $('#edit-tag-artist').value,
      album: $('#edit-tag-album').value,
      albumArtist: $('#edit-tag-album-artist').value,
      date: $('#edit-tag-date').value,
    },
    embedThumbnail: $('#edit-embed-thumbnail').checked,
  };
  try {
    await api('/api/subscription', {method:'POST', body: JSON.stringify(payload)});
    toast(`${$('#edit-title').textContent} settings saved`);
    appState.editDirty = false;
    await refresh();
  } catch (error) { toast(error.message, 'error'); }
});
$('#subscription-form').addEventListener('input', () => { appState.editDirty = true; });
$('#subscription-form').addEventListener('change', () => { appState.editDirty = true; });
$$('[data-log]').forEach(button => button.addEventListener('click', () => {
  appState.log = button.dataset.log;
  $$('[data-log]').forEach(item => item.classList.toggle('active', item === button));
  renderActivity();
}));
$$('[data-download-filter]').forEach(button => button.addEventListener('click', () => {
  appState.downloadFilter = button.dataset.downloadFilter;
  $$('[data-download-filter]').forEach(item => item.classList.toggle('active', item === button));
  renderActivity();
}));
$$('[data-settings-page]').forEach(button => button.addEventListener('click', () => setSettingsPage(button.dataset.settingsPage)));
$('#preset-form').addEventListener('submit', async event => {
  event.preventDefault();
  const readPreset = prefix => ({
    format: FormatBuilder.value($(`#${prefix}-format`)),
    fileName: $(`#${prefix}-file-name`).value,
    archiveName: $(`#${prefix}-archive-name`).value,
    titleTag: $(`#${prefix}-title-tag`).value,
    artistTag: $(`#${prefix}-artist-tag`).value,
    maintainArchive: $(`#${prefix}-maintain-archive`).checked,
    syncWithSource: $(`#${prefix}-sync-source`).checked,
    breakOnExisting: $(`#${prefix}-break-existing`).checked,
  });
  const payload = {
    workingDirectory: $('#setting-working-dir').value,
    video: {...readPreset('video'), mergeFormat: $('#video-merge-format').value},
    audio: {...readPreset('audio'), codec: $('#audio-codec').value, embedThumbnail: $('#audio-embed-thumbnail').checked},
  };
  try {
    await api('/api/settings/config', {method:'POST', body: JSON.stringify(payload)});
    toast('Download presets saved and backed up');
    await loadSettings();
  } catch (error) { toast(error.message, 'error'); }
});
$('#schedule-form').addEventListener('submit', async event => {
  event.preventDefault();
  const schedules = $$('[data-schedule-id]').map(input => ({id: input.dataset.scheduleId, intervalMinutes: Number(input.value)}));
  try {
    await api('/api/settings/schedule', {method:'POST', body: JSON.stringify({schedules})});
    toast('Scheduler intervals saved and backed up');
    await Promise.all([loadSettings(), refresh()]);
  } catch (error) { toast(error.message, 'error'); }
});
$$('.file-list button').forEach(button => button.addEventListener('click', () => loadFile(button.dataset.file)));
$('#save-file').addEventListener('click', async () => {
  try {
    await api(`/api/file/${encodeURIComponent(appState.file)}`, {method:'POST', body: JSON.stringify({content: $('#file-editor').value})});
    toast(`${appState.file} saved and backed up`);
    appState.settings = null;
    await Promise.all([refresh(), loadSettings()]);
  } catch (error) { toast(error.message, 'error'); }
});
$('#file-editor').addEventListener('keydown', event => {
  if (event.key === 'Tab') {
    event.preventDefault();
    const start = event.target.selectionStart;
    event.target.value = `${event.target.value.slice(0,start)}  ${event.target.value.slice(event.target.selectionEnd)}`;
    event.target.selectionStart = event.target.selectionEnd = start + 2;
  }
});

addStaticHelp();

(async () => {
  try {
    const session = await api('/api/session');
    if (session.authenticated) showApp(); else showLogin();
  } catch (error) { showLogin(); $('#login-error').textContent = error.message; }
})();
