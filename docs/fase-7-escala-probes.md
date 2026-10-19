# Fase 7 — escala operacional e probes

## Entregas

### Lotes limitados

`POST /v1/batches` recebe de 1 a 25 itens no formato:

```json
{
  "subjects": [
    {"subject_uri": "https://example.org/"},
    {"subject_uri": "https://example.net/"}
  ]
}
```

A rota exige a mesma permissão `analysis:create`, usa um bucket próprio
`analysis.batch_create` e valida todos os itens antes da primeira escrita. URLs
duplicadas, credenciais embutidas, esquemas diferentes de HTTP(S), campos extras e
corpos acima do limite são rejeitados.

Cada item é uma análise independente e durável. A resposta `202` não repete URLs:
ela contém `batch_id`, posição, `analysis_id`, caminho do relatório e contagens de
aceitos/falhos. Uma falha operacional em um item não desfaz análises já enfileiradas;
o histórico privado permite recuperá-las. O `batch_id` é uma correlação da resposta,
não um agregado persistente, e não deve ser usado como garantia transacional.

### Identidade e estratificação de probes

Cada implantação de worker define `ARCHIVABILITY_PROBE_ID`, um identificador estável,
opaco e sem localização precisa. As observações HTTP e de browser recebem os sufixos
`-http` e `-browser`. O relatório `1.1` expõe `probe_ids` e `tool_versions` em cada
resultado; o histórico também lista os probes usados.

Comparações automáticas só escolhem baseline com o mesmo alvo, metodologia, versão e
conjunto de probes. Diferenças entre probes permanecem contexto de observação — não
são convertidas em defeito intrínseco nem em causalidade geográfica.

Para operar mais de um probe, implante stacks independentes com IDs e redes distintas.
Não compartilhe o mesmo ID entre localidades. O banco pode ser comum somente quando
a política institucional, TLS, latência e segregação de credenciais tiverem sido
avaliados; fora disso, exporte JSONs canônicos e faça a análise estratificada offline.

### Observabilidade

`GET /internal/metrics` inclui os totais do pipeline e a série
`archivability_queue_stage_jobs{stage,state}` para `collection`, `browser` e
`assessment`. Os labels são fixos para impedir cardinalidade não limitada; IDs, URLs,
usuários e mensagens de erro não entram nas métricas. A leitura continua auditada.

### Docker Compose local

O arquivo [`compose.yaml`](../compose.yaml) inicia PostgreSQL, migração, grants,
API, worker e sidecar Chromium. Migração e runtime usam papéis diferentes; o runtime
é validado contra privilégios administrativos/DDL. API e worker usam o socket Unix
compartilhado, enquanto worker e browser compartilham somente o namespace de rede
necessário para o renderer loopback.

```bash
cp deploy/compose/.env.example deploy/compose/.env
# substitua senhas e parâmetros OIDC
docker compose --env-file deploy/compose/.env up --build
```

A API fica em `http://127.0.0.1:8080` por padrão. A interface de negócio exige OIDC;
`/health/live`, `/health/ready` e `/internal/metrics` são rotas operacionais. Não
publique métricas nem PostgreSQL na Internet.

O comando `archivability-migrate` aceita a conexão exclusivamente por
`ARCHIVABILITY_MIGRATION_DATABASE_DSN`, rejeita usuários administrativos genéricos e
exige `sslmode=verify-full` em conexões remotas. A tabela `schema_migrations` registra
versão, checksum, executor e data.

## Object storage

Nenhum object storage é iniciado nesta versão porque o produto deliberadamente não
persiste corpos HTTP, DOM, screenshots, WARC, URLs descobertas ou console do browser.
Adicionar MinIO/S3 sem artefatos autorizados aumentaria superfície de ataque, custo e
risco de retenção sem cumprir uma necessidade funcional.

Se o escopo futuro aprovar artefatos binários, a evolução deve incluir antes:

- classificação, base legal, retenção e deleção verificável;
- bucket privado por ambiente, identidade própria e criptografia gerenciada;
- nomes opacos, hash, tamanho, MIME allowlisted e vínculo append-only;
- upload fora da API, URLs assinadas curtas e auditoria de leitura;
- limites por análise/lote, antivírus quando aplicável e teste de restauração.

Até essa decisão, os JSONs canônicos versionados são os artefatos de pesquisa e podem
ser armazenados externamente pelo pesquisador sem conceder acesso do sistema a um
bucket.

## Limites operacionais

- O Compose é uma implantação local de referência, não alta disponibilidade.
- Escala horizontal exige observar leases, pool do banco, quotas por destino e egress.
- O build do sidecar Chromium precisa ser validado novamente quando o daemon Docker
  estiver disponível.
- A identidade do probe registra contexto; não prova localização nem causa de uma
  divergência.
