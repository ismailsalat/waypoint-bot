'use strict';
(() => {
  const $ = s => document.querySelector(s);
  let state = null;
  let timer;
  const e = (tag, text, cls) => { const n = document.createElement(tag); if (text != null) n.textContent = text; if (cls) n.className = cls; return n; };
  const numberFields = new Set(['server_cooldown_min','account_cooldown_min','random_offset_min','cooldown_min','start_offset_min','max_failures','retry_base_seconds','retry_max_seconds','refresh_seconds','default_server_cooldown_min','default_account_cooldown_min','default_random_offset_min']);
  async function api(path, method = 'GET', data) {
    const r = await fetch(path, {method, headers: data === undefined ? {} : {'Content-Type':'application/json'}, body: data === undefined ? undefined : JSON.stringify(data)});
    const value = await r.json();
    if (!r.ok) throw new Error(Array.isArray(value.detail) ? value.detail.map(x => `${x.loc.slice(1).join('.')}: ${x.msg}`).join('; ') : value.detail || `Request failed (${r.status})`);
    return value;
  }
  function error(message) { $('#scheduler-error').textContent = message; $('#scheduler-error').hidden = !message; }
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
      data[f.name] = f.type === 'checkbox' ? f.checked : numberFields.has(f.name) ? (f.value === '' ? null : Number(f.value)) : f.value;
    }
    return data;
  }
  function showAccount(account) {
    if (!state) return;
    const prefs = state.settings;
    fill($('#account-form'), account || {name:'', token_type:'user', enabled:true, auto_start:false,
      server_cooldown_min:prefs.default_server_cooldown_min, account_cooldown_min:prefs.default_account_cooldown_min,
      random_offset_min:prefs.default_random_offset_min, message:prefs.default_message});
    $('#account-heading').textContent = account ? `Edit ${account.name}` : 'Add account';
    $('#account-dialog').showModal();
  }
  function showTarget(account, server) {
    fill($('#target-form'), {...(server || {enabled:true,follow_managed_channel:false}), account_id:account.account_id});
    $('#known-server').value = '';
    $('#target-form').elements.channel_id.required=!$('#target-form').elements.follow_managed_channel.checked;
    $('#target-dialog').showModal();
  }
  function button(text, action, aid, sid) {
    const b = e('button', text); b.type = 'button'; b.dataset.action = action;
    if (aid) b.dataset.account = aid;
    if (sid) b.dataset.server = sid;
    return b;
  }
  function formatDate(value) { return value ? new Date(value).toLocaleString() : 'Not scheduled'; }
  function render() {
    const prefs = state.settings;
    $('#scheduler-mode').textContent = !prefs.enabled ? 'Scheduler disabled' : prefs.dry_run ? 'Simulation mode' : 'Live scheduler';
    const targets = state.accounts.flatMap(a => a.servers);
    const stats = $('#scheduler-summary'); stats.replaceChildren();
    for (const [label, count] of [['Accounts',state.accounts.length],['Targets',targets.length],['Running',state.accounts.filter(a=>a.is_running).length],['Confirmed',targets.reduce((n,s)=>n+s.total_ok,0)],['Unconfirmed',targets.reduce((n,s)=>n+s.total_sent,0)],['Simulated',targets.reduce((n,s)=>n+s.total_simulated,0)]]) {
      const box = e('div',null,'card'); box.append(e('strong',count),e('span',label,'muted')); stats.append(box);
    }
    const list = $('#account-list'); list.replaceChildren();
    if (!state.accounts.length) { const empty = e('div',null,'card'); empty.append(e('h2','Add your first account'),e('p','Create an account, choose its server targets, then enable the scheduler in simulation mode to check your configuration.')); list.append(empty); }
    for (const account of state.accounts) {
      const card = e('section',null,'card account-card');
      const heading = e('div',null,'suite-toolbar');
      heading.append(e('h2',account.name),e('span',`${account.token_type === 'bot' ? 'Bot messages' : 'User transport'} · ${account.status}${account.enabled ? '' : ' · disabled'}`, 'muted'));
      card.append(heading);
      card.append(e('p',`${account.credential_storage === 'os_keychain' ? 'Credential in OS keychain' : account.has_credential ? 'Credential for this session only; re-enter after restart' : 'No credential saved'} · ${account.server_cooldown_min} min per server · ${account.account_cooldown_min} min account gap${account.auto_start ? ' · auto-start' : ''}`, 'muted'));
      const controls = e('div',null,'suite-toolbar');
      for (const [label,action] of [['Start','start'],['Pause','pause'],['Resume','resume'],['Stop','stop'],['Edit','edit'],['Add server','add-target'],['Delete','delete']]) {
        const b = button(label,action,account.account_id);
        if (['edit','add-target','delete'].includes(action)) b.disabled = account.is_running;
        if (['start','resume'].includes(action)) b.disabled = !prefs.enabled || !account.enabled;
        controls.append(b);
      }
      card.append(controls);
      const tableWrap = e('div',null,'suite-table'); const table = e('table');
      const header = e('tr'); for (const t of ['Server / channel','Timing','Next run (local time)','Result','Controls']) header.append(e('th',t));
      const thead=e('thead'); thead.append(header);table.append(thead);const body=e('tbody');
      for (const server of account.servers) {
        const row=e('tr');const name=e('td');name.append(e('strong',server.name),e('div',`${server.guild_id} / ${server.effective_channel_id || server.channel_id}`,'muted'),e('div',server.enabled?'Enabled':'Disabled','muted'));
        row.append(name,e('td',`${server.cooldown_min ?? account.server_cooldown_min} min + 0–${server.random_offset_min ?? account.random_offset_min} min`),e('td',formatDate(server.next_run_at)));
        const result=e('td');result.append(e('span',server.setup_waiting ? 'Waiting for feeder setup' : server.last_result||'No runs yet'),e('div',`${server.total_ok} confirmed · ${server.total_sent} unconfirmed · ${server.total_fail} failed · ${server.total_simulated} simulated`,'muted'));
        if(server.last_error) result.append(e('div',server.last_error,'form-error'));row.append(result);
        const actions=e('td');for(const [label,action] of [['Edit','edit-target'],['Remove','delete-target']]) {const b=button(label,action,account.account_id,server.server_id);b.disabled=account.is_running;actions.append(b);}row.append(actions);body.append(row);
      }
      table.append(body);tableWrap.append(table);card.append(tableWrap);
      if(!account.servers.length) card.append(e('p','No servers assigned. Use Add server to select one from the bot or enter IDs.','muted'));
      if(account.is_running) card.append(e('p','Stop this account before editing. A request already in flight may finish first.','muted'));
      list.append(card);
    }
    const events=$('#scheduler-events');events.replaceChildren();
    for(const item of state.events.slice().reverse()) events.append(e('p',`${formatDate(item.time)} · ${item.level} · ${item.message}`));
  }
  async function refresh() {
    clearTimeout(timer);
    try {state=await api('/api/scheduler');render();error('');}catch(err){error(`Status unavailable: ${err.message}. Displayed data may be stale.`);}
    timer=setTimeout(refresh,(state?.settings.refresh_seconds||5)*1000);
  }
  async function action(path, method='POST') {try{await api(path,method);await refresh();}catch(err){error(err.message);}}
  $('#add-account').onclick=()=>showAccount();
  $('#start-all').onclick=()=>action('/api/scheduler/control/start-all');
  $('#stop-all').onclick=()=>action('/api/scheduler/control/stop-all');
  $('#open-settings').onclick=()=>{if(state){fill($('#scheduler-settings-form'),state.settings);$('#settings-dialog').showModal();}};
  document.querySelectorAll('[data-close]').forEach(b=>b.onclick=()=>b.closest('dialog').close());
  $('#known-server').onchange=event=>{const opt=event.target.selectedOptions[0];if(!opt.value)return;const form=$('#target-form');form.elements.name.value=opt.dataset.name;form.elements.guild_id.value=opt.value;form.elements.channel_id.value=opt.dataset.channel;form.elements.follow_managed_channel.checked=true;form.elements.channel_id.required=false;};
  $('#target-form').elements.follow_managed_channel.onchange=event=>{$('#target-form').elements.channel_id.required=!event.target.checked;};
  $('#account-list').onclick=async event=>{
    const b=event.target.closest('button[data-action]');if(!b||!state)return;
    const account=state.accounts.find(a=>a.account_id===b.dataset.account);if(!account)return;
    const server=account.servers.find(s=>s.server_id===b.dataset.server);
    const base=`/api/scheduler/accounts/${encodeURIComponent(account.account_id)}`;
    if(b.dataset.action==='edit')return showAccount(account);
    if(b.dataset.action==='add-target')return showTarget(account);
    if(b.dataset.action==='edit-target')return showTarget(account,server);
    if(b.dataset.action==='delete'){if(confirm(`Delete ${account.name} and its scheduler targets?`))await action(base,'DELETE');return;}
    if(b.dataset.action==='delete-target'){if(confirm(`Remove ${server.name} from this schedule?`))await action(`${base}/targets/${encodeURIComponent(server.server_id)}`,'DELETE');return;}
    await action(`${base}/actions/${b.dataset.action}`);
  };
  async function save(form, handler) {
    const submit=form.querySelector('[type="submit"]');submit.disabled=true;
    try{await handler(read(form));form.closest('dialog').close();await refresh();}catch(err){form.querySelector('.form-error').textContent=err.message;}finally{submit.disabled=false;}
  }
  $('#account-form').onsubmit=event=>{event.preventDefault();save(event.target,async data=>{const id=data.account_id;delete data.account_id;await api('/api/scheduler/accounts'+(id?`/${encodeURIComponent(id)}`:''),id?'PUT':'POST',data);event.target.elements.token.value='';});};
  $('#target-form').onsubmit=event=>{event.preventDefault();save(event.target,async data=>{const aid=data.account_id,sid=data.server_id;delete data.account_id;delete data.server_id;await api(`/api/scheduler/accounts/${encodeURIComponent(aid)}/targets`+(sid?`/${encodeURIComponent(sid)}`:''),sid?'PUT':'POST',data);});};
  $('#scheduler-settings-form').onsubmit=event=>{event.preventDefault();save(event.target,data=>api('/api/scheduler/settings','PUT',data));};
  $('#import-accounts').onclick=async()=>{try{const file=$('#import-file').files[0];if(!file)throw new Error('Choose a JSON file first.');if(file.size>1024*1024)throw new Error('File exceeds the 1 MB import limit.');const data=JSON.parse(await file.text());const result=await api('/api/scheduler/import/accounts','POST',{accounts:Array.isArray(data)?data:data.accounts});$('#import-result').textContent=`Imported ${result.imported}. ${result.note}`;await refresh();}catch(err){$('#import-result').textContent=err.message;}};
  refresh();
})();
