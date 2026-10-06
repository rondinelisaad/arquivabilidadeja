# Proposta de arquitetura e metodologia para avaliação de arquivabilidade de websites

Status: rascunho para revisão
Versão da proposta: 0.1.0-draft
Data: 2026-10-03

Este documento é a primeira entrega do projeto Arquivabilidade JA. Ele transforma o briefing em uma arquitetura e uma metodologia iniciais sem implementar a aplicação. A principal recomendação é não reduzir a conclusão a uma média: o produto deve apresentar um perfil de seis dimensões, condições impeditivas, cobertura das medições e contexto da coleta. Os dois scores CLEAR+ permanecem disponíveis apenas para comparação histórica.

## 1. Decisões, hipóteses e fontes

Todas as escolhas metodológicas usam uma das marcações abaixo.

| Marca | Significado |
|---|---|
| **CLEAR+** | Derivado do artigo ou dos valores reproduzidos no briefing. |
| **Projeto** | Decisão de arquitetura ou produto proposta para esta plataforma. |
| **Hipótese** | Relação ou limiar que precisa de validação empírica. |

O artigo integral de Banos e Manolopoulos foi conferido nesta revisão. O CLEAR+ de agosto de 2014 contém 32 avaliações: 10 de Accessibility, 10 de Standards Compliance, 6 de Cohesion e 6 de Metadata Usage. A matriz teste a teste está em [Revisão crítica e matriz de rastreabilidade do CLEAR+](revisao-critica-clear-plus.md). O artigo é tratado como referência metodológica e evidência histórica, não como especificação normativa da nova plataforma.

Referências primárias usadas nesta proposta:

- Banos, V.; Manolopoulos, Y. *A quantitative approach to evaluate Website Archivability using the CLEAR+ method*. DOI: [10.1007/s00799-015-0144-4](https://doi.org/10.1007/s00799-015-0144-4).
- IETF. [RFC 9309 — Robots Exclusion Protocol](https://www.rfc-editor.org/rfc/rfc9309.html). O protocolo orienta crawlers; não é controle de autorização.
- ISO. [ISO 28500:2017 — WARC file format](https://www.iso.org/standard/68004.html). A norma descreve o armazenamento de payloads, metadados e informações de controle da coleta.
- W3C. [Navigation Timing Level 2](https://www.w3.org/TR/navigation-timing-2/).
- Playwright. [Request timing](https://playwright.dev/docs/api/class-request#request-timing).

### 1.1 Constatações após a leitura integral do CLEAR+

- **CLEAR+:** cada avaliação recebe significância high, medium ou low, convertida respectivamente em pesos 4, 2 e 1.
- **CLEAR+:** os pesos máximos das facetas são FA=29, FS=24, FC=12 e FM=8, totalizando 73.
- **CLEAR+:** algumas verificações aparecem em mais de uma faceta: caching headers alimentam A9, S6 e M2; `Content-Type` alimenta S10 e M1.
- **CLEAR+:** A4 usa o maior tempo inicial de resposta, com 100% até 0,2 s e 0% acima de 2 s. A nova proposta rejeita essa interpretação como propriedade intrínseca.
- **CLEAR+:** o exemplo publicado exclui avaliações não aplicáveis e renormaliza os pesos disponíveis; por isso usa 29, 20, 6 e 6, com denominador 61, embora a tabela teórica totalize 73.
- **CLEAR+:** a validação da homepage usou 783 sites que possuíam RSS detectável, dez páginas por site e a métrica CLEAR+ da época. A diferença absoluta média entre homepage e média das páginas foi 3,87 pontos, mas essa amostra condicionada não prova que uma homepage represente SPAs, rotas sem feed ou aplicações modernas.
- **CLEAR+/ArchiveReady:** o artigo apresenta o ArchiveReady como implementação de referência do método. O serviço público continua acessível e expõe os quatro scores e mensagens via API, mas sua presença online não comprova manutenção metodológica ou tecnológica atual.
- **Projeto:** o crawl pequeno e a comparação HTML bruto versus DOM renderizado permanecem necessários; não adotaremos a hipótese de página única como regra universal.

## 2. Análise crítica dos requisitos

### 2.1 Pontos fortes

O briefing corrige quatro fragilidades comuns em avaliações de arquivabilidade:

1. separa uma característica do site da condição em que ela foi observada;
2. preserva observações brutas para permitir recálculo;
3. trata `UNKNOWN` como incerteza, não como reprovação;
4. impede que uma dimensão forte esconda uma barreira real à coleta.

Essa base é adequada tanto para um serviço operacional quanto para pesquisa científica, desde que versões de metodologia, software, configuração e ambiente sejam registradas de forma imutável por análise.

### 2.2 Ambiguidades que precisam de decisão

| Tema | Ambiguidade | Proposta inicial |
|---|---|---|
| Unidade avaliada | URL, origem, domínio registrável ou conjunto editorial | **Projeto:** a análise parte de uma URL canônica e registra origem e domínio; o escopo do crawl é configurável e explícito. |
| Conteúdo essencial | Não pode ser inferido universalmente | **Projeto:** permitir declaração pelo solicitante e heurísticas explicáveis; sem declaração, usar `UNKNOWN`, nunca hard gate automático. |
| Respeito a robots.txt | Política institucional pode variar | **Projeto:** registrar política escolhida por execução; o sistema respeita por padrão, mas a avaliação separa “regra observada” de “coleta executada”. |
| Identidade do crawler | Sites podem responder por User-Agent e origem | **Projeto:** perfis versionados de User-Agent, sem técnicas de dissimulação; comparar perfis somente quando autorizado pela configuração. |
| Validação de HTML/CSS | Erro sintático nem sempre afeta captura | **Projeto:** medir parseabilidade e impacto observado, não contar erros de validação como falhas equivalentes. |
| Risco de formato | Formato moderno não é, por si, ruim | **Projeto:** identificação, validação e risco de preservação são três resultados independentes. |
| Score geral | Um único número facilita comparação, mas esconde gargalos | **Projeto:** perfil dimensional é o resultado primário; índice sintético é secundário e sempre acompanhado de gates e cobertura. |
| Geografia | Um probe não representa o mundo | **Projeto:** todo resultado operacional inclui `probe_id`; divergência entre probes é contexto, não defeito intrínseco. |
| Escala científica | Milhares de sites conflitam com análise em navegador cara | **Projeto:** análise em camadas; HTTP básico em lote e browser sob amostragem/configuração. |

### 2.3 Escopo recomendado para o MVP

O MVP deve analisar uma URL e um crawl pequeno (`depth=2`, `max_urls=100`), produzir observações HTTP e de descoberta, renderizar uma amostra no Chromium, derivar indicadores versionados, avaliar gates declarativos e exportar JSON. Histórico, comparação entre execuções e documentação da metodologia entram no mesmo MVP. PDF, CSV, lotes extensos e probes globais ficam preparados no modelo, mas não precisam estar operacionais na primeira versão.

Não entram no MVP: Kubernetes, descoberta irrestrita da internet, bypass de bloqueios, autenticação em sites-alvo, captura WARC completa ou uma alegação de equivalência científica ao CLEAR+.

## 3. Taxonomia inicial

As dimensões descrevem **o que** está sendo avaliado. A classificação (`intrinsic`, `harvest_dependent`, `observation_context`) descreve **de onde vem** a evidência. Esses eixos são ortogonais: uma dimensão pode conter resultados de mais de uma classificação, mas resultados contextuais não entram automaticamente no score.

| Dimensão | Pergunta respondida | Limite proposto |
|---|---|---|
| Discoverability | Um crawler consegue descobrir as URLs relevantes? | Descoberta, não sucesso da recuperação. |
| Retrievability | Recursos descobertos podem ser recuperados de modo correto e estável? | Resultado por recurso; comportamento de campanha vai para Operational. |
| Standards & Formats | Sintaxe, MIME e formatos permitem interpretação e preservação? | Não equiparar modernidade a risco. |
| Dependency / Cohesion | Quanto da captura/reprodução depende de recursos e serviços externos? | Dependência não é falha sem impacto demonstrado. |
| Metadata & Context | Existem metadados úteis para identificação e interpretação? | **Proposta de nome:** “Metadata & Descriptive Context”, para não confundir com contexto do probe. A alteração ainda não foi aplicada. |
| Operational Harvestability | Como o site se comporta durante uma coleta controlada? | Inclui bloqueios, rate limiting e execução JS; latência pura permanece contextual. |

Há sobreposição potencial entre Retrievability e Operational Harvestability. A regra proposta é: Retrievability agrega propriedades e resultados por recurso; Operational agrega padrões de interação ao longo da execução, como taxa de sucesso, throttling e diferenças por User-Agent. Essa regra deverá ser testada em fixtures para evitar dupla contagem.

## 4. Modelo conceitual

```text
Target + Scope + Probe + Methodology + Toolchain
                         |
                         v
                 Analysis execution
                         |
              +----------+-----------+
              |                      |
       Raw observations        Audit events
              |
              v
        Evidence records
              |
              v
       Indicator results
              |
       +------+-------+
       |              |
Dimension results   Gate evaluations
       |              |
       +------+-------+
              v
        Assessment report
```

Princípios do modelo:

- observações são imutáveis e registram o que a ferramenta realmente viu;
- evidências apontam para observações, sem copiar corpos completos desnecessariamente;
- indicadores são derivados por uma versão de metodologia;
- scores podem ser recalculados sem repetir a coleta;
- cada derivação registra versão, configuração e hash de entrada;
- falha do analisador gera `UNKNOWN` e um erro da ferramenta, não um `FAIL` do site;
- dados de avaliação e eventos de auditoria têm retenções e controles distintos.

## 5. Arquitetura proposta

### 5.1 Escolhas de stack

| Componente | Escolha inicial | Justificativa |
|---|---|---|
| API e aplicação web | FastAPI, templates Jinja e HTMX; JavaScript pontual para gráficos | **Projeto:** reduz a superfície e o número de runtimes no MVP. A API permanece separada logicamente e permite trocar o frontend por React/Next.js depois. |
| Persistência | PostgreSQL + SQLAlchemy + Alembic | Transações, JSONB para payloads versionados e bom suporte analítico. |
| Fila e workers | Celery + Redis | Fluxos, retries controlados, filas separadas e limites por tarefa. O acoplamento fica atrás de uma interface. |
| Cliente HTTP | httpx + lxml/BeautifulSoup | Cliente assíncrono e parsers maduros; limites devem ser impostos antes do parsing. |
| Navegador | Playwright + Chromium, em worker isolado | Observa DOM renderizado e tráfego de rede, com custo e risco isolados. |
| Metodologia | YAML versionado, validado por JSON Schema/Pydantic | Pesos, fórmulas e gates ficam fora do código de execução. |
| Implantação | Docker Compose | Suficiente para desenvolvimento e uma implantação pequena; Kubernetes fica fora desta fase. |

O frontend simples é uma recomendação de MVP, não uma restrição arquitetural. Dependency graph e séries temporais podem usar bibliotecas pequenas carregadas como assets versionados. Uma SPA completa só se justifica quando a interação real exigir esse custo.

### 5.2 Componentes e limites de confiança

```text
[Browser do usuário]
        |
        v
[Web/API FastAPI] ----> [PostgreSQL]
        |                    ^
        v                    |
     [Redis] ----------> [Orchestrator worker]
                              |
                    +---------+----------+
                    |                    |
             [HTTP worker]        [Browser worker]
                    |                    |
                    +--------+-----------+
                             v
                 [Egress policy/proxy + DNS]
                             |
                       [Internet pública]
```

Separações necessárias:

- API não acessa URLs-alvo diretamente; ela valida a solicitação e agenda uma análise.
- Workers de rede rodam sem acesso à rede interna, ao socket Docker, ao banco administrativo ou a metadados de cloud.
- HTTP e browser usam o mesmo serviço/política de resolução e redirects contra SSRF.
- Browser worker tem filesystem efêmero, limites de CPU/memória/processos e perfil descartável.
- O scoring engine não faz rede; lê observações persistidas e metodologia validada.
- Exportadores consomem um snapshot imutável do relatório.

### 5.3 Fluxo de uma análise

1. A API normaliza a URL, rejeita esquemas não permitidos e cria `analysis_run` em estado `queued`.
2. O worker resolve DNS e valida todos os endereços retornados contra a política de egress.
3. O coletor HTTP executa amostras limitadas e registra timings, headers selecionados e hashes.
4. O crawler constrói uma fronteira limitada, respeitando a política de robots registrada.
5. O browser worker renderiza apenas URLs selecionadas e captura DOM, requests e APIs observadas.
6. Analisadores transformam observações em evidências; nenhuma exceção vira resultado negativo do site.
7. O scoring engine carrega uma versão imutável da metodologia e deriva indicadores, dimensões e gates.
8. O relatório e o JSON exportável são gerados do mesmo snapshot.

Estados propostos: `queued`, `validating`, `collecting_http`, `crawling`, `rendering`, `deriving`, `scoring`, `completed`, `partial`, `failed`, `cancelled`. `partial` significa que há resultados utilizáveis e lacunas explícitas; `failed` significa que não foi possível formar uma avaliação válida.

### 5.4 Contratos externos

O contrato REST inicial deve usar OpenAPI gerado pelo FastAPI, idempotency key na criação e URLs opacas para recursos. A resposta de criação é assíncrona (`202 Accepted`) e não mantém uma conexão HTTP aberta durante a coleta.

| Operação | Função | Observações |
|---|---|---|
| `POST /api/v1/analyses` | Criar análise com URL, escopo e metodologia | Valida sintaxe e política; retorna ID e status. Não faz fetch na request web. |
| `GET /api/v1/analyses/{id}` | Ler snapshot/status | Exibe resultados parciais somente com marcação explícita. |
| `POST /api/v1/analyses/{id}/cancel` | Solicitar cancelamento | Auditável e idempotente; workers verificam cancelamento entre etapas. |
| `GET /api/v1/analyses/{id}/export.json` | Exportar resultado canônico | Inclui versão de schema, hashes e limitações. |
| `GET /api/v1/sites/{domain}/history` | Consultar série histórica | Domínio é normalizado; resultados são estratificados por metodologia/probe. |
| `GET /api/v1/methodology` | Listar versões publicadas | Rascunhos exigem autorização administrativa. |
| `GET /api/v1/indicators` | Listar definições | Permite filtrar por versão, dimensão e classificação. |

As páginas `/methodology` e `/methodology/indicators/{id}` são projeções humanas da mesma configuração versionada usada pelo scoring engine. Assim, documentação e execução não divergem silenciosamente.

## 6. Modelo de dados

### 6.1 Entidades principais

| Entidade | Campos essenciais | Observação |
|---|---|---|
| `target` | `id`, `input_url`, `canonical_url`, `origin`, `registrable_domain` | Não presume que todo o domínio está no escopo. |
| `analysis_run` | `id`, `target_id`, `status`, `requested_at`, `started_at`, `finished_at`, `methodology_version`, `software_version`, `config_snapshot`, `scope`, `failure_class` | Snapshot de configuração impede reinterpretar a execução. |
| `probe` | `id`, `name`, `country`, `region`, `network_provider`, `runtime_fingerprint` | Localização pode ser aproximada para não expor infraestrutura. |
| `measurement_attempt` | `id`, `analysis_id`, `probe_id`, `sequence`, `started_at`, `ended_at`, `tool_component`, `outcome` | Agrupa repetições e retries. |
| `observation` | `id`, `attempt_id`, `kind`, `subject_uri`, `observed_at`, `payload_json`, `payload_schema_version`, `content_hash`, `error_code` | Append-only; corpo bruto pode ir para object storage. |
| `resource` | `id`, `analysis_id`, `url`, `normalized_url`, `scope_class`, `media_type`, `bytes_observed` | URL é dado potencialmente sensível e deve ser protegida de XSS/log injection. |
| `dependency_edge` | `from_resource_id`, `to_resource_id`, `relation`, `discovery_source`, `essentiality`, `confidence` | `essentiality` pode ser declarada, inferida ou desconhecida. |
| `evidence` | `id`, `analysis_id`, `observation_ids`, `kind`, `summary`, `structured_data`, `redaction_state` | Sem segredos, cookies ou corpos completos por padrão. |
| `indicator_definition` | `id`, `name`, `description`, `methodology_version`, `definition_hash`, `dimension`, `clear_plus_facet`, `classification`, `default_severity`, `weight`, `blocking_candidate`, `measurement_method`, `scoring_rule` | Materializado a partir do YAML publicado. |
| `indicator_result` | `id`, `analysis_id`, `indicator_id`, `status`, `score`, `confidence`, `severity`, `blocking`, `evidence_ids`, `measurement_method_version`, `observed_at`, `probe_id`, `tool_version`, `derived_at`, `derivation_hash` | `score` nulo em `UNKNOWN`, `NOT_APPLICABLE` e `CONTEXT`. Campos repetidos fixam a proveniência do resultado exportado. |
| `dimension_result` | `analysis_id`, `dimension_id`, `score`, `coverage`, `confidence_summary`, `status`, `formula_snapshot` | Score e cobertura nunca são confundidos. |
| `gate_evaluation` | `analysis_id`, `gate_id`, `state`, `evidence_ids`, `rule_version` | Estados: `triggered`, `not_triggered`, `unknown`, `inactive`. |
| `assessment` | `analysis_id`, `overall_state`, `profile_json`, `legacy_clear_plus_json`, `generated_at` | Snapshot publicado e exportável. |
| `methodology_release` | `version`, `status`, `released_at`, `source_hash`, `changelog`, `schema_version` | Status: draft, candidate, released, retired. |
| `audit_event` | `timestamp`, `actor_id`, `session_id`, `source_ip`, `action`, `resource_type`, `resource_id`, `result`, `safe_diff`, `correlation_id` | Trilha separada; nunca armazena tokens ou corpos não filtrados. |

Índices devem privilegiar `analysis_id`, `target_id + requested_at`, `methodology_version`, `indicator_id` e domínio normalizado. Particionamento por data é uma evolução para grande escala, não requisito do primeiro banco.

### 6.2 Separação entre payloads e banco relacional

HTML, DOM, HAR, screenshots e futuros WARCs podem exceder o tamanho adequado ao PostgreSQL. A proposta é manter no banco metadados, hashes, tamanhos, MIME e URI do artefato; o payload vai para object storage compatível com S3, com retenção configurável. O MVP pode usar armazenamento local atrás da mesma interface.

Cookies, cabeçalhos de autenticação, query strings sensíveis e valores de formulários devem ser removidos antes da persistência. Headers só entram por allowlist. O armazenamento de corpos de páginas precisa de política de retenção, finalidade de pesquisa e avaliação jurídica própria.

## 7. Semântica de evidências e resultados

| Estado | Significado |
|---|---|
| `PASS` | Evidência suficiente sustenta a condição desejada. |
| `FAIL` | Evidência suficiente demonstra a condição adversa definida. |
| `WARNING` | Há risco parcial, degradação ou evidência inconclusiva relevante. |
| `UNKNOWN` | A ferramenta não mediu, falhou ou não reuniu evidência suficiente. |
| `NOT_APPLICABLE` | A regra não se aplica ao alvo/escopo, com justificativa. |
| `CONTEXT` | Medida descritiva que não é, isoladamente, julgamento de arquivabilidade. |

Toda evidência deve registrar sujeito, método, instante, probe, tentativa, ferramenta, parâmetros relevantes, resultado estruturado, unidade, limites/truncamento e vínculo com observações. Evidência exibida na interface é uma projeção sanitizada; a observação original preserva a proveniência.

A frase “não encontrado” só é permitida quando o método e o espaço de busca estão claros. Caso contrário, usar “não foi possível determinar”. Por exemplo, um sitemap ausente em `/sitemap.xml` não prova que não exista outro sitemap; robots, HTML e índices também devem ser examinados antes de um `FAIL` definitivo.

## 8. Proposta de scoring

### 8.1 Níveis de cálculo

1. **Resultado do indicador.** Regra declarativa produz estado, score opcional de 0 a 100, severidade e confiança.
2. **Resultado da dimensão.** Média ponderada apenas de indicadores aplicáveis e conhecidos, junto de cobertura.
3. **Perfil de avaliação.** Vetor das seis dimensões, gates e incertezas; é a conclusão principal.
4. **Índice sintético experimental.** Opcional e secundário, para pesquisa, nunca exibido sem o perfil.
5. **Scores legados.** CLEAR+ Flat e Weighted, calculados separadamente.

Confiança não multiplica o score: isso faria incerteza parecer baixo desempenho. Ela controla a possibilidade de publicar uma conclusão e é exibida com a cobertura.

### 8.2 Cálculo dimensional proposto

Para os indicadores conhecidos e aplicáveis de uma dimensão `d`:

```text
dimension_score[d] = sum(weight[i] * score[i]) / sum(weight[i])
coverage[d] = sum(weight[i] de resultados conhecidos) /
              sum(weight[i] de resultados aplicáveis esperados)
```

**Hipótese inicial:** não publicar a classificação nominal da dimensão quando `coverage < 0,60`; mostrar score provisório com o rótulo “evidência insuficiente”. O limiar 0,60 é uma decisão de projeto a validar, não um fato científico.

Pesos iniciais do catálogo são todos `1` dentro da dimensão. Isso é deliberadamente neutro, mas não cientificamente validado. Mudanças exigem nova versão de metodologia e relatório de impacto.

### 8.3 Resultado principal e não compensação

O relatório primário mostra:

- estado geral: `blocked`, `assessable`, `partially_assessable` ou `indeterminate`;
- gates acionados ou desconhecidos;
- seis scores dimensionais com cobertura;
- principais achados e evidências;
- contexto de rede separado.

**Hipótese de pesquisa:** um índice secundário pode usar média geométrica ponderada das dimensões conhecidas, porque penaliza desequilíbrios mais que a média aritmética. Mesmo assim, ele não substitui gates e não é calculado quando a cobertura mínima não é satisfeita. Pesos entre dimensões começam iguais e ficam marcados como hipótese.

### 8.4 Comparação CLEAR+

**CLEAR+ conforme artigo e briefing:**

```text
Legacy CLEAR+ Flat Score = (FA + FS + FC + FM) / 4

Legacy CLEAR+ Weighted Score =
    (29*FA + 24*FS + 12*FC + 8*FM) / 73
```

Os pesos `29/24/12/8` resultam da soma dos pesos 4/2/1 das 32 avaliações CLEAR+ e não representam consenso científico para o novo modelo. As facetas são calculadas por regras próprias de compatibilidade; um mesmo indicador pode alimentar uma faceta legada e uma dimensão nova, mas nunca no mesmo acumulador.

Há duas políticas historicamente relevantes para `NOT_APPLICABLE`:

1. `fixed_max`, solicitada no briefing: conserva `29/73`, `24/73`, `12/73` e `8/73` como coeficientes fixos;
2. `paper_applicable`, observada no exemplo do artigo: remove os pesos dos testes não aplicáveis e renormaliza o denominador; no exemplo, `29+20+6+6=61`.

**Projeto:** o relatório deve informar explicitamente a política usada. `Legacy CLEAR+ Weighted Score` usará `fixed_max` para cumprir o requisito; `paper_applicable` ficará disponível como modo de reprodução metodológica, nunca misturado silenciosamente à série principal.

## 9. Mapeamento CLEAR+ para o novo modelo

| Faceta legada | Dimensões novas mais próximas | Tratamento |
|---|---|---|
| FA Accessibility | Discoverability, Retrievability, Operational Harvestability | A1–A10 cobrem links, JS inline, sitemap, tempo inicial, formatos proprietários, robots, mídia, cache e feeds. O nome legado não significa WCAG; A4 é retirado do julgamento intrínseco. |
| FS Standards Compliance | Standards & Formats, parte de Retrievability | S1–S10 cobrem HTML, formatos proprietários, imagens, RSS, headers, CSS, áudio, vídeo e Content-Type. Validação é separada de impacto e risco de preservação. |
| FC Cohesion | Dependency / Cohesion, parte de Operational Harvestability | C1–C6 medem percentuais local/remoto por tipo de recurso. A nova proposta preserva o inventário, mas exige impacto/essencialidade antes de penalizar. |
| FM Metadata Usage | Metadata & Context | M1–M6 cobrem Content-Type, cache, meta robots, Dublin Core, FOAF e meta description. A nova proposta adiciona proveniência, direitos e consistência. |

O catálogo associado registra a origem exata de cada indicador. Itens marcados como “adaptação de ID” preservam a intenção geral, mas alteram método ou interpretação. Itens “novos” cobrem Web dinâmica, probes, incerteza e segurança operacional.

## 10. Hard gates e condições impeditivas

Gates são regras declarativas, versionadas e avaliadas após os indicadores. Nesta proposta todos começam como `candidate` e `inactive`; ativá-los exige revisão metodológica.

| ID | Candidato | Condição mínima proposta | Classe | Cautela |
|---|---|---|---|---|
| HG01 | Homepage persistentemente irrecuperável | Tentativas independentes e válidas falham; falha local de DNS/rede foi excluída | harvest_dependent | Um probe não prova indisponibilidade global. |
| HG02 | Exclusão total por robots | Regra aplicável ao User-Agent configurado impede todo o escopo e a política exige respeito | intrinsic/policy | RFC 9309 não é autorização; o efeito depende da política de coleta. |
| HG03 | Autenticação obrigatória | Todo conteúdo essencial conhecido exige credenciais não disponíveis | intrinsic | Acesso autenticado pode ser arquivável em outro contexto. |
| HG04 | Bloqueio sistemático do crawler | 401/403/429 ou desafio repetido para o crawler em amostra suficiente | harvest_dependent | Deve distinguir defesa temporária, rate limit e proibição estável. |
| HG05 | Conteúdo essencial não descobrível | Conteúdo declarado essencial só aparece por mecanismo não capturado | intrinsic | “Essencial” precisa ser declarado ou ter alta confiança; do contrário `unknown`. |
| HG06 | Recursos essenciais irrecuperáveis | Recursos declarados essenciais falham persistentemente | harvest_dependent | Falhas externas podem ser transitórias e específicas do probe. |
| HG07 | Renderização essencial não reprodutível | HTML inicial não contém conteúdo essencial e APIs/recursos necessários falham na captura | harvest_dependent | Não inferir essencialidade apenas por volume de DOM. |

Exemplo conceitual de configuração:

```yaml
id: HG04
status: candidate
when:
  all:
    - indicator: crawler_access_outcome
      state: FAIL
    - indicator: retrieval_success_rate
      score_lte: 20
minimum_confidence: 0.8
effect: blocked
```

O relatório deve exibir gates `unknown`, pois uma regra não avaliada pode ser mais importante que um score alto.

## 11. Tratamento de latência e response time

Tempo de resposta é contexto operacional, não característica intrínseca. Cada tentativa deve registrar, quando tecnicamente disponível:

- DNS lookup;
- conexão TCP;
- handshake TLS;
- TTFB;
- download/total;
- HTTP status, timeout, timestamp e IP/família efetivamente usados;
- origem da medição, versão do cliente, reuso de conexão e estado de cache.

DNS, TCP e TLS podem aparecer como indisponíveis quando a biblioteca ou o reuso de conexão não permitem isolar a etapa. O sistema deve registrar `null + reason`, nunca inventar zero.

Para cada série homogênea por probe, URL, perfil e configuração, calcular `min`, `max`, mediana, desvio-padrão amostral, taxa de sucesso e tamanho da amostra. **Hipótese:** exibir p95 somente com `n >= 20`; abaixo disso, marcar como amostra insuficiente. No MVP, cinco tentativas espaçadas são uma base conservadora para mediana, mas não para p95. Aumentar amostras exige orçamento e política antiabuso.

Regras de interpretação:

- TTFB alto com sucesso consistente gera `CONTEXT`, não penalidade.
- timeouts persistentes e baixa taxa de sucesso podem afetar Operational Harvestability.
- séries de probes diferentes não são combinadas sem estratificação.
- falha de DNS localizada, rota ou TLS do probe é contexto até haver evidência suficiente do alvo.
- diferenças entre primeira conexão e conexão reutilizada são preservadas.

## 12. Relatório e exportação

A ordem recomendada da página final é:

1. website, escopo, timestamp, probe, versões e estado geral;
2. achados críticos;
3. gates acionados, desconhecidos e inativos;
4. dimensões com score, cobertura e incerteza;
5. comparação CLEAR+ identificada como legada;
6. evidências;
7. contexto de rede;
8. grafo de dependências;
9. detalhes técnicos e limitações;
10. recomendações;
11. metodologia e hashes de configuração.

Cada recomendação vincula problema, evidência, impacto na coleta, possível correção e responsável provável (`site_admin`, `harvester_operator`, `third_party` ou `shared`). Recomendações nunca devem afirmar causalidade além da evidência.

O JSON exportável deve ser um snapshot canônico com schema versionado. Deve incluir observações por referência/hashes, resultados derivados, gates, contexto, versões e limitações. CSV será uma projeção tabular; PDF, uma projeção de apresentação. Nenhum deles substitui o JSON canônico.

### 12.1 Histórico e interpretação de mudanças

Séries temporais devem comparar resultados compatíveis por metodologia, escopo e probe. Cada diferença recebe inicialmente um estado descritivo: `observed_change`, `probable_structural_change`, `probable_observation_variation` ou `indeterminate`. Somente evidências persistentes e coerentes entre recursos/tentativas permitem elevar uma mudança observada a “provável mudança estrutural”. O sistema não declara causalidade automaticamente.

## 13. Riscos metodológicos

| Risco | Consequência | Mitigação |
|---|---|---|
| Pesos refletem disponibilidade de testes | Aparência de rigor sem validade | Versionar, justificar, testar sensibilidade e validar com especialistas/datasets. |
| Viés do probe | Site distante parece pior | Estratificar por probe e separar contexto. |
| Viés temporal | Incidente transitório vira diagnóstico | Repetições, timestamps, histórico e linguagem probabilística. |
| Essencialidade inferida | Gate falso | Preferir declaração; exigir confiança alta; usar `unknown`. |
| Sites grandes subamostrados | Falsa certeza | Reportar escopo, cobertura e estratégia de amostragem. |
| Browser não representa crawler arquivístico | Resultado pouco transferível | Perfis explícitos e comparação com ferramentas de captura em validação futura. |
| Validação sintática excessiva | Erros sem impacto dominam score | Medir parseabilidade e impacto, não contagem bruta. |
| Dependência externa penalizada por princípio | Sites modernos recebem nota artificialmente baixa | Avaliar disponibilidade, essencialidade e capacidade de captura. |
| Mudança de metodologia quebra séries | Comparações históricas inválidas | Recalcular a partir de observações e manter resultados por versão. |
| Seleção de sites para validação | Thresholds superajustados | Dataset público estratificado e validação fora da amostra. O estudo CLEAR+ de página única exigiu RSS detectável e não deve ser generalizado sem nova validação. |

## 14. Riscos técnicos e de segurança

| Risco | Controle obrigatório proposto |
|---|---|
| SSRF e DNS rebinding | Somente HTTP/HTTPS; rejeitar credenciais na URL; resolver todos os A/AAAA; bloquear loopback, privado, link-local, multicast, reservado e metadados de cloud; revalidar cada redirect e conexão; usar proxy/egress deny-by-default. |
| Redirect para rede interna | Limite de redirects e validação integral a cada salto, inclusive hostname e IP final conectado. |
| Respostas gigantes, compressão maliciosa e XML entities | Limites de bytes comprimidos/descomprimidos, tempo e razão; parsing em streaming; DTD/entidades externas desativadas. |
| JavaScript hostil | Chromium em container/VM sem rede interna, sem mounts/segredos, com limites de CPU, memória, processos e tempo; perfil descartável. |
| Abuso da plataforma para varredura/DoS | Autenticação/rate limit quando pública, quotas, concorrência por domínio, intervalo mínimo e identificação honesta do crawler. |
| XSS armazenado em evidências | Nunca renderizar HTML-alvo diretamente; escape contextual, CSP forte e sanitização de campos exibidos. |
| Vazamento por logs e artefatos | Allowlist de headers; remover cookies, Authorization, tokens, query params sensíveis e corpos de formulário; retenção mínima. |
| Injeção em logs | Logs estruturados, normalização de quebras de linha e truncamento. |
| Comprometimento da fila | Redis não exposto; autenticação/TLS fora de localhost; mensagens assinadas/validadas; workers com privilégios mínimos. |
| Supply chain | Dependências pinadas, SBOM, verificação de imagens, scanners de vulnerabilidade e atualização documentada. |
| Corridas e duplicação | Idempotency key na API, leases e transações; derivação determinada por hashes. |
| Risco jurídico/privacidade | Minimização, política de retenção, exclusão controlada, termos de uso e revisão LGPD/copyright antes de coleta em escala. |

### 14.1 Auditoria

Eventos mínimos: criação/cancelamento de análise, mudança de metodologia, alteração de configuração/gate, exportação, exclusão/expiração de artefato, mudança de acesso e início/fim/falha de jobs. Cada evento registra timestamp UTC, ator opaco, IP de origem quando aplicável, sessão opaca, ação, recurso/ID, resultado, correlação e diff sanitizado.

Nunca registrar senhas, tokens, cookies, `Authorization`, variáveis de ambiente, corpos completos ou dados pessoais sensíveis. Falhas do crawler ficam nas observações técnicas; decisões administrativas ficam na trilha de auditoria. Retenção e acesso às duas categorias são independentes.

### 14.2 Banco de dados e menor privilégio

- Usuário de runtime sem `CREATE`, `ALTER`, `DROP`, `TRUNCATE`, superuser ou ownership de schema.
- Usuário separado de migration, disponível apenas no pipeline de implantação.
- Credenciais distintas em desenvolvimento, homologação e produção, fora do repositório.
- TLS obrigatório fora do ambiente local e PostgreSQL não exposto publicamente.
- Worker pode receber permissões restritas às tabelas necessárias; exportador pode ser somente leitura.
- Backups e object storage usam identidades próprias e testes de restauração.

## 15. Estrutura de diretórios proposta

```text
arquivabilidade-ja/
├── apps/
│   ├── api/                 # rotas, autenticação, schemas e serviços HTTP
│   └── web/                 # templates, assets e componentes de interface
├── src/archivability/
│   ├── domain/              # entidades e tipos, sem I/O
│   ├── orchestration/       # fluxo e estado de análises
│   ├── collectors/
│   │   ├── http/
│   │   ├── browser/
│   │   └── crawler/
│   ├── analyzers/           # observação -> evidência
│   ├── scoring/             # evidência -> indicador/dimensão/gate
│   ├── persistence/
│   ├── reporting/
│   ├── security/            # validação de destino e política de egress
│   └── audit/
├── methodology/
│   └── v0.1.0/
│       ├── manifest.yaml
│       ├── dimensions.yaml
│       ├── indicators.yaml
│       ├── scoring.yaml
│       ├── gates.yaml
│       └── schemas/
├── migrations/
├── tests/
│   ├── unit/
│   ├── integration/
│   ├── methodology/
│   ├── security/
│   └── fixtures/sites/
├── docs/
│   ├── methodology/
│   ├── architecture/
│   └── decisions/           # ADRs
├── deploy/compose/
├── scripts/
└── pyproject.toml
```

Cada release de metodologia é imutável. Correções que alterem resultados criam uma nova versão, mesmo que o software permaneça igual.

## 16. Roadmap de implementação

| Fase | Entrega | Critério de saída |
|---|---|---|
| 0. Revisão metodológica | Aprovar taxonomia, catálogo, estados, gates candidatos, política de robots e política de denominador legado | Decisões registradas em ADR; matriz CLEAR+ teste a teste revisada. |
| 1. Fundação segura | Domínio, schema, metodologia validada, SSRF policy, persistência e auditoria | Testes unitários e de segurança passam; nenhum fetch fora do broker/política. |
| 2. Análise HTTP | Homepage, robots, sitemap, links, headers, formatos e timings | Fixtures determinísticas; JSON reproduzível; `UNKNOWN` comprovado em falha da ferramenta. |
| 3. Crawl limitado | Fronteira, redirects, quotas, links quebrados e grafo | Limites e concorrência por domínio verificados em testes de integração. |
| 4. Browser isolado | DOM pós-renderização, requests, APIs e delta JS | Sandbox e budgets validados; fixtures SPA/API cobertas. |
| 5. Scoring e relatório | Dimensões, cobertura, gates, CLEAR+ legado, histórico e metodologia | Golden tests de scoring e snapshots de exportação. |
| 6. Validação científica | Dataset de referência, análise de sensibilidade e concordância de especialistas | Relatório de validação; thresholds revisados e versionados. |
| 7. Escala e probes | Lotes, object storage e múltiplos probes | Resultados estratificados e observabilidade operacional. |

Cada fase só começa após aprovação da anterior quando houver mudança metodológica. A primeira implementação deve priorizar fixtures e golden tests antes de ampliar o número de indicadores.

### 16.1 Estratégia de testes

- Testes unitários para normalização, classificação de IP, parsing, derivação, cobertura e fórmulas.
- Golden tests para cada versão da metodologia: um conjunto fixo de observações deve produzir resultados byte a byte explicáveis.
- Testes de integração com sites locais controlados: saudável, robots total, links quebrados, lentidão, timeout, redirect loop, HTML inválido, dependência externa, SPA, API, 429, 403, sitemap e metadata ausentes.
- Testes específicos de SSRF: IPv4/IPv6 privado, representação alternativa de IP, DNS rebinding simulado, CNAME/redirect interno, metadata de cloud e URLs com credenciais.
- Testes de falha da ferramenta para comprovar que exceções, parser indisponível e budget esgotado resultam em `UNKNOWN`, não zero.
- Testes de contrato OpenAPI e schema JSON; migrações são testadas com usuário separado do runtime.
- Testes de carga conservadores no ambiente de fixtures, nunca contra sites públicos sem autorização.

## 17. Checklist de segurança da fase de análise e arquitetura

Este é o checklist aplicável à fase atual, conforme NSI.04 seção 4.3.

- [ ] Especificar autenticação e autorização para uso público, administração e pesquisadores.
  - Por quê: a plataforma pode ser usada para varredura ou acesso indevido a resultados.
  - Como: definir se análises anônimas existem, quotas, papéis e autorização no backend.
  - Referência NSI.04: seções 4.3 e 3.3.
- [ ] Classificar dados por sensibilidade.
  - Por quê: URLs, artefatos capturados e histórico podem conter dados pessoais ou conteúdo restrito.
  - Como: classificar observações, evidências, auditoria e artefatos; definir retenção e acesso.
  - Referência NSI.04: seção 4.3.
- [ ] Especificar confidencialidade, integridade e disponibilidade.
  - Por quê: resultados científicos exigem integridade; artefatos podem exigir confidencialidade; filas precisam de limites de disponibilidade.
  - Como: hashes e snapshots para integridade, TLS, backups, RPO/RTO e política de retenção.
  - Referência NSI.04: seção 4.3.
- [ ] Definir interfaces de log e monitoramento.
  - Por quê: jobs distribuídos sem correlação não são auditáveis.
  - Como: eventos estruturados, `correlation_id`, métricas de fila/coleta e trilha administrativa sanitizada.
  - Referência NSI.04: seções 4.3 e 3.6.
- [ ] Garantir separação de ambientes na arquitetura.
  - Por quê: sites e dados coletados em testes não devem contaminar produção.
  - Como: redes, bancos, buckets, filas e credenciais distintas; fixtures sintéticas em teste.
  - Referência NSI.04: seção 4.3.

## 18. Decisões solicitadas antes do código

1. Manter “Metadata & Context” ou aprovar “Metadata & Descriptive Context”.
2. Aprovar a fronteira entre Retrievability e Operational Harvestability.
3. Aprovar o frontend server-rendered para o MVP.
4. Definir a política padrão de robots e o User-Agent institucional.
5. Decidir se algum gate candidato ficará ativo em `v0.1.0`.
6. Revisar o limiar proposto de cobertura e a opção de índice geométrico experimental.
7. Escolher se o modo `paper_applicable` será exibido além do `fixed_max` exigido pelo briefing.
8. Definir retenção de corpos, DOM, screenshots e futuros WARCs.

Após essa revisão, a próxima entrega deve ser uma especificação executável da metodologia em YAML e JSON Schema, acompanhada dos primeiros golden tests. O sistema não deve ser implementado antes dessa aprovação.
