/* Guided controls for the yt-dlp format expressions used by this dashboard. */
const FormatBuilder = (() => {
  const videoPattern = /^bestvideo(?:\[vcodec\^=(avc1|vp9|av01)\])?(?:\[height<=(720|1080|1440|2160)\])?\+bestaudio(?:\[ext=(m4a|webm)\])?(?:\/best(?:\[ext=(mp4|webm)\])?(?:\[height<=(720|1080|1440|2160)\])?)?$/;
  const audioPattern = /^bestaudio(?:\[ext=(m4a|webm|mp3)\])?(\/bestaudio)?$/;

  function parseVideo(value) {
    const match = videoPattern.exec(value);
    if (!match) return null;
    const [, codec = '', height = '', audio = '', container = '', fallbackHeight = ''] = match;
    const fallback = value.includes('/best');
    if (fallback && height !== fallbackHeight) return null;
    return {codec, height, audio, fallback, container};
  }

  function buildVideo({codec = '', height = '', audio = '', fallback = false, container = ''}) {
    let result = `bestvideo${codec ? `[vcodec^=${codec}]` : ''}${height ? `[height<=${height}]` : ''}+bestaudio${audio ? `[ext=${audio}]` : ''}`;
    if (fallback) result += `/best${container ? `[ext=${container}]` : ''}${height ? `[height<=${height}]` : ''}`;
    return result;
  }

  function parseAudio(value) {
    const match = audioPattern.exec(value);
    return match ? {audio: match[1] || '', fallback: Boolean(match[2])} : null;
  }

  function buildAudio({audio = '', fallback = false}) {
    return `bestaudio${audio ? `[ext=${audio}]` : ''}${fallback ? '/bestaudio' : ''}`;
  }

  function option(value, label, selected) {
    return `<option value="${value}"${value === selected ? ' selected' : ''}>${label}</option>`;
  }

  function field(name, label, choices, selected) {
    return `<label>${label}<select data-format-field="${name}">${choices.map(([value, text]) => option(value, text, selected)).join('')}</select></label>`;
  }

  function mount(host, mode, value = '', allowInherit = false) {
    const parsed = (mode === 'audio' ? parseAudio : parseVideo)(value);
    const choice = !value && allowInherit ? 'inherit' : parsed ? 'guided' : 'custom';
    const current = parsed || (mode === 'audio' ? {audio: 'm4a', fallback: true} : {codec: 'avc1', height: '1080', audio: 'm4a', fallback: true, container: 'mp4'});
    const fields = mode === 'video' ? `
      ${field('codec', 'Video codec', [['','Any'],['avc1','H.264 / AVC'],['vp9','VP9'],['av01','AV1']], current.codec)}
      ${field('height', 'Maximum resolution', [['','Any'],['720','720p'],['1080','1080p'],['1440','1440p'],['2160','2160p']], current.height)}
      ${field('audio', 'Preferred audio format', [['','Any'],['m4a','M4A'],['webm','WebM']], current.audio)}
      ${field('container', 'Fallback container', [['','Any'],['mp4','MP4'],['webm','WebM']], current.container)}
    ` : field('audio', 'Preferred audio format', [['','Any'],['m4a','M4A'],['webm','WebM'],['mp3','MP3']], current.audio);
    host.dataset.formatMode = mode;
    host.innerHTML = `
      <label>Format selection<select data-format-choice>
        ${allowInherit ? option('inherit', `Use ${mode} preset`, choice) : ''}
        ${option('guided', 'Choose options', choice)}
        ${option('custom', 'Custom yt-dlp format', choice)}
      </select></label>
      <div class="format-guided"><div class="format-grid">${fields}</div>
        <label class="format-check"><input type="checkbox" data-format-fallback${current.fallback ? ' checked' : ''}> ${mode === 'video' ? 'Try best combined video if preferred formats are unavailable' : 'Try any best audio if preferred format is unavailable'}</label>
        <p class="format-note">${mode === 'video' ? 'The resolution limit also applies to the fallback.' : 'The fallback keeps downloads working when the preferred format is unavailable.'}</p>
      </div>
      <label class="format-custom">yt-dlp format expression<input type="text" data-format-raw></label>
      <div class="format-preview">${allowInherit ? `<span class="format-inherited">Uses ${mode} preset</span>` : ''}<code></code></div>`;
    host.querySelector('[data-format-raw]').value = value;
    const update = () => {
      const selected = host.querySelector('[data-format-choice]').value;
      host.querySelector('.format-guided').classList.toggle('hidden', selected !== 'guided');
      host.querySelector('.format-custom').classList.toggle('hidden', selected !== 'custom');
      host.querySelector('.format-preview').classList.toggle('hidden', selected === 'inherit');
      host.querySelector('.format-preview code').textContent = selected === 'custom' ? host.querySelector('[data-format-raw]').value : selected === 'guided' ? readGuided(host) : '';
      const fallbackContainer = host.querySelector('[data-format-field="container"]');
      if (fallbackContainer) fallbackContainer.disabled = !host.querySelector('[data-format-fallback]').checked;
    };
    host.oninput = update;
    host.onchange = update;
    update();
  }

  function readGuided(host) {
    const get = name => host.querySelector(`[data-format-field="${name}"]`)?.value || '';
    const fallback = host.querySelector('[data-format-fallback]').checked;
    return host.dataset.formatMode === 'video'
      ? buildVideo({codec: get('codec'), height: get('height'), audio: get('audio'), fallback, container: get('container')})
      : buildAudio({audio: get('audio'), fallback});
  }

  function value(host) {
    const choice = host.querySelector('[data-format-choice]').value;
    return choice === 'inherit' ? '' : choice === 'custom' ? host.querySelector('[data-format-raw]').value.trim() : readGuided(host);
  }

  return {mount, value, parseVideo, buildVideo, parseAudio, buildAudio};
})();
if (typeof module !== 'undefined') module.exports = FormatBuilder;
