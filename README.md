# Arquivabilidade JA

Este repositório contém o desenho de uma plataforma aberta para avaliar a arquivabilidade de websites. A proposta usa o CLEAR+ como referência histórica, mas separa propriedades do website, comportamento durante a coleta e condições externas de observação.

## Estado do projeto

O projeto está na fase inicial de implementação do domínio. Já existem uma metodologia executável, o motor determinístico de pontuação, a cadeia imutável `Observation → Evidence → IndicatorResult`, o ciclo de vida local `Analysis → Attempt`, adaptadores mínimos de DNS/HTTP protegidos contra SSRF, o primeiro probe de metadados HTTP, uma fila local, um contrato de API e um adaptador ASGI. SQLite é usado somente em desenvolvimento e testes.

- [Proposta de arquitetura e metodologia](docs/proposta-arquitetura-metodologia.md)
- [Catálogo inicial de indicadores](docs/catalogo-inicial-indicadores.md)
- [Revisão crítica e matriz de rastreabilidade do CLEAR+](docs/revisao-critica-clear-plus.md)
- [Requisitos de segurança](docs/requisitos-seguranca.md)
- [Métodos de derivação HTTP](docs/metodos-derivacao-http.md)
- [Metodologia executável candidata 0.1.0](methodology/v0.1.0/README.md)

## Validação

Os arquivos da metodologia usam o subconjunto JSON do YAML 1.2 e os golden tests não exigem pacotes externos:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_*.py' -v
```

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

Em produção, o próximo adaptador deverá usar PostgreSQL com usuários distintos para migrations e runtime. O usuário da aplicação deve possuir somente `SELECT` e `INSERT` nas tabelas append-only, sem `CREATE`, `ALTER`, `DROP`, `TRUNCATE` ou privilégios administrativos.

As tabelas mutáveis de ciclo de vida (`analyses`, `attempts` e `assessment_jobs`) também exigirão `UPDATE`, sempre protegido por revisão otimista. O runtime não necessita `DELETE` nem qualquer permissão DDL. A migration da fila deve ser aplicada pelo usuário separado de migrations; o usuário da aplicação recebe apenas `SELECT`, `INSERT` e `UPDATE` em `assessment_jobs`. A tabela append-only `analysis_ownership` exige apenas `SELECT` e `INSERT`: o vínculo é criado na mesma transação da análise e não pode ser alterado ou excluído.

O schema PostgreSQL de produção é provisionado separadamente com uma conexão ociosa, transacional e pertencente ao usuário exclusivo de migrations. O executor serializa concorrentes com advisory lock, registra versão, checksum, ator e instante de aplicação e recusa migrations já aplicadas cujo checksum tenha mudado:

```python
from archivability import apply_postgresql_migrations

apply_postgresql_migrations(migration_connection)
```

Depois do provisionamento, um administrador aplica `deploy/postgresql/runtime_grants.sql` com nomes de roles e banco fornecidos como variáveis do `psql`. O script não cria usuários nem contém credenciais: ele remove privilégios implícitos, concede somente `CONNECT`/`USAGE` e o DML necessário por tabela. A role de runtime deve ser criada externamente como `NOSUPERUSER NOCREATEDB NOCREATEROLE` e usar TLS/SCRAM conforme a política do ambiente; a credencial vem do cofre ou do mecanismo de secrets, nunca do repositório.

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

`HttpMetadataProbe` converte somente status, cabeçalhos permitidos, contagem de redirects, transporte final e tamanho observado em uma `Observation` imutável. Corpos, cookies e URLs de redirect não são persistidos nem enviados à auditoria. A execução continua condicionada a uma análise/tentativa válida pelo `ProbeRunner`.

`ProbeExecutionService` conecta o runner ao ciclo de vida. Em caso de sucesso, as observações e a transição do `Attempt` para `succeeded` são persistidas na mesma transação. Falhas do probe produzem `PROBE_FAILED`; falhas de persistência produzem `PERSISTENCE_FAILED`, sempre sem copiar mensagens de exceção para o domínio ou para a auditoria. Os eventos `job.probe`, `data.create`, `job.probe_persistence` e `job.analysis_attempt` distinguem as etapas com metadados não sensíveis.

`derive_http_metadata_indicators` transforma a observação HTTP em evidências versionadas para `D01` (acessibilidade da homepage) e `R06` (completude da resposta), preservando o hash da fonte. As regras distinguem sucesso, redirect não terminado, erro HTTP, truncamento por limite e inconsistência de `Content-Length`. Os demais indicadores permanecem sem resultado até existirem as evidências específicas exigidas pela metodologia.

`HttpMetadataAssessmentService` carrega uma observação já persistida, confirma a metodologia e a tentativa bem-sucedida, deriva `Evidence` e `IndicatorResult` e encerra a análise. A gravação dos artefatos e a transição de `Analysis` ocorrem na mesma transação. Repetir exatamente o mesmo processamento produz um replay idempotente auditado, sem duplicar os registros append-only nem avançar novamente a revisão; conteúdo parcial ou conflitante é rejeitado.

`HttpAssessmentQueueService` e `HttpAssessmentWorker` fornecem uma fila SQLite local e durável para essa derivação. Cada observação possui no máximo um job; a reivindicação usa lease exclusivo e revisão otimista, falhas recebem backoff exponencial limitado e um worker pode recuperar leases expirados. O ACK acontece depois da avaliação: se houver interrupção entre as duas etapas, a recuperação executa o replay idempotente. Os eventos de enqueue, claim, retry, sucesso e falha são auditados sem URL ou mensagem de exceção, e jobs não podem ser excluídos.

`HttpAssessmentWorkflow` oferece o caso de uso explícito `Analysis → Attempt → Probe → AssessmentJob`. Ele valida limites antes da coleta, aceita somente o contrato `http-metadata`, converte saída incompatível do probe em falha de tentativa antes da persistência e permite retry manual apenas após uma tentativa realmente falha. Uma tentativa bem-sucedida gera exatamente um job durável e bloqueia novas coletas para a mesma análise. O evento agregado `job.http_assessment_workflow` registra o desfecho somente com IDs opacos e códigos estáveis. O workflow não é endpoint, scheduler ou daemon: a aplicação chamadora ainda precisa acioná-lo explicitamente e fornecer o probe.

`AnalysisReportService` cria um snapshot transacional de progresso, tentativas, jobs, resultados e proveniência. O contrato de saída reduz o alvo à origem (`scheme://host[:port]/`) e não inclui caminho, query string, fragmento, payload de observação, cabeçalhos ou dados derivados internos. A proveniência pública contém somente IDs, hashes, métodos, versões e timestamps. Cada leitura — inclusive análise inexistente — gera `access.analysis_report` com contagens e códigos estáveis, nunca com o conteúdo do relatório.

`AnalysisApi` define a fronteira Web sem acoplamento a framework. O adaptador externo deve fornecer um `ApiPrincipal` já autenticado; políticas injetadas autorizam criação e leitura por análise e aplicam rate limiting antes da validação ou coleta. A fronteira oferece as operações equivalentes a `POST /v1/analyses` e `GET /v1/analyses/{analysis_id}`, sempre com erros estáveis, `Cache-Control: no-store`, `nosniff` e sem mensagens internas. Ausência de autenticação, negação, limite excedido, sucesso e falha são auditados sem corpo, URL analisada ou credencial. Falhas dos provedores de autorização e limite são fechadas como erro interno, sem bypass.

`AnalysisAsgiApp` materializa esse contrato como aplicação ASGI 3 sem dependências externas. Ele aceita somente as duas rotas previstas, limita o corpo a 4 KiB por padrão, exige JSON UTF-8, trata corpos fragmentados e emite cabeçalhos defensivos. Rejeições de transporte são auditadas com rota lógica, método, status e código estável; caminhos, query strings, cabeçalhos e corpos não entram no log.

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

`InMemoryTokenBucketRateLimiter` aplica regras distintas por usuário opaco e operação. O bucket é protegido contra concorrência, recarrega com relógio monotônico, nega operações desconhecidas e limita a quantidade de chaves em memória sem expulsar buckets ativos para abrir espaço a um atacante:

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

O limiter local é adequado a desenvolvimento ou uma única instância. Uma implantação com vários processos ou réplicas deve substituí-lo por um backend distribuído que preserve a mesma interface e aplique limites adicionais por IP no proxy, especialmente antes da verificação criptográfica de tokens inválidos.

Depois da verificação, o middleware insere objetos já construídos no estado ASGI. O adaptador nunca interpreta diretamente `Authorization`, `X-User-ID` ou `X-Request-ID` enviados pelo cliente:

```python
scope["state"]["archivability.principal"] = ApiPrincipal(
    user_id="identificador-opaco",
    session_id="sessao-opaca",
)
scope["state"]["archivability.request_id"] = "request-id-confiavel"
```

Sucesso e falha de autenticação produzem eventos `auth.bearer_token` com identidade opaca, sessão, IP e correlação quando disponíveis, mas sem o token. Credencial malformada, duplicada ou rejeitada falha fechada com `401` e `WWW-Authenticate`; falha da auditoria impede a autenticação e retorna erro interno sanitizado.

O runner padrão executa o núcleo síncrono no mesmo worker, preservando a afinidade da conexão SQLite usada em desenvolvimento. Uma implantação concorrente deve injetar um `AsgiSyncRunner` compatível com o pool do banco de produção. A implantação ainda deve preencher os valores do provedor OIDC por configuração segura, restringir egress ao host JWKS e substituir o limiter local quando houver múltiplas réplicas. O próximo passo recomendado é portar as operações do repositório para PostgreSQL sobre o schema já versionado, preservando transações, autorização por objeto e revisão otimista.

As regras seguem o [OWASP SSRF Prevention Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Server_Side_Request_Forgery_Prevention_Cheat_Sheet.html) e a classificação de endereços especiais do [RFC 6890](https://www.rfc-editor.org/rfc/rfc6890.html).

As decisões marcadas como hipótese ou proposta precisam ser revisadas antes do início da implementação.
