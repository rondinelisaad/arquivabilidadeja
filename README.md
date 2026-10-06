# Arquivabilidade JA

Este repositório contém o desenho de uma plataforma aberta para avaliar a arquivabilidade de websites. A proposta usa o CLEAR+ como referência histórica, mas separa propriedades do website, comportamento durante a coleta e condições externas de observação.

## Estado do projeto

O projeto está na fase inicial de implementação do domínio. Já existem uma metodologia executável, o motor determinístico de pontuação, a cadeia imutável `Observation → Evidence → IndicatorResult` e o ciclo de vida local `Analysis → Attempt`. Ainda não há API, fila ou coleta em rede; SQLite é usado somente em desenvolvimento e testes.

- [Proposta de arquitetura e metodologia](docs/proposta-arquitetura-metodologia.md)
- [Catálogo inicial de indicadores](docs/catalogo-inicial-indicadores.md)
- [Revisão crítica e matriz de rastreabilidade do CLEAR+](docs/revisao-critica-clear-plus.md)
- [Requisitos de segurança](docs/requisitos-seguranca.md)
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

As tabelas mutáveis de ciclo de vida (`analyses` e `attempts`) também exigirão `UPDATE`, sempre protegido por revisão otimista. O runtime não necessita `DELETE` nem qualquer permissão DDL.

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

As decisões marcadas como hipótese ou proposta precisam ser revisadas antes do início da implementação.
