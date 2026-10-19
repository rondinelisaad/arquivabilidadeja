(() => {
  "use strict";

  const $ = (selector) => document.querySelector(selector);
  const terminalStates = new Set(["completed", "partially_completed", "failed", "cancelled"]);
  const phaseOrder = ["collection", "browser", "assessment", "completed"];
  const phaseLabels = {
    requested: "Preparando",
    collection_queued: "Coleta na fila",
    collecting: "Coletando metadados",
    browser_queued: "Browser na fila",
    rendering: "Renderizando com segurança",
    assessment_queued: "Avaliação na fila",
    assessment_queued_degraded: "Avaliação parcial na fila",
    assessing: "Derivando evidências",
    completed: "Concluída",
    partially_completed: "Concluída parcialmente",
    failed: "Falhou",
    cancelled: "Cancelada",
  };
  const dimensionNames = {
    discoverability: "Descoberta",
    retrievability: "Recuperação",
    standards_formats: "Padrões e formatos",
    dependency_cohesion: "Dependências",
    metadata_context: "Metadados",
    operational_harvestability: "Operação de coleta",
  };
  let pendingSubmission = null;
  let indicatorCatalogPromise = null;

  const request = async (path, options = {}) => {
    const response = await fetch(path, {
      credentials: "same-origin",
      headers: { Accept: "application/json", ...(options.headers || {}) },
      ...options,
    });
    const body = await response.json().catch(() => ({ error: { code: "INVALID_RESPONSE" } }));
    if (!response.ok) {
      const error = new Error(body.error?.message || "Não foi possível concluir a solicitação.");
      error.code = body.error?.code || "REQUEST_FAILED";
      error.status = response.status;
      throw error;
    }
    return body;
  };

  const errorMessage = (error) => {
    if (error.status === 401) return "Sua sessão não está autenticada. Entre pelo provedor institucional e tente novamente.";
    if (error.status === 429) return "O limite de solicitações foi atingido. Aguarde um pouco e tente novamente.";
    if (error.code === "NOT_FOUND") return "A avaliação solicitada não foi encontrada.";
    return "Não foi possível concluir a solicitação agora.";
  };

  const stateLabel = (state) => phaseLabels[state] || String(state || "indisponível").replaceAll("_", " ");
  const formatDate = (value) => value ? new Intl.DateTimeFormat("pt-BR", { dateStyle: "medium", timeStyle: "short" }).format(new Date(value)) : "—";

  const hidePrimaryViews = () => {
    ["#home-view", "#history-section", "#report-view", "#methodology-view", "#indicator-view"].forEach((selector) => {
      $(selector).hidden = true;
    });
  };

  const appendMeta = (container, values) => {
    container.replaceChildren();
    values.filter(Boolean).forEach((value) => {
      const item = document.createElement("span");
      item.textContent = value;
      container.append(item);
    });
  };

  const indicatorCard = (indicator) => {
    const link = document.createElement("a");
    link.className = "methodology-indicator-card";
    link.href = indicator.path;
    const id = document.createElement("span");
    id.className = "eyebrow";
    id.textContent = indicator.id;
    const title = document.createElement("h3");
    title.textContent = indicator.name;
    const description = document.createElement("p");
    description.textContent = indicator.description;
    link.append(id, title, description);
    return link;
  };

  const loadMethodology = async () => {
    const status = $("#methodology-status");
    try {
      const [methodology, catalog] = await Promise.all([
        request("/v1/methodology"),
        request("/v1/indicators"),
      ]);
      $("#methodology-title").textContent = methodology.title;
      $("#methodology-summary").textContent = "A avaliação produz um perfil em seis dimensões, preserva resultados desconhecidos e explicita evidências, confiança e limitações sem reduzi-las a uma nota única.";
      appendMeta($("#methodology-meta"), [
        `Versão ${methodology.version}`,
        `Estado ${methodology.status}`,
        `${methodology.indicator_count} indicadores`,
        `Fonte ${methodology.source_sha256.slice(0, 12)}…`,
      ]);
      const dimensions = $("#methodology-dimensions");
      dimensions.replaceChildren();
      methodology.dimensions.forEach((dimension) => {
        const card = document.createElement("article");
        card.className = "dimension-card";
        const title = document.createElement("h3");
        title.textContent = dimension.name;
        const description = document.createElement("p");
        description.className = "muted";
        description.textContent = dimension.description;
        const weight = document.createElement("p");
        weight.textContent = dimension.score_enabled ? `Peso candidato: ${dimension.weight}` : "Dimensão contextual, sem pontuação";
        card.append(title, description, weight);
        dimensions.append(card);
      });
      const indicators = $("#methodology-indicators");
      indicators.replaceChildren(...catalog.items.map(indicatorCard));
      const limitations = $("#methodology-limitations");
      limitations.replaceChildren();
      methodology.limitations.forEach((value) => {
        const item = document.createElement("li");
        item.textContent = value;
        limitations.append(item);
      });
      status.textContent = "";
    } catch (error) {
      status.textContent = errorMessage(error);
    }
  };

  const documentationCard = (titleText, content) => {
    const card = document.createElement("section");
    card.className = "documentation-card";
    const title = document.createElement("h2");
    title.textContent = titleText;
    card.append(title);
    if (Array.isArray(content)) {
      const list = document.createElement("ul");
      content.forEach((value) => {
        const item = document.createElement("li");
        item.textContent = value;
        list.append(item);
      });
      card.append(list);
    } else {
      const paragraph = document.createElement("p");
      paragraph.textContent = content;
      card.append(paragraph);
    }
    return card;
  };

  const loadIndicatorDocumentation = async (identifier) => {
    const status = $("#indicator-status");
    try {
      const payload = await request(`/v1/indicators/${encodeURIComponent(identifier)}`);
      const indicator = payload.indicator;
      $("#indicator-id").textContent = indicator.id;
      $("#indicator-title").textContent = indicator.name;
      $("#indicator-description").textContent = indicator.description;
      appendMeta($("#indicator-tags"), [
        indicator.dimension ? `Dimensão ${dimensionNames[indicator.dimension] || indicator.dimension}` : "Contexto transversal",
        `Classificação ${indicator.classification}`,
        `Severidade ${indicator.severity}`,
        `Metodologia ${payload.methodology_version}`,
      ]);
      const documentation = $("#indicator-documentation");
      documentation.replaceChildren(
        documentationCard("Definição", indicator.description),
        documentationCard("Por que importa", `Este indicador documenta uma condição de ${indicator.dimension ? dimensionNames[indicator.dimension] || indicator.dimension : "contexto"} que afeta a interpretação da arquivabilidade observada.`),
        documentationCard("Medição", indicator.measurement),
        documentationCard("Evidências necessárias", indicator.evidence_requirements),
        documentationCard("Classificação", `${indicator.classification}; severidade padrão ${indicator.severity}.`),
        documentationCard("Pontuação", indicator.scoring.enabled ? `Peso ${indicator.scoring.weight}. Regra: ${indicator.scoring.rule}.` : "Indicador contextual: não compõe score."),
        documentationCard("Limitações", indicator.limitations),
        documentationCard("Referências", indicator.origin.references.length ? indicator.origin.references : ["Sem referência externa declarada nesta versão."]),
      );
      status.textContent = "";
      document.title = `${indicator.id} — ${indicator.name} | Arquivabilidade Já`;
    } catch (error) {
      status.textContent = error.code === "NOT_FOUND" ? "O indicador solicitado não foi encontrado." : errorMessage(error);
    }
  };

  const renderHistory = (items) => {
    const container = $("#history-list");
    container.replaceChildren();
    if (!items.length) {
      const empty = document.createElement("p");
      empty.className = "empty-state";
      empty.textContent = "Nenhuma avaliação encontrada nesta conta.";
      container.append(empty);
      return;
    }
    items.forEach((item) => {
      const link = document.createElement("a");
      link.className = "history-card";
      link.href = `/analyses/${encodeURIComponent(item.analysis_id)}`;
      const label = document.createElement("p");
      label.className = "eyebrow";
      label.textContent = stateLabel(item.state);
      const title = document.createElement("h3");
      title.textContent = item.target_origin;
      const meta = document.createElement("div");
      meta.className = "history-meta";
      const date = document.createElement("span");
      date.textContent = formatDate(item.finished_at || item.created_at);
      const overall = document.createElement("span");
      overall.textContent = item.overall_state ? stateLabel(item.overall_state) : "Em processamento";
      meta.append(date, overall);
      link.append(label, title, meta);
      container.append(link);
    });
  };

  const loadHistory = async () => {
    const container = $("#history-list");
    container.textContent = "Carregando histórico…";
    try {
      const history = await request("/v1/analyses");
      renderHistory(history.items || []);
    } catch (error) {
      container.textContent = errorMessage(error);
    }
  };

  const renderProgress = (report) => {
    const phase = report.progress?.phase || report.state;
    $("#progress-label").textContent = stateLabel(phase);
    const stage = phase.includes("collection") || phase === "collecting" ? 0 : phase.includes("browser") || phase === "rendering" ? 1 : phase.includes("assessment") || phase === "assessing" ? 2 : terminalStates.has(report.state) ? 3 : 0;
    $("#stage-progress").style.width = `${Math.max(8, ((stage + 1) / 4) * 100)}%`;
    const list = $("#stage-list");
    list.replaceChildren();
    ["Coleta HTTP", "Browser isolado", "Derivação", "Relatório"].forEach((label, index) => {
      const item = document.createElement("li");
      item.textContent = label;
      if (index <= stage) item.className = "active";
      list.append(item);
    });
  };

  const renderAssessment = (report) => {
    const section = $("#assessment-section");
    if (!report.assessment) { section.hidden = true; return; }
    section.hidden = false;
    $("#overall-state").textContent = stateLabel(report.assessment.overall_state);
    const grid = $("#dimension-grid");
    grid.replaceChildren();
    Object.entries(report.assessment.dimensions || {}).forEach(([id, value]) => {
      const card = document.createElement("article");
      card.className = "dimension-card";
      const title = document.createElement("h3");
      title.textContent = dimensionNames[id] || id;
      const score = document.createElement("p");
      score.className = "dimension-score";
      score.textContent = value.score == null ? "—" : `${Math.round(value.score)}`;
      const coverage = document.createElement("p");
      coverage.className = "muted";
      coverage.textContent = `Cobertura ${Math.round((value.coverage || 0) * 100)}%`;
      const bar = document.createElement("div");
      bar.className = "coverage-bar";
      const fill = document.createElement("span");
      fill.style.width = `${Math.round((value.coverage || 0) * 100)}%`;
      bar.append(fill);
      card.append(title, score, coverage, bar);
      grid.append(card);
    });
  };

  const renderIndicators = (results, catalogById) => {
    const section = $("#findings-section");
    section.hidden = !results.length;
    const list = $("#indicator-list");
    list.replaceChildren();
    results.forEach((result) => {
      const row = document.createElement("div");
      row.className = "indicator-row";
      const id = document.createElement("span");
      id.className = "indicator-id";
      const definition = catalogById.get(result.indicator_id);
      const indicatorLink = document.createElement("a");
      indicatorLink.href = definition?.path || `/methodology/indicators/${encodeURIComponent(result.indicator_id)}`;
      indicatorLink.textContent = result.indicator_id;
      id.append(indicatorLink);
      const confidence = document.createElement("span");
      confidence.textContent = `Confiança ${Math.round((result.confidence || 0) * 100)}%`;
      const state = document.createElement("span");
      state.className = `indicator-state state-${result.state}`;
      state.textContent = stateLabel(result.state);
      row.append(id, confidence, state);
      list.append(row);
    });
  };

  const findingCard = (eyebrow, titleText, descriptionText) => {
    const card = document.createElement("article");
    card.className = "finding-card";
    const label = document.createElement("span");
    label.className = "eyebrow";
    label.textContent = eyebrow;
    const title = document.createElement("h3");
    title.textContent = titleText;
    const description = document.createElement("p");
    description.textContent = descriptionText;
    card.append(label, title, description);
    return card;
  };

  const renderCriticalFindings = (report, catalogById) => {
    const findings = (report.indicator_results || []).filter((result) => {
      const severity = catalogById.get(result.indicator_id)?.severity;
      return ["fail", "warning"].includes(result.state) && ["critical", "high"].includes(severity);
    });
    const section = $("#critical-section");
    section.hidden = !findings.length;
    const container = $("#critical-findings");
    container.replaceChildren(...findings.map((result) => {
      const definition = catalogById.get(result.indicator_id);
      return findingCard(
        `${result.indicator_id} · ${definition?.severity || "prioridade"}`,
        definition?.name || result.indicator_id,
        `${stateLabel(result.state)} com confiança de ${Math.round((result.confidence || 0) * 100)}%.`,
      );
    }));
  };

  const renderBlockingConditions = (assessment) => {
    const section = $("#blocking-section");
    section.hidden = !assessment;
    if (!assessment) return;
    const container = $("#blocking-conditions");
    const gates = Object.entries(assessment.gates || {});
    const triggered = gates.filter(([, gate]) => gate.state === "triggered");
    container.replaceChildren();
    if (!triggered.length) {
      const note = document.createElement("p");
      note.textContent = "Nenhuma condição impeditiva ativa foi acionada. Nesta versão candidata, os gates permanecem inativos e não alteram o resultado.";
      container.append(note);
      return;
    }
    triggered.forEach(([gateId, gate]) => {
      container.append(findingCard(gateId, "Condição impeditiva acionada", gate.reason));
    });
  };

  const renderLegacyComparison = (assessment) => {
    const section = $("#legacy-section");
    section.hidden = !assessment;
    if (!assessment) return;
    const container = $("#legacy-content");
    container.replaceChildren();
    const paragraph = document.createElement("p");
    if (!assessment.legacy_clear_plus) {
      paragraph.textContent = "Legacy CLEAR+ Flat e Weighted permanecem indisponíveis nesta versão: ainda não existe mapeamento cientificamente validado entre as evidências atuais e as quatro facetas originais.";
    } else {
      paragraph.textContent = `Flat ${assessment.legacy_clear_plus.flat}; Weighted ${assessment.legacy_clear_plus.fixed_max}. Estes valores servem apenas para comparação histórica.`;
    }
    container.append(paragraph);
  };

  const renderNetworkContext = (report, catalogById) => {
    const contextual = (report.indicator_results || []).filter(
      (result) => catalogById.get(result.indicator_id)?.classification === "observation_context",
    );
    const probeDetails = report.probes || [];
    const probeIds = new Set((report.indicator_results || []).flatMap((result) => result.probe_ids || []));
    const section = $("#network-section");
    section.hidden = !contextual.length && !probeDetails.length && !probeIds.size;
    const container = $("#network-content");
    const cards = [];
    if (probeDetails.length) {
      probeDetails.forEach((probe) => cards.push(findingCard(
        "Probe",
        probe.probe_id,
        `País: ${probe.country}; região: ${probe.region}; observado de ${formatDate(probe.first_observed_at)} a ${formatDate(probe.last_observed_at)}. Localização declarada pela implantação, sem inferência de precisão.`,
      )));
    } else {
      probeIds.forEach((probe) => cards.push(findingCard("Probe", probe, "Registro legado sem país ou região declarados.")));
    }
    contextual.forEach((result) => {
      const definition = catalogById.get(result.indicator_id);
      cards.push(findingCard(result.indicator_id, definition?.name || result.indicator_id, `${stateLabel(result.state)}; dado contextual, sem penalização automática do website.`));
    });
    container.replaceChildren(...cards);
  };

  const renderDependencies = (report) => {
    const run = (report.browser_runs || []).at(-1);
    const section = $("#dependency-section");
    section.hidden = !run;
    if (!run) return;
    const summary = run.summary || {};
    const container = $("#dependency-content");
    container.replaceChildren(
      findingCard("Requisições", String(summary.request_count ?? "—"), `${summary.external_request_count ?? 0} externas; ${summary.failed_request_count ?? 0} falharam.`),
      findingCard("APIs", String(summary.api_request_count ?? "—"), `${summary.external_api_request_count ?? 0} APIs externas observadas.`),
      findingCard("URLs dinâmicas", String(summary.additional_url_count ?? "—"), "URLs adicionais após renderização, sem persistir endereços brutos."),
    );
  };

  const renderTechnicalDetails = (report) => {
    const cards = [];
    (report.crawl_runs || []).forEach((run) => cards.push(findingCard(
      `Crawl · ${run.probe_id}`,
      `${run.summary.visited ?? 0} URL(s) visitadas`,
      `Término: ${run.summary.termination_reason}; bytes observados: ${run.summary.response_bytes ?? 0}.`,
    )));
    (report.browser_runs || []).forEach((run) => cards.push(findingCard(
      `Browser · ${run.probe_id}`,
      run.profile_id,
      `Término: ${run.summary.termination_reason}; duração: ${run.summary.elapsed_milliseconds ?? 0} ms.`,
    )));
    const section = $("#technical-section");
    section.hidden = !cards.length;
    $("#technical-content").replaceChildren(...cards);
  };

  const renderRecommendations = (report, catalogById) => {
    const actionable = (report.indicator_results || []).filter((result) => ["fail", "warning"].includes(result.state));
    const section = $("#recommendations-section");
    section.hidden = !actionable.length;
    const container = $("#recommendations-content");
    container.replaceChildren(...actionable.map((result) => {
      const definition = catalogById.get(result.indicator_id) || {};
      const card = document.createElement("article");
      card.className = "recommendation-card";
      const title = document.createElement("h3");
      title.textContent = `${result.indicator_id} — ${definition.name || "Condição observada"}`;
      const details = document.createElement("dl");
      const rows = [
        ["Problema", stateLabel(result.state)],
        ["Evidência", `${(result.evidence_ids || []).length} evidência(s); confiança ${Math.round((result.confidence || 0) * 100)}%.`],
        ["Impacto", definition.description || "Pode afetar a captura ou a interpretação do resultado."],
        ["Possível correção", `Revisar a condição pelo método: ${definition.measurement || "consulte a documentação do indicador"}.`],
        ["Responsabilidade", definition.classification === "intrinsic" ? "Depende principalmente do administrador do site." : definition.classification === "observation_context" ? "Depende principalmente do contexto/probe de coleta." : "Pode depender do site e da configuração do coletor."],
      ];
      rows.forEach(([term, value]) => {
        const dt = document.createElement("dt");
        dt.textContent = term;
        const dd = document.createElement("dd");
        dd.textContent = value;
        details.append(dt, dd);
      });
      card.append(title, details);
      return card;
    }));
  };

  const renderReport = (report, catalog) => {
    const catalogById = new Map((catalog.items || []).map((item) => [item.id, item]));
    $("#report-origin").textContent = report.target_origin;
    $("#report-title").textContent = terminalStates.has(report.state) ? "Relatório de arquivabilidade" : "Avaliação em andamento";
    $("#report-updated").textContent = `Atualizado em ${formatDate(report.updated_at)}`;
    $("#cancel-button").hidden = terminalStates.has(report.state);
    renderProgress(report);
    renderAssessment(report);
    renderIndicators(report.indicator_results || [], catalogById);
    renderCriticalFindings(report, catalogById);
    renderBlockingConditions(report.assessment);
    renderLegacyComparison(report.assessment);
    renderNetworkContext(report, catalogById);
    renderDependencies(report);
    renderTechnicalDetails(report);
    renderRecommendations(report, catalogById);
    const limitations = $("#limitations");
    limitations.hidden = !(report.limitations || []).length;
    const list = limitations.querySelector("ul");
    list.replaceChildren();
    (report.limitations || []).forEach((text) => {
      const item = document.createElement("li");
      item.textContent = text;
      list.append(item);
    });
  };

  const loadReport = async (analysisId) => {
    const status = $("#report-status");
    try {
      indicatorCatalogPromise ||= request("/v1/indicators").catch(() => ({ items: [] }));
      const [report, catalog] = await Promise.all([
        request(`/v1/analyses/${encodeURIComponent(analysisId)}`),
        indicatorCatalogPromise,
      ]);
      renderReport(report, catalog);
      status.textContent = "";
      if (!terminalStates.has(report.state)) window.setTimeout(() => loadReport(analysisId), 2200);
    } catch (error) {
      status.textContent = errorMessage(error);
    }
  };

  const loadComparison = async (analysisId) => {
    const section = $("#comparison-section");
    const content = $("#comparison-content");
    section.hidden = false;
    content.textContent = "Buscando execução compatível…";
    try {
      const comparison = await request(`/v1/analyses/${encodeURIComponent(analysisId)}/comparison`);
      content.replaceChildren();
      const summary = document.createElement("p");
      summary.textContent = comparison.classification === "observed_change" ? `${comparison.indicator_changes.length} indicador(es) mudaram desde a execução anterior.` : "Nenhuma mudança foi observada nos indicadores comparáveis.";
      content.append(summary);
      comparison.indicator_changes.forEach((change) => {
        const row = document.createElement("p");
        row.textContent = `${change.indicator_id}: ${stateLabel(change.baseline_state)} → ${stateLabel(change.current_state)}`;
        content.append(row);
      });
    } catch (error) {
      content.textContent = error.code === "NOT_FOUND" ? "Ainda não existe uma execução anterior compatível." : errorMessage(error);
    }
  };

  const cancelAnalysis = async (analysisId) => {
    const button = $("#cancel-button");
    const status = $("#report-status");
    button.disabled = true;
    status.textContent = "Cancelando avaliação…";
    try {
      await request(`/v1/analyses/${encodeURIComponent(analysisId)}/cancel`, { method: "POST" });
      await loadReport(analysisId);
    } catch (error) {
      status.textContent = errorMessage(error);
      button.disabled = false;
    }
  };

  const initialize = () => {
    const indicatorMatch = window.location.pathname.match(/^\/methodology\/indicators\/([A-Za-z0-9][A-Za-z0-9_-]{0,127})$/);
    if (indicatorMatch) {
      hidePrimaryViews();
      $("#indicator-view").hidden = false;
      loadIndicatorDocumentation(indicatorMatch[1]);
      return;
    }
    if (["/methodology", "/methodology/indicators"].includes(window.location.pathname)) {
      hidePrimaryViews();
      $("#methodology-view").hidden = false;
      loadMethodology();
      return;
    }
    const match = window.location.pathname.match(/^\/analyses\/([A-Za-z0-9][A-Za-z0-9_.:-]{0,255})$/);
    if (match) {
      const analysisId = match[1];
      hidePrimaryViews();
      $("#report-view").hidden = false;
      $("#export-link").href = `/v1/analyses/${encodeURIComponent(analysisId)}/export.json`;
      $("#compare-button").addEventListener("click", () => loadComparison(analysisId));
      $("#cancel-button").addEventListener("click", () => cancelAnalysis(analysisId));
      loadReport(analysisId);
      return;
    }
    $("#analysis-form").addEventListener("submit", async (event) => {
      event.preventDefault();
      const input = $("#subject-uri");
      const status = $("#form-status");
      if (!input.reportValidity()) return;
      const subjectUri = input.value.trim();
      if (!pendingSubmission || pendingSubmission.subjectUri !== subjectUri) {
        pendingSubmission = {
          subjectUri,
          key: crypto.randomUUID(),
        };
      }
      status.textContent = "Criando avaliação…";
      try {
        const outcome = await request("/v1/analyses", {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "Idempotency-Key": pendingSubmission.key,
          },
          body: JSON.stringify({ subject_uri: subjectUri }),
        });
        pendingSubmission = null;
        window.location.assign(`/analyses/${encodeURIComponent(outcome.analysis_id)}`);
      } catch (error) {
        status.textContent = errorMessage(error);
      }
    });
    $("#refresh-history").addEventListener("click", loadHistory);
    loadHistory();
  };

  initialize();
})();
