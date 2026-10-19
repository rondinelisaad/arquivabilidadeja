# Arquivabilidade JA

Este repositório contém o desenho de uma plataforma aberta para avaliar a arquivabilidade de websites. A proposta usa o CLEAR+ como referência histórica, mas separa propriedades do website, comportamento durante a coleta e condições externas de observação.

## Estado do projeto

A fundação segura, a análise HTTP/crawl e a etapa de browser isolado estão implementadas. A API apenas cria a análise e enfileira trabalho; o pipeline durável executa coleta HTTP, Chromium em sidecar sem privilégios e derivação. Robots RFC 9309, SSRF, quotas, redirects, DOM/requests agregados e retries são validados sem persistir corpos, URLs descobertas ou mensagens de console. O projeto também possui metodologia executável, scoring determinístico, cadeia imutável `Observation → Evidence → IndicatorResult`, filas duráveis em SQLite/PostgreSQL, autenticação OIDC, contrato ASGI, relatórios e manifests Kubernetes. SQLite permanece restrito ao desenvolvimento e aos testes.

- [Proposta de arquitetura e metodologia](docs/proposta-arquitetura-metodologia.md)
- [Catálogo inicial de indicadores](docs/catalogo-inicial-indicadores.md)
- [Revisão crítica e matriz de rastreabilidade do CLEAR+](docs/revisao-critica-clear-plus.md)
- [Requisitos de segurança](docs/requisitos-seguranca.md)
- [Métodos de derivação HTTP](docs/metodos-derivacao-http.md)
- [Fase 1 — coleta HTTP assíncrona](docs/fase-1-coleta-http-assincrona.md)
- [Fase 3 — crawl limitado](docs/fase-3-crawl-limitado.md)
- [Fase 4 — browser isolado](docs/fase-4-browser-isolado.md)
- [Fase 5 — scoring e relatório](docs/fase-5-scoring-relatorio.md)
- [Fase 6 — protocolo de validação científica](docs/fase-6-validacao-cientifica.md)
- [Fase 7 — escala operacional e probes](docs/fase-7-escala-probes.md)
- [Estado de entrega e bloqueios de release](docs/estado-entrega.md)
- [Interface Web](docs/interface-web.md)
- [Metodologia executável candidata 0.1.0](methodology/v0.1.0/README.md)

## Validação

Os arquivos da metodologia usam o subconjunto JSON do YAML 1.2 e os golden tests não exigem pacotes externos:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_*.py' -v
```

O ambiente local completo está em `compose.yaml`. Copie
`deploy/compose/.env.example` para `deploy/compose/.env`, substitua todos os valores e
execute `docker compose --env-file deploy/compose/.env up --build`.

## Scoring engine

O primeiro componente implementado é um motor puro e determinístico, sem rede, banco ou fila. Ele carrega a metodologia versionada, calcula dimensões e cobertura, avalia gates explicitamente ativados e produz as variantes CLEAR+.

```python
from archivability import IndicatorResult, ResultState, ScoringEngine, load_methodology

methodology = load_methodology("methodology/v0.1.0")
engine = ScoringEngine(methodology)
assessment = engine.evaluate(
    [IndicatorResult("D01", ResultState.PASS, confidence=1.0)],
    applicable_indicator_ids=["D01"],
)
```

Para executar o exemplo diretamente do checkout, use `PYTHONPATH=src` ou instale o pacote em um ambiente virtual.

## Proveniência das medições

Observações brutas e evidências derivadas são objetos imutáveis, serializáveis em JSON e protegidos por hash canônico SHA-256. O hash serve para integridade e reprodutibilidade — não para senhas ou assinatura digital. A derivação de um indicador preserva análise, evidências, hashes, horário observado, probes, ferramentas e versões do método.

```python
from datetime import datetime, timezone
from archivability import Evidence, Observation

observation = Observation.create(
    observation_id="obs-1",
    analysis_id="analysis-1",
    attempt_id="attempt-1",
    kind="http_response",
    subject_uri="https://example.org/",
    observed_at=datetime.now(timezone.utc),
    probe_id="probe-http",
    tool_name="http-probe",
    tool_version="1.0.0",
    payload_schema_version="1.0",
    payload={"status": 200},
)
```

Os contratos de intercâmbio estão em `schemas/domain/v1`.

## Persistência local transacional

O adaptador SQLite persiste a cadeia completa de forma atômica, valida referências por ID + hash e bloqueia `UPDATE` e `DELETE` nas tabelas de domínio e auditoria. Os logs usam eventos `data.create` e contêm somente identificadores opacos e metadados seguros — payload, URL e mensagens de erro não são registrados.

SQLite é suportado **somente para desenvolvimento e testes locais**. A criação do schema é uma operação explícita e separada do runtime:

```python
import sqlite3
from archivability import SqliteEvidenceRepository, apply_sqlite_migrations

connection = sqlite3.connect("arquivabilidade-dev.sqlite3")
apply_sqlite_migrations(connection)  # ferramenta de provisionamento, não executar no runtime
repository = SqliteEvidenceRepository(connection)
```

Em produção, o adaptador PostgreSQL usa usuários distintos para migrations e runtime. O usuário da aplicação possui somente `SELECT` e `INSERT` nas tabelas append-only, sem `CREATE`, `ALTER`, `DROP`, `TRUNCATE` ou privilégios administrativos.

As tabelas mutáveis de ciclo de vida (`analyses`, `attempts`, `collection_jobs`, `browser_jobs` e `assessment_jobs`) também exigem `UPDATE`, sempre protegido por revisão otimista. Nenhuma tabela de domínio concede `DELETE` ao runtime. A única exceção é a tabela operacional `rate_limit_buckets`, na qual `DELETE` é necessário para remover buckets ociosos já totalmente recarregados e manter o armazenamento limitado. O runtime não recebe qualquer permissão DDL. As migrations das filas são aplicadas pelo usuário separado de migrations; o usuário da aplicação recebe apenas `SELECT`, `INSERT` e `UPDATE` nessas tabelas. A tabela append-only `analysis_ownership` exige apenas `SELECT` e `INSERT`: o vínculo é criado na mesma transação da análise e não pode ser alterado ou excluído.

O schema PostgreSQL de produção é provisionado separadamente com uma conexão ociosa, transacional e pertencente ao usuário exclusivo de migrations. O executor serializa concorrentes com advisory lock, registra versão, checksum, ator e instante de aplicação e recusa migrations já aplicadas cujo checksum tenha mudado:

```python
from archivability import apply_postgresql_migrations

apply_postgresql_migrations(migration_connection)
```

Depois do provisionamento, um administrador aplica `deploy/postgresql/runtime_grants.sql` com nomes de roles e banco fornecidos como variáveis do `psql`. O script não cria usuários nem contém credenciais: ele remove privilégios implícitos, concede somente `CONNECT`/`USAGE` e o DML necessário por tabela. A role de runtime deve ser criada externamente como `NOSUPERUSER NOCREATEDB NOCREATEROLE` e usar TLS/SCRAM conforme a política do ambiente; a credencial vem do cofre ou do mecanismo de secrets, nunca do repositório.

`PostgreSqlLifecycleRepository` implementa o primeiro recorte do adaptador de runtime: criação e transições de análises e tentativas, vínculo de proprietário e auditoria. A conexão Psycopg deve usar `autocommit=True`; cada comando de escrita abre uma transação explícita, enquanto leituras curtas não deixam transações ociosas. Todas as consultas usam o schema qualificado e parâmetros posicionais, e conflitos de revisão falham fechados.

`PostgreSqlEvidenceRepository` estende esse contrato com observações, evidências, resultados e proveniência verificável. A conclusão bem-sucedida do probe grava todas as observações e a transição da tentativa na mesma transação; qualquer duplicidade, inconsistência ou falha de persistência desfaz o lote inteiro e produz somente um evento de falha sanitizado. A derivação HTTP também grava evidências, resultados e a transição terminal da análise atomicamente. Uma repetição determinística vira replay auditado sem duplicação; conteúdo parcial ou conflitante é rejeitado.

`PostgreSqlAssessmentJobRepository` implementa duas filas duráveis: coleta por análise e derivação por observação. Os enqueues são idempotentes; workers concorrentes selecionam jobs disponíveis com `FOR UPDATE SKIP LOCKED`, mantêm leases recuperáveis e concluem cada transição por revisão otimista. Se houver interrupção depois de persistir a observação, a retomada a reutiliza e não repete o acesso externo. Retry, esgotamento e ACK são auditados com códigos estáveis, sem mensagens de exceção.

O mesmo adaptador produz snapshots de relatório em uma transação PostgreSQL `REPEATABLE READ READ ONLY`, evitando misturar revisões concorrentes de análises, jobs, evidências e resultados. Acessos ao relatório e eventos das fronteiras API, ASGI e autenticação são gravados na trilha append-only somente com rota lógica, IDs opacos, status e códigos estáveis; URL, corpo, cabeçalhos e credenciais não são registrados. O runtime continua precisando apenas dos grants `SELECT`, `INSERT` e `UPDATE` já documentados, sem DDL.

## Composição ASGI de produção

`archivability.asgi:create_app` é uma factory para servidores ASGI. Ela valida toda a configuração antes de abrir o pool, carrega a metodologia uma vez e cria um repositório PostgreSQL por requisição. O lifespan abre o pool somente no startup, aguarda as conexões mínimas, registra `system.application_lifecycle` e fecha o pool no shutdown. A checagem de cada conexão rejeita papéis com `SUPERUSER`, `CREATEDB`, `CREATEROLE`, `CREATE`, `TEMP`, `TRUNCATE` ou `TRIGGER`, e aceita `DELETE` exclusivamente em `rate_limit_buckets`.

Variáveis obrigatórias:

```text
ARCHIVABILITY_ENVIRONMENT
ARCHIVABILITY_DATABASE_DSN
ARCHIVABILITY_METHODOLOGY_PATH
ARCHIVABILITY_OIDC_ISSUER
ARCHIVABILITY_OIDC_AUDIENCE
ARCHIVABILITY_OIDC_JWKS_URI
```

O DSN deve declarar banco e usuário de runtime. Conexões remotas exigem `sslmode=verify-full` e a factory fixa TLS 1.2 como versão mínima; senha e certificados devem ser injetados pelo ambiente ou pelo mecanismo de segredos da plataforma, nunca versionados. `ARCHIVABILITY_DB_POOL_MIN_SIZE`, `ARCHIVABILITY_DB_POOL_MAX_SIZE`, `ARCHIVABILITY_DB_POOL_TIMEOUT_SECONDS`, `ARCHIVABILITY_DB_POOL_CLOSE_TIMEOUT_SECONDS`, `ARCHIVABILITY_MAX_REQUEST_BODY_BYTES` e `ARCHIVABILITY_MAX_BEARER_TOKEN_BYTES` permitem ajustar somente limites previamente validados.

Exemplo de inicialização, assumindo um servidor ASGI já instalado pelo ambiente de implantação:

```bash
uvicorn --factory archivability.asgi:create_app
```

O limite `ARCHIVABILITY_MAX_RATE_LIMIT_BUCKETS` controla a quantidade máxima de identidades ativas. O limiter usa relógio do PostgreSQL, bloqueio por linha e lock transacional na criação para manter o consumo consistente entre processos e réplicas. Uma implementação alternativa ainda pode ser injetada em `create_production_app` para testes ou outra infraestrutura.

### Saúde e métricas operacionais

A composição de produção expõe três rotas operacionais exatas, antes da autenticação de negócio:

- `GET /health/live` confirma somente que o processo ASGI responde;
- `GET /health/ready` confirma startup, pool PostgreSQL e a migration mínima esperada;
- `GET /internal/metrics` retorna contagens agregadas das filas de coleta e derivação no formato Prometheus.

As respostas não incluem configuração, DSN, IDs de análise, URLs ou payloads. A leitura de métricas gera `access.assessment_queue_metrics` com IP, contagens agregadas e resultado; os probes de saúde não escrevem no banco para que liveness continue independente e readiness possa sinalizar indisponibilidade do próprio banco. A rota de métricas deve permanecer em rede interna e não deve ser publicada pelo Ingress.

## Worker de avaliação

O comando `archivability-worker` consome primeiro a fila PostgreSQL de coleta externa e depois a fila de derivação. A coleta usa resolução validada, IP fixado, TLS verificado, redirects revalidados e limites de tempo, bytes e quantidade. `ARCHIVABILITY_PROBE_ID`, `ARCHIVABILITY_PROBE_COUNTRY` e `ARCHIVABILITY_PROBE_REGION` fixam identidade e localização aproximada declarada na proveniência; omissões de país/região ficam explicitamente como `unknown`. O processo valida a mesma role restrita do servidor, registra `system.assessment_worker_lifecycle` e usa eventos distintos de job para enqueue, claim, retry, falha e sucesso. Ao receber `SIGTERM` ou `SIGINT`, termina o job em andamento e não reivindica outro; interrupções abruptas são recuperadas pelo lease.

Além das variáveis de banco, ambiente e metodologia usadas pelo servidor, o processo aceita limites estritos opcionais:

```text
ARCHIVABILITY_WORKER_ID
ARCHIVABILITY_WORKER_POLL_MILLISECONDS
ARCHIVABILITY_WORKER_LEASE_SECONDS
ARCHIVABILITY_WORKER_RETRY_BASE_SECONDS
ARCHIVABILITY_DB_CONNECT_TIMEOUT_SECONDS
```

Se `ARCHIVABILITY_WORKER_ID` não for definido, cada inicialização gera um UUID opaco. O identificador não deve conter hostname, credencial ou dado pessoal. Um exemplo de execução após instalar o pacote é:

```bash
archivability-worker
```

`archivability-worker-health` valida configuração, conexão, role restrita e migration, retornando apenas o exit code. Ele é adequado para startup/readiness probes e nunca imprime DSN ou detalhes de erro.

## Imagem e implantação inicial

O `Dockerfile` usa usuário sem privilégios e instala o extra `server`, que fixa a linha compatível do Uvicorn. A base em `deploy/kubernetes/base` separa API e worker, define requests/limits, filesystem somente leitura, seccomp, capabilities removidas, probes, tempo de encerramento do worker, PodDisruptionBudget e políticas default-deny.

O manifesto não contém Secret. Antes de aplicar, crie externamente `arquivabilidade-runtime` com a chave `database-dsn`, substitua os endpoints OIDC `.invalid`, use uma imagem por digest e adapte os seletores de rede ao PostgreSQL e ao controlador de entrada reais. Consulte `deploy/kubernetes/README.md` para os pré-requisitos e limitações da política de egress HTTPS.

## Ciclo de vida da análise

O orquestrador controla somente estados e persistência; ele não abre conexões de rede nem executa probes. Análises e tentativas são imutáveis no domínio, e cada transição produz uma nova revisão:

```text
Analysis: requested → running → completed | partially_completed | failed | cancelled
Attempt:  running → succeeded | failed | cancelled
```

```python
from archivability import (
    AnalysisOrchestrator,
    AnalysisState,
    AttemptState,
    SqliteLifecycleRepository,
    load_methodology,
)

repository = SqliteLifecycleRepository(connection)
orchestrator = AnalysisOrchestrator(repository)
methodology = load_methodology("methodology/v0.1.0")

analysis = orchestrator.create_analysis(
    subject_uri="https://example.org/",
    methodology=methodology,
)
attempt = orchestrator.start_attempt(analysis.analysis_id)
orchestrator.finish_attempt(
    analysis.analysis_id,
    attempt.attempt_id,
    target=AttemptState.SUCCEEDED,
)
orchestrator.finalize_analysis(
    analysis.analysis_id,
    target=AnalysisState.COMPLETED,
)
```

O banco aplica concorrência otimista por revisão, limite de tentativas e auditoria das transições. URLs analisadas não são copiadas para os eventos de auditoria.

## Contrato de probes e proteção SSRF

O pacote `archivability.probes` define o contrato dos probes e fornece adaptadores mínimos de resolução DNS e HTTP. Eles não são acionados automaticamente: um probe concreto ainda precisa recebê-los explicitamente. Antes de qualquer conexão, `SsrfPolicy`:

- aceita somente HTTP e HTTPS nas portas configuradas;
- rejeita credenciais, fragmentos, controles, espaços e barras invertidas;
- normaliza hostname e IDNA;
- exige que todos os endereços retornados pelo resolver injetado sejam globais;
- entrega ao probe os IPs já aprovados para conexão direta, evitando nova resolução;
- revalida integralmente cada redirect e limita tempo, bytes, redirects e observações.

`SystemAddressResolver` consulta apenas endereços para TCP e não mantém cache. `PinnedHttpClient` ignora proxies do ambiente, desativa redirects automáticos, conecta somente a um IP presente em `ApprovedTarget.addresses`, preserva o hostname aprovado para `Host`, SNI e validação do certificado, exige TLS 1.2 ou superior e limita timeout total, cabeçalhos e corpo. Cada redirect volta à política antes de uma nova conexão, e downgrade de HTTPS para HTTP é bloqueado por padrão. Firewall e filtragem de saída continuam necessários como segunda camada.

`HttpMetadataProbe` coleta de forma limitada a homepage e, na mesma origem configurada, os caminhos convencionais `/robots.txt` e `/sitemap.xml`. A observação 1.3 contém somente status, cabeçalhos permitidos, contagem de redirects, transporte final, bytes, agregados de HTML/robots/sitemap e contexto de latência. O cliente mede TCP, TLS e TTFB quando essas fases são observáveis; DNS permanece `null` com motivo porque a resolução segura ocorre antes do cronômetro do transporte. Cinco GETs adicionais, espaçados e limitados a um byte, formam a série homogênea de latência para mínimo, máximo, mediana, desvio-padrão e taxa de sucesso; p95 permanece indisponível abaixo de 20 sucessos. Corpos, cookies, IP conectado, URLs descobertas e URLs de redirect não são persistidos nem enviados à auditoria. A execução continua condicionada a uma análise/tentativa válida pelo `ProbeRunner`.

`ProbeExecutionService` conecta o runner ao ciclo de vida. Em caso de sucesso, as observações e a transição do `Attempt` para `succeeded` são persistidas na mesma transação. Falhas do probe produzem `PROBE_FAILED`; falhas de persistência produzem `PERSISTENCE_FAILED`, sempre sem copiar mensagens de exceção para o domínio ou para a auditoria. Os eventos `job.probe`, `data.create`, `job.probe_persistence` e `job.analysis_attempt` distinguem as etapas com metadados não sensíveis.

`derive_http_metadata_indicators` preserva o hash da fonte e produz evidências versionadas para `D01`, `D02`, `D04`, `D05`, `D06`, `R02`, `R04`, `R05`, `R06`, `S01`, `S03`, `S04`, `O01`, `N01` e `N02`. Crawl acrescenta `D03`, `D08`, `R01`, `R03` e `O07`; o browser acrescenta `D07`, `C01`, `C04`, `C05` e `O05` por thresholds declarados como hipótese de projeto. O grafo de dependências preserva até 100 origens externas sanitizadas, ordenadas por frequência, com contagens por tipo; URLs completas, caminhos, parâmetros e fragmentos são descartados. Execução truncada ou ausente gera `UNKNOWN`, sem inferir sucesso. Em análises terminais, indicadores que exigem validadores especializados ou interação adicional aparecem explicitamente como `UNKNOWN`, com confiança zero e evidência versionada `measurement_unavailable`; não são omitidos nem recebem nota zero.

`HttpMetadataAssessmentService` carrega uma observação já persistida, confirma a metodologia e a tentativa bem-sucedida, deriva `Evidence` e `IndicatorResult` e encerra a análise. A gravação dos artefatos e a transição de `Analysis` ocorrem na mesma transação. Repetir exatamente o mesmo processamento produz um replay idempotente auditado, sem duplicar os registros append-only nem avançar novamente a revisão; conteúdo parcial ou conflitante é rejeitado.

As filas de coleta, browser e avaliação formam o pipeline durável. Cada tentativa HTTP possui no máximo um job de browser e cada observação HTTP no máximo um job de derivação. Claims usam lease exclusivo e revisão otimista; falhas recebem backoff exponencial limitado. A retomada fecha tentativas abandonadas e reutiliza observações já persistidas. Todos os eventos são auditados sem URL ou mensagem de exceção, e jobs não podem ser excluídos.

`HttpAssessmentWorkflow` executa somente `Analysis → CollectionJob`. Ele valida a URL e os limites, persiste a análise e retorna `collection_queued` sem DNS, socket ou fetch no processo da API. O worker cria a tentativa, executa o probe e encaminha a observação à derivação. Retries pertencem exclusivamente às filas duráveis.

`AnalysisReportService` cria um snapshot transacional de progresso, tentativas, jobs das três etapas, resultados, proveniência e avaliação versionada. Em análises terminais, o mesmo snapshot calcula dimensões, cobertura, estado geral e o estado declarativo dos gates; gates candidatos continuam inativos até aprovação metodológica explícita. O contrato de saída reduz o alvo à origem (`scheme://host[:port]/`) e não inclui caminho, query string, fragmento, payload, cabeçalhos, DOM ou hashes internos de DOM/URLs. `crawl_runs` e `browser_runs` expõem somente agregados allowlisted. Cada leitura — inclusive análise inexistente — gera `access.analysis_report` com contagens e códigos estáveis, nunca com o conteúdo do relatório.

`GET /v1/analyses/{analysis_id}/export.json` entrega exatamente esse snapshot como JSON determinístico, com `report_schema_version`, limitações metodológicas e `Content-Disposition` de download. A exportação reutiliza a autorização de leitura, rate limit e sanitização do relatório e registra `access.analysis_api_export` sem copiar o conteúdo exportado para a auditoria. O CLEAR+ legado permanece explicitamente indisponível até existir um mapeamento validado entre as evidências atuais e as facetas originais.

`POST /v1/analyses/{analysis_id}/cancel` cancela de forma idempotente uma análise enfileirada ou em execução. A operação exige a permissão explícita `analysis:cancel` e autorização sobre a análise, encerra tentativas ativas e impede novos claims de coleta, browser e derivação. Repetir o comando devolve o mesmo estado terminal sem criar transições adicionais.

`GET /v1/analyses` lista o histórico sanitizado do proprietário autenticado. `GET /v1/analyses/{analysis_id}/comparison` compara uma avaliação terminal com a anterior mais recente somente quando alvo exato, metodologia, versão e conjunto de probes coincidem. A classificação é descritiva (`observed_change` ou `no_observed_change`), sem inferência automática de causalidade ou mudança estrutural. As ações `access.analysis_history` e `access.analysis_comparison` registram apenas IDs opacos, contagens, resultado e código estável.

`GET /v1/sites/{domain}/history` produz a série privada de um hostname normalizado exato. A consulta percorre somente análises pertencentes ao usuário autenticado, nunca agrega subdomínios implicitamente e organiza a resposta em estratos de metodologia, versão e conjunto de probes. O domínio consultado não é copiado para a trilha de auditoria.

`POST /v1/batches` aceita de 1 a 25 URLs previamente validadas e cria análises duráveis independentes. A resposta preserva a posição e os IDs sem repetir os alvos; falhas por item são explícitas e o acesso gera `access.analysis_api_batch_create`. O bucket de rate limit é separado da criação unitária.

`POST /v1/analyses` aceita o cabeçalho opcional `Idempotency-Key` (1–256 caracteres ASCII visíveis). A chave é vinculada ao proprietário autenticado e ao hash da URL/metodologia: um replay idêntico retorna `202` com o mesmo `analysis_id` e `replayed: true`; reutilização com conteúdo diferente retorna `409 IDEMPOTENCY_CONFLICT`. Apenas hashes SHA-256 da chave e da requisição ficam na tabela append-only `analysis_idempotency_keys`; a chave recebida não entra no banco nem na auditoria.

`GET /v1/methodology` publica a identidade, versão, hash da fonte, dimensões, regras de scoring, gates e limitações da mesma configuração carregada pelo motor. `GET /v1/indicators` publica as 45 definições e aceita filtros exatos `version`, `dimension` e `classification`; `GET /v1/indicators/{id-ou-slug}` entrega a documentação individual. Essas leituras exigem `analysis:read` ou `analysis:read:any`, têm rate limit próprio e geram auditoria sem copiar o conteúdo metodológico.

`GET /openapi.json` publica o contrato OpenAPI 3.1 das operações efetivamente implementadas. A especificação declara autenticação Bearer JWT, schemas fechados e limites das entradas de criação unitária e em lote. A leitura exige permissão de análise, compartilha o rate limit metodológico e gera `access.analysis_api_openapi_read`.

## Interface Web

`GET /` serve uma interface responsiva e sem dependências de frontend. Depois da autenticação, a página inicial pede somente a URL; o navegador cria a análise pelo contrato JSON existente e acompanha coleta, browser e derivação até o relatório terminal. A mesma interface lista histórico, exibe dimensões/cobertura e indicadores, solicita comparação compatível e baixa o snapshot canônico.

O relatório humano organiza separadamente achados críticos, condições impeditivas, perfil dimensional, comparação CLEAR+ (ou a indisponibilidade metodológica declarada), indicadores/evidências, contexto de probes, dependências agregadas, detalhes técnicos, recomendações e metodologia. Recomendações mostram problema, evidência, impacto, método de revisão e se a ação depende do administrador do site, do coletor ou de ambos; não transformam contexto de rede em defeito intrínseco.

`GET /methodology` e `GET /methodology/indicators` apresentam a metodologia e o catálogo completos. Cada definição possui uma projeção própria em `GET /methodology/indicators/{id-ou-slug}`, com definição, relevância, medição, evidências, classificação, pontuação, limitações e referências obtidas do pacote versionado — sem uma segunda fonte editorial que possa divergir da execução.

HTML, CSS, JavaScript e favicon são assets locais empacotados com a aplicação. A interface usa CSP sem código inline, `textContent` para dados dinâmicos, `Cache-Control: no-store` nas páginas e não renderiza conteúdo coletado. `access.web_ui` registra acesso às páginas com rota lógica e identidade opaca, sem URL submetida ou conteúdo do relatório.

Em produção, um proxy de identidade compatível com OIDC deve autenticar a sessão do navegador e encaminhar o access token como `Authorization: Bearer` para a aplicação. O token precisa conter `analysis:create`, `analysis:read` e `analysis:cancel`; a aplicação continua verificando assinatura, emissor, audiência, validade e escopos. O proxy deve remover qualquer `Authorization` recebido do cliente antes de inserir o token autenticado.

O Compose local habilita `ARCHIVABILITY_LOCAL_AUTH_ENABLED=true` no arquivo de exemplo para permitir a jornada manual sem provedor institucional. Esse modo usa uma identidade opaca fixa, rejeita qualquer cabeçalho `Authorization`, audita cada autenticação como `auth.local_development` e só inicia quando `ARCHIVABILITY_ENVIRONMENT=development` e o banco é local. A porta da API permanece publicada exclusivamente em `127.0.0.1`. Em qualquer ambiente compartilhado, desabilite a opção e configure o proxy OIDC real.

`AnalysisApi` define a fronteira Web sem acoplamento a framework. O adaptador externo deve fornecer um `ApiPrincipal` já autenticado; políticas injetadas autorizam criação e leitura por análise e aplicam rate limiting antes da validação ou coleta. A fronteira oferece criação unitária e em lote, histórico, leitura, comparação e exportação, sempre com erros estáveis, `Cache-Control: no-store`, `nosniff` e sem mensagens internas. Ausência de autenticação, negação, limite excedido, sucesso e falha são auditados sem corpo, URL analisada ou credencial. Falhas dos provedores de autorização e limite são fechadas como erro interno, sem bypass.

`AnalysisAsgiApp` materializa esse contrato como aplicação ASGI 3 sem dependências externas. Ele mantém uma allowlist explícita de rotas, limita o corpo a 4 KiB por padrão, exige JSON UTF-8, trata corpos fragmentados e emite cabeçalhos defensivos. Rejeições de transporte são auditadas com rota lógica, método, status e código estável; caminhos, query strings, cabeçalhos e corpos não entram no log.

A autenticação continua deliberadamente fora do núcleo. `BearerAuthenticationMiddleware` aceita exclusivamente um cabeçalho `Authorization: Bearer`, limita seu tamanho e entrega o valor opaco a um `BearerTokenVerifier` injetado. Claims não verificados nunca são interpretados pelo middleware; tokens, cabeçalhos e detalhes do provedor nunca entram na auditoria.

`OidcJwtVerifier` é a implementação concreta para access tokens JWT. Sua configuração fixa emissor, audiência, endpoint JWKS, `typ`, claim de sessão e uma allowlist exclusivamente assimétrica (`PS256`, `RS256`, `ES256` ou `EdDSA`). O verificador usa PyJWT com `cryptography`, exige assinatura válida, chave com tamanho mínimo, `iss`, audiência única, `exp`, `iat`, `nbf`, `sub` e sessão, limita duração e clock skew e acessa JWKS somente por HTTPS com timeout, TLS 1.2 mínimo e cache de curta duração. `sub` e sessão são transformados em identificadores SHA-256 opacos antes de chegar à aplicação ou à auditoria.

```python
verifier = OidcJwtVerifier(
    OidcVerifierConfig(
        issuer="https://identity.example.org/realms/archivability",
        audience="arquivabilidade-api",
        jwks_uri="https://identity.example.org/realms/archivability/jwks",
        algorithms=("ES256",),
        token_types=("at+jwt",),
        session_claim="sid",
        permissions_claim="scope",
    )
)
app = BearerAuthenticationMiddleware(
    asgi_app,
    verifier=verifier,
    audit_recorder=repository,
)
```

O claim OAuth2 `scope` é validado como uma lista limitada de permissões e copiado para o `ApiPrincipal` imutável. `OwnershipAuthorizationPolicy` exige `analysis:create` para iniciar análises. A criação grava o usuário opaco como proprietário na mesma transação da análise; `analysis:read` permite consultar somente os próprios relatórios e `analysis:read:any` concede leitura administrativa explícita. A decisão consulta a tabela append-only `analysis_ownership`, nunca a trilha de auditoria. Leituras sem vínculo ou de outro proprietário falham fechadas antes de acessar o relatório, evitando IDOR e sem revelar se o ID existe. `PermissionAuthorizationPolicy` permanece disponível apenas para cenários globais sem conteúdo privado.

`InMemoryTokenBucketRateLimiter` continua disponível para testes e desenvolvimento local. Ele aplica regras distintas por usuário opaco e operação, protege o bucket contra concorrência e limita a quantidade de chaves em memória:

```python
authorization = OwnershipAuthorizationPolicy(repository)
rate_limiter = InMemoryTokenBucketRateLimiter(
    {
        "analysis.create": RateLimitRule(capacity=5, refill_seconds=60),
        "analysis.read": RateLimitRule(capacity=60, refill_seconds=60),
    },
    max_buckets=10_000,
)
```

Em produção, `PostgreSqlTokenBucketRateLimiter` é ligado automaticamente à conexão da requisição. O estado distribuído é transacional, recarregado pelo relógio do banco e limitado por poda conservadora de buckets ociosos. Limites adicionais por IP ainda devem ser aplicados no proxy, especialmente antes da verificação criptográfica de tokens inválidos.

Depois da verificação, o middleware insere objetos já construídos no estado ASGI. O adaptador nunca interpreta diretamente `Authorization`, `X-User-ID` ou `X-Request-ID` enviados pelo cliente:

```python
scope["state"]["archivability.principal"] = ApiPrincipal(
    user_id="identificador-opaco",
    session_id="sessao-opaca",
)
scope["state"]["archivability.request_id"] = "request-id-confiavel"
```

Sucesso e falha de autenticação produzem eventos `auth.bearer_token` com identidade opaca, sessão, IP e correlação quando disponíveis, mas sem o token. Credencial malformada, duplicada ou rejeitada falha fechada com `401` e `WWW-Authenticate`; falha da auditoria impede a autenticação e retorna erro interno sanitizado.

O runner da API executa apenas persistência e enqueue fora do event loop por meio de `ThreadedAsgiSyncRunner`; a coleta externa pertence ao worker. A implantação deve restringir o egress HTTPS da API ao JWKS e o egress HTTP/HTTPS público do worker aos destinos permitidos pelo ambiente. A fronteira e a decisão versionada de robots da Fase 3 estão integradas ao cliente HTTP e à fila durável; `D03`, `D08`, `R01`, `R03` e `O07` são derivados da observação agregada. Fixtures HTTP locais exercitam o transporte real, robots, redirects e quotas sem afrouxar a política SSRF. O relatório público expõe `crawl_runs` validado e sanitizado, sem URLs ou payloads coletados.

A Fase 4 possui contratos determinísticos, fila/worker duráveis, sidecar Playwright isolado, derivação inicial e exposição sanitizada em `browser_runs`. A imagem-base e as dependências são fixadas. Em 2026-10-19, o build e uma renderização HTTPS real foram validados com Chromium sandboxed, usuário não-root, raiz read-only, perfil efêmero, seccomp restritivo e sem segredos, mounts de host ou socket Docker.

As regras seguem o [OWASP SSRF Prevention Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Server_Side_Request_Forgery_Prevention_Cheat_Sheet.html) e a classificação de endereços especiais do [RFC 6890](https://www.rfc-editor.org/rfc/rfc6890.html).

As decisões marcadas como hipótese ou proposta precisam ser revisadas antes do início da implementação.
