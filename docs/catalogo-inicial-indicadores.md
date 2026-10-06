# Catálogo inicial de indicadores de arquivabilidade

Status: rascunho para revisão
Metodologia alvo: 0.1.0-draft
Data: 2026-10-03

Este catálogo propõe um conjunto inicial de indicadores para o MVP. Ele não afirma validação científica. As origens foram conferidas no artigo CLEAR+ integral; a [matriz de rastreabilidade](revisao-critica-clear-plus.md) registra os 32 testes originais e suas adaptações.

## Convenções

- Origem: `CLEAR+ ID` indica correspondência direta conferida; `adaptação de ID` preserva a intenção com método atualizado; `novo` cobre requisitos não presentes nas tabelas do CLEAR+.
- Tipo: `I` = intrinsic, `H` = harvest_dependent, `O` = observation_context.
- Resultados possíveis usam `PASS`, `WARNING`, `FAIL`, `UNKNOWN`, `NOT_APPLICABLE` e `CONTEXT`.
- Peso inicial `1` é uma decisão neutra de projeto, não um peso científico. `0` indica indicador não pontuado, apenas contextual.
- Severidade é a severidade padrão quando a condição adversa é confirmada; a regra pode reduzi-la conforme o impacto.
- `Candidato a gate` não significa gate ativo.

## Discoverability

| ID e nome | Origem | Tipo | Método de medição | Resultado | Severidade | Peso | Gate | Limitações e relação com arquivabilidade |
|---|---|---|---|---|---|---:|---|---|
| `D01 homepage_accessible` | adaptação da premissa FA | H | GET controlado da URL inicial, redirects validados e repetição | PASS/FAIL/UNKNOWN | critical | 1 | HG01 | Um probe pode sofrer falha local. A homepage costuma iniciar a descoberta, mas não representa todo o site. |
| `D02 robots_available_parseable` | adaptação de A6/A7 | I | Buscar `/robots.txt`, registrar status e parsear conforme RFC 9309 | PASS/WARNING/FAIL/UNKNOWN | medium | 1 | não | Ausência não impede coleta; conteúdo inválido pode produzir interpretação ambígua. Importa por definir orientação ao crawler. |
| `D03 robots_scope_policy` | adaptação de A6/M3 | I | Avaliar grupos aplicáveis ao User-Agent e escopo configurados | PASS/WARNING/FAIL/UNKNOWN/NA | high | 1 | HG02 | O resultado depende do User-Agent e da política; robots não é autorização. Pode restringir a fronteira capturável. |
| `D04 sitemap_discovery` | adaptação de A3/A7 | I | Examinar robots, locais convencionais e links declarados; validar índice/URLs | PASS/WARNING/FAIL/UNKNOWN | low | 1 | não | Não encontrar nos locais examinados não prova ausência global. Sitemap melhora cobertura de descoberta. |
| `D05 feed_discovery` | CLEAR+ A10 | I | Detectar `link rel=alternate`, RSS/Atom e validar parse básico | PASS/WARNING/FAIL/UNKNOWN/NA | low | 1 | não | Nem todo site precisa de feed. Quando presente, expõe conteúdo e ordem temporal ao crawler. |
| `D06 initial_html_link_yield` | adaptação de A1 | I | Contar URLs únicas e válidas extraídas do HTML bruto, por tipo/escopo | PASS/WARNING/FAIL/UNKNOWN | medium | 1 | não | Páginas de entrada legítimas podem ter poucos links. Baixo rendimento pode limitar descoberta sem renderização. |
| `D07 rendered_url_gain` | adaptação de A2 | H | Comparar URLs do HTML bruto com DOM e requests após renderização | PASS/WARNING/FAIL/UNKNOWN/NA | medium | 1 | HG05 | O ganho depende do estado do browser e da janela de observação. Mostra dependência de JS para descoberta. |
| `D08 crawl_frontier_coverage` | novo | H | Registrar URLs descobertas, visitadas, descartadas e motivo dentro dos budgets | PASS/WARNING/FAIL/UNKNOWN | medium | 1 | não | Sem universo conhecido, mede cobertura operacional da fronteira, não cobertura total do site. |

## Retrievability

| ID e nome | Origem | Tipo | Método de medição | Resultado | Severidade | Peso | Gate | Limitações e relação com arquivabilidade |
|---|---|---|---|---|---|---:|---|---|
| `R01 retrieval_success_rate` | adaptação de A1/A8 | H | Proporção de respostas recuperadas dentro do escopo, separada por status e tentativa | PASS/WARNING/FAIL/UNKNOWN | high | 1 | HG01/HG06 | Amostra e instante influenciam. Falhas persistentes reduzem a capacidade de captura. |
| `R02 redirect_integrity` | novo | I/H | Seguir cadeia limitada, validar cada destino e detectar loop/oscilações | PASS/WARNING/FAIL/UNKNOWN | high | 1 | não | Mudança geográfica pode alterar a cadeia. Loops e destinos inválidos impedem recuperação determinística. |
| `R03 broken_internal_links` | adaptação de A1 | I | Verificar amostra/total de links internos e classificar 4xx, 5xx e falhas de rede | PASS/WARNING/FAIL/UNKNOWN | medium | 1 | não | HEAD pode divergir de GET; links temporários expiram. Quebras reduzem cobertura e contexto. |
| `R04 http_status_distribution` | adaptação de A1/A8 | H | Distribuir status por URL, tipo e tentativa; não colapsar redirects em sucesso | PASS/WARNING/FAIL/UNKNOWN | medium | 1 | não | Um código isolado não caracteriza o site. A distribuição revela recuperabilidade do escopo. |
| `R05 content_type_consistency` | adaptação de S10/M1 | I | Comparar `Content-Type`, sniff seguro limitado, extensão e parser | PASS/WARNING/FAIL/UNKNOWN | medium | 1 | não | Sniffing é heurístico. MIME incorreto pode prejudicar processamento e reprodução. |
| `R06 response_completeness` | novo | H | Conferir truncamento, erro de stream, comprimento declarado e limites do coletor | PASS/WARNING/FAIL/UNKNOWN | high | 1 | HG06 | Ausência de `Content-Length` é válida. Captura incompleta compromete preservação. |
| `R07 conditional_retrieval_support` | adaptação de A9/S6/M2 | I | Registrar ETag/Last-Modified e testar condicional opcionalmente | PASS/WARNING/FAIL/UNKNOWN/NA | info | 1 | não | Validators não são obrigatórios e podem ser fracos. Ajudam recrawls eficientes e versionamento. |
| `R08 canonical_target_consistency` | novo | I | Comparar URL final, `rel=canonical` e escopo | PASS/WARNING/FAIL/UNKNOWN/NA | low | 1 | não | Canonical é sugestão editorial. Inconsistência pode fragmentar identidade e deduplicação. |

## Standards and Formats

| ID e nome | Origem | Tipo | Método de medição | Resultado | Severidade | Peso | Gate | Limitações e relação com arquivabilidade |
|---|---|---|---|---|---|---:|---|---|
| `S01 html_parseability` | adaptação de S1 | I | Parse tolerante, erros estruturais e comparação com DOM obtido | PASS/WARNING/FAIL/UNKNOWN | medium | 1 | não | Contagem de erros de validação não mede impacto. HTML que não parseia consistentemente reduz captura/reprodução. |
| `S02 css_parseability` | adaptação de S7 | I | Parse de folhas recuperadas, importações e erros impeditivos | PASS/WARNING/FAIL/UNKNOWN/NA | low | 1 | não | Browsers recuperam muitos erros. Só impacto demonstrável deve elevar severidade. |
| `S03 xml_feed_sitemap_validity` | adaptação de S4 | I | Parser seguro sem DTD/entidades externas; validar estrutura aplicável | PASS/WARNING/FAIL/UNKNOWN/NA | medium | 1 | não | Schema estrito pode rejeitar extensões válidas. XML inválido prejudica descoberta automatizada. |
| `S04 format_inventory` | adaptação de A5/S2/S3/S8/S9 | I | Identificar MIME/assinatura/extensão e contar bytes/recursos por família | CONTEXT/UNKNOWN | info | 0 | não | Inventário não é julgamento. É base para planejamento de captura e preservação. |
| `S05 format_validation` | adaptação de S3/S8/S9 | I | Validadores específicos apenas para formatos suportados, com cobertura explícita | PASS/WARNING/FAIL/UNKNOWN/NA | medium | 1 | não | Cobertura de validadores é desigual. Corrupção comprovada afeta preservação; ausência de validador gera UNKNOWN. |
| `S06 preservation_format_risk` | adaptação de A5/S2 | I | Consultar registro de risco versionado: abertura, documentação, suporte e dependências | PASS/WARNING/FAIL/UNKNOWN | medium | 1 | não | O registro e os pesos exigem governança; formato moderno não é automaticamente arriscado. Relaciona longevidade e reprodutibilidade. |
| `S07 character_encoding_consistency` | novo | I | Comparar bytes/BOM, headers e declarações do documento | PASS/WARNING/FAIL/UNKNOWN/NA | medium | 1 | não | Detecção pode ser probabilística. Inconsistência compromete texto e metadados. |

## Dependency and Cohesion

| ID e nome | Origem | Tipo | Método de medição | Resultado | Severidade | Peso | Gate | Limitações e relação com arquivabilidade |
|---|---|---|---|---|---|---:|---|---|
| `C01 external_resource_share` | adaptação de C1–C6 | I/H | Proporção de requests/bytes externos no HTML e no browser, por tipo | CONTEXT/WARNING/UNKNOWN | low | 1 | não | Proporções dependem da amostra; CDN próprio pode parecer externo. Dependência amplia o escopo de captura. |
| `C02 external_domain_diversity` | adaptação de C1–C6 | I/H | Contar origens e domínios registráveis externos; classificar relação conhecida | CONTEXT/WARNING/UNKNOWN | low | 1 | não | Quantidade não equivale a risco. Mais autoridades podem aumentar fragilidade operacional. |
| `C03 dependency_concentration` | novo | I/H | Medir participação de recursos essenciais por terceiro e pontos únicos de falha | PASS/WARNING/FAIL/UNKNOWN | medium | 1 | HG06 | Essencialidade pode ser desconhecida. Alta concentração pode comprometer reprodução se o terceiro faltar. |
| `C04 third_party_active_content` | adaptação de C2–C6 | I/H | Identificar JS, CSS, iframes, fontes e workers externos no grafo | CONTEXT/WARNING/UNKNOWN | medium | 1 | não | Terceiro não significa malicioso ou incapturável. Conteúdo ativo pode exigir política e ferramentas adicionais. |
| `C05 external_api_dependency` | novo | H | Correlacionar XHR/fetch/GraphQL com mudanças relevantes no DOM | PASS/WARNING/FAIL/UNKNOWN/NA | high | 1 | HG07 | Correlação não prova causalidade; exige janela de observação. APIs podem ser essenciais à captura. |
| `C06 essential_dependency_availability` | novo | H | Repetir acesso limitado a dependências declaradas/inferidas essenciais | PASS/WARNING/FAIL/UNKNOWN/NA | high | 1 | HG06/HG07 | Falha pode ser regional ou transitória. Dependência essencial indisponível impede reprodução completa. |

## Metadata and Context

| ID e nome | Origem | Tipo | Método de medição | Resultado | Severidade | Peso | Gate | Limitações e relação com arquivabilidade |
|---|---|---|---|---|---|---:|---|---|
| `M01 descriptive_metadata_presence` | adaptação de M4–M6 | I | Detectar título, descrição, idioma e metadados estruturados por página | PASS/WARNING/FAIL/UNKNOWN | low | 1 | não | Presença não garante qualidade. Auxilia identificação e interpretação do arquivado. |
| `M02 metadata_consistency` | adaptação de S10/M1 | I | Comparar idioma, título, canonical e identificadores entre HTTP/HTML/estruturado | PASS/WARNING/FAIL/UNKNOWN/NA | medium | 1 | não | Variações legítimas existem. Contradições dificultam contexto e deduplicação. |
| `M03 temporal_metadata` | adaptação de A9/S6/M2 | I | Registrar datas HTTP e metadados editoriais, com origem e parse | PASS/WARNING/FAIL/UNKNOWN/NA | low | 1 | não | Datas podem significar publicação, modificação ou geração. Contexto temporal melhora interpretação. |
| `M04 rights_license_metadata` | novo | I | Detectar sinais explícitos de licença/direitos e seus alvos | PASS/WARNING/FAIL/UNKNOWN/NA | info | 1 | não | Ausência não significa proibição; interpretação jurídica fica fora do indicador. Metadados apoiam gestão do acervo. |
| `M05 provenance_context` | adaptação de M4/M5 | I | Detectar organização/autoria, identificadores persistentes e páginas de contexto | PASS/WARNING/FAIL/UNKNOWN/NA | low | 1 | não | Heurísticas variam por domínio. Proveniência ajuda autenticidade e uso científico. |

## Operational Harvestability

| ID e nome | Origem | Tipo | Método de medição | Resultado | Severidade | Peso | Gate | Limitações e relação com arquivabilidade |
|---|---|---|---|---|---|---:|---|---|
| `O01 persistent_timeout_rate` | adaptação de A4 | H | Repetições espaçadas; separar timeout de conexão, TLS, headers e corpo | PASS/WARNING/FAIL/UNKNOWN | high | 1 | HG01/HG06 | Um probe e uma janela curta não demonstram persistência global. Falhas repetidas impedem coleta. |
| `O02 rate_limiting_behavior` | novo | H | Detectar 429, Retry-After, degradação por cadência e recuperação após pausa | PASS/WARNING/FAIL/UNKNOWN/NA | high | 1 | HG04 | Teste deve ser conservador para não causar carga. Rate limiting pode ser administrável pelo harvester. |
| `O03 crawler_access_outcome` | adaptação da premissa FA | H | Comparar perfil declarado de crawler com perfil HTTP padrão autorizado | PASS/WARNING/FAIL/UNKNOWN/NA | critical | 1 | HG04 | Não usar dissimulação. Diferença pode depender de WAF, região ou cookies. Bloqueio sistemático impede coleta pelo perfil. |
| `O04 authentication_challenge` | novo | H/I | Detectar 401, redirects de login e conteúdo essencial atrás de sessão | PASS/WARNING/FAIL/UNKNOWN/NA | high | 1 | HG03 | Login detectado não prova que todo o site seja fechado. Credenciais podem existir em outra política de coleta. |
| `O05 javascript_dependency` | adaptação de A2 | H | Comparar conteúdo/links do HTML bruto e DOM; observar requests necessários | PASS/WARNING/FAIL/UNKNOWN/NA | medium | 1 | HG07 | A janela e as interações limitam a observação. JS não é falha; dependência muda o método de captura. |
| `O06 dynamic_interaction_complexity` | novo | H | Detectar lazy load, scroll, Shadow DOM, iframes, WebSocket e service worker | CONTEXT/WARNING/UNKNOWN/NA | medium | 1 | não | Detecção não prova conteúdo perdido. Complexidade aumenta esforço e reduz determinismo de coleta. |
| `O07 campaign_success_rate` | novo | H | Agregar recuperação por tentativas, classes de recurso e budgets | PASS/WARNING/FAIL/UNKNOWN | high | 1 | HG01/HG06 | Resultado depende de escopo e orçamento. Resume viabilidade operacional sem confundir falha local com ausência. |

## Network context

Estes indicadores pertencem ao contexto da análise e têm peso zero. Podem aparecer próximos de Operational Harvestability, mas não reduzem o score isoladamente.

| ID e nome | Origem | Tipo | Método de medição | Resultado | Severidade | Peso | Gate | Limitações e relação com arquivabilidade |
|---|---|---|---|---|---|---:|---|---|
| `N01 latency_breakdown` | correção de A4 | O | Medir DNS, TCP, TLS, TTFB e total por tentativa, com null reason | CONTEXT/UNKNOWN | info | 0 | não | Reuso de conexão e APIs podem ocultar fases. Contextualiza desempenho sem culpar o site pela distância. |
| `N02 latency_distribution` | novo | O | Mediana, min, max, desvio, n e p95 somente com amostra suficiente | CONTEXT/UNKNOWN | info | 0 | não | Estatística de amostra pequena é instável. Ajuda distinguir lentidão de falha. |
| `N03 probe_variance` | novo | O | Comparar status, DNS e latência entre probes com configuração compatível | CONTEXT/UNKNOWN | info | 0 | não | Probes diferem em rede e runtime. Divergência sugere geoblocking/CDN/rota, não causa definitiva. |
| `N04 dns_observation` | novo | O | Registrar respostas A/AAAA/CNAME, TTL quando disponível e resolver usado | CONTEXT/UNKNOWN | info | 0 | não | DNS varia por localização e tempo. É necessário para reprodutibilidade e diagnóstico de SSRF/rota. |

## Headers HTTP observados sem score próprio inicial

`Content-Length`, `Last-Modified`, `ETag`, `Cache-Control`, `Content-Encoding`, `Vary`, `Retry-After` e redirects devem ser preservados como observações estruturadas. Eles alimentam indicadores específicos quando houver relação defensável; a mera presença ou ausência não gera pontuação automática.

## Cobertura dos formatos

O inventário deve reconhecer inicialmente HTML, CSS, JavaScript, JPEG, PNG, GIF, SVG, WebP, AVIF, PDF, áudio, vídeo, fontes e `other/unknown`. Cada recurso mantém três campos independentes:

1. identificação e confiança;
2. resultado de validação, quando houver validador;
3. risco de preservação segundo registro metodológico versionado.

Essa separação evita que um formato recente ou desconhecido seja automaticamente rotulado como inadequado.

## Questões de validação antes da versão 0.1.0

1. Revisar com especialistas a matriz CLEAR+ já conferida e decidir a política de denominador para itens não aplicáveis.
2. Definir o conjunto mínimo de indicadores esperados por dimensão para cálculo de cobertura.
3. Criar fixtures que diferenciem ausência comprovada, não descoberta e falha do analisador.
4. Testar sensibilidade dos scores a pesos e amostragem.
5. Definir protocolo de anotação de “conteúdo essencial”.
6. Validar thresholds e gates com arquivistas, operadores de crawl e um dataset estratificado.
7. Verificar quais indicadores têm confiabilidade inter-probe e inter-execução suficiente para uso científico.
