function root() { return document.getElementById('root').value.trim(); }
function channel() { return document.getElementById('channel').value; }
function log(text) {
  const el = document.getElementById('log');
  el.textContent += text + "\n";
  el.scrollTop = el.scrollHeight;
}
function clearLog() { document.getElementById('log').textContent = ''; }

async function api(path, opts) {
  const resp = await fetch(path, opts);
  const data = await resp.json();
  if (!resp.ok) throw new Error(data.error || resp.statusText);
  return data;
}

let lastScan = null;

async function scanRoot() {
  clearLog();
  try {
    lastScan = await api(`/api/scan?root=${encodeURIComponent(root())}`);
    renderScan(lastScan);
    log('Скан завершён (без обращения к сети).');
  } catch (e) {
    log('Ошибка: ' + e.message);
  }
}

async function checkUpdates() {
  clearLog();
  log('Проверяю GitHub (' + channel() + ')...');
  try {
    lastScan = await api(`/api/check-updates?root=${encodeURIComponent(root())}&channel=${channel()}`);
    renderScan(lastScan);
    log('Готово.');
  } catch (e) {
    log('Ошибка: ' + e.message);
  }
}

function renderScan(data) {
  document.getElementById('components-section').hidden = false;
  document.getElementById('opencore-section').hidden = false;
  document.getElementById('drivers-section').hidden = false;

  const tbody = document.querySelector('#components-table tbody');
  tbody.innerHTML = '';
  for (const c of data.components) {
    const localVersions = c.kexts.map(k => `${k.bundle}: ${k.local_version || '—'}`).join('<br>');
    const latest = c.latest_version || (c.error ? 'ошибка' : '?');
    let statusClass = 'status-unknown', statusText = '?';
    if (c.error) { statusClass = 'status-unknown'; statusText = c.error; }
    else if ('outdated' in c) { statusClass = c.outdated ? 'status-outdated' : 'status-ok';
      statusText = c.outdated ? 'обновление есть' : 'актуально'; }
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td><input type="checkbox" class="comp-check" value="${c.name}"></td>
      <td>${c.name}</td>
      <td>${localVersions}</td>
      <td>${latest}</td>
      <td class="${statusClass}">${statusText}</td>
      <td><a href="${c.release_url || '#'}" target="_blank">${c.release_url ? 'релиз' : ''}</a></td>`;
    tbody.appendChild(tr);
  }

  const oc = data.opencore;
  const ocInfo = document.getElementById('opencore-info');
  if (!oc.present) {
    ocInfo.textContent = 'OpenCore.efi не найден по этому пути.';
  } else {
    const lines = [
      `Последняя версия, применённая этим инструментом: ${oc.last_known_version || 'неизвестно (ещё не обновляли через этот инструмент)'}`,
      oc.changed_since_last_update === false ? 'Файл не менялся с тех пор.' :
        (oc.last_known_version ? 'Файл изменился с последнего known-апдейта (обновили чем-то другим или вручную).' : ''),
      oc.latest_version ? `Актуальная версия (${channel()}): ${oc.latest_version}` : (oc.error || ''),
    ].filter(Boolean);
    ocInfo.innerHTML = lines.join('<br>');
  }

  const dtbody = document.querySelector('#drivers-table tbody');
  dtbody.innerHTML = '';
  for (const d of data.drivers) {
    const tr = document.createElement('tr');
    tr.innerHTML = `<td>${d.file}</td><td>${d.last_known_version || '—'}</td>
      <td>${d.changed_since_last_update ? 'да' : 'нет'}</td>`;
    dtbody.appendChild(tr);
  }
}

async function updateSelectedKexts() {
  const checked = [...document.querySelectorAll('.comp-check:checked')].map(c => c.value);
  if (!checked.length) { log('Ничего не выбрано.'); return; }
  for (const name of checked) {
    log(`Обновляю ${name}...`);
    try {
      const res = await api('/api/update-kext', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ component: name, root: root(), channel: channel() }),
      });
      res.log.forEach(log);
    } catch (e) {
      log(`[${name}] ошибка: ${e.message}`);
    }
  }
  scanRoot();
}

async function updateOpenCore() {
  const parts = [];
  if (document.getElementById('oc-part-efi').checked) parts.push('efi');
  if (document.getElementById('oc-part-drivers').checked) parts.push('drivers');
  if (document.getElementById('oc-part-resources').checked) parts.push('resources');
  if (!parts.length) { log('Ничего не выбрано.'); return; }
  if (!confirm(`Применить к OpenCorePkg (${parts.join(', ')})? Это затрагивает загрузчик напрямую.`)) return;
  try {
    const res = await api('/api/update-opencore', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ root: root(), channel: channel(), parts }),
    });
    res.log.forEach(log);
  } catch (e) {
    log('Ошибка: ' + e.message);
  }
  scanRoot();
}

async function applyTheme() {
  const repo = document.getElementById('theme-repo').value.trim();
  const ref = document.getElementById('theme-ref').value.trim();
  if (!repo) { log('Укажите репозиторий темы.'); return; }
  if (!confirm(`Заменить Resources/ содержимым из ${repo}?`)) return;
  try {
    const res = await api('/api/apply-theme', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ repo, root: root(), ref: ref || undefined }),
    });
    res.log.forEach(log);
  } catch (e) {
    log('Ошибка: ' + e.message);
  }
}

async function previewMigration() {
  try {
    const res = await api('/api/migrate-config', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ root: root(), channel: channel() }),
    });
    renderMigrationReport(res.report);
  } catch (e) {
    log('Ошибка миграции: ' + e.message);
  }
}

async function saveMigration() {
  if (!confirm('Сохранить результат как config.migrated.plist рядом с текущим config.plist? ' +
    'Текущий config.plist не будет тронут.')) return;
  try {
    const res = await api('/api/migrate-config/save', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ root: root(), channel: channel() }),
    });
    renderMigrationReport(res.report);
    log(`Сохранено: ${res.saved_to}`);
  } catch (e) {
    log('Ошибка миграции: ' + e.message);
  }
}

function renderMigrationReport(report) {
  const lines = [];
  lines.push(`Целевая версия OpenCore: ${report.target_version} (${report.channel})`);
  lines.push('');
  lines.push(`=== Несовместимость типов (${report.type_mismatch.length}) — использован новый дефолт, проверьте руками ===`);
  report.type_mismatch.forEach(t => lines.push(`  ${t.path}: было ${t.old_type}, стало ${t.new_type}`));
  lines.push('');
  lines.push(`=== Убрано из новой схемы (${report.removed_in_new.length}) ===`);
  report.removed_in_new.forEach(p => lines.push('  ' + p));
  lines.push('');
  lines.push(`=== Новые ключи, оставлены по дефолту новой схемы (${report.kept_new_default.length}) ===`);
  report.kept_new_default.forEach(p => lines.push('  ' + p));
  document.getElementById('migration-report').textContent = lines.join('\n');
}
