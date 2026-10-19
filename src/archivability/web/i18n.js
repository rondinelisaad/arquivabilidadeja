(() => {
  "use strict";

  const messages = {
    pt: {
      "common.skip": "Pular para o conteúdo", "common.home": "Início", "nav.main": "Navegação principal", "nav.structural": "Navegação estrutural", "method.version": "Versão 0.1.0", "brand.home": "Arquivabilidade Já — início", "home.placeholder": "https://exemplo.org",
      "language.label": "Idioma da interface",
      "nav.methodology": "Metodologia",
      "home.eyebrow": "Diagnóstico aberto para preservação da Web",
      "home.title": "Seu site está pronto para ser arquivado?",
      "home.lede": "Informe uma URL. Nós observamos acesso, descoberta, crawl e renderização sem armazenar o conteúdo das páginas.",
      "home.urlLabel": "Endereço do website",
      "home.submit": "Avaliar agora",
      "home.help": "Use uma URL pública com http:// ou https://.",
      "home.processTitle": "Uma leitura em quatro movimentos",
      "home.step1": "Metadados HTTP", "home.step2": "Crawl limitado", "home.step3": "Browser isolado", "home.step4": "Evidências e score",
      "home.privacy": "Sem screenshots, DOM bruto, cookies ou corpos de resposta no relatório.",
      "history.eyebrow": "Seu acervo de avaliações", "history.title": "Histórico recente", "history.refresh": "Atualizar",
      "report.breadcrumb": "Avaliação", "report.pending": "Avaliação em andamento", "report.title": "Relatório de arquivabilidade",
      "report.new": "Nova avaliação", "report.export": "Baixar JSON", "report.compare": "Comparar anterior", "report.cancel": "Cancelar avaliação",
      "report.nav": "Seções do relatório", "report.summary": "Resumo", "report.indicators": "Indicadores", "report.recommendations": "Recomendações", "report.dependencies": "Dependências", "report.execution": "Execução",
      "progress.title": "Progresso", "progress.preparing": "Preparando", "progress.http": "Coleta HTTP", "progress.browser": "Browser isolado", "progress.derive": "Derivação", "progress.report": "Relatório",
      "assessment.eyebrow": "Resumo", "assessment.title": "Resultado da avaliação", "assessment.coverage": "Cobertura da avaliação",
      "assessment.calculating": "Calculando indicadores avaliados…", "assessment.explanation": "Cobertura indica quanto foi possível observar nesta execução. Não é uma nota única de arquivabilidade.", "assessment.profile": "Perfil por dimensão",
      "critical.eyebrow": "Prioridade de revisão", "critical.title": "Achados críticos",
      "blocking.eyebrow": "Condições declarativas", "blocking.title": "Condições impeditivas",
      "legacy.eyebrow": "Comparabilidade histórica", "legacy.title": "Comparação CLEAR+",
      "indicators.eyebrow": "Evidências derivadas", "indicators.filters": "Filtros dos indicadores", "indicators.filterResult": "Filtrar por resultado",
      "indicators.all": "Todos", "indicators.issues": "Só problemas", "indicators.pass": "Atendem", "indicators.unknown": "Inconclusivos",
      "indicators.dimension": "Dimensão", "indicators.allDimensions": "Todas", "indicators.empty": "Nenhum indicador corresponde aos filtros selecionados.",
      "network.eyebrow": "Sem penalização geográfica automática", "network.title": "Contexto de rede e probes",
      "dependencies.eyebrow": "Visão agregada", "dependencies.title": "Dependências observadas",
      "technical.eyebrow": "Execução reproduzível", "technical.title": "Detalhes técnicos",
      "recommendations.eyebrow": "Ações orientadas por evidência", "recommendations.title": "Recomendações",
      "comparison.eyebrow": "Leitura longitudinal", "comparison.title": "Comparação com a execução anterior",
      "limitations.title": "Como interpretar", "methodology.calloutTitle": "Metodologia",
      "methodology.calloutText": "Consulte definições, fórmulas, classificações, gates e limitações da versão usada nesta análise.", "methodology.open": "Abrir metodologia",
      "methodology.breadcrumb": "Metodologia", "methodology.eyebrow": "Método aberto e versionado", "methodology.title": "Como avaliamos a arquivabilidade",
      "methodology.loading": "Carregando a especificação metodológica…", "methodology.dimensionsEyebrow": "Perfil, não nota única", "methodology.dimensionsTitle": "Dimensões avaliadas",
      "methodology.catalogEyebrow": "Documentação verificável", "methodology.catalogTitle": "Catálogo de indicadores", "methodology.viewAll": "Ver todos", "methodology.limitations": "Limitações declaradas",
      "indicator.definition": "Definição", "indicator.importance": "Por que importa", "indicator.measurement": "Medição", "indicator.evidence": "Evidências necessárias",
      "indicator.classification": "Classificação", "indicator.scoring": "Pontuação", "indicator.limitations": "Limitações", "indicator.references": "Referências", "indicator.viewMethod": "Ver definição e método",
      "footer": "Software livre para diagnósticos transparentes. Resultados indicam evidências observadas, não garantias de preservação.",
      "common.loadingHistory": "Carregando histórico…", "common.noHistory": "Nenhuma avaliação encontrada nesta conta.", "common.updated": "Atualizado em {date}",
      "common.confidence": "Confiança {value}%", "common.coverage": "Cobertura {value}%", "common.crossCutting": "Contexto transversal",
      "error.auth": "Sua sessão não está autenticada. Entre pelo provedor institucional e tente novamente.", "error.rate": "O limite de solicitações foi atingido. Aguarde um pouco e tente novamente.",
      "error.notFound": "A avaliação solicitada não foi encontrada.", "error.generic": "Não foi possível concluir a solicitação agora."
    },
    en: {
      "common.skip": "Skip to content", "common.home": "Home", "nav.main": "Main navigation", "nav.structural": "Breadcrumb", "method.version": "Version 0.1.0", "brand.home": "Arquivabilidade Já — home", "home.placeholder": "https://example.org",
      "language.label": "Interface language",
      "nav.methodology": "Methodology",
      "home.eyebrow": "Open diagnostics for Web preservation",
      "home.title": "Is your website ready to be archived?",
      "home.lede": "Enter a URL. We observe access, discovery, crawling, and rendering without storing page content.",
      "home.urlLabel": "Website address", "home.submit": "Assess now", "home.help": "Use a public URL starting with http:// or https://.",
      "home.processTitle": "An assessment in four stages", "home.step1": "HTTP metadata", "home.step2": "Bounded crawl", "home.step3": "Isolated browser", "home.step4": "Evidence and score",
      "home.privacy": "No screenshots, raw DOM, cookies, or response bodies are included in the report.",
      "history.eyebrow": "Your assessment archive", "history.title": "Recent history", "history.refresh": "Refresh",
      "report.breadcrumb": "Assessment", "report.pending": "Assessment in progress", "report.title": "Archivability report",
      "report.new": "New assessment", "report.export": "Download JSON", "report.compare": "Compare previous", "report.cancel": "Cancel assessment",
      "report.nav": "Report sections", "report.summary": "Summary", "report.indicators": "Indicators", "report.recommendations": "Recommendations", "report.dependencies": "Dependencies", "report.execution": "Execution",
      "progress.title": "Progress", "progress.preparing": "Preparing", "progress.http": "HTTP collection", "progress.browser": "Isolated browser", "progress.derive": "Derivation", "progress.report": "Report",
      "assessment.eyebrow": "Summary", "assessment.title": "Assessment result", "assessment.coverage": "Assessment coverage", "assessment.calculating": "Calculating assessed indicators…",
      "assessment.explanation": "Coverage shows how much could be observed in this run. It is not a single archivability score.", "assessment.profile": "Profile by dimension",
      "critical.eyebrow": "Review priority", "critical.title": "Critical findings", "blocking.eyebrow": "Declarative conditions", "blocking.title": "Blocking conditions",
      "legacy.eyebrow": "Historical comparability", "legacy.title": "CLEAR+ comparison",
      "indicators.eyebrow": "Derived evidence", "indicators.filters": "Indicator filters", "indicators.filterResult": "Filter by result",
      "indicators.all": "All", "indicators.issues": "Issues only", "indicators.pass": "Passing", "indicators.unknown": "Inconclusive",
      "indicators.dimension": "Dimension", "indicators.allDimensions": "All", "indicators.empty": "No indicators match the selected filters.",
      "network.eyebrow": "No automatic geographic penalty", "network.title": "Network and probe context",
      "dependencies.eyebrow": "Aggregate view", "dependencies.title": "Observed dependencies", "technical.eyebrow": "Reproducible execution", "technical.title": "Technical details",
      "recommendations.eyebrow": "Evidence-based actions", "recommendations.title": "Recommendations", "comparison.eyebrow": "Longitudinal view", "comparison.title": "Comparison with the previous run",
      "limitations.title": "How to interpret", "methodology.calloutTitle": "Methodology", "methodology.calloutText": "Review the definitions, formulas, classifications, gates, and limitations for the version used in this assessment.", "methodology.open": "Open methodology",
      "methodology.breadcrumb": "Methodology", "methodology.eyebrow": "Open, versioned method", "methodology.title": "How we assess archivability", "methodology.loading": "Loading the methodology specification…",
      "methodology.dimensionsEyebrow": "A profile, not a single score", "methodology.dimensionsTitle": "Assessed dimensions", "methodology.catalogEyebrow": "Verifiable documentation", "methodology.catalogTitle": "Indicator catalog", "methodology.viewAll": "View all", "methodology.limitations": "Declared limitations",
      "indicator.definition": "Definition", "indicator.importance": "Why it matters", "indicator.measurement": "Measurement", "indicator.evidence": "Required evidence", "indicator.classification": "Classification", "indicator.scoring": "Scoring", "indicator.limitations": "Limitations", "indicator.references": "References", "indicator.viewMethod": "View definition and method",
      "footer": "Open-source software for transparent diagnostics. Results describe observed evidence, not preservation guarantees.",
      "common.loadingHistory": "Loading history…", "common.noHistory": "No assessments were found for this account.", "common.updated": "Updated {date}", "common.confidence": "Confidence {value}%", "common.coverage": "Coverage {value}%", "common.crossCutting": "Cross-cutting context",
      "error.auth": "Your session is not authenticated. Sign in through the institutional provider and try again.", "error.rate": "The request limit has been reached. Wait a moment and try again.", "error.notFound": "The requested assessment was not found.", "error.generic": "The request could not be completed right now."
    }
  };

  const dimensionsPt = {
    discoverability: ["Descoberta", "Capacidade de um crawler descobrir URLs relevantes e caminhos de navegação."],
    retrievability: ["Recuperação", "Capacidade de recuperar recursos descobertos de forma correta e consistente."],
    standards_formats: ["Padrões e formatos", "Análise sintática, identificação e validação de formatos e riscos de preservação."],
    dependency_cohesion: ["Dependências", "Dependências externas e seu impacto demonstrado na captura e reprodução."],
    metadata_context: ["Metadados", "Metadados úteis para identificação, proveniência e interpretação futura."],
    operational_harvestability: ["Operação de coleta", "Comportamento observado durante uma campanha de coleta controlada."]
  };

  const indicatorsPt = {
    D01: ["Página inicial acessível", "A URL inicial configurada pode ser recuperada por redirecionamentos validados.", "Executar requisições GET repetidas e limitadas, validando o destino em cada redirecionamento.", "Uma única probe não permite estabelecer disponibilidade global."],
    D02: ["Disponibilidade e análise do arquivo robots", "O arquivo robots.txt pode ser recuperado e interpretado pelo parser configurado.", "Recuperar /robots.txt e analisá-lo segundo a RFC 9309.", "A ausência do arquivo, isoladamente, não impede a coleta."],
    D03: ["Política de escopo do robots", "As regras aplicáveis ao User-Agent configurado e ao escopo de coleta são avaliadas explicitamente.", "Avaliar grupos aplicáveis do robots e diretivas meta robots conforme uma política de coleta versionada.", "Diretivas robots não são controles de autorização e seu efeito depende da política de coleta."],
    D04: ["Descoberta de sitemaps", "Sitemaps são procurados em diretivas robots, locais convencionais e declarações das páginas.", "Procurar locais declarados e convencionais de sitemap e analisar índices dentro dos limites.", "Não encontrar um sitemap nos locais examinados não comprova sua ausência global."],
    D05: ["Descoberta de feeds", "Feeds RSS ou Atom são detectados por declarações HTML e verificações convencionais limitadas.", "Inspecionar declarações rel=alternate e analisar recursos RSS/Atom descobertos.", "Muitos websites válidos não publicam feeds."],
    D06: ["Rendimento de links no HTML inicial", "URLs válidas e únicas descobertas no HTML bruto são contadas por tipo e escopo.", "Analisar o HTML bruto e normalizar referências de URL sem executar JavaScript.", "Um rendimento baixo de links pode ser legítimo em algumas páginas iniciais."],
    D07: ["Ganho de URLs após renderização", "URLs adicionais expostas após a renderização no navegador são comparadas à descoberta no HTML bruto.", "Comparar URLs normalizadas do HTML bruto com URLs do DOM renderizado e das requisições de rede.", "O resultado depende do estado do navegador, das interações e da janela de observação."],
    D08: ["Cobertura da fronteira de crawl", "O crawl limitado registra URLs descobertas, visitadas e descartadas, com seus motivos.", "Acompanhar transições da fronteira conforme limites de profundidade, URLs, bytes e tempo.", "Sem um universo conhecido, esta é uma cobertura operacional, não a cobertura total do site."],
    R01: ["Taxa de sucesso na recuperação", "Recursos no escopo são agrupados por recuperação bem-sucedida, resposta HTTP e falha de transporte.", "Agregar tentativas limitadas de recuperação por classe de recurso e resultado.", "A amostra e a janela de observação afetam a taxa."],
    R02: ["Integridade dos redirecionamentos", "As cadeias de redirecionamento terminam com segurança, sem ciclos, destinos inválidos ou ambiguidade de escopo.", "Seguir uma cadeia limitada, revalidando DNS e destino conectado a cada salto.", "As cadeias podem variar por geografia, cookies e User-Agent."],
    R03: ["Links internos quebrados", "Links internos são verificados e as falhas são separadas entre causas HTTP, de rede e da ferramenta.", "Recuperar uma amostra declarada ou o conjunto completo limitado de links internos normalizados.", "HEAD pode diferir de GET e alguns links são intencionalmente efêmeros."],
    R04: ["Distribuição de status HTTP", "Resultados HTTP são preservados por classe de status, classe de recurso e tentativa.", "Agregar códigos de status finais e intermediários sem reduzir redirecionamentos a sucesso.", "Um único status não caracteriza o website."],
    R05: ["Consistência do tipo de conteúdo", "O Content-Type declarado é comparado à detecção segura por assinatura, extensão e resultado do parser.", "Comparar cabeçalhos permitidos com evidências limitadas de assinatura e parser.", "A detecção do conteúdo é heurística e precisa ser limitada."],
    R06: ["Completude da resposta", "O coletor diferencia respostas completas de truncamentos, erros de fluxo e término por limite.", "Registrar conclusão do fluxo, tamanho declarado, bytes observados e limites do coletor.", "A ausência de Content-Length é válida e não representa falha."],
    R07: ["Suporte à recuperação condicional", "O suporte a ETag e Last-Modified é registrado e pode ser testado de forma conservadora.", "Registrar validadores e, opcionalmente, emitir uma requisição condicional.", "Validadores são opcionais e podem ser fracos."],
    R08: ["Consistência do destino canônico", "A URL final, a declaração canônica e o escopo configurado são comparados.", "Comparar as URLs normalizadas de entrada, final e rel=canonical.", "Canonical é uma indicação editorial, não uma regra de acesso."],
    S01: ["Análise sintática de HTML", "Evidências de validade do HTML são separadas da análise tolerante e do impacto observado.", "Analisar com parser tolerante versionado e registrar erros estruturais e o DOM resultante.", "A quantidade de erros do validador, isoladamente, não mede o impacto arquivístico."],
    S02: ["Análise sintática de CSS", "Folhas de estilo são analisadas e os erros são relacionados ao impacto observado na renderização quando possível.", "Analisar folhas de estilo recuperadas e imports com parser versionado.", "Navegadores se recuperam de muitos erros de CSS."],
    S03: ["Validade de feeds e sitemaps XML", "Recursos XML são analisados com segurança e verificados quanto à estrutura utilizável.", "Analisar com DTD e entidades externas desativadas e registrar a extração de URLs utilizáveis.", "Schemas estritos podem rejeitar extensões válidas."],
    S04: ["Inventário de formatos", "Formatos são identificados independentemente de validação ou risco de preservação.", "Identificar formatos usando MIME declarado, extensão e assinaturas limitadas.", "O inventário é descritivo e não estabelece risco."],
    S05: ["Validação de formatos", "Formatos suportados são validados com registro de cobertura e versão do validador.", "Executar validadores específicos somente para tipos de recurso suportados.", "A disponibilidade e a qualidade dos validadores variam por formato."],
    S06: ["Risco de preservação dos formatos", "O risco é obtido de um registro versionado, não inferido pela idade ou novidade do formato.", "Relacionar formatos identificados a um registro versionado de divulgação, ferramentas e dependências.", "O registro e seus pesos exigem governança e revisão empírica."],
    S07: ["Consistência da codificação de caracteres", "Evidências de bytes, BOM e declarações HTTP e do documento são comparadas.", "Comparar codificações de caracteres declaradas e detectadas, incluindo a confiança.", "A detecção de codificação pode ser probabilística."],
    C01: ["Proporção de recursos externos", "Requisições e bytes externos são medidos por tipo de recurso, sem penalização automática por distância.", "Classificar requisições brutas e do navegador por origem, domínio registrável e tipo de recurso.", "Uma CDN delegada pode parecer externa; externalidade, isoladamente, não é falha."],
    C02: ["Diversidade de domínios externos", "Origens externas e domínios registráveis distintos são contados e classificados.", "Contar autoridades de dependência usando uma Public Suffix List versionada.", "Quantidade de domínios não equivale a risco."],
    C03: ["Concentração de dependências", "Possíveis pontos únicos de falha entre dependências essenciais conhecidas são medidos.", "Agregar a proporção de recursos essenciais por autoridade de terceiros.", "A essencialidade geralmente é desconhecida e não deve ser inferida apenas pelo volume."],
    C04: ["Conteúdo ativo de terceiros", "Scripts, estilos, frames, fontes e workers externos são representados no grafo de dependências.", "Classificar arestas de dependência ativa a partir de HTML, DOM e requisições do navegador.", "Conteúdo de terceiros não é necessariamente indisponível ou prejudicial."],
    C05: ["Dependência de API externa", "Respostas XHR, fetch ou GraphQL são correlacionadas a conteúdo renderizado relevante.", "Observar requisições de API e mudanças limitadas no DOM durante um script de interação versionado.", "Correlação não prova causalidade e depende da janela de observação."],
    C06: ["Disponibilidade de dependências essenciais", "Dependências essenciais conhecidas são verificadas em tentativas repetidas e limitadas.", "Repetir recuperação limitada de dependências declaradas ou essenciais com alta confiança.", "Falhas podem ser transitórias ou regionais."],
    M01: ["Presença de metadados descritivos", "Título, descrição, idioma e metadados estruturados são inventariados por página.", "Extrair listas permitidas e versionadas de campos HTML e metadados estruturados.", "Presença não estabelece correção ou qualidade."],
    M02: ["Consistência dos metadados", "Declarações de identidade e idioma são comparadas entre HTTP, HTML e dados estruturados.", "Comparar declarações normalizadas de metadados e registrar contradições.", "Existem variações multilíngues e de múltiplas identidades legítimas."],
    M03: ["Metadados temporais", "Datas HTTP e editoriais são registradas com sua fonte e semântica.", "Extrair declarações temporais analisáveis e identificar fonte e significado.", "Datas podem representar publicação, modificação, geração ou cache."],
    M04: ["Metadados de direitos e licença", "Sinais explícitos de direitos e licença são registrados sem conclusões jurídicas.", "Detectar sinais explícitos legíveis por máquina e no nível da página.", "Ausência não implica proibição e a interpretação jurídica está fora do escopo."],
    M05: ["Contexto de proveniência", "Organização, autoria e identificadores persistentes úteis à interpretação futura são detectados.", "Extrair declarações de proveniência de vocabulários versionados e páginas contextuais.", "As heurísticas variam consideravelmente por domínio."],
    O01: ["Taxa de timeouts persistentes", "Timeouts repetidos são separados por fase da conexão e de latência isolada.", "Executar tentativas espaçadas e limitadas e classificar a fase do timeout.", "Uma janela curta e uma probe não estabelecem persistência global."],
    O02: ["Comportamento de limitação de taxa", "Respostas 429, Retry-After e degradação relacionada a cadência conservadora são registradas.", "Observar apenas tráfego normal de crawl; nunca aumentar a carga somente para provocar limitação.", "O teste não deve criar carga prejudicial."],
    O03: ["Resultado de acesso do crawler", "Um perfil de crawler declarado é comparado a uma referência autorizada, sem disfarce.", "Comparar perfis de User-Agent versionados somente quando permitido pela política.", "Diferenças podem depender de WAF, região, cookies e momento."],
    O04: ["Desafio de autenticação", "Requisitos de autenticação para conteúdo essencial conhecido são identificados.", "Detectar respostas 401, redirecionamentos para login e barreiras de sessão sem enviar credenciais.", "Uma página de login não prova que todo conteúdo relevante esteja restrito."],
    O05: ["Dependência de JavaScript", "Diferenças materiais entre HTML bruto e conteúdo renderizado são medidas sem penalizar o JavaScript em si.", "Comparar conteúdo, links e requisições antes e depois de uma renderização limitada no navegador.", "O resultado depende do perfil do navegador, do tempo e das interações."],
    O06: ["Complexidade de interação dinâmica", "Lazy loading, rolagem, Shadow DOM, frames, WebSockets e service workers são inventariados.", "Executar um perfil de interação versionado e limitado e registrar mecanismos detectados.", "A detecção não prova que houve perda de conteúdo."],
    O07: ["Taxa de sucesso da campanha", "O sucesso geral da coleta controlada é agregado por tentativa e classe de recurso.", "Agregar resultados do crawl limitado preservando o escopo e o término por orçamento.", "O sucesso depende do escopo, dos limites, da probe e da janela de observação."],
    N01: ["Detalhamento da latência", "DNS, TCP, TLS, TTFB e tempo total são medições contextuais.", "Coletar tempos de fase quando disponíveis e registrar o motivo das fases indisponíveis.", "Reutilização de conexão e APIs do cliente podem ocultar fases individuais."],
    N02: ["Distribuição da latência", "Mediana, mínimo, máximo, desvio padrão e p95 com amostragem suficiente são informados.", "Calcular estatísticas para grupos homogêneos de URL, probe, perfil e cache.", "Amostras pequenas são instáveis; nesta metodologia candidata, p95 exige pelo menos 20 observações."],
    N03: ["Variação entre probes", "Probes comparáveis são contrastadas quanto a status, DNS e diferenças de latência.", "Comparar somente grupos de probes com configurações compatíveis.", "Diferenças sugerem, mas não comprovam, bloqueio geográfico, CDN ou causas de roteamento."],
    N04: ["Observação de DNS", "Observações A, AAAA e CNAME são preservadas com o contexto do resolvedor e da probe.", "Registrar respostas DNS limitadas e TTL, quando disponível, antes de cada conexão validada.", "DNS depende de localização e tempo."]
  };

  const resultLabels = {
    pt: { pass: "Atende", warning: "Atenção", fail: "Não atende", unknown: "Inconclusivo", not_applicable: "Não se aplica", context: "Contexto", assessable: "Avaliável", partially_assessable: "Parcialmente avaliável", not_assessable: "Não avaliável" },
    en: { pass: "Pass", warning: "Attention", fail: "Fail", unknown: "Inconclusive", not_applicable: "Not applicable", context: "Context", assessable: "Assessable", partially_assessable: "Partially assessable", not_assessable: "Not assessable" }
  };

  const limitationsPt = new Map([
    ["Scores and thresholds are a versioned project hypothesis, not a scientifically validated equivalence to CLEAR+.", "Scores e limiares são uma hipótese versionada do projeto, não uma equivalência cientificamente validada ao CLEAR+."],
    ["Candidate gates remain inactive until explicitly approved in a methodology release.", "Os gates candidatos permanecem inativos até aprovação explícita em uma versão da metodologia."],
    ["The legacy CLEAR+ comparison is unavailable until a validated mapping from current evidence to the original facets is approved.", "A comparação com o CLEAR+ legado permanece indisponível até a aprovação de um mapeamento validado entre as evidências atuais e as facetas originais."]
  ]);

  const phaseLabels = {
    pt: { requested: "Preparando", collection_queued: "Coleta na fila", collecting: "Coletando metadados", browser_queued: "Browser na fila", rendering: "Renderizando com segurança", assessment_queued: "Avaliação na fila", assessment_queued_degraded: "Avaliação parcial na fila", assessing: "Derivando evidências", completed: "Concluída", partially_completed: "Concluída parcialmente", failed: "Falhou", cancelled: "Cancelada" },
    en: { requested: "Preparing", collection_queued: "Collection queued", collecting: "Collecting metadata", browser_queued: "Browser queued", rendering: "Rendering safely", assessment_queued: "Assessment queued", assessment_queued_degraded: "Partial assessment queued", assessing: "Deriving evidence", completed: "Completed", partially_completed: "Partially completed", failed: "Failed", cancelled: "Cancelled" }
  };

  const storedLocale = (() => { try { return localStorage.getItem("archivability.locale"); } catch (_) { return null; } })();
  const queryLocale = new URLSearchParams(window.location.search).get("lang");
  const locale = ["pt", "en"].includes(queryLocale) ? queryLocale : ["pt", "en"].includes(storedLocale) ? storedLocale : "pt";
  const interpolate = (value, params) => Object.entries(params).reduce((text, [key, replacement]) => text.replaceAll(`{${key}}`, String(replacement)), value);
  const t = (key, params = {}) => interpolate(messages[locale][key] || messages.pt[key] || key, params);
  const localizeIndicator = (indicator) => {
    if (locale !== "pt" || !indicator) return indicator;
    const translation = indicatorsPt[indicator.id];
    if (!translation) return indicator;
    return { ...indicator, name: translation[0], description: translation[1], measurement: translation[2], limitations: translation[3] };
  };
  const dimension = (id, fallback) => locale === "pt" && dimensionsPt[id] ? dimensionsPt[id][0] : fallback;
  const dimensionDescription = (id, fallback) => locale === "pt" && dimensionsPt[id] ? dimensionsPt[id][1] : fallback;
  const localizeLimitation = (value) => locale === "pt" ? limitationsPt.get(value) || value : value;
  const state = (value) => phaseLabels[locale][value] || resultLabels[locale][value] || String(value || (locale === "pt" ? "indisponível" : "unavailable")).replaceAll("_", " ");
  const setLocale = (value) => {
    if (!["pt", "en"].includes(value) || value === locale) return;
    try { localStorage.setItem("archivability.locale", value); } catch (_) { /* Preference remains session-only when storage is unavailable. */ }
    const nextUrl = new URL(window.location.href);
    nextUrl.searchParams.set("lang", value);
    window.location.assign(nextUrl);
  };
  const translateDocument = () => {
    document.documentElement.lang = locale === "pt" ? "pt-BR" : "en";
    document.querySelectorAll("[data-i18n]").forEach((element) => { element.textContent = t(element.dataset.i18n); });
    document.querySelectorAll("[data-i18n-placeholder]").forEach((element) => { element.placeholder = t(element.dataset.i18nPlaceholder); });
    document.querySelectorAll("[data-i18n-aria-label]").forEach((element) => { element.setAttribute("aria-label", t(element.dataset.i18nAriaLabel)); });
    document.querySelectorAll("[data-locale]").forEach((button) => {
      const active = button.dataset.locale === locale;
      button.classList.toggle("active", active);
      button.setAttribute("aria-pressed", String(active));
    });
    const languageControl = document.querySelector(".language-control");
    if (languageControl) languageControl.setAttribute("aria-label", t("language.label"));
  };

  window.ArchivabilityI18n = Object.freeze({ locale, t, state, dimension, dimensionDescription, localizeIndicator, localizeLimitation, setLocale, translateDocument });
})();
