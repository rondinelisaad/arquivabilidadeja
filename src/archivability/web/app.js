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

  const renderIndicators = (results) => {
    const section = $("#findings-section");
    section.hidden = !results.length;
    const list = $("#indicator-list");
    list.replaceChildren();
    results.forEach((result) => {
      const row = document.createElement("div");
      row.className = "indicator-row";
      const id = document.createElement("span");
      id.className = "indicator-id";
      id.textContent = result.indicator_id;
      const confidence = document.createElement("span");
      confidence.textContent = `Confiança ${Math.round((result.confidence || 0) * 100)}%`;
      const state = document.createElement("span");
      state.className = `indicator-state state-${result.state}`;
      state.textContent = stateLabel(result.state);
      row.append(id, confidence, state);
      list.append(row);
    });
  };

  const renderReport = (report) => {
    $("#report-origin").textContent = report.target_origin;
    $("#report-title").textContent = terminalStates.has(report.state) ? "Relatório de arquivabilidade" : "Avaliação em andamento";
    $("#report-updated").textContent = `Atualizado em ${formatDate(report.updated_at)}`;
    $("#cancel-button").hidden = terminalStates.has(report.state);
    renderProgress(report);
    renderAssessment(report);
    renderIndicators(report.indicator_results || []);
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
      const report = await request(`/v1/analyses/${encodeURIComponent(analysisId)}`);
      renderReport(report);
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
