/* 招商工作台前端逻辑 */
'use strict';

const $ = (id) => document.getElementById(id);

const state = {
  config: null,
  cardId: null,
  card: null,
  panel: 'card',
  spaces: [],
  fields: {},
  stateLabels: {},
  purposes: {},
  manualChecks: [],
  report: null,
  cardsPage: 1,
  cardsPageSize: 10,
  cardsTotal: 0,
  lastMatch: null,
};

const STATUS_TEXT = {
  draft: '草稿',
  pending: '待交接',
  handover: '已交接',
  returned: '已退回',
  escalated: '已升级',
};

const STATE_TEXT = {
  specified: '已明确',
  unknown: '未知待补',
  not_requested: '未提出要求',
  none_confirmed: '确认无此要求',
};

const STATE_ORDER = ['specified', 'unknown', 'not_requested', 'none_confirmed'];

const REVIEW_ACTIONS = {
  accept: '接受',
  change_level: '修改等级',
  change_explanation: '修改解释',
  reject: '驳回',
  escalate: '升级',
};

// ---------------------------------------------------------------------------
// 基础设施
// ---------------------------------------------------------------------------

function role() { return $('actorRole').value; }
function actor() { return $('actorName').value.trim(); }

function headers() {
  const h = { 'Content-Type': 'application/json' };
  if (role()) h['X-Actor-Role'] = role();
  if (actor()) h['X-Actor'] = actor();
  return h;
}

async function api(path, options) {
  const opts = Object.assign({}, options || {});
  opts.headers = Object.assign(headers(), opts.headers || {});
  const res = await fetch(path, opts);
  let data = null;
  try { data = await res.json(); } catch (e) { data = null; }
  if (!res.ok) throw new Error((data && data.error) || ('请求失败（' + res.status + '）'));
  return data;
}

function showMessage(text, ok) {
  const box = $('message');
  box.textContent = text || '';
  box.hidden = !text;
  box.className = ok ? 'message ok' : 'message';
}

async function withBusy(button, fn) {
  const label = button ? button.textContent : '';
  if (button) { button.disabled = true; button.textContent = '处理中…'; }
  try {
    await fn();
  } catch (err) {
    showMessage(err.message || String(err));
  } finally {
    if (button) { button.disabled = false; button.textContent = label; }
  }
}

function el(tag, text, parent) {
  const node = document.createElement(tag);
  if (text !== undefined && text !== null) node.textContent = String(text);
  if (parent) parent.appendChild(node);
  return node;
}

function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }

function fmtTime(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  if (isNaN(d)) return iso;
  const p = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

// ---------------------------------------------------------------------------
// 侧边栏导航与当前需求卡上下文
// ---------------------------------------------------------------------------

const PANEL_IDS = ['card', 'extract', 'fields', 'handover', 'spaces', 'match', 'audit'];

function showPanel(name) {
  if (PANEL_IDS.indexOf(name) < 0) name = 'card';
  state.panel = name;
  document.querySelectorAll('.panel').forEach((p) => {
    p.classList.toggle('active', p.id === 'panel-' + name);
  });
  document.querySelectorAll('.nav-item').forEach((btn) => {
    btn.classList.toggle('active', btn.dataset.panel === name);
  });
  window.scrollTo(0, 0);
}

function renderCtx() {
  const bar = $('ctxBar');
  clear(bar);
  if (!state.card) {
    el('span', '未选择需求卡：新建或从列表载入后，其余环节共用这张卡。', bar).className = 'muted';
    const go = el('button', '前往需求卡', bar);
    go.className = 'ghost ctx-go';
    go.onclick = () => showPanel('card');
    return;
  }
  const status = STATUS_TEXT[state.card.status] || state.card.status;
  el('span', `当前需求卡：${state.card.title || '（未命名）'} · ${state.card.customer || '客户未填'} · ${status} · v${state.card.version}`, bar).className = 'ctx-main';
  const go = el('button', '返回需求卡', bar);
  go.className = 'ghost ctx-go';
  go.onclick = () => showPanel('card');
}

// ---------------------------------------------------------------------------
// 启动
// ---------------------------------------------------------------------------

async function boot() {
  try {
    const config = await api('/api/config');
    state.config = config;
    state.fields = config.fields || {};
    state.stateLabels = config.state_labels || STATE_TEXT;
    state.purposes = config.purposes || {};
    state.manualChecks = config.manual_checks || [];
    $('mode').textContent = config.ai_enabled
      ? '已启用 AI 抽取：访谈将发送至已配置的模型服务（发送前自动脱敏）'
      : '本地规则演示模式 · 可配置模型启用 AI 抽取';
    fillPurposes();
    fillReturnReasons();
    renderCtx();
    initAssistant();
    await Promise.all([reloadCards(), reloadSpaces(), reloadAudit()]);
  } catch (err) {
    showMessage(err.message || String(err));
  }
}

function fillPurposes() {
  const select = $('cardPurpose');
  const current = select.value;
  clear(select);
  const placeholder = document.createElement('option');
  placeholder.value = '';
  placeholder.textContent = '请选择用途';
  select.appendChild(placeholder);
  Object.keys(state.purposes).forEach((key) => {
    const option = document.createElement('option');
    option.value = key;
    option.textContent = key;
    select.appendChild(option);
  });
  select.value = current;
}

function fillReturnReasons() {
  const select = $('reviewReasonCode');
  clear(select);
  const reasons = (state.config && state.config.return_reasons) || {};
  Object.keys(reasons).forEach((code) => {
    const option = el('option', `${code} · ${reasons[code]}`, select);
    option.value = code;
  });
}

// ---------------------------------------------------------------------------
// 需求卡
// ---------------------------------------------------------------------------

async function reloadCards() {
  const params = new URLSearchParams();
  params.set('page', String(state.cardsPage));
  params.set('page_size', String(state.cardsPageSize));
  const q = $('cardSearch').value.trim();
  const status = $('cardStatusFilter').value;
  if (q) params.set('q', q);
  if (status) params.set('status', status);
  const data = await api('/api/cards?' + params.toString());
  const tbody = $('cardList').querySelector('tbody');
  clear(tbody);
  state.cardsTotal = data.total !== undefined ? data.total : data.cards.length;
  if (!data.cards.length) {
    const tr = el('tr', null, tbody);
    const td = el('td', '没有符合条件的需求卡。', tr);
    td.colSpan = 6;
  }
  data.cards.forEach((card) => {
    const tr = el('tr', null, tbody);
    tr.className = 'pick' + (card.id === state.cardId ? ' current' : '');
    el('td', card.title || '（未命名）', tr);
    el('td', card.customer || '—', tr);
    el('td', card.purpose || '未选择', tr);
    el('td', STATUS_TEXT[card.status] || card.status, tr);
    el('td', 'v' + card.version, tr);
    el('td', fmtTime(card.updated_at), tr);
    tr.onclick = () => loadCard(card.id);
  });
  renderCardPager();
}

function renderCardPager() {
  const total = state.cardsTotal;
  const pages = Math.max(1, Math.ceil(total / state.cardsPageSize));
  if (state.cardsPage > pages) state.cardsPage = pages;
  $('cardPageInfo').textContent = total
    ? `共 ${total} 张 · 第 ${state.cardsPage} / ${pages} 页`
    : '';
  $('cardPrevPage').disabled = state.cardsPage <= 1;
  $('cardNextPage').disabled = state.cardsPage >= pages;
}

async function loadCard(cardId) {
  const card = await api('/api/card?card_id=' + encodeURIComponent(cardId));
  state.cardId = card.id;
  state.card = card;
  $('cardTitle').value = card.title || '';
  $('cardCustomer').value = card.customer || '';
  $('cardPurpose').value = card.purpose || '';
  $('cardStatus').textContent = STATUS_TEXT[card.status] || card.status;
  $('source').value = card.source || '';
  state.report = null;
  state.lastMatch = null;
  renderCtx();
  $('export').disabled = true;
  $('exportMd').disabled = false;
  renderFields(card);
  renderValidation(card.validation);
  renderReviews(card.review_history || []);
  await Promise.all([reloadCards(), loadQuotes()]);
  showMessage('已载入需求卡：' + (card.title || card.id), true);
}

function currentItems() {
  const items = {};
  Object.keys(state.fields).forEach((key) => {
    const card = document.querySelector(`[data-field="${key}"]`);
    if (!card) return;
    const stateSelect = card.querySelector('.state-select');
    const valueInput = card.querySelector('.value-input');
    const evidenceInput = card.querySelector('.evidence-input');
    const stateValue = stateSelect ? stateSelect.value : 'unknown';
    let value = null;
    if (stateValue === 'specified' && valueInput && valueInput.value !== '') {
      const raw = valueInput.value;
      value = state.fields[key].kind === 'enum' ? raw : Number(raw);
    }
    items[key] = {
      field: key,
      state: stateValue,
      value: value,
      unit: state.fields[key].unit,
      evidence: evidenceInput ? evidenceInput.value.trim() : '',
      origin: 'manual',
    };
  });
  return items;
}

function renderFields(card) {
  const container = $('fields');
  clear(container);
  const items = (card && card.items) || {};
  const purpose = (card && card.purpose) || $('cardPurpose').value;
  const rules = state.purposes[purpose] || {};
  const required = rules.required || [];
  const conditional = rules.conditional || {};
  const professional = rules.professional || [];
  const confirmations = (card && card.confirmations) || [];
  const confirmedFields = new Set(confirmations.filter((c) => c.valid).map((c) => c.field));
  const adjustments = (card && card.level_adjustments) || {};

  Object.keys(state.fields).forEach((key) => {
    const spec = state.fields[key];
    const item = items[key] || { state: 'unknown', value: null, evidence: '' };
    const isRequired = required.indexOf(key) >= 0;
    const isConditional = Object.prototype.hasOwnProperty.call(conditional, key);
    const isProfessional = professional.indexOf(key) >= 0;

    const wrap = el('div', null, container);
    wrap.className = 'field-card';
    wrap.dataset.field = key;

    const head = el('div', null, wrap);
    head.className = 'field-head';
    const strong = el('strong', spec.label + (spec.unit ? `（${spec.unit}）` : ''), head);
    const badges = el('span', null, head);
    if (isRequired) el('span', '必填', badges).className = 'badge-req must';
    else if (isConditional) el('span', '条件必填', badges).className = 'badge-req';
    if (isProfessional) el('span', '需专业确认', badges).className = 'badge-req pro';
    if (adjustments[key]) el('span', '等级已调整为 ' + adjustments[key], badges).className = 'badge-req';

    if (spec.note) el('p', spec.note, wrap).className = 'field-note';

    const actions = el('div', null, wrap);
    actions.className = 'field-actions';
    const select = el('select', null, actions);
    select.className = 'state-select';
    select.setAttribute('aria-label', spec.label + ' 取值状态');
    STATE_ORDER.forEach((value) => {
      const option = el('option', STATE_TEXT[value] || value, select);
      option.value = value;
    });
    select.value = item.state || 'unknown';
    select.onchange = () => onStateChange(key, select.value, valueInput);

    const valueInput = el('input', null, actions);
    valueInput.className = 'value-input';
    valueInput.type = 'number';
    valueInput.step = 'any';
    valueInput.min = '0.000001';
    valueInput.placeholder = spec.unit ? '数值' : '枚举值';
    if (spec.kind === 'enum') {
      valueInput.type = 'text';
      valueInput.setAttribute('list', 'enum-' + key);
      const datalist = el('datalist', null, wrap);
      datalist.id = 'enum-' + key;
      (spec.choices || []).forEach((choice) => el('option', choice, datalist));
    }
    if (item.value !== null && item.value !== undefined) valueInput.value = item.value;
    valueInput.disabled = select.value !== 'specified';

    const evidenceInput = el('input', null, wrap);
    evidenceInput.className = 'evidence-input';
    evidenceInput.type = 'text';
    evidenceInput.placeholder = '原文证据（必填，可从访谈摘录）';
    evidenceInput.value = item.evidence || '';

    if (isProfessional) {
      const info = el('p', null, wrap);
      info.className = 'field-note';
      info.textContent = confirmedFields.has(key)
        ? '已由专业角色确认，可解除阻断'
        : '待专业确认：需工程或复核角色确认后方可交接';
    }
  });
}

function onStateChange(key, value, valueInput) {
  if (valueInput) valueInput.disabled = value !== 'specified';
}

function fieldSummary() {
  const items = currentItems();
  const counts = { specified: 0, unknown: 0, not_requested: 0, none_confirmed: 0 };
  Object.values(items).forEach((item) => { counts[item.state] = (counts[item.state] || 0) + 1; });
  return `已明确 ${counts.specified} · 未知待补 ${counts.unknown} · 未提出要求 ${counts.not_requested} · 确认无此要求 ${counts.none_confirmed}`;
}

// ---------------------------------------------------------------------------
// 交接校验
// ---------------------------------------------------------------------------

function renderValidation(validation) {
  const box = $('validation');
  clear(box);
  if (!validation || !validation.issues) {
    el('p', '尚未校验。提交交接前建议先跑一次校验。', box).className = 'muted';
    return;
  }
  const verdict = el('div', null, box);
  verdict.className = 'verdict ' + (validation.passed ? 'pass' : 'fail');
  verdict.textContent = validation.passed
    ? `校验通过：${validation.advisory_count} 项提示待留痕，0 项阻断`
    : `校验未通过：${validation.blocking_count} 项阻断必须处理，${validation.advisory_count} 项提示`;

  if (validation.issues.length) {
    validation.issues.forEach((issue) => {
      const div = el('div', null, box);
      div.className = 'issue ' + issue.level;
      el('strong', issue.message, div);
      if (issue.question) el('div', '补问：' + issue.question, div).className = 'q';
    });
  } else {
    el('p', '未发现阻断项。', box).className = 'muted';
  }

  if (validation.questions && validation.questions.length) {
    el('h3', '按用途需补充确认', box);
    const ul = el('ul', null, box);
    validation.questions.forEach((q) => el('li', q, ul));
  }
}

function renderReviews(history) {
  const box = $('reviewHistory');
  clear(box);
  if (!history || !history.length) {
    el('p', '暂无复核记录。', box).className = 'muted';
    return;
  }
  const wrap = el('div', null, box);
  wrap.className = 'timeline';
  history.forEach((item) => {
    const div = el('div', null, wrap);
    div.className = 'timeline-item';
    el('div', `${item.action_label} · ${item.actor || '未署名'}（${item.actor_role || '—'}） · 引用 v${item.ref_version} · ${fmtTime(item.created_at)}`, div).className = 'meta';
    if (item.reason_code) {
      el('div', `退回原因：${item.reason_code}${item.reason_code_label ? '（' + item.reason_code_label + '）' : ''}`, div).className = 'body';
    }
    if (item.reason) el('div', '理由：' + item.reason, div).className = 'body';
    if (item.resolution) el('div', '处理：' + item.resolution, div).className = 'body';
  });
}

// ---------------------------------------------------------------------------
// 房源台账
// ---------------------------------------------------------------------------

async function reloadSpaces() {
  const data = await api('/api/spaces');
  state.spaces = data.spaces || [];
  renderSpaces();
}

function renderSpaces() {
  const box = $('inventory');
  clear(box);
  const complete = state.spaces.filter((s) => s.trace_complete).length;
  box.textContent = state.spaces.length
    ? `当前台账 ${state.spaces.length} 个房源，其中 ${complete} 个溯源信息完整。溯源不完整的房源不会被判为"满足"。`
    : '当前台账为空，请导入房源 JSON。';

  const wrap = $('spaceTableWrap');
  clear(wrap);
  if (!state.spaces.length) return;
  const table = el('table', null, wrap);
  table.className = 'datatable';
  const head = el('thead', null, table);
  const headRow = el('tr', null, head);
  ['房源', '参数', '溯源（来源/核实人/日期/版本）', '状态'].forEach((t) => el('th', t, headRow));
  const body = el('tbody', null, table);
  state.spaces.forEach((space) => {
    const tr = el('tr', null, body);
    el('td', space.name, tr);
    const params = Object.keys(space.values || {})
      .filter((k) => space.values[k] !== null && space.values[k] !== undefined)
      .map((k) => `${state.fields[k] ? state.fields[k].label : k} ${space.values[k]}${state.fields[k] ? state.fields[k].unit : ''}`)
      .join('；');
    el('td', params || '（无参数）', tr);
    const trace = space.trace || {};
    el('td', `${trace.source || '缺'} / ${trace.verifier || '缺'} / ${trace.verified_at || '缺'} / ${trace.version || '缺'}`, tr);
    el('td', space.trace_complete ? '溯源完整' : '待核实', tr);
  });
}

// ---------------------------------------------------------------------------
// 匹配
// ---------------------------------------------------------------------------

function renderResults(result) {
  const box = $('results');
  clear(box);
  if (result.missing_requirements && result.missing_requirements.length) {
    el('p', '未参与匹配的字段：' + result.missing_requirements.join('、'), box);
  }
  if (result.manual_checks && result.manual_checks.length) {
    el('h3', '交接前待人工核实', box);
    const ul = el('ul', null, box);
    result.manual_checks.forEach((c) => el('li', c, ul));
  }
  (result.results || []).forEach((row) => {
    const card = el('div', null, box);
    card.className = 'result-card ' + (
      row.status === '已填条件满足' ? 'pass'
        : row.status === '不匹配' ? 'fail' : 'unverified');
    const head = el('div', null, card);
    head.className = 'result-head';
    el('h3', row.name, head);
    el('span', row.status, head).className = 'tag-' + (
      row.status === '已填条件满足' ? 'ok' : row.status === '不匹配' ? 'bad' : 'warn');
    if (row.status === '已填条件满足' || row.status === '条件满足但溯源不全') {
      const quoteBtn = el('button', '生成报价单', head);
      quoteBtn.className = 'ghost quote-btn';
      quoteBtn.onclick = () => generateQuote(row.name);
    }
    row.checks.forEach((check) => {
      const line = el('div', null, card);
      line.className = 'checkline';
      const actual = check.actual === null || check.actual === undefined ? '未知' : check.actual;
      el('span', `${check.label || check.field}：需求 ${check.required}${check.unit || ''} / 房源 ${actual}${check.actual === null ? '' : (check.unit || '')}`, line);
      el('span', check.status, line).className = 'tag-' + (
        check.status === '满足' ? 'ok' : check.status === '不满足' ? 'bad' : 'warn');
      if (check.basis) el('span', check.basis, line).className = 'trace';
    });
    if (row.trace && !row.trace_complete) {
      el('p', '溯源信息不完整，该结论仅为待核实素材，不可作为交付依据。', card).className = 'trace';
    }
  });
}

// ---------------------------------------------------------------------------
// 报价单
// ---------------------------------------------------------------------------

const QUOTE_STATUS_TEXT = { draft: '草稿', issued: '已发出', voided: '已作废' };

async function loadQuotes() {
  const box = $('quoteList');
  if (!state.cardId) {
    clear(box);
    el('p', '载入需求卡后查看报价单。', box).className = 'muted';
    return;
  }
  const data = await api('/api/quotes?card_id=' + encodeURIComponent(state.cardId));
  renderQuotes(data.quotes || []);
}

function renderQuotes(quotes) {
  const box = $('quoteList');
  clear(box);
  if (!quotes.length) {
    el('p', '暂无报价单。匹配通过后可在上方结果卡中生成。', box).className = 'muted';
    return;
  }
  const fmtMoney = (value) => (value === null || value === undefined ? '—' : value);
  quotes.forEach((quote) => {
    const terms = quote.terms || {};
    const card = el('div', null, box);
    card.className = 'quote-card' + (quote.status === 'voided' ? ' voided' : '');
    const head = el('div', null, card);
    head.className = 'result-head';
    el('h3', `${quote.quote_no || quote.id} ${quote.space_name}`, head);
    el('span', `${QUOTE_STATUS_TEXT[quote.status] || quote.status} · v${quote.quote_version || 1} · ${quote.round || (quote.quote_version > 1 ? '修订报价' : '首次报价')}`, head).className = 'tag-' + (
      quote.status === 'issued' ? 'ok' : quote.status === 'voided' ? 'bad' : 'warn');
    const lines = el('div', null, card);
    lines.className = 'quote-terms';
    el('div', `月单价 ${terms.unit_price}${terms.unit_label || ''}（台账价 ${terms.ledger_rent ?? '—'}，折扣 ${terms.discount_pct || 0}%） × 计租面积 ${fmtMoney(terms.rentable_area)}㎡（登记 ${terms.area}${terms.area_label || ''} × 得房率 ${terms.efficiency_ratio}%） = ${terms.monthly_total} 元/月`, lines);
    el('div', `物业费 ${fmtMoney(terms.property_fee)} ${terms.property_fee_unit || ''} · 年递增 ${terms.escalation_pct || 0}% · 押金 ${terms.deposit_months} 个月 · 免租 ${terms.free_rent_days} 天 · 租期 ${terms.lease_term_months} 个月`, lines);
    el('div', `三年总占用成本 ${fmtMoney(terms.total_3y)} 元 = 三年租金 ${fmtMoney(terms.rent_3y)} + 三年物业费 ${fmtMoney(terms.property_3y)} + 装修投入 ${fmtMoney(terms.renovation_input)} − 补贴 ${fmtMoney(terms.subsidies_total)}（行业参照，需园区确认）`, lines);
    el('div', `有效期 ${terms.validity_days} 天（至 ${fmtTime(quote.valid_until)}） · 租期收入估算 ${fmtMoney(terms.total_revenue)} 元`, lines);
    if (terms.requires_approval) {
      const authLine = el('div', quote.approved_by
        ? `让利超授权，已由 ${quote.approved_by} 审批（${fmtTime(quote.approved_at)}）`
        : '让利超授权（免租>92天或折扣>5%），尚未授权审批，不能发出', lines);
      authLine.className = quote.approved_by ? 'hint' : 'field-note';
    }
    (terms.assumptions || []).forEach((a) => el('div', '口径：' + a, lines).className = 'trace');
    if (terms.note) el('div', '备注：' + terms.note, lines);
    const meta = el('div', `匹配结论：${quote.match_conclusion} · 生成：${quote.actor || '未署名'} · ${fmtTime(quote.created_at)}`, card);
    meta.className = 'trace';
    const actions = el('div', null, card);
    actions.className = 'row';
    if (quote.status === 'draft' && terms.requires_approval && !quote.approved_by) {
      const approveBtn = el('button', '管理员授权审批', actions);
      approveBtn.className = 'ghost';
      approveBtn.onclick = () => approveQuote(quote.id);
    }
    if (quote.status === 'draft') {
      const issueBtn = el('button', '标记为已发出', actions);
      issueBtn.className = 'ghost';
      issueBtn.onclick = () => changeQuoteStatus(quote.id, 'issue');
    }
    if (quote.status !== 'voided') {
      const voidBtn = el('button', '作废', actions);
      voidBtn.className = 'ghost';
      voidBtn.onclick = () => changeQuoteStatus(quote.id, 'void');
    }
  });
}

async function generateQuote(spaceName) {
  if (!state.cardId) throw new Error('请先选择需求卡');
  const unitPrice = window.prompt(`为「${spaceName}」生成报价单。月单价（元/㎡/月），留空使用台账登记值：`);
  if (unitPrice === null) return;
  const overrides = {};
  if (unitPrice.trim() !== '') overrides.unit_price = Number(unitPrice.trim());
  const note = window.prompt('报价备注（可留空）：');
  if (note === null) return;
  if (note.trim() !== '') overrides.note = note.trim();
  const revision = window.prompt('修订原因（若同一房源已报过价，此次为修订报价，必填；首次报价可留空）：');
  if (revision === null) return;
  const payload = {
    card_id: state.cardId,
    space_name: spaceName,
    overrides: overrides,
    actor_role: role(),
    actor: actor(),
  };
  if (revision.trim() !== '') payload.revision_reason = revision.trim();
  const data = await api('/api/quotes', {
    method: 'POST',
    body: JSON.stringify(payload),
  });
  await Promise.all([loadQuotes(), reloadAudit()]);
  showMessage(`报价单已生成：${data.quote.quote_no || data.quote.id}（v${data.quote.quote_version} · ${data.quote.round}）`, true);
}

async function approveQuote(quoteId) {
  const data = await api('/api/quote/approve', {
    method: 'POST',
    body: JSON.stringify({
      quote_id: quoteId,
      actor_role: role(),
      actor: actor(),
    }),
  });
  await Promise.all([loadQuotes(), reloadAudit()]);
  showMessage(`报价单 ${data.quote.quote_no || quoteId} 已完成让利授权审批，可发出。`, true);
}

async function changeQuoteStatus(quoteId, action) {
  const data = await api('/api/quote/status', {
    method: 'POST',
    body: JSON.stringify({
      quote_id: quoteId,
      action: action,
      actor_role: role(),
      actor: actor(),
    }),
  });
  await Promise.all([loadQuotes(), reloadAudit()]);
  showMessage(`报价单已${action === 'issue' ? '标记为已发出' : '作废'}。`, true);
}

// ---------------------------------------------------------------------------
// 审计与指标
// ---------------------------------------------------------------------------

async function reloadAudit() {
  const query = state.cardId ? '?card_id=' + encodeURIComponent(state.cardId) : '';
  const data = await api('/api/audit' + query);
  const wrap = $('auditTableWrap');
  clear(wrap);
  if (data.chain_valid === false) {
    const warn = el('p', '审计 hash 链校验失败：留痕可能被篡改，请立即核查！', wrap);
    warn.className = 'field-note';
  } else if (data.audit && data.audit.length) {
    const ok = el('p', `审计 hash 链校验：通过（共 ${data.audit.length} 条留痕，防篡改）。`, wrap);
    ok.className = 'hint';
  }
  if (!data.audit.length) {
    el('p', '暂无留痕记录。', wrap).className = 'muted';
    return;
  }
  const table = el('table', null, wrap);
  table.className = 'datatable';
  const head = el('thead', null, table);
  const headRow = el('tr', null, head);
  ['时间', '操作', '操作者', '角色', '引用版本', '明细'].forEach((t) => el('th', t, headRow));
  const body = el('tbody', null, table);
  data.audit.forEach((row) => {
    const tr = el('tr', null, body);
    el('td', fmtTime(row.created_at), tr);
    el('td', row.action, tr);
    el('td', row.actor || '—', tr);
    el('td', row.actor_role || '—', tr);
    el('td', 'v' + row.ref_version, tr);
    const detail = row.detail || {};
    const parts = Object.keys(detail).map((k) => `${k}=${typeof detail[k] === 'object' ? JSON.stringify(detail[k]) : detail[k]}`);
    el('td', parts.join(' · '), tr);
  });
}

function barRow(label, value, max) {
  const row = el('div', null, null);
  row.className = 'bar-row';
  const name = el('span', label, row);
  name.className = 'bar-label';
  const track = el('span', null, row);
  track.className = 'bar-track';
  const fill = el('span', null, track);
  fill.className = 'bar-fill';
  const pct = max > 0 ? Math.round((value / max) * 100) : 0;
  fill.style.width = pct + '%';
  const num = el('span', String(value), row);
  num.className = 'bar-value';
  return row;
}

async function showMetrics() {
  const data = await api('/api/metrics');
  const box = $('metricsBox');
  clear(box);

  const statusNames = { draft: '草稿', pending: '待交接', handover: '已交接', returned: '已退回', escalated: '已升级' };
  const byStatus = data.cards_by_status || {};
  const maxStatus = Math.max(1, ...Object.values(byStatus).map(Number));
  el('h3', '需求卡状态分布', box);
  const statusBars = el('div', null, box);
  statusBars.className = 'bars';
  Object.keys(statusNames).forEach((key) => {
    statusBars.appendChild(barRow(statusNames[key], byStatus[key] || 0, maxStatus));
  });

  const total = data.spaces_total || 0;
  const complete = data.spaces_trace_complete || 0;
  el('h3', '台账溯源完整率', box);
  const traceBars = el('div', null, box);
  traceBars.className = 'bars';
  traceBars.appendChild(barRow('溯源完整', complete, Math.max(1, total)));
  traceBars.appendChild(barRow('待核实', Math.max(0, total - complete), Math.max(1, total)));
  if (total) {
    el('p', `溯源完整 ${complete} / ${total}（${Math.round((complete / total) * 100)}%）。溯源不完整的房源不会被判为"满足"。`, box).className = 'hint';
  }

  const grid = el('div', null, box);
  grid.className = 'kv';
  const entries = [
    ['需求卡总数', data.cards_total],
    ['存在阻断的需求卡', data.cards_with_blocking],
    ['复核动作数', data.reviews_total],
    ['驳回数', data.rejects_total],
    ['升级数', data.escalations_total],
    ['专业确认数', data.confirmations_total],
    ['报价单总数', data.quotes_total],
    ['其中已发出', data.quotes_issued],
    ['台账房源数', data.spaces_total],
  ];
  entries.forEach(([label, value]) => {
    const div = el('div', null, grid);
    el('b', label, div);
    el('span', value, div);
  });
  el('p', data.caveat, box).className = 'hint';
}

// ---------------------------------------------------------------------------
// 事件绑定
// ---------------------------------------------------------------------------

$('createCard').onclick = () => withBusy($('createCard'), async () => {
  const title = $('cardTitle').value.trim();
  if (!title) throw new Error('请填写需求卡标题');
  const card = await api('/api/cards', {
    method: 'POST',
    body: JSON.stringify({
      title: title,
      customer: $('cardCustomer').value.trim(),
      purpose: $('cardPurpose').value,
      source: $('source').value,
      actor_role: role(),
      actor: actor(),
    }),
  });
  await loadCard(card.id);
  showMessage('需求卡已创建：' + card.id, true);
});

$('saveCard').onclick = () => withBusy($('saveCard'), async () => {
  if (!state.cardId) throw new Error('请先选择需求卡');
  const card = await api('/api/card', {
    method: 'PUT',
    body: JSON.stringify({
      card_id: state.cardId,
      title: $('cardTitle').value.trim(),
      customer: $('cardCustomer').value.trim(),
      purpose: $('cardPurpose').value,
      actor_role: role(),
      actor: actor(),
    }),
  });
  state.card = card;
  $('cardStatus').textContent = STATUS_TEXT[card.status] || card.status;
  renderFields(card);
  renderCtx();
  await reloadCards();
  showMessage('已保存。', true);
});

$('reloadCard').onclick = () => withBusy($('reloadCard'), async () => {
  if (!state.cardId) throw new Error('请先选择需求卡');
  await loadCard(state.cardId);
});

$('saveFields').onclick = () => withBusy($('saveFields'), async () => {
  if (!state.cardId) throw new Error('请先选择需求卡');
  const card = await api('/api/card', {
    method: 'PUT',
    body: JSON.stringify({
      card_id: state.cardId,
      items: Object.values(currentItems()),
      actor_role: role(),
      actor: actor(),
    }),
  });
  state.card = card;
  renderFields(card);
  $('fieldSummary').textContent = fieldSummary();
  await reloadCards();
  showMessage('字段已保存。', true);
});

$('validateNow').onclick = () => withBusy($('validateNow'), async () => {
  if (!state.cardId) throw new Error('请先选择需求卡');
  const data = await api('/api/card/validate', {
    method: 'POST',
    body: JSON.stringify({ card_id: state.cardId }),
  });
  renderValidation(data.validation);
  showMessage(data.summary, data.validation.passed);
});

$('submitHandover').onclick = () => withBusy($('submitHandover'), async () => {
  if (!state.cardId) throw new Error('请先选择需求卡');
  const data = await api('/api/card/submit', {
    method: 'POST',
    body: JSON.stringify({ card_id: state.cardId, actor_role: role(), actor: actor() }),
  });
  renderValidation(data.validation);
  if (data.card) {
    state.card = data.card;
    $('cardStatus').textContent = STATUS_TEXT[data.card.status] || data.card.status;
    renderFields(data.card);
    renderCtx();
  }
  await Promise.all([reloadCards(), reloadAudit()]);
  showMessage(data.summary, data.submitted);
});

$('completeHandover').onclick = () => withBusy($('completeHandover'), async () => {
  if (!state.cardId) throw new Error('请先选择需求卡');
  const data = await api('/api/card/submit', {
    method: 'POST',
    body: JSON.stringify({ card_id: state.cardId, complete: true, actor_role: role(), actor: actor() }),
  });
  renderValidation(data.validation);
  if (data.card) {
    state.card = data.card;
    $('cardStatus').textContent = STATUS_TEXT[data.card.status] || data.card.status;
    renderCtx();
  }
  await Promise.all([reloadCards(), reloadAudit()]);
  showMessage(data.summary, data.submitted);
});

$('doReview').onclick = () => withBusy($('doReview'), async () => {
  if (!state.cardId) throw new Error('请先选择需求卡');
  const action = $('reviewAction').value;
  const field = $('reviewField').value;
  const reason = $('reviewReason').value.trim();
  const extra = $('reviewExtra').value.trim();
  const detail = {};
  if (action === 'change_level') {
    detail.field = field;
    detail.level = extra || 'advisory';
  } else if (action === 'change_explanation') {
    detail.field = field;
    detail.explanation = extra;
  } else if (action === 'reject') {
    detail.reason_code = $('reviewReasonCode').value;
    detail.resolution = reason;
  } else if (action === 'escalate') {
    detail.escalate_to = extra || '上级专业角色';
  }
  const data = await api('/api/card/review', {
    method: 'POST',
    body: JSON.stringify({
      card_id: state.cardId,
      action: action,
      reason: reason,
      detail: detail,
      actor_role: role(),
      actor: actor(),
    }),
  });
  $('reviewReason').value = '';
  $('reviewExtra').value = '';
  await loadCard(state.cardId);
  showMessage(`复核动作「${data.action_label}」已记录，状态：${STATUS_TEXT[data.status] || data.status}`, true);
});

$('extract').onclick = () => withBusy($('extract'), async () => {
  const text = $('source').value;
  if (!text.trim()) throw new Error('请填写访谈内容');
  const data = await api('/api/extract', {
    method: 'POST',
    body: JSON.stringify({ text: text }),
  });
  if (text !== $('source').value) throw new Error('原文已变更，本次提取结果已丢弃，请重新提取。');
  $('extractHint').textContent = data.mode + (data.redacted ? '（本次内容含敏感信息，已脱敏后送模型）' : '');
  if (!state.cardId) {
    showMessage('抽取完成，请先新建需求卡以保存这些字段。', true);
    showPanel('card');
    return;
  }
  const card = await api('/api/card', {
    method: 'PUT',
    body: JSON.stringify({
      card_id: state.cardId,
      items: Object.values(data.items),
      actor_role: role(),
      actor: actor(),
    }),
  });
  state.card = card;
  renderFields(card);
  $('fieldSummary').textContent = fieldSummary();
  await reloadCards();
  showMessage('抽取完成，请逐项裁定字段状态。', true);
  showPanel('fields');
});

$('sample').onclick = () => {
  $('source').value = '客户计划用于设备研发。面积至少600平方米，租金不超过50元/㎡/月，供电容量不低于160kVA，承重不低于500kg/㎡，净高不低于4米。层高5米。希望两个月内入驻，需要确认消防条件。客户明确表示不需要特殊地面加固。';
  $('extractHint').textContent = '';
};

$('upload').onchange = (event) => withBusy(null, async () => {
  const file = event.target.files[0];
  if (!file) return;
  if (file.size > 1500000) throw new Error('文件不能超过 1.5MB');
  const parsed = JSON.parse(await file.text());
  const list = Array.isArray(parsed) ? parsed : parsed.spaces;
  if (!Array.isArray(list) || !list.length) throw new Error('请导入非空房源数组');
  const data = await api('/api/spaces', {
    method: 'POST',
    body: JSON.stringify({ spaces: list, actor_role: role(), actor: actor() }),
  });
  state.spaces = data.spaces || [];
  renderSpaces();
  await reloadAudit();
  showMessage(`已导入 ${data.count} 个房源。` + (data.issues && data.issues.length ? '\n' + data.issues.join('\n') : ''), true);
});

$('reloadSpaces').onclick = () => withBusy($('reloadSpaces'), reloadSpaces);
$('reloadAudit').onclick = () => withBusy($('reloadAudit'), reloadAudit);
$('showMetrics').onclick = () => withBusy($('showMetrics'), showMetrics);

$('match').onclick = () => withBusy($('match'), async () => {
  if (!state.cardId) throw new Error('请先选择需求卡');
  const items = currentItems();
  const usable = Object.values(items).filter((i) => i.state === 'specified' && i.value !== null);
  if (!usable.length) throw new Error('请至少裁定一项"已明确"的字段，不能以空需求匹配');
  const data = await api('/api/match', {
    method: 'POST',
    body: JSON.stringify({
      items: usable,
      spaces: state.spaces,
      actor_role: role(),
      actor: actor(),
    }),
  });
  renderResults(data);
  state.report = {
    created_at: new Date().toISOString(),
    card_id: state.cardId,
    card_title: $('cardTitle').value.trim(),
    purpose: $('cardPurpose').value,
    confirmed_requirements: items,
    spaces: state.spaces,
    ...data,
  };
  $('export').disabled = false;
  showMessage(`匹配完成：满足 ${data.matched_count}，待核实 ${data.unverified_count}，不匹配 ${data.failed_count}。`, true);
});

$('export').onclick = () => {
  if (!state.report) return;
  const blob = new Blob([JSON.stringify(state.report, null, 2)], { type: 'application/json' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = '招商需求与匹配报告.json';
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
};

$('exportMd').onclick = () => withBusy($('exportMd'), async () => {
  if (!state.cardId) throw new Error('请先选择需求卡');
  const data = await api('/api/card/report?card_id=' + encodeURIComponent(state.cardId));
  const blob = new Blob([data.markdown], { type: 'text/markdown;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = data.filename || '需求报告.md';
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
  showMessage('Markdown 报告已导出（匹配结论基于当前台账重算）。', true);
});

// 需求卡列表：搜索、筛选与分页
$('cardSearchBtn').onclick = () => withBusy($('cardSearchBtn'), async () => {
  state.cardsPage = 1;
  await reloadCards();
});
$('cardSearch').addEventListener('keydown', (event) => {
  if (event.key === 'Enter') $('cardSearchBtn').click();
});
$('cardStatusFilter').onchange = () => {
  state.cardsPage = 1;
  reloadCards();
};
$('cardPrevPage').onclick = () => withBusy(null, async () => {
  if (state.cardsPage > 1) {
    state.cardsPage -= 1;
    await reloadCards();
  }
});
$('cardNextPage').onclick = () => withBusy(null, async () => {
  const pages = Math.max(1, Math.ceil(state.cardsTotal / state.cardsPageSize));
  if (state.cardsPage < pages) {
    state.cardsPage += 1;
    await reloadCards();
  }
});

$('exportAudit').onclick = () => withBusy($('exportAudit'), async () => {
  if (!state.cardId) throw new Error('请先选择需求卡');
  const [card, audit] = await Promise.all([
    api('/api/card?card_id=' + encodeURIComponent(state.cardId)),
    api('/api/audit?card_id=' + encodeURIComponent(state.cardId)),
  ]);
  const payload = {
    exported_at: new Date().toISOString(),
    exported_by: { actor: actor(), role: role() },
    card: {
      id: card.id, title: card.title, customer: card.customer, purpose: card.purpose,
      status: card.status, version: card.version,
      created_at: card.created_at, updated_at: card.updated_at,
    },
    items: card.items,
    validation: card.validation,
    confirmations: card.confirmations,
    reviews: card.review_history || card.reviews || [],
    audit: audit.audit,
    caveat: '本报告是留痕导出，不替代独立审计。交接遗漏率等业务口径须在试点前确认。',
  };
  const blob = new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = '交接留痕报告_' + card.id + '.json';
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
});

$('cardPurpose').onchange = () => {
  if (state.card) {
    state.card.purpose = $('cardPurpose').value;
    renderFields(state.card);
  }
};

$('actorRole').onchange = () => {
  showMessage('角色已切换为：' + $('actorRole').selectedOptions[0].text + '。权限在后端校验。', true);
};

// 侧边栏导航：点击切换显示面板
document.querySelectorAll('.nav-item').forEach((btn) => {
  btn.addEventListener('click', () => showPanel(btn.dataset.panel));
});

// 复核动作的输入控件随动作变化
function refreshReviewFields() {
  const action = $('reviewAction').value;
  const fieldSelect = $('reviewField');
  const codeSelect = $('reviewReasonCode');
  clear(fieldSelect);
  if (action === 'change_level' || action === 'change_explanation') {
    fieldSelect.hidden = false;
    Object.keys(state.fields).forEach((key) => {
      const option = el('option', state.fields[key].label, fieldSelect);
      option.value = key;
    });
  } else {
    fieldSelect.hidden = true;
  }
  codeSelect.hidden = action !== 'reject';
}
$('reviewAction').onchange = refreshReviewFields;

// ---------------------------------------------------------------------------
// 智能助手（右侧抽屉）
// ---------------------------------------------------------------------------

const ASSISTANT_GREETING = () =>
  '你好，我是工作台智能助手，可以基于当前需求卡生成补问清单、状态总结、' +
  '报价与字段口径说明。' +
  (state.config && state.config.ai_enabled
    ? '当前为 AI 模式（发送内容已脱敏）。'
    : '当前为本地规则模式，识别固定意图；配置 AGENT_API_KEY / AGENT_MODEL 后可自由问答。');

let assistantGreeted = false;

function assistantOpen() {
  const panel = $('assistantPanel');
  panel.hidden = false;
  requestAnimationFrame(() => panel.classList.add('open'));
  $('assistantFab').classList.add('active');
  if (!assistantGreeted) {
    assistantGreeted = true;
    assistantBubble('assistant', ASSISTANT_GREETING());
  }
  $('assistantText').focus();
}

function assistantClosePanel() {
  const panel = $('assistantPanel');
  panel.classList.remove('open');
  $('assistantFab').classList.remove('active');
  setTimeout(() => { panel.hidden = true; }, 270);
}

function assistantScroll(smooth) {
  const wrap = $('assistantMessages');
  wrap.scrollTo({ top: wrap.scrollHeight, behavior: smooth ? 'smooth' : 'auto' });
}

function assistantBubble(kind, text, pending) {
  const wrap = $('assistantMessages');
  const row = document.createElement('div');
  row.className = 'assistant-row ' + kind;
  const bubble = document.createElement('div');
  bubble.className = 'assistant-bubble' + (pending ? ' pending' : '');
  if (pending) {
    const typing = el('div', null, bubble);
    typing.className = 'assistant-typing';
    el('i', undefined, typing);
    el('i', undefined, typing);
    el('i', undefined, typing);
  } else {
    bubble.textContent = text;
  }
  row.appendChild(bubble);
  wrap.appendChild(row);
  assistantScroll(true);
  while (wrap.children.length > 40) wrap.removeChild(wrap.firstChild);
  return bubble;
}

async function assistantSend(message) {
  const text = (message || '').trim();
  if (!text) return;
  assistantBubble('user', text);
  const loading = assistantBubble('assistant', '', true);
  try {
    const data = await api('/api/assistant/chat', {
      method: 'POST',
      body: JSON.stringify({ message: text, card_id: state.cardId || '' }),
    });
    loading.textContent = data.reply;
    loading.classList.remove('pending');
    $('assistantMode').textContent = data.mode === 'ai' ? 'AI 模式' : '本地规则模式';
  } catch (err) {
    loading.textContent = '出错了：' + (err.message || String(err));
    loading.classList.remove('pending');
    loading.classList.add('error');
  }
  assistantScroll(true);
}

function initAssistant() {
  $('assistantFab').onclick = () => {
    if ($('assistantPanel').hidden) assistantOpen();
    else assistantClosePanel();
  };
  $('assistantClose').onclick = assistantClosePanel;
  $('assistantForm').onsubmit = (event) => {
    event.preventDefault();
    const input = $('assistantText');
    const value = input.value;
    input.value = '';
    assistantSend(value);
  };
  $('assistantQuick').querySelectorAll('button').forEach((btn) => {
    btn.onclick = () => assistantSend(btn.dataset.q);
  });
}

boot().then(refreshReviewFields);
