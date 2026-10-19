(() => {
  "use strict";

  const $ = (selector) => document.querySelector(selector);
  const i18n = window.ArchivabilityI18n;
  const t = i18n.t;
  const isPortuguese = i18n.locale === "pt";
  const localized = (pt, en) => isPortuguese ? pt : en;
  const terminalStates = new Set(["completed", "partially_completed", "failed", "cancelled"]);
  const phaseOrder = ["collection", "browser", "assessment", "completed"];
  const dimensionNames = {
    discoverability: "Discoverability",
    retrievability: "Retrievability",
    standards_formats: "Standards & Formats",
    dependency_cohesion: "Dependency / Cohesion",
    metadata_context: "Metadata & Context",
    operational_harvestability: "Operational Harvestability",
  };
  let pendingSubmission = null;
  let indicatorCatalogPromise = null;
  let activeIndicatorFilter = "all";

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
    if (error.status === 401) return t("error.auth");
    if (error.status === 429) return t("error.rate");
    if (error.code === "NOT_FOUND") return t("error.notFound");
    return t("error.generic");
  };

  const stateLabel = (state) => i18n.state(state);
  const dimensionLabel = (id) => i18n.dimension(id, dimensionNames[id] || id);
  const taxonomyLabel = (value) => {
    if (!isPortuguese) return value;
    return ({
      candidate: "candidato",
      intrinsic: "intrínseco",
      harvest_dependent: "dependente da coleta",
      observation_context: "contexto de observação",
      critical: "crítica",
      high: "alta",
      medium: "média",
      low: "baixa",
      info: "informativa",
    })[value] || value;
  };
  const formatDate = (value) => value ? new Intl.DateTimeFormat(isPortuguese ? "pt-BR" : "en", { dateStyle: "medium", timeStyle: "short" }).format(new Date(value)) : "—";

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
    indicator = i18n.localizeIndicator(indicator);
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
      $("#methodology-title").textContent = t("methodology.title");
      $("#methodology-summary").textContent = localized(
        "A avaliação produz um perfil em seis dimensões, preserva resultados desconhecidos e explicita evidências, confiança e limitações sem reduzi-las a uma nota única.",
        "The assessment produces a six-dimension profile, preserves unknown results, and exposes evidence, confidence, and limitations without reducing them to a single score.",
      );
      appendMeta($("#methodology-meta"), [
        localized(`Versão ${methodology.version}`, `Version ${methodology.version}`),
        localized(`Estado ${taxonomyLabel(methodology.status)}`, `Status ${methodology.status}`),
        localized(`${methodology.indicator_count} indicadores`, `${methodology.indicator_count} indicators`),
        localized(`Fonte ${methodology.source_sha256.slice(0, 12)}…`, `Source ${methodology.source_sha256.slice(0, 12)}…`),
      ]);
      const dimensions = $("#methodology-dimensions");
      dimensions.replaceChildren();
      methodology.dimensions.forEach((dimension) => {
        const card = document.createElement("article");
        card.className = "dimension-card";
        const title = document.createElement("h3");
        title.textContent = dimensionLabel(dimension.id);
        const description = document.createElement("p");
        description.className = "muted";
        description.textContent = i18n.dimensionDescription(dimension.id, dimension.description);
        const weight = document.createElement("p");
        weight.textContent = dimension.score_enabled
          ? localized(`Peso candidato: ${dimension.weight}`, `Candidate weight: ${dimension.weight}`)
          : localized("Dimensão contextual, sem pontuação", "Contextual dimension, not scored");
        card.append(title, description, weight);
        dimensions.append(card);
      });
      const indicators = $("#methodology-indicators");
      indicators.replaceChildren(...catalog.items.map(indicatorCard));
      const limitations = $("#methodology-limitations");
      limitations.replaceChildren();
      methodology.limitations.forEach((value) => {
        const item = document.createElement("li");
        item.textContent = i18n.localizeLimitation(value);
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
      const indicator = i18n.localizeIndicator(payload.indicator);
      $("#indicator-id").textContent = indicator.id;
      $("#indicator-title").textContent = indicator.name;
      $("#indicator-description").textContent = indicator.description;
      appendMeta($("#indicator-tags"), [
        indicator.dimension ? localized(`Dimensão ${dimensionLabel(indicator.dimension)}`, `Dimension ${dimensionLabel(indicator.dimension)}`) : t("common.crossCutting"),
        localized(`Classificação ${taxonomyLabel(indicator.classification)}`, `Classification ${indicator.classification}`),
        localized(`Severidade ${taxonomyLabel(indicator.severity)}`, `Severity ${indicator.severity}`),
        localized(`Metodologia ${payload.methodology_version}`, `Methodology ${payload.methodology_version}`),
      ]);
      const documentation = $("#indicator-documentation");
      documentation.replaceChildren(
        documentationCard(t("indicator.definition"), indicator.description),
        documentationCard(t("indicator.importance"), localized(
          `Este indicador documenta uma condição de ${indicator.dimension ? dimensionLabel(indicator.dimension) : "contexto"} que afeta a interpretação da arquivabilidade observada.`,
          `This indicator documents a ${indicator.dimension ? dimensionLabel(indicator.dimension) : "context"} condition that affects the interpretation of observed archivability.`,
        )),
        documentationCard(t("indicator.measurement"), indicator.measurement),
        documentationCard(t("indicator.evidence"), indicator.evidence_requirements),
        documentationCard(t("indicator.classification"), localized(`${taxonomyLabel(indicator.classification)}; severidade padrão ${taxonomyLabel(indicator.severity)}.`, `${indicator.classification}; default severity ${indicator.severity}.`)),
        documentationCard(t("indicator.scoring"), indicator.scoring.enabled ? localized(`Peso ${indicator.scoring.weight}. Regra: ${indicator.scoring.rule}.`, `Weight ${indicator.scoring.weight}. Rule: ${indicator.scoring.rule}.`) : localized("Indicador contextual: não compõe score.", "Contextual indicator: not included in the score.")),
        documentationCard(t("indicator.limitations"), indicator.limitations),
        documentationCard(t("indicator.references"), indicator.origin.references.length ? indicator.origin.references : [localized("Sem referência externa declarada nesta versão.", "No external reference declared in this version.")]),
      );
      status.textContent = "";
      document.title = `${indicator.id} — ${indicator.name} | Arquivabilidade Já`;
    } catch (error) {
      status.textContent = error.code === "NOT_FOUND" ? localized("O indicador solicitado não foi encontrado.", "The requested indicator was not found.") : errorMessage(error);
    }
  };

  const renderHistory = (items) => {
    const container = $("#history-list");
    container.replaceChildren();
    if (!items.length) {
      const empty = document.createElement("p");
      empty.className = "empty-state";
      empty.textContent = t("common.noHistory");
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
      overall.textContent = item.overall_state ? stateLabel(item.overall_state) : localized("Em processamento", "Processing");
      meta.append(date, overall);
      link.append(label, title, meta);
      container.append(link);
    });
  };

  const loadHistory = async () => {
    const container = $("#history-list");
    container.textContent = t("common.loadingHistory");
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
    [t("progress.http"), t("progress.browser"), t("progress.derive"), t("progress.report")].forEach((label, index) => {
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
    const results = report.indicator_results || [];
    const assessed = results.filter((result) => ["pass", "warning", "fail"].includes(result.state)).length;
    const coverage = results.length ? Math.round((assessed / results.length) * 100) : 0;
    $("#coverage-value").textContent = `${coverage}%`;
    $("#coverage-detail").textContent = localized(
      `${assessed} de ${results.length} indicadores produziram resultado avaliável.`,
      `${assessed} of ${results.length} indicators produced an assessable result.`,
    );
    const grid = $("#dimension-grid");
    grid.replaceChildren();
    Object.entries(report.assessment.dimensions || {}).forEach(([id, value]) => {
      const card = document.createElement("article");
      card.className = "dimension-card";
      const title = document.createElement("h3");
      title.textContent = dimensionLabel(id);
      const score = document.createElement("p");
      score.className = "dimension-score";
      score.textContent = value.score == null ? "—" : `${Math.round(value.score)}`;
      const coverage = document.createElement("p");
      coverage.className = "muted";
      coverage.textContent = t("common.coverage", { value: Math.round((value.coverage || 0) * 100) });
      const bar = document.createElement("div");
      bar.className = "coverage-bar";
      const fill = document.createElement("span");
      fill.style.width = `${Math.round((value.coverage || 0) * 100)}%`;
      bar.append(fill);
      card.append(title, score, coverage, bar);
      grid.append(card);
    });
  };

  const indicatorMatchesFilter = (row, filter, dimension) => {
    const stateMatches = filter === "all"
      || (filter === "attention" && ["fail", "warning"].includes(row.dataset.state))
      || (filter === "pass" && row.dataset.state === "pass")
      || (filter === "unknown" && ["unknown", "not_applicable", "context"].includes(row.dataset.state));
    return stateMatches && (dimension === "all" || row.dataset.dimension === dimension);
  };

  const applyIndicatorFilters = () => {
    const dimension = $("#indicator-dimension-filter").value;
    const rows = [...$("#indicator-list").children];
    let visible = 0;
    rows.forEach((row) => {
      row.hidden = !indicatorMatchesFilter(row, activeIndicatorFilter, dimension);
      if (!row.hidden) visible += 1;
    });
    $("#indicator-result-count").textContent = localized(`${visible} de ${rows.length}`, `${visible} of ${rows.length}`);
    $("#indicator-empty").hidden = visible !== 0;
  };

  const evidenceFieldLabels = {
    request_count: ["Requisições observadas", "Observed requests"],
    internal_request_count: ["Requisições internas", "Internal requests"],
    external_request_count: ["Requisições externas", "External requests"],
    external_request_share: ["Proporção de requisições externas", "External request share"],
    external_origin_count: ["Origens externas distintas", "Distinct external origins"],
    api_request_count: ["Requisições de API", "API requests"],
    external_api_request_count: ["Requisições de API externas", "External API requests"],
    external_api_share: ["Proporção de APIs externas", "External API share"],
    status_code: ["Status HTTP", "HTTP status"],
    http_status: ["Status HTTP", "HTTP status"],
    redirect_count: ["Redirecionamentos", "Redirects"],
    attempt_count: ["Tentativas", "Attempts"],
    bytes_observed: ["Bytes observados", "Observed bytes"],
    response_bytes: ["Bytes recebidos", "Received bytes"],
    content_length: ["Tamanho declarado", "Declared size"],
    content_length_matches_observed: ["Tamanho declarado confere", "Declared size matches"],
    termination_reason: ["Motivo do término", "Termination reason"],
    outcome: ["Resultado da coleta", "Collection outcome"],
    parseable: ["Conteúdo analisável", "Parseable content"],
    parse_result: ["Resultado do parser", "Parser result"],
    directive_count: ["Diretivas encontradas", "Found directives"],
    declared_url_count: ["URLs declaradas", "Declared URLs"],
    urls_extracted: ["URLs extraídas", "Extracted URLs"],
    feed_count: ["Feeds encontrados", "Found feeds"],
    total: ["Total", "Total"],
    internal: ["Internas", "Internal"],
    external: ["Externas", "External"],
    truncated: ["Coleta truncada", "Collection truncated"],
    successful: ["Recuperações bem-sucedidas", "Successful retrievals"],
    success_count: ["Recuperações bem-sucedidas", "Successful retrievals"],
    broken_count: ["Links quebrados", "Broken links"],
    visited: ["URLs visitadas", "Visited URLs"],
    enqueued: ["URLs incluídas na fila", "Queued URLs"],
    discovered_references: ["Referências descobertas", "Discovered references"],
    additional_url_count: ["URLs adicionais após renderização", "Additional URLs after rendering"],
    raw_url_count: ["URLs no HTML original", "URLs in raw HTML"],
    rendered_url_count: ["URLs após renderização", "URLs after rendering"],
    rendered_gain_share: ["Ganho de URLs após renderização", "Rendered URL gain"],
    dependency_graph_observed: ["Grafo de dependências observado", "Dependency graph observed"],
    external_script: ["Scripts externos", "External scripts"],
    external_stylesheet: ["Folhas de estilo externas", "External stylesheets"],
    external_image: ["Imagens externas", "External images"],
    external_font: ["Fontes externas", "External fonts"],
    external_api: ["APIs externas", "External APIs"],
    external_frame: ["Frames externos", "External frames"],
    external_other: ["Outros recursos externos", "Other external resources"],
    rendered_content_changed: ["Conteúdo renderizado mudou", "Rendered content changed"],
    raw_dom_bytes: ["Tamanho do HTML original", "Raw HTML size"],
    rendered_dom_bytes: ["Tamanho após renderização", "Rendered content size"],
    dom_delta_share: ["Variação do conteúdo após renderização", "Rendered content change"],
    elapsed_milliseconds: ["Duração observada", "Observed duration"],
    sample_size: ["Tamanho da amostra", "Sample size"],
    denominator: ["Links internos verificados", "Checked internal links"],
    success_rate: ["Taxa de sucesso", "Success rate"],
    status_classes: ["Classes de status HTTP", "HTTP status classes"],
    failure_classes: ["Classes de falha", "Failure classes"],
    outcome_distribution: ["Distribuição dos resultados", "Outcome distribution"],
    reason: ["Motivo", "Reason"],
  };

  const evidenceValueLabels = {
    browser_observation_unavailable: ["observação do navegador indisponível", "browser observation unavailable"],
    observation_schema_does_not_support_measurement: ["a versão da observação não oferece esta medição", "the observation version does not provide this measurement"],
    measurement_not_implemented: ["esta medição ainda não está implementada na versão atual", "this measurement is not yet implemented in the current version"],
    complete: ["coleta concluída", "collection completed"],
    response: ["resposta recebida", "response received"],
  };

  const evidenceFieldLabel = (path) => {
    const key = path.split(".").at(-1);
    const translated = evidenceFieldLabels[key];
    if (translated) return localized(...translated);
    return key.replaceAll("_", " ").replace(/^./, (letter) => letter.toUpperCase());
  };

  const formatEvidenceValue = (path, value) => {
    if (value === null || value === undefined) return localized("Não disponível", "Unavailable");
    if (typeof value === "boolean") return value ? localized("Sim", "Yes") : localized("Não", "No");
    const key = path.split(".").at(-1);
    if (typeof value === "number" && (key.endsWith("_share") || key.endsWith("_rate"))) {
      return new Intl.NumberFormat(isPortuguese ? "pt-BR" : "en", { style: "percent", maximumFractionDigits: 1 }).format(value);
    }
    if (typeof value === "number" && key.includes("milliseconds")) return `${new Intl.NumberFormat(isPortuguese ? "pt-BR" : "en").format(value)} ms`;
    if (Array.isArray(value)) return value.join(", ");
    const translated = evidenceValueLabels[String(value)];
    return translated ? localized(...translated) : String(value).replaceAll("_", " ");
  };

  const flattenEvidenceData = (value, prefix = "", depth = 0) => {
    if (!value || typeof value !== "object" || depth > 2) return [];
    return Object.entries(value).flatMap(([key, item]) => {
      const path = prefix ? `${prefix}.${key}` : key;
      if (key.includes("hash") || ["budgets", "limits", "threshold_basis", "profile_id", "parser_version", "external_origin_evidence", "external_origin_evidence_truncated"].includes(key)) return [];
      if (item && typeof item === "object" && !Array.isArray(item)) return flattenEvidenceData(item, path, depth + 1);
      return [[path, item]];
    });
  };

  const evidenceNarrative = (indicatorId, evidence) => {
    const data = evidence?.data || {};
    if (data.reason) {
      return localized(
        `Não foi possível medir este indicador nesta execução: ${formatEvidenceValue("reason", data.reason)}.`,
        `This indicator could not be measured in this run: ${formatEvidenceValue("reason", data.reason)}.`,
      );
    }
    if (indicatorId === "C01" && Number.isFinite(data.request_count)) {
      const share = formatEvidenceValue("external_request_share", data.external_request_share || 0);
      const requests = localized(data.request_count === 1 ? "requisição" : "requisições", data.request_count === 1 ? "request" : "requests");
      const externalRequests = localized(data.external_request_count === 1 ? "foi externa" : "foram externas", data.external_request_count === 1 ? "was external" : "were external");
      const origins = localized(data.external_origin_count === 1 ? "origem externa" : "origens externas", data.external_origin_count === 1 ? "external origin" : "external origins");
      return localized(
        `O navegador fez ${data.request_count} ${requests}. ${data.external_request_count || 0} ${externalRequests} (${share}), distribuídas em ${data.external_origin_count || 0} ${origins}. Caminhos, parâmetros e fragmentos não são armazenados.`,
        `The browser made ${data.request_count} ${requests}. ${data.external_request_count || 0} ${externalRequests} (${share}), across ${data.external_origin_count || 0} ${origins}. Paths, parameters, and fragments are not stored.`,
      );
    }
    if (indicatorId === "R01" && Number.isFinite(data.sample_size)) {
      const rate = formatEvidenceValue("success_rate", Number(data.success_rate || 0));
      return localized(
        `${data.success_count || 0} de ${data.sample_size} recuperações observadas tiveram resultado de sucesso (${rate}). A evidência abaixo também explicita a distribuição das respostas e falhas da amostra.`,
        `${data.success_count || 0} of ${data.sample_size} observed retrievals succeeded (${rate}). The evidence below also shows the response and failure distribution for the sample.`,
      );
    }
    if (indicatorId === "R03" && Number.isFinite(data.denominator)) {
      return localized(
        `${data.broken_count || 0} de ${data.denominator} links internos verificados foram classificados como quebrados. A coleta preserva as contagens e classes de resultado, mas não os endereços individuais dos links.`,
        `${data.broken_count || 0} of ${data.denominator} checked internal links were classified as broken. The collection preserves result counts and classes, but not individual link addresses.`,
      );
    }
    if (indicatorId === "O05" && Number.isFinite(data.raw_dom_bytes) && Number.isFinite(data.rendered_dom_bytes)) {
      const delta = formatEvidenceValue("dom_delta_share", Number(data.dom_delta_share || 0));
      const additionalUrls = Number(data.additional_url_count || 0);
      const apiRequests = Number(data.api_request_count || 0);
      return localized(
        `O conteúdo passou de ${formatEvidenceValue("raw_dom_bytes", data.raw_dom_bytes)} para ${formatEvidenceValue("rendered_dom_bytes", data.rendered_dom_bytes)} bytes após a execução de JavaScript, uma variação de ${delta}. Também foram observadas ${additionalUrls} ${additionalUrls === 1 ? "URL adicional" : "URLs adicionais"} e ${apiRequests} ${apiRequests === 1 ? "requisição de API" : "requisições de API"}.`,
        `Content changed from ${formatEvidenceValue("raw_dom_bytes", data.raw_dom_bytes)} to ${formatEvidenceValue("rendered_dom_bytes", data.rendered_dom_bytes)} bytes after JavaScript ran, a ${delta} change. The browser also observed ${additionalUrls} additional ${additionalUrls === 1 ? "URL" : "URLs"} and ${apiRequests} API ${apiRequests === 1 ? "request" : "requests"}.`,
      );
    }
    return localized(
      "A conclusão foi calculada a partir dos valores agregados observados abaixo. Nenhum corpo de página, cookie ou endereço individual faz parte desta evidência.",
      "The conclusion was calculated from the aggregate values observed below. No page body, cookie, or individual address is included in this evidence.",
    );
  };

  const formatEvidenceNumber = (value) => new Intl.NumberFormat(isPortuguese ? "pt-BR" : "en").format(Number(value || 0));

  const formatDistribution = (distribution) => {
    const entries = Object.entries(distribution || {}).filter(([, count]) => Number(count) > 0);
    return entries.length
      ? entries.map(([label, count]) => `${label}: ${formatEvidenceNumber(count)}`).join(" · ")
      : localized("Nenhuma", "None");
  };

  const renderExplicitMeasurements = (indicatorId, evidence) => {
    if (!["R01", "R03", "O05"].includes(indicatorId)) return null;
    const data = evidence?.data || {};
    const block = document.createElement("div");
    block.className = "explicit-measurement-evidence";
    const title = document.createElement("h5");
    title.textContent = localized("Evidência explícita do resultado", "Explicit result evidence");
    const values = document.createElement("dl");
    const appendValue = (ptLabel, enLabel, value) => {
      const term = document.createElement("dt");
      term.textContent = localized(ptLabel, enLabel);
      const description = document.createElement("dd");
      description.textContent = value;
      values.append(term, description);
    };
    block.append(title, values);

    if (indicatorId === "R01") {
      appendValue(
        "Recuperações bem-sucedidas",
        "Successful retrievals",
        `${formatEvidenceNumber(data.success_count)} / ${formatEvidenceNumber(data.sample_size)} (${formatEvidenceValue("success_rate", Number(data.success_rate || 0))})`,
      );
      appendValue("Respostas HTTP observadas", "Observed HTTP responses", formatDistribution(data.outcome_distribution?.status_classes));
      appendValue("Falhas de transporte ou ferramenta", "Transport or tool failures", formatDistribution(data.outcome_distribution?.failure_classes));
      appendValue("Tentativas de coleta", "Collection attempts", formatEvidenceNumber(data.attempt_count));
    }

    if (indicatorId === "R03") {
      appendValue(
        "Links internos quebrados",
        "Broken internal links",
        `${formatEvidenceNumber(data.broken_count)} / ${formatEvidenceNumber(data.denominator)}`,
      );
      appendValue("Respostas HTTP na coleta", "HTTP responses in the crawl", formatDistribution(data.status_classes));
      appendValue("Falhas de transporte ou ferramenta", "Transport or tool failures", formatDistribution(data.failure_classes));
      const note = document.createElement("p");
      note.className = "explicit-evidence-note";
      note.textContent = localized(
        "Limitação: esta versão não persiste as URLs individuais dos links quebrados. Portanto, a evidência explícita disponível é agregada por quantidade e classe de resultado.",
        "Limitation: this version does not persist the individual URLs of broken links. The available explicit evidence is therefore aggregated by count and result class.",
      );
      block.append(note);
    }

    if (indicatorId === "O05") {
      const rawBytes = Number(data.raw_dom_bytes || 0);
      const renderedBytes = Number(data.rendered_dom_bytes || 0);
      const byteDifference = renderedBytes - rawBytes;
      const signedDifference = `${byteDifference >= 0 ? "+" : "−"}${formatEvidenceNumber(Math.abs(byteDifference))}`;
      appendValue("HTML antes do JavaScript", "HTML before JavaScript", `${formatEvidenceNumber(rawBytes)} bytes`);
      appendValue("Conteúdo após renderização", "Content after rendering", `${formatEvidenceNumber(renderedBytes)} bytes`);
      appendValue("Variação observada", "Observed change", `${formatEvidenceValue("dom_delta_share", Number(data.dom_delta_share || 0))} (${signedDifference} bytes)`);
      appendValue("URLs adicionais", "Additional URLs", formatEvidenceNumber(data.additional_url_count));
      appendValue("Requisições de API", "API requests", formatEvidenceNumber(data.api_request_count));
      const note = document.createElement("p");
      note.className = "explicit-evidence-note";
      note.textContent = localized(
        "Interpretação: a mudança foi observada após executar JavaScript. Isso indica dependência dinâmica, mas não prova, isoladamente, que todo o conteúdo dependa de JavaScript.",
        "Interpretation: the change was observed after JavaScript ran. This indicates dynamic dependency, but does not by itself prove that all content depends on JavaScript.",
      );
      block.append(note);
    }
    return block;
  };

  const renderExplicitOrigins = (indicatorId, evidence) => {
    if (!["C01", "C04", "C05"].includes(indicatorId)) return null;
    const data = evidence?.data || {};
    const block = document.createElement("div");
    block.className = "explicit-origin-evidence";
    const title = document.createElement("h5");
    title.textContent = localized("Origens externas observadas", "Observed external origins");
    block.append(title);
    const origins = Array.isArray(data.external_origin_evidence)
      ? data.external_origin_evidence
      : [];
    if (!origins.length) {
      const expectedCount = indicatorId === "C05"
        ? Number(data.external_api_request_count || 0)
        : indicatorId === "C04"
          ? ["external_script", "external_stylesheet", "external_font", "external_api", "external_frame"]
            .reduce((total, key) => total + Number(data.dependency_edges?.[key] || 0), 0)
          : Number(data.external_request_count || 0);
      const message = document.createElement("p");
      message.textContent = expectedCount === 0
        ? localized(
          "Nenhuma origem externa foi observada para este indicador.",
          "No external origin was observed for this indicator.",
        )
        : localized(
          "Os nomes das origens não foram preservados nesta execução. Faça uma nova avaliação para produzir esta evidência explícita.",
          "Origin names were not preserved in this run. Start a new assessment to produce this explicit evidence.",
        );
      block.append(message);
      return block;
    }
    const typeLabels = {
      api_count: ["API", "APIs", "API", "APIs"],
      script_count: ["script", "scripts", "script", "scripts"],
      stylesheet_count: ["folha de estilo", "folhas de estilo", "stylesheet", "stylesheets"],
      image_count: ["imagem", "imagens", "image", "images"],
      font_count: ["fonte", "fontes", "font", "fonts"],
      frame_count: ["frame", "frames", "frame", "frames"],
      other_count: ["outro", "outros", "other", "other"],
    };
    const list = document.createElement("ul");
    origins.forEach((origin) => {
      const item = document.createElement("li");
      const address = document.createElement("code");
      address.textContent = origin.origin;
      const types = Object.entries(typeLabels)
        .filter(([key]) => Number(origin[key]) > 0)
        .map(([key, labels]) => {
          const pluralOffset = Number(origin[key]) === 1 ? 0 : 1;
          const label = isPortuguese ? labels[pluralOffset] : labels[2 + pluralOffset];
          return `${origin[key]} ${label}`;
        });
      const detail = document.createElement("span");
      detail.textContent = localized(
        `${origin.request_count} ${origin.request_count === 1 ? "requisição" : "requisições"} · ${types.join(" · ")}`,
        `${origin.request_count} ${origin.request_count === 1 ? "request" : "requests"} · ${types.join(" · ")}`,
      );
      item.append(address, detail);
      list.append(item);
    });
    block.append(list);
    const privacy = document.createElement("p");
    privacy.className = "explicit-origin-note";
    privacy.textContent = localized(
      "Evidência limitada à origem (protocolo, domínio e porta); caminhos, parâmetros e fragmentos foram removidos.",
      "Evidence is limited to the origin (scheme, domain, and port); paths, parameters, and fragments were removed.",
    );
    block.append(privacy);
    if (data.external_origin_evidence_truncated) {
      const truncated = document.createElement("p");
      truncated.className = "explicit-origin-note";
      truncated.textContent = localized(
        "A lista atingiu o limite de 100 origens e mostra as mais frequentes.",
        "The list reached the 100-origin limit and shows the most frequent origins.",
      );
      block.append(truncated);
    }
    return block;
  };

  const renderEvidence = (result, evidenceById) => {
    const evidenceItems = (result.evidence_ids || []).map((id) => evidenceById.get(id)).filter(Boolean);
    const panel = document.createElement("section");
    panel.className = "indicator-evidence";
    const title = document.createElement("h4");
    title.textContent = localized("O que foi observado", "What was observed");
    panel.append(title);
    if (!evidenceItems.length) {
      const unavailable = document.createElement("p");
      unavailable.textContent = localized("Os detalhes desta evidência não estão disponíveis neste relatório.", "Evidence details are not available in this report.");
      panel.append(unavailable);
      return panel;
    }
    evidenceItems.forEach((evidence, index) => {
      const narrative = document.createElement("p");
      narrative.className = "evidence-narrative";
      narrative.textContent = evidenceNarrative(result.indicator_id, evidence);
      panel.append(narrative);
      const explicitOrigins = renderExplicitOrigins(result.indicator_id, evidence);
      if (explicitOrigins) panel.append(explicitOrigins);
      const explicitMeasurements = renderExplicitMeasurements(result.indicator_id, evidence);
      if (explicitMeasurements) panel.append(explicitMeasurements);
      const facts = flattenEvidenceData(evidence.data).slice(0, 12);
      if (facts.length) {
        const values = document.createElement("dl");
        values.className = "evidence-facts";
        facts.forEach(([path, value]) => {
          const term = document.createElement("dt");
          term.textContent = evidenceFieldLabel(path);
          const description = document.createElement("dd");
          description.textContent = formatEvidenceValue(path, value);
          values.append(term, description);
        });
        panel.append(values);
      }
      const trace = document.createElement("details");
      trace.className = "evidence-trace";
      const traceTitle = document.createElement("summary");
      traceTitle.textContent = localized(`Rastreabilidade técnica${evidenceItems.length > 1 ? ` ${index + 1}` : ""}`, `Technical trace${evidenceItems.length > 1 ? ` ${index + 1}` : ""}`);
      const source = (evidence.sources || [])[0];
      const traceText = document.createElement("p");
      traceText.textContent = localized(
        `Método ${evidence.method_id}@${evidence.method_version}; observada em ${formatDate(source?.observed_at || evidence.created_at)} por ${source?.tool_name || "probe"} ${source?.tool_version || ""}; confiança ${Math.round((evidence.confidence || 0) * 100)}%; evidência ${evidence.evidence_id}.`,
        `Method ${evidence.method_id}@${evidence.method_version}; observed ${formatDate(source?.observed_at || evidence.created_at)} by ${source?.tool_name || "probe"} ${source?.tool_version || ""}; ${Math.round((evidence.confidence || 0) * 100)}% confidence; evidence ${evidence.evidence_id}.`,
      );
      trace.append(traceTitle, traceText);
      panel.append(trace);
    });
    return panel;
  };

  const renderIndicators = (results, catalogById, evidenceItems) => {
    const section = $("#findings-section");
    section.hidden = !results.length;
    const list = $("#indicator-list");
    const evidenceById = new Map((evidenceItems || []).map((item) => [item.evidence_id, item]));
    list.replaceChildren();
    $("#nav-indicator-count").textContent = results.length;
    $("#filter-all-count").textContent = results.length;
    $("#filter-attention-count").textContent = results.filter((result) => ["fail", "warning"].includes(result.state)).length;
    $("#filter-pass-count").textContent = results.filter((result) => result.state === "pass").length;
    $("#filter-unknown-count").textContent = results.filter((result) => ["unknown", "not_applicable", "context"].includes(result.state)).length;

    const dimensionSelect = $("#indicator-dimension-filter");
    const selectedDimension = dimensionSelect.value || "all";
    const dimensions = [...new Set(results.map((result) => catalogById.get(result.indicator_id)?.dimension || "cross_cutting"))];
    dimensionSelect.replaceChildren();
    const allOption = document.createElement("option");
    allOption.value = "all";
    allOption.textContent = t("indicators.allDimensions");
    dimensionSelect.append(allOption);
    dimensions.sort((left, right) => dimensionLabel(left).localeCompare(dimensionLabel(right), isPortuguese ? "pt-BR" : "en")).forEach((dimension) => {
      const option = document.createElement("option");
      option.value = dimension;
      option.textContent = dimension === "cross_cutting" ? t("common.crossCutting") : dimensionLabel(dimension);
      dimensionSelect.append(option);
    });
    dimensionSelect.value = dimensions.includes(selectedDimension) ? selectedDimension : "all";

    results.forEach((result) => {
      const row = document.createElement("details");
      row.className = "indicator-row";
      row.dataset.state = result.state;
      const id = document.createElement("span");
      id.className = "indicator-id";
      const definition = i18n.localizeIndicator(catalogById.get(result.indicator_id));
      const dimension = definition?.dimension || "cross_cutting";
      row.dataset.dimension = dimension;
      id.textContent = result.indicator_id;
      const summary = document.createElement("summary");
      summary.className = "indicator-summary";
      const heading = document.createElement("span");
      heading.className = "indicator-heading";
      const name = document.createElement("strong");
      name.textContent = definition?.name || localized("Indicador metodológico", "Methodology indicator");
      const meta = document.createElement("small");
      meta.textContent = `${dimension === "cross_cutting" ? t("common.crossCutting") : dimensionLabel(dimension)} · ${t("common.confidence", { value: Math.round((result.confidence || 0) * 100) })}`;
      heading.append(name, meta);
      const state = document.createElement("span");
      state.className = `indicator-state state-${result.state}`;
      state.textContent = stateLabel(result.state);
      summary.append(id, heading, state);
      const detail = document.createElement("div");
      detail.className = "indicator-detail";
      const description = document.createElement("p");
      description.textContent = definition?.description || localized("Consulte a documentação metodológica para interpretar este indicador.", "Review the methodology documentation to interpret this indicator.");
      const measurement = document.createElement("p");
      measurement.className = "muted";
      measurement.textContent = localized(
        `Como medimos: ${definition?.measurement || "medição documentada na metodologia."}`,
        `How we measure: ${definition?.measurement || "measurement documented in the methodology."}`,
      );
      const indicatorLink = document.createElement("a");
      indicatorLink.className = "indicator-method-link";
      indicatorLink.href = definition?.path || `/methodology/indicators/${encodeURIComponent(result.indicator_id)}`;
      indicatorLink.textContent = t("indicator.viewMethod");
      detail.append(description, measurement, renderEvidence(result, evidenceById), indicatorLink);
      row.append(summary, detail);
      list.append(row);
    });
    applyIndicatorFilters();
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
      const definition = i18n.localizeIndicator(catalogById.get(result.indicator_id));
      return findingCard(
        `${result.indicator_id} · ${taxonomyLabel(definition?.severity || localized("prioridade", "priority"))}`,
        definition?.name || result.indicator_id,
        localized(
          `${stateLabel(result.state)} com confiança de ${Math.round((result.confidence || 0) * 100)}%.`,
          `${stateLabel(result.state)} with ${Math.round((result.confidence || 0) * 100)}% confidence.`,
        ),
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
      note.textContent = localized(
        "Nenhuma condição impeditiva ativa foi acionada. Nesta versão candidata, os gates permanecem inativos e não alteram o resultado.",
        "No active blocking condition was triggered. In this candidate version, gates remain inactive and do not change the result.",
      );
      container.append(note);
      return;
    }
    triggered.forEach(([gateId, gate]) => {
      container.append(findingCard(gateId, localized("Condição impeditiva acionada", "Blocking condition triggered"), gate.reason));
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
      paragraph.textContent = localized(
        "CLEAR+ legado Flat e Weighted permanecem indisponíveis nesta versão: ainda não existe mapeamento cientificamente validado entre as evidências atuais e as quatro facetas originais.",
        "Legacy CLEAR+ Flat and Weighted remain unavailable in this version: there is no scientifically validated mapping between current evidence and the four original facets.",
      );
    } else {
      paragraph.textContent = localized(
        `Flat ${assessment.legacy_clear_plus.flat}; Weighted ${assessment.legacy_clear_plus.fixed_max}. Estes valores servem apenas para comparação histórica.`,
        `Flat ${assessment.legacy_clear_plus.flat}; Weighted ${assessment.legacy_clear_plus.fixed_max}. These values are for historical comparison only.`,
      );
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
        localized(
          `País: ${probe.country}; região: ${probe.region}; observado de ${formatDate(probe.first_observed_at)} a ${formatDate(probe.last_observed_at)}. Localização declarada pela implantação, sem inferência de precisão.`,
          `Country: ${probe.country}; region: ${probe.region}; observed from ${formatDate(probe.first_observed_at)} to ${formatDate(probe.last_observed_at)}. Deployment-declared location, without a precision inference.`,
        ),
      )));
    } else {
      probeIds.forEach((probe) => cards.push(findingCard("Probe", probe, localized("Registro legado sem país ou região declarados.", "Legacy record without a declared country or region."))));
    }
    contextual.forEach((result) => {
      const definition = i18n.localizeIndicator(catalogById.get(result.indicator_id));
      cards.push(findingCard(result.indicator_id, definition?.name || result.indicator_id, localized(
        `${stateLabel(result.state)}; dado contextual, sem penalização automática do website.`,
        `${stateLabel(result.state)}; contextual data, without an automatic website penalty.`,
      )));
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
    const categoryEdges = [
      [localized("Scripts externos", "External scripts"), summary.external_script_count ?? 0],
      [localized("CSS externo", "External CSS"), summary.external_stylesheet_count ?? 0],
      [localized("Imagens externas", "External images"), summary.external_image_count ?? 0],
      [localized("Fontes externas", "External fonts"), summary.external_font_count ?? 0],
      [localized("APIs externas", "External APIs"), summary.external_api_request_count ?? 0],
      [localized("Frames externos", "External frames"), summary.external_frame_count ?? 0],
      [localized("Outros externos", "Other external resources"), summary.external_other_count ?? 0],
    ].filter(([, count]) => count > 0);
    const explicitOrigins = Array.isArray(summary.external_origin_evidence)
      ? summary.external_origin_evidence
      : [];
    const originDetail = Number(summary.external_request_count || 0) === 0
      ? localized("Nenhuma requisição externa observada.", "No external request was observed.")
      : explicitOrigins.length
        ? localized(
          `${explicitOrigins.length} origens identificadas; somente protocolo, domínio e porta são preservados.`,
          `${explicitOrigins.length} origins identified; only scheme, domain, and port are retained.`,
        )
        : localized(
          "Os nomes não foram preservados nesta execução antiga; faça uma nova avaliação.",
          "Names were not retained in this older run; start a new assessment.",
        );
    container.replaceChildren(
      findingCard(localized("Site → origens externas", "Site → external origins"), String(summary.external_origin_count ?? 0), originDetail),
      ...categoryEdges.map(([label, count]) => findingCard(`Site → ${label}`, String(count), localized("Aresta agregada por tipo; presença externa não implica falha.", "Edge aggregated by type; external presence does not imply failure."))),
      findingCard(localized("URLs dinâmicas", "Dynamic URLs"), String(summary.additional_url_count ?? "—"), localized("URLs adicionais após renderização, sem persistir endereços brutos.", "Additional URLs after rendering, without persisting raw addresses.")),
    );
  };

  const renderTechnicalDetails = (report) => {
    const cards = [];
    (report.crawl_runs || []).forEach((run) => cards.push(findingCard(
      `Crawl · ${run.probe_id}`,
      localized(`${run.summary.visited ?? 0} URL(s) visitadas`, `${run.summary.visited ?? 0} URL(s) visited`),
      localized(`Término: ${run.summary.termination_reason}; bytes observados: ${run.summary.response_bytes ?? 0}.`, `Termination: ${run.summary.termination_reason}; observed bytes: ${run.summary.response_bytes ?? 0}.`),
    )));
    (report.browser_runs || []).forEach((run) => cards.push(findingCard(
      `Browser · ${run.probe_id}`,
      run.profile_id,
      localized(`Término: ${run.summary.termination_reason}; duração: ${run.summary.elapsed_milliseconds ?? 0} ms.`, `Termination: ${run.summary.termination_reason}; duration: ${run.summary.elapsed_milliseconds ?? 0} ms.`),
    )));
    const section = $("#technical-section");
    section.hidden = !cards.length;
    $("#technical-content").replaceChildren(...cards);
  };

  const renderRecommendations = (report, catalogById) => {
    const actionable = (report.indicator_results || []).filter((result) => ["fail", "warning"].includes(result.state));
    const evidenceById = new Map((report.evidence || []).map((item) => [item.evidence_id, item]));
    const section = $("#recommendations-section");
    section.hidden = !actionable.length;
    $("#nav-recommendation-count").textContent = actionable.length;
    const container = $("#recommendations-content");
    container.replaceChildren(...actionable.map((result) => {
      const definition = i18n.localizeIndicator(catalogById.get(result.indicator_id)) || {};
      const card = document.createElement("details");
      card.className = "recommendation-card";
      const summary = document.createElement("summary");
      summary.className = "recommendation-summary";
      const title = document.createElement("h3");
      title.textContent = `${result.indicator_id} — ${definition.name || localized("Condição observada", "Observed condition")}`;
      const state = document.createElement("span");
      state.className = `indicator-state state-${result.state}`;
      state.textContent = stateLabel(result.state);
      summary.append(title, state);
      const details = document.createElement("dl");
      const primaryEvidence = (result.evidence_ids || []).map((id) => evidenceById.get(id)).find(Boolean);
      const rows = [
        [localized("Problema", "Issue"), stateLabel(result.state)],
        [localized("Evidência", "Evidence"), primaryEvidence
          ? `${evidenceNarrative(result.indicator_id, primaryEvidence)} ${t("common.confidence", { value: Math.round((result.confidence || 0) * 100) })}.`
          : localized("Detalhes da evidência indisponíveis.", "Evidence details unavailable.")],
        [localized("Impacto", "Impact"), definition.description || localized("Pode afetar a captura ou a interpretação do resultado.", "May affect capture or interpretation of the result.")],
        [localized("Possível correção", "Possible fix"), localized(`Revisar a condição pelo método: ${definition.measurement || "consulte a documentação do indicador"}.`, `Review the condition using this method: ${definition.measurement || "see the indicator documentation"}.`)],
        [localized("Responsabilidade", "Responsibility"), definition.classification === "intrinsic" ? localized("Depende principalmente do administrador do site.", "Depends mainly on the website administrator.") : definition.classification === "observation_context" ? localized("Depende principalmente do contexto/probe de coleta.", "Depends mainly on the collection context/probe.") : localized("Pode depender do site e da configuração do coletor.", "May depend on the website and collector configuration.")],
      ];
      rows.forEach(([term, value]) => {
        const dt = document.createElement("dt");
        dt.textContent = term;
        const dd = document.createElement("dd");
        dd.textContent = value;
        details.append(dt, dd);
      });
      card.append(summary, details);
      return card;
    }));
  };

  const renderReport = (report, catalog) => {
    const catalogById = new Map((catalog.items || []).map((item) => [item.id, i18n.localizeIndicator(item)]));
    $("#report-origin").textContent = report.target_origin;
    $("#report-title").textContent = terminalStates.has(report.state) ? t("report.title") : t("report.pending");
    $("#report-updated").textContent = t("common.updated", { date: formatDate(report.updated_at) });
    $("#cancel-button").hidden = terminalStates.has(report.state);
    renderProgress(report);
    renderAssessment(report);
    renderIndicators(report.indicator_results || [], catalogById, report.evidence || []);
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
      item.textContent = i18n.localizeLimitation(text);
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
    content.textContent = localized("Buscando execução compatível…", "Looking for a compatible run…");
    try {
      const comparison = await request(`/v1/analyses/${encodeURIComponent(analysisId)}/comparison`);
      content.replaceChildren();
      const summary = document.createElement("p");
      summary.textContent = comparison.classification === "observed_change"
        ? localized(`${comparison.indicator_changes.length} indicador(es) mudaram desde a execução anterior.`, `${comparison.indicator_changes.length} indicator(s) changed since the previous run.`)
        : localized("Nenhuma mudança foi observada nos indicadores comparáveis.", "No changes were observed in comparable indicators.");
      content.append(summary);
      comparison.indicator_changes.forEach((change) => {
        const row = document.createElement("p");
        row.textContent = `${change.indicator_id}: ${stateLabel(change.baseline_state)} → ${stateLabel(change.current_state)}`;
        content.append(row);
      });
    } catch (error) {
      content.textContent = error.code === "NOT_FOUND" ? localized("Ainda não existe uma execução anterior compatível.", "There is no compatible previous run yet.") : errorMessage(error);
    }
  };

  const cancelAnalysis = async (analysisId) => {
    const button = $("#cancel-button");
    const status = $("#report-status");
    button.disabled = true;
    status.textContent = localized("Cancelando avaliação…", "Cancelling assessment…");
    try {
      await request(`/v1/analyses/${encodeURIComponent(analysisId)}/cancel`, { method: "POST" });
      await loadReport(analysisId);
    } catch (error) {
      status.textContent = errorMessage(error);
      button.disabled = false;
    }
  };

  const initialize = () => {
    i18n.translateDocument();
    document.querySelectorAll("[data-locale]").forEach((button) => {
      button.addEventListener("click", () => i18n.setLocale(button.dataset.locale));
    });
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
      document.querySelectorAll("[data-indicator-filter]").forEach((button) => {
        button.addEventListener("click", () => {
          activeIndicatorFilter = button.dataset.indicatorFilter;
          document.querySelectorAll("[data-indicator-filter]").forEach((candidate) => {
            const active = candidate === button;
            candidate.classList.toggle("active", active);
            candidate.setAttribute("aria-pressed", String(active));
          });
          applyIndicatorFilters();
        });
      });
      $("#indicator-dimension-filter").addEventListener("change", applyIndicatorFilters);
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
      status.textContent = localized("Criando avaliação…", "Creating assessment…");
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
