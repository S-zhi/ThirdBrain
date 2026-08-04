/** ThirdBrain single-question experience. */
(function () {
  'use strict';

  const API_URL = '/api/v1/retrieval/query';
  const REQUEST_TIMEOUT_MS = 25_000;
  const SCOPE = Object.freeze({
    wiki_id: 'wiki-1',
    namespace: 'com.huawei.cann.ascendc',
    version: '910beta3',
  });

  const form = document.getElementById('queryForm');
  const question = document.getElementById('question');
  const counter = document.getElementById('charCount');
  const card = document.getElementById('queryCard');
  const button = document.getElementById('askButton');
  const loadingView = document.getElementById('loadingView');
  const resultView = document.getElementById('resultView');
  const status = document.getElementById('queryStatus');
  const resetButton = document.getElementById('resetButton');
  const resultState = document.getElementById('resultState');
  const resultRank = document.getElementById('resultRank');
  const resultTitle = document.getElementById('resultTitle');
  const resultMeta = document.getElementById('resultMeta');
  const resultSummary = document.getElementById('resultSummary');
  const resultContent = document.getElementById('resultContent');
  const sourceLink = document.getElementById('sourceLink');

  let activeController = null;

  function setView(view) {
    const isInput = view === 'input';
    const isLoading = view === 'loading';
    const isResult = view === 'result';

    form.hidden = !isInput;
    form.setAttribute('aria-hidden', String(!isInput));
    loadingView.hidden = !isLoading;
    loadingView.setAttribute('aria-hidden', String(!isLoading));
    resultView.hidden = !isResult;
    resultView.setAttribute('aria-hidden', String(!isResult));
    card.dataset.state = view;
  }

  function setStatus(tone, message) {
    status.classList.remove('is-active', 'is-done', 'is-error');
    if (tone) status.classList.add(`is-${tone}`);
    const text = status.querySelector('p');
    if (text) text.textContent = message;
  }

  function updateCount() {
    counter.textContent = `${String(question.value.length).padStart(3, '0')} / 1000`;
  }

  function appendMeta(value) {
    if (!value) return;
    const item = document.createElement('span');
    item.textContent = value;
    resultMeta.appendChild(item);
  }

  function safeSourceUrl(value) {
    if (typeof value !== 'string' || !value.trim()) return null;
    try {
      const parsed = new URL(value, window.location.origin);
      return parsed.protocol === 'http:' || parsed.protocol === 'https:' ? parsed.href : null;
    } catch (_error) {
      return null;
    }
  }

  function firstSourceUrl(hit) {
    const provenance = Array.isArray(hit?.provenance) ? hit.provenance : [];
    for (const evidence of provenance) {
      const safe = safeSourceUrl(evidence?.source_url);
      if (safe) return safe;
    }
    return safeSourceUrl(hit?.source_origin?.url);
  }

  function rankedHits(data) {
    const knowledge = Array.isArray(data?.knowledge_hits) ? data.knowledge_hits : [];
    const source = Array.isArray(data?.source_hits) ? data.source_hits : [];
    const preferred = knowledge.length ? knowledge : source;
    return preferred.slice().sort((left, right) => Number(right?.score || 0) - Number(left?.score || 0));
  }

  function clearResult() {
    resultMeta.replaceChildren();
    resultTitle.textContent = '';
    resultSummary.textContent = '';
    resultSummary.hidden = true;
    resultContent.textContent = '';
    resultContent.hidden = true;
    sourceLink.hidden = true;
    sourceLink.removeAttribute('href');
    card.removeAttribute('data-tone');
  }

  function showHit(hit) {
    clearResult();
    resultState.textContent = 'VERIFIED CONTEXT';
    resultRank.textContent = 'RANK 01';
    resultTitle.textContent = hit.title || '已定位可信上下文';
    appendMeta(hit.namespace || SCOPE.namespace);
    appendMeta(hit.version || SCOPE.version);
    appendMeta(hit.match_confidence ? `${hit.match_confidence} match` : 'retrieved');

    const summary = typeof hit.summary === 'string' ? hit.summary.trim() : '';
    const content = typeof hit.content === 'string' ? hit.content.trim() : '';
    if (summary) {
      resultSummary.textContent = summary;
      resultSummary.hidden = false;
    }
    if (content && content !== summary) {
      resultContent.textContent = content;
      resultContent.hidden = false;
    }
    if (!summary && !content) {
      resultSummary.textContent = '已命中对应 API 契约，但当前索引未返回可展示的正文。';
      resultSummary.hidden = false;
    }

    const sourceUrl = firstSourceUrl(hit);
    if (sourceUrl) {
      sourceLink.href = sourceUrl;
      sourceLink.hidden = false;
    }
    setView('result');
    setStatus('done', '已按固定版本与命名空间返回最高排名的可信上下文。');
    resetButton.focus({ preventScroll: true });
  }

  function showAbstention(abstention) {
    clearResult();
    card.dataset.tone = 'abstain';
    resultState.textContent = 'NOT ENOUGH EVIDENCE';
    resultRank.textContent = 'ABSTAIN';
    resultTitle.textContent = '这次不猜。';
    appendMeta(SCOPE.namespace);
    appendMeta(SCOPE.version);

    const reason = typeof abstention?.reason === 'string' && abstention.reason.trim()
      ? abstention.reason.trim()
      : '当前版本范围内没有足够可信的文档证据。';
    resultSummary.textContent = reason;
    resultSummary.hidden = false;

    const guidance = typeof abstention?.guidance === 'string' ? abstention.guidance.trim() : '';
    if (guidance) {
      resultContent.textContent = guidance;
      resultContent.hidden = false;
    }
    setView('result');
    setStatus('error', '证据不足，ThirdBrain 已明确拒绝给出不可靠上下文。');
    resetButton.focus({ preventScroll: true });
  }

  function showError(message, statusCode) {
    clearResult();
    card.dataset.tone = 'error';
    resultState.textContent = 'CONNECTION INTERRUPTED';
    resultRank.textContent = statusCode ? `HTTP ${statusCode}` : 'RETRY';
    resultTitle.textContent = '这次没有连上知识库。';
    resultSummary.textContent = message;
    resultSummary.hidden = false;
    setView('result');
    setStatus('error', message);
    resetButton.focus({ preventScroll: true });
  }

  async function errorMessage(response) {
    if (response.status === 401) return '当前入口尚未获得检索权限，请检查同源网关的凭证注入。';
    if (response.status === 422) return '问题格式无法被检索服务识别，请换一种更具体的描述。';
    if (response.status === 503) return '检索服务暂时不可用，请稍后重新询问。';
    try {
      const payload = await response.json();
      if (typeof payload?.detail === 'string') return payload.detail;
      if (typeof payload?.message === 'string') return payload.message;
    } catch (_error) {
      // Fall through to the stable public error below.
    }
    return `检索请求失败（HTTP ${response.status}）。`;
  }

  async function submitQuestion() {
    const query = question.value.trim();
    if (!query) {
      setStatus('error', '先写下一个具体的 API 调用问题。');
      question.focus();
      return;
    }
    if (activeController) return;

    activeController = new AbortController();
    const timeout = window.setTimeout(() => activeController?.abort(), REQUEST_TIMEOUT_MS);
    button.disabled = true;
    setView('loading');
    setStatus('active', '正在按版本与命名空间定位可信上下文…');

    const payload = {
      query,
      wiki_id: SCOPE.wiki_id,
      rag_collection_ids: [],
      namespace: SCOPE.namespace,
      version: SCOPE.version,
      top_k: 3,
      budget: 'small',
      include_stale: false,
      expand_relations: false,
      relation_limit: 0,
      update_wiki: true,
    };

    try {
      const request = typeof window.__THIRDBRAIN_FETCH__ === 'function'
        ? window.__THIRDBRAIN_FETCH__
        : window.fetch.bind(window);
      const response = await request(API_URL, {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
        signal: activeController.signal,
      });

      if (!response.ok) {
        showError(await errorMessage(response), response.status);
        return;
      }

      const data = await response.json();
      const hits = rankedHits(data);
      if (data?.found === true && hits.length) showHit(hits[0]);
      else showAbstention(data?.abstention);
    } catch (error) {
      const message = error?.name === 'AbortError'
        ? '检索超过 25 秒仍未返回，请重新询问。'
        : '网络连接中断，请确认服务可访问后重试。';
      showError(message);
    } finally {
      window.clearTimeout(timeout);
      activeController = null;
      button.disabled = false;
    }
  }

  function resetQuery() {
    if (activeController) activeController.abort();
    clearResult();
    question.value = '';
    updateCount();
    setView('input');
    setStatus('', '当前只开放一个入口。结果会在原位返回。');
    question.focus({ preventScroll: true });
  }

  form.addEventListener('submit', (event) => {
    event.preventDefault();
    submitQuestion();
  });
  question.addEventListener('input', updateCount);
  question.addEventListener('keydown', (event) => {
    if ((event.metaKey || event.ctrlKey) && event.key === 'Enter') {
      event.preventDefault();
      submitQuestion();
    }
  });
  resetButton.addEventListener('click', resetQuery);
  updateCount();
})();
