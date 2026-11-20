(() => {
  'use strict';
  const form = $('#providerForm');
  const list = $('#providerList');
  const add = $('#addProviderBtn');
  const refresh = $('#refreshProvidersBtn');
  const message = $('#providerError');
  const fields = form.elements;
  const protocols = {chat_completions: 'Chat Completions', anthropic_messages: 'Anthropic Messages', codex_responses: 'Responses'};
  let editing = null;
  let busy = false;
  let ready = false;

  function element(tag, text, css) {
    const node = document.createElement(tag);
    node.textContent = text;
    if (css) node.className = css;
    return node;
  }

  function button(text, action) {
    const node = element('button', text, 'sm');
    node.type = 'button';
    node.onclick = action;
    return node;
  }

  function showForm(row = null) {
    if (busy) return;
    editing = row?.id ?? null;
    form.reset();
    fields.id.readOnly = editing !== null;
    if (row) {
      for (const key of ['id', 'name', 'url', 'transport', 'defaultModel', 'keyEnv']) fields.namedItem(key).value = row[key];
      fields.models.value = row.models.join('\n');
    }
    message.hidden = true;
    form.hidden = false;
    (row ? fields.namedItem('name') : fields.id).focus();
  }

  function providerRow(row) {
    const card = element('div', '', 'provider-row');
    card.append(element('strong', row.name), element('div', `${row.provider} · ${protocols[row.transport]}`, 'file'),
      element('div', row.url, 'file'), element('div', `默认：${row.defaultModel} · 共 ${row.entry.models.length} 个模型`, 'hint'));
    if (row.keyEnv) card.append(element('div', `密钥变量：${row.keyEnv}`, 'file'));
    const actions = element('div', '', 'bar');
    actions.append(button('复制配置命令', () => copy(row.command, '配置命令已复制')),
      button('编辑', () => showForm(row)), button('移除预设', () => remove(row)));
    const details = element('details', '', 'hint');
    details.append(element('summary', '查看配置命令'), element('pre', row.command));
    card.append(actions, details);
    return card;
  }

  async function loadProviders(select = null) {
    ready = false;
    add.disabled = refresh.disabled = true;
    try {
      const {providers} = await api('/api/providers');
      const rows = providers.map(providerRow);
      list.replaceChildren(...(rows.length ? rows : [element('p', '尚未添加其他提供商。', 'hint')]));
      if (select !== null) {
        const detail = rows[providers.findIndex(row => row.id === select)]?.querySelector('details');
        if (detail) detail.open = true;
      }
      ready = true;
    } catch (error) {
      list.replaceChildren(element('p', `无法读取提供商：${error.message}`, 'hint'));
    } finally {
      add.disabled = !ready || busy;
      refresh.disabled = busy;
    }
  }

  function setBusy(next) {
    busy = next;
    add.disabled = !ready || busy;
    refresh.disabled = next;
    $('#saveProviderBtn').disabled = next;
    $('#cancelProviderBtn').disabled = next;
  }

  async function remove(row) {
    if (busy || !confirm(`移除「${row.name}」的网页预设？这不会移除已导入 Hermes 的配置。`)) return;
    setBusy(true);
    try {
      await post('/api/providers/remove', {id: row.id});
      if (editing === row.id) form.hidden = true;
      await loadProviders();
      toast('网页预设已移除');
    } catch (error) {
      toast(error.message, true);
    } finally {
      setBusy(false);
    }
  }

  form.onsubmit = async event => {
    event.preventDefault();
    if (busy || !ready) return;
    const data = Object.fromEntries(new FormData(form));
    data.models = data.models.split(/\r?\n/).map(s => s.trim()).filter(Boolean);
    setBusy(true);
    message.hidden = true;
    try {
      const {provider} = await post(editing === null ? '/api/providers/add' : '/api/providers/edit', data);
      form.hidden = true;
      await loadProviders(provider.id);
      toast('预设已保存，复制配置命令接入 Hermes');
    } catch (error) {
      message.textContent = error.message;
      message.hidden = false;
    } finally {
      setBusy(false);
    }
  };

  add.onclick = () => showForm();
  refresh.onclick = () => loadProviders();
  $('#cancelProviderBtn').onclick = () => { form.hidden = true; add.focus(); };
  loadProviders();
})();
