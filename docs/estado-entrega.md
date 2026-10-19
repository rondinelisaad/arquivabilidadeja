# Estado de entrega

Data de referência: 2026-10-19.

## Resumo

O produto possui fluxo Web completo para submissão de URL, coleta assíncrona,
crawl limitado, renderização isolada, derivação, scoring, relatório, histórico,
comparação e exportação. A metodologia `0.1.0` continua candidata: o software não
declara validação científica, não ativa gates candidatos e não calcula equivalência
CLEAR+ sem evidência aprovada.

O relatório terminal enumera os 45 indicadores. Vinte e cinco possuem medição
executável; os vinte restantes aparecem como `UNKNOWN`, com confiança zero e
evidência `measurement_unavailable`, em vez de serem omitidos ou receberem nota
zero.

## Critérios por fase

| Fase | Estado técnico | Evidência e limite atual |
|---|---|---|
| 0 — revisão metodológica | candidata | Taxonomia, catálogo, estados e gates estão versionados. Aprovação institucional dos gates, pesos e equivalência CLEAR+ permanece externa. |
| 1 — fundação segura | concluída | Domínio, migrações, SSRF, autenticação/autorização, auditoria append-only, filas e separação de papéis do banco possuem testes. |
| 2 — análise HTTP | concluída | Homepage, robots, sitemap, HTML agregado, cabeçalhos allowlisted, formatos e latência geram observações reproduzíveis sem persistir corpo, cookie, IP ou URL descoberta. |
| 3 — crawl limitado | concluída | Fronteira de mesma origem, robots RFC 9309, redirects revalidados, quotas e resultados agregados estão ligados ao worker durável. |
| 4 — browser isolado | funcional; validação operacional pendente | Contrato, sidecar, budgets, atestação, fila, derivação e testes Node estão implementados. O build/execução da imagem ainda depende de um daemon Docker funcional. |
| 5 — scoring e relatório | concluída tecnicamente | Cobertura, estados, gates inativos, exportação canônica, histórico, comparação e interface Web possuem golden tests. CLEAR+ legado continua nulo por decisão metodológica. |
| 6 — validação científica | infraestrutura concluída; estudo externo pendente | Schema, exemplo e analisador reproduzível existem. Dataset real, especialistas, calibração, holdout e pareceres não podem ser substituídos por dados sintéticos. |
| 7 — escala e probes | implementação de referência concluída | Lotes, identidade/localização declarada de probes, métricas, Compose e manifests estão implementados. Implantação multiprobe real e teste PostgreSQL/Chromium dependem do ambiente operacional. Object storage não se aplica enquanto nenhum artefato binário for persistido. |

## Cobertura executável da metodologia

Indicadores medidos: `D01`–`D08`, `R01`–`R06`, `S01`, `S03`, `S04`,
`C01`, `C04`, `C05`, `O01`, `O05`, `O07`, `N01` e `N02`.

Indicadores ainda sem medição suficiente:

- requisição condicional e identidade canônica: `R07`, `R08`;
- parsing/validação/risco de formatos e encoding: `S02`, `S05`, `S06`, `S07`;
- diversidade, concentração e disponibilidade de dependências: `C02`, `C03`, `C06`;
- metadados descritivos, temporais, direitos e proveniência: `M01`–`M05`;
- limitação, comparação de perfis, autenticação e interação dinâmica: `O02`, `O03`, `O04`, `O06`;
- comparação multiprobe e DNS contextual: `N03`, `N04`.

Esses indicadores exigem novos dados, validadores, perfis autorizados ou mais de um
probe. O sistema não infere estados favoráveis ou desfavoráveis sem a evidência
declarada no catálogo.

## Verificações executadas

- suíte Python: `274 passed`, `23 skipped`, `91 subtests passed`;
- sidecar Node: `3 passed`;
- sintaxe JavaScript da interface: válida;
- `docker compose --env-file deploy/compose/.env.example config --quiet`: válido;
- diff Git: sem erros de whitespace;
- varredura das adições antes dos commits: nenhum padrão de segredo encontrado.

Os 23 skips são integrações PostgreSQL/Docker condicionadas por variável de ambiente.
Em 2026-10-19, o Docker Desktop abriu, mas permaneceu em `Resource Saver mode` após
tentativas de `Resume`; o cliente continuou sem acesso ao socket do daemon. Por isso,
build do Chromium, subida do Compose e testes PostgreSQL não foram declarados como
executados.

## Bloqueios para release pública

1. escolher e registrar a licença open source; `pyproject.toml` ainda declara a
   licença como não definida;
2. aprovar responsável, papéis LGPD, finalidade, base legal, retenção, exclusão e
   transferências entre probes;
3. configurar OIDC, DNS/TLS, segredos e políticas de egress do ambiente alvo;
4. restabelecer o daemon Docker e executar o runbook operacional completo;
5. realizar a validação científica externa antes de promover a metodologia de
   candidata ou ativar gates;
6. implementar os vinte métodos restantes somente quando seus contratos de dados e
   decisões metodológicas estiverem aprovados.

Nenhum desses itens deve ser preenchido automaticamente com uma escolha jurídica,
institucional ou científica presumida.
