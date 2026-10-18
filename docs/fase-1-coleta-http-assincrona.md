# Fase 1 — coleta HTTP assíncrona

Status: concluída em 2026-10-18.

## Resultado

O `POST /v1/analyses` valida e persiste a solicitação, cria um `collection_job` e retorna `collection_queued`. Nenhuma resolução DNS ou conexão com o website é feita pelo processo da API.

O worker executa duas etapas duráveis:

1. `collection_jobs`: cria a tentativa, aplica a política SSRF e coleta homepage, `/robots.txt` e `/sitemap.xml` sob limites de tempo, bytes e redirects;
2. `assessment_jobs`: deriva evidências e resultados a partir da observação já persistida e finaliza a análise.

Cada etapa usa enqueue idempotente, claim com lease, revisão otimista, retry com backoff e eventos de auditoria sanitizados. Uma interrupção depois da persistência da observação não provoca nova coleta: o worker retomado reutiliza a observação e completa o enqueue seguinte. Um lease vencido também encerra a tentativa abandonada antes de abrir outra.

## Dados coletados

A observação `http_metadata` 1.2 persiste apenas:

- status e cabeçalhos allowlisted da homepage;
- bytes observados, truncamento, redirects, TLS final e tempo total;
- MIME declarado e família de formato;
- contagens agregadas de links internos, externos, feeds e canonical;
- disponibilidade, status, parseabilidade e contagens agregadas de robots e sitemap.

Corpos, cookies, URLs descobertas, destinos de redirect e mensagens de exceção não são persistidos nem registrados na auditoria.

Os indicadores derivados nesta camada são `D01`, `D02`, `D04`, `D05`, `D06`, `R02`, `R04`, `R05`, `R06`, `S01`, `S03`, `S04` e `N01`. A evidência identifica explicitamente as limitações da amostra de homepage e da classificação baseada no MIME declarado.

## Segurança e operação

- todos os endereços DNS precisam ser globais e cada redirect é revalidado;
- a conexão usa os IPs aprovados, com hostname preservado para `Host`, SNI e certificado;
- TLS exige versão 1.2 ou superior;
- a role PostgreSQL de runtime não possui DDL nem privilégios administrativos;
- a API tem egress público somente para HTTPS/JWKS; o worker recebe HTTP/HTTPS público com redes IPv4 privadas e especiais excluídas;
- métricas somam as filas de coleta e derivação sem expor IDs ou alvos.

## Critérios verificados

- suíte unitária e de integração em SQLite e PostgreSQL;
- replay após interrupção e recuperação de lease;
- migrations e grants com role separada;
- análise estática dos arquivos alterados;
- auditoria de dependências sem vulnerabilidades conhecidas;
- busca de segredos antes dos commits.

## Continuidade

A Fase 3 acrescentou ao job durável a fronteira por origem, profundidade e quantidade máximas, orçamento global de tempo/bytes, classificação de descartes, links quebrados, grafo agregado e política aplicável de robots. A derivação dos indicadores de crawl é o incremento seguinte. Browser/JavaScript permanece fora dessa fase.
