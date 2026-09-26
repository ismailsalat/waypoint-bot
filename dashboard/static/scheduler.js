 'use strict';
(() => {
  const $ = s => document.querySelector(s);
  let state = null;
  let timer;
  let clockTimer;

  const e = (tag, text, cls) => {
    const n = document.createElement(tag);
    if (text != null) n.textContent = text;
    if (cls) n.className = cls;
    return n;
  };

  const numberFields = new Set([
    'server_cooldown_min','account_cooldown_min','random_offset_min','cooldown_min',
    'start_offset_min','max_failures','retry_base_seconds','retry_max_seconds',
    'refresh_seconds','default_server_cooldown_min','default_account_cooldown_min',
    'default_random_offset_min'
  ]);

  async function api(path, method = 'GET', data) {
    const r = await fetch(path, {
      method,
      headers: data === undefined ? {} : {'Content-Type':'application/json'},
      body: data === undefined ? undefined : JSON.stringify(data)
    });
    const value = await r.json();
    if (!r.ok) {
      throw new Error(
        Array.isArray(value.detail)
          ? value.detail.map(x => `${x.loc.slice(1).join('.')}: ${x.msg}`).join('; ')
          : value.detail || `Request failed (${r.status})`
      );
    }
    return value;
  }

  function error(message) {
    $('#scheduler-error').textContent = message;
    $('#scheduler-error').hidden = !message;
  }

  function fill(form, values) {
    form.reset();
    form.querySelector('.form-error').textContent = '';
    for (const field of form.elements) {
      if (!field.name) continue;
      if (field.type === 'checkbox') field.checked = Boolean(values[field.name]);
      else field.value = values[field.name] ?? '';
    }
  }

  function read(form) {
    const data = {};
    for (const f of form.elements) {
      if (!f.name) continue;
      data[f.name] = f.type === 'checkbox'
        ? f.checked
        : numberFields.has(f.name)
          ? (f.value === '' ? null : Number(f.value))
          : f.value;
    }
    return data;
  }

  function button(text, action, aid, sid, cls = '') {
    const b = e('button', text, cls);
    b.type = 'button';
    b.dataset.action = action;
    if (aid) b.dataset.account = aid;
    if (sid) b.dataset.server = sid;
    return b;
  }

  function formatExact(value) {
    return value ? new Date(value).toLocaleString() : 'Not scheduled';
  }

  function duration(ms) {
    if (ms <= 0) return 'Ready now';
    const total = Math.ceil(ms / 1000);
    const d = Math.floor(total / 86400);
    const h = Math.floor((total % 86400) / 3600);
    const m = Math.floor((total % 3600) / 60);
    const s = total % 60;
    if (d) return `in ${d}d ${h}h`;
    if (h) return `in ${h}h ${m}m`;
    if (m) return `in ${m}m ${s}s`;
    return `in ${s}s`;
  }

  function countdown(value) {
    if (!value) return 'Ready now';
    return duration(new Date(value).getTime() - Date.now());
  }

  function resultMeta(server) {
    if (server.setup_waiting) return ['Waiting for feeder setup', 'result-waiting'];
    switch (server.last_result) {
      case 'confirmed': return ['Confirmed', 'result-confirmed'];
      case 'sent_unconfirmed': return ['Sent · unverified', 'result-unconfirmed'];
      case 'simulated': return ['Simulation only', 'result-simulated'];
      case 'simulated_shared_cooldown': return ['Simulation cooldown', 'result-simulated'];
      case 'waiting_shared_cooldown': return ['Shared cooldown', 'result-waiting'];
      case 'waiting_for_setup': return ['Waiting for setup', 'result-waiting'];
      case 'in_progress': return ['Sending…', 'result-waiting'];
      case 'failed': return ['Failed', 'result-failed'];
      case 'cancelled_before_send': return ['Cancelled before send', 'result-neutral'];
      default: return ['No runs yet', 'result-neutral'];
    }
  }

  function accountStatusMeta(account) {
    if (!account.enabled) return ['Disabled', 'status-stopped'];
    if (account.status === 'error') return ['Error', 'status-error'];
    if (account.status === 'paused') return ['Paused', 'status-paused'];
    if (account.status === 'running') return ['Sending', 'status-running'];
    if (account.status === 'waiting_for_setup') return ['Waiting for setup', 'status-waiting'];
    if (account.is_running || account.status === 'waiting') return ['Running · waiting', 'status-waiting'];
    if (account.status === 'stopping') return ['Stopping…', 'status-paused'];
    return ['Stopped', 'status-stopped'];
  }

  function effectiveTime(account, server) {
    const now = Date.now();
    let when = server.next_run_at ? new Date(server.next_run_at).getTime() : now;
    if (account.last_action_at) {
      const gap = Number(account.account_cooldown_min || 0) * 60000;
      when = Math.max(when, new Date(account.last_action_at).getTime() + gap);
    }
    return when;
  }

  function nextTarget(account) {
    const candidates = account.servers
      .filter(s => s.enabled && !s.setup_waiting && (s.effective_channel_id || s.channel_id))
      .map((s, index) => ({server:s, index, when:effectiveTime(account, s)}));
    if (!candidates.length) return null;
    candidates.sort((a,b) => a.when - b.when || a.index - b.index);
    return candidates[0];
  }

  function showAccount(account) {
    if (!state) return;
    const prefs = state.settings;
    fill($('#account-form'), account || {
      name:'', token_type:'user', enabled:true, auto_start:false,
      server_cooldown_min:prefs.default_server_cooldown_min,
      account_cooldown_min:prefs.default_account_cooldown_min,
      random_offset_min:prefs.default_random_offset_min,
      message:prefs.default_message
    });
    $('#account-heading').textContent = account ? `Edit ${account.name}` : 'Add account';
    $('#account-dialog').showModal();
  }

  function showTarget(account, server) {
    fill($('#target-form'), {
      ...(server || {enabled:true,follow_managed_channel:false}),
      account_id:account.account_id
    });
    $('#known-server').value = '';
    $('#target-form').elements.channel_id.required =
      !$('#target-form').elements.follow_managed_channel.checked;
    $('#target-dialog').showModal();
  }

  function statCard(label, count, cls = '') {
    const box = e('div', null, `card stat-card ${cls}`.trim());
    box.append(e('strong', count), e('span', label, 'muted'));
    return box;
  }

  function renderSummary() {
    const targets = state.accounts.flatMap(a => a.servers);
    const stats = $('#scheduler-summary');
    stats.replaceChildren(
      statCard('Accounts', state.accounts.length),
      statCard('Targets', targets.length),
      statCard('Running', state.accounts.filter(a => a.is_running).length, 'stat-green'),
      statCard('Confirmed', targets.reduce((n,s) => n + s.total_ok, 0), 'stat-green'),
      statCard('Sent · unverified', targets.reduce((n,s) => n + s.total_sent, 0), 'stat-amber'),
      statCard('Failed', targets.reduce((n,s) => n + s.total_fail, 0), 'stat-red'),
      statCard('Simulation history', targets.reduce((n,s) => n + s.total_simulated, 0), 'stat-purple')
    );
  }

  function renderAccount(account) {
    const prefs = state.settings;
    const card = e('section', null, 'card scheduler-account');
    const top = e('div', null, 'account-top');

    const main = e('div');
    const title = e('div', null, 'account-title-row');
    const [statusText, statusClass] = accountStatusMeta(account);
    title.append(
      e('h2', account.name),
      e('span', statusText, `status-chip ${statusClass}`),
      e('span', account.token_type === 'bot' ? 'Official bot' : 'User transport', 'mini-chip')
    );
    main.append(title);

    const route = e('div', null, 'account-route');
    route.append(e('span', account.name, 'route-account'));
    for (const server of account.servers) {
      route.append(e('span', '→', 'route-arrow'));
      route.append(e('span', server.name, 'route-server'));
    }
    if (!account.servers.length) {
      route.append(e('span', '→ no targets', 'muted'));
    }
    main.append(route);
    top.append(main);

    const next = e('div', null, 'account-next');
    const candidate = nextTarget(account);
    next.append(e('div', 'NEXT TARGET', 'next-label'));
    if (candidate) {
      next.append(
        e('div', candidate.server.name, 'next-server'),
        e('div', duration(candidate.when - Date.now()), 'next-time live-countdown')
      );
      next.lastChild.dataset.time = new Date(candidate.when).toISOString();
    } else {
      next.append(e('div', 'No eligible target', 'next-server'));
    }
    top.append(next);
    card.append(top);

    const info = e('div', null, 'account-info');
    const infoRows = [
      ['Credential', account.credential_storage === 'os_keychain'
        ? 'OS keychain'
        : account.has_credential ? 'Session only' : 'Missing'],
      ['Same server', `${account.server_cooldown_min} min`],
      ['Between targets', `${account.account_cooldown_min} min`],
      ['Auto-start', account.auto_start ? 'On' : 'Off']
    ];
    for (const [label, value] of infoRows) {
      const cell = e('div', null, 'info-cell');
      cell.append(e('span', label), e('strong', value));
      info.append(cell);
    }
    card.append(info);

    const controls = e('div', null, 'account-controls');
    const left = e('div', null, 'left');
    const right = e('div', null, 'right');

    const start = button('▶ Start', 'start', account.account_id, null, 'control-start');
    start.disabled = account.is_running || !prefs.enabled || !account.enabled;
    const pause = button('Ⅱ Pause', 'pause', account.account_id, null, 'control-pause');
    pause.disabled = !account.is_running || account.status === 'paused';
    const resume = button('▶ Resume', 'resume', account.account_id, null, 'control-start');
    resume.disabled = !account.is_running || account.status !== 'paused';
    const stop = button('■ Stop', 'stop', account.account_id, null, 'control-stop');
    stop.disabled = !account.is_running;
    left.append(start, pause, resume, stop);

    for (const [label, action, cls] of [
      ['Edit account','edit',''],
      ['+ Add server','add-target',''],
      ['Reset all timers','reset-account-schedule','control-reset'],
      ['Clear all stats','reset-account-stats','control-reset'],
      ['Delete account','delete','danger-soft']
    ]) {
      const b = button(label, action, account.account_id, null, cls);
      b.disabled = account.is_running;
      right.append(b);
    }
    controls.append(left, right);
    card.append(controls);

    const list = e('div', null, 'target-list');
    if (!account.servers.length) {
      list.append(e('div', 'No servers assigned. Use “+ Add server” to connect a target to this account.', 'empty-targets'));
    } else {
      account.servers.forEach((server, index) => {
        const row = e('div', null, `target-row${server.enabled ? '' : ' target-disabled'}`);

        const mainCell = e('div', null, 'target-main');
        const line = e('div', null, 'target-name-line');
        line.append(
          e('span', server.name, 'target-name'),
          e('span', server.enabled ? `Target ${index + 1}` : 'Disabled', 'mini-chip')
        );
        mainCell.append(
          line,
          e('div', `Account: ${account.name}`, 'target-owner'),
          e('div', `${server.guild_id} / ${server.effective_channel_id || server.channel_id || 'no channel'}`, 'target-id')
        );

        const timing = e('div', null, 'target-timing');
        const cooldown = server.cooldown_min ?? account.server_cooldown_min;
        const jitter = server.random_offset_min ?? account.random_offset_min;
        timing.append(
          e('div', `${cooldown} min`, 'big'),
          e('div', `same server · + 0–${jitter} min random`, 'small'),
          e('div', `${account.account_cooldown_min} min account gap`, 'small')
        );

        const nextCell = e('div', null, 'target-next');
        const nextBig = e('div', countdown(server.next_run_at), 'big countdown live-countdown');
        nextBig.dataset.time = server.next_run_at || '';
        nextCell.append(nextBig, e('div', formatExact(server.next_run_at), 'small'));

        const result = e('div', null, 'target-result');
        const [resultText, resultClass] = resultMeta(server);
        result.append(
          e('span', resultText, `result-chip ${resultClass}`),
          e('div',
            `${server.total_ok} confirmed · ${server.total_sent} unverified · ${server.total_fail} failed · ${server.total_simulated} simulated`,
            'counts')
        );
        if (server.last_error) result.append(e('div', server.last_error, 'error-text'));

        const actions = e('div', null, 'target-actions');
        for (const [label, action, cls] of [
          ['Edit','edit-target',''],
          ['Reset timer','reset-target-schedule','control-reset'],
          ['Clear stats','reset-target-stats','control-reset'],
          ['Remove','delete-target','danger-soft']
        ]) {
          const b = button(label, action, account.account_id, server.server_id, cls);
          b.disabled = account.is_running;
          actions.append(b);
        }

        row.append(mainCell, timing, nextCell, result, actions);
        list.append(row);
      });
    }
    card.append(list);

    if (account.is_running) {
      card.append(e(
        'div',
        'Stop this account before editing, resetting timers/stats, or removing targets. A request already in flight may finish first.',
        'running-warning'
      ));
    }

    return card;
  }

  function renderEvents() {
    const events = $('#scheduler-events');
    events.replaceChildren();
    const rows = state.events.slice().reverse();
    if (!rows.length) {
      events.append(e('p', 'No scheduler events yet.', 'muted'));
      return;
    }
    for (const item of rows) {
      const row = e('div', null, 'event-line');
      const when = item.time ? new Date(item.time).toLocaleString() : '';
      row.append(
        e('span', when, 'event-time'),
        e('span', item.level, `event-level ${item.level}`),
        e('span', item.message)
      );
      events.append(row);
    }
  }

  function renderMode() {
    const prefs = state.settings;
    const chip = $('#scheduler-mode');
    chip.className = 'mode-chip';
    if (!prefs.enabled) {
      chip.textContent = '● Scheduler disabled';
      chip.classList.add('mode-off');
    } else if (prefs.dry_run) {
      chip.textContent = '● Simulation mode';
      chip.classList.add('mode-sim');
    } else {
      chip.textContent = '● LIVE scheduler';
      chip.classList.add('mode-live');
    }
  }

  function updateCountdowns() {
    document.querySelectorAll('.live-countdown').forEach(node => {
      const value = node.dataset.time;
      node.textContent = value ? countdown(value) : 'Ready now';
    });
  }

  function render() {
    renderMode();
    renderSummary();
    const list = $('#account-list');
    list.replaceChildren();
    if (!state.accounts.length) {
      const empty = e('div', null, 'card');
      empty.append(
        e('h2', 'Add your first account'),
        e('p', 'Create an account, assign its server targets, then start it when you are ready.')
      );
      list.append(empty);
    } else {
      for (const account of state.accounts) list.append(renderAccount(account));
    }
    renderEvents();
    updateCountdowns();
  }

  async function refresh() {
    clearTimeout(timer);
    try {
      state = await api('/api/scheduler');
      render();
      error('');
    } catch (err) {
      error(`Status unavailable: ${err.message}. Displayed data may be stale.`);
    }
    timer = setTimeout(refresh, (state?.settings.refresh_seconds || 5) * 1000);
  }

  async function action(path, method = 'POST') {
    try {
      await api(path, method);
      await refresh();
    } catch (err) {
      error(err.message);
    }
  }

  $('#add-account').onclick = () => showAccount();
  $('#start-all').onclick = () => action('/api/scheduler/control/start-all');
  $('#stop-all').onclick = () => action('/api/scheduler/control/stop-all');
  $('#clear-events').onclick = async () => {
    if (confirm('Clear the scheduler event log shown on this page?')) {
      await action('/api/scheduler/events', 'DELETE');
    }
  };

  $('#open-settings').onclick = () => {
    if (state) {
      fill($('#scheduler-settings-form'), state.settings);
      $('#settings-dialog').showModal();
    }
  };

  document.querySelectorAll('[data-close]').forEach(
    b => b.onclick = () => b.closest('dialog').close()
  );

  $('#known-server').onchange = event => {
    const opt = event.target.selectedOptions[0];
    if (!opt.value) return;
    const form = $('#target-form');
    form.elements.name.value = opt.dataset.name;
    form.elements.guild_id.value = opt.value;
    form.elements.channel_id.value = opt.dataset.channel;
    form.elements.follow_managed_channel.checked = true;
    form.elements.channel_id.required = false;
  };

  $('#target-form').elements.follow_managed_channel.onchange = event => {
    $('#target-form').elements.channel_id.required = !event.target.checked;
  };

  $('#account-list').onclick = async event => {
    const b = event.target.closest('button[data-action]');
    if (!b || !state) return;
    const account = state.accounts.find(a => a.account_id === b.dataset.account);
    if (!account) return;
    const server = account.servers.find(s => s.server_id === b.dataset.server);
    const base = `/api/scheduler/accounts/${encodeURIComponent(account.account_id)}`;
    const kind = b.dataset.action;

    if (kind === 'edit') return showAccount(account);
    if (kind === 'add-target') return showTarget(account);
    if (kind === 'edit-target') return showTarget(account, server);

    if (kind === 'delete') {
      if (confirm(`Delete ${account.name} and all of its scheduler targets?`)) {
        await action(base, 'DELETE');
      }
      return;
    }
    if (kind === 'delete-target') {
      if (confirm(`Remove ${server.name} from ${account.name}?`)) {
        await action(`${base}/targets/${encodeURIComponent(server.server_id)}`, 'DELETE');
      }
      return;
    }
    if (kind === 'reset-account-schedule') {
      if (confirm(`Reset every timer for ${account.name}? Its targets become eligible again, subject to shared reservations.`)) {
        await action(`${base}/reset-schedule`);
      }
      return;
    }
    if (kind === 'reset-account-stats') {
      if (confirm(`Clear all scheduler counters/results for ${account.name}? Timers are not changed.`)) {
        await action(`${base}/reset-stats`);
      }
      return;
    }
    if (kind === 'reset-target-schedule') {
      if (confirm(`Reset ${server.name}'s timer? This also clears ${account.name}'s account-gap timer so scheduling can continue immediately.`)) {
        await action(`${base}/targets/${encodeURIComponent(server.server_id)}/reset-schedule`);
      }
      return;
    }
    if (kind === 'reset-target-stats') {
      if (confirm(`Clear ${server.name}'s scheduler counters/results? Its timer is not changed.`)) {
        await action(`${base}/targets/${encodeURIComponent(server.server_id)}/reset-stats`);
      }
      return;
    }

    await action(`${base}/actions/${kind}`);
  };

  async function save(form, handler) {
    const submit = form.querySelector('[type="submit"]');
    submit.disabled = true;
    try {
      await handler(read(form));
      form.closest('dialog').close();
      await refresh();
    } catch (err) {
      form.querySelector('.form-error').textContent = err.message;
    } finally {
      submit.disabled = false;
    }
  }

  $('#account-form').onsubmit = event => {
    event.preventDefault();
    save(event.target, async data => {
      const id = data.account_id;
      delete data.account_id;
      await api(
        '/api/scheduler/accounts' + (id ? `/${encodeURIComponent(id)}` : ''),
        id ? 'PUT' : 'POST',
        data
      );
      event.target.elements.token.value = '';
    });
  };

  $('#target-form').onsubmit = event => {
    event.preventDefault();
    save(event.target, async data => {
      const aid = data.account_id;
      const sid = data.server_id;
      delete data.account_id;
      delete data.server_id;
      await api(
        `/api/scheduler/accounts/${encodeURIComponent(aid)}/targets` +
          (sid ? `/${encodeURIComponent(sid)}` : ''),
        sid ? 'PUT' : 'POST',
        data
      );
    });
  };

  $('#scheduler-settings-form').onsubmit = event => {
    event.preventDefault();
    save(event.target, data => api('/api/scheduler/settings', 'PUT', data));
  };

  $('#import-accounts').onclick = async () => {
    try {
      const file = $('#import-file').files[0];
      if (!file) throw new Error('Choose a JSON file first.');
      if (file.size > 1024 * 1024) throw new Error('File exceeds the 1 MB import limit.');
      const data = JSON.parse(await file.text());
      const result = await api('/api/scheduler/import/accounts', 'POST', {
        accounts:Array.isArray(data) ? data : data.accounts
      });
      $('#import-result').textContent = `Imported ${result.imported}. ${result.note}`;
      await refresh();
    } catch (err) {
      $('#import-result').textContent = err.message;
    }
  };

  clockTimer = setInterval(updateCountdowns, 1000);
  refresh();
})();
