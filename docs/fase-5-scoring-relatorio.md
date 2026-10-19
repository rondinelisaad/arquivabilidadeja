# Fase 5 — scoring e relatório

Status: scoring, exportação canônica, histórico privado, comparação entre execuções e interface Web implementados.

## Snapshot versionado

O relatório usa um único snapshot transacional para progresso, jobs, tentativas, observações agregadas, evidências, resultados e avaliação. `report_schema_version` começou em `1.0` e passou a `1.1` ao expor `probe_ids` e `tool_versions` por resultado; a resposta normal e a exportação JSON são projeções do mesmo objeto para evitar divergência entre a tela e o arquivo baixado.

Análises ainda em execução retornam `assessment: null`. Quando a análise termina com resultado completo ou parcial, o `ScoringEngine` avalia todos os indicadores declarados aplicáveis pela versão da metodologia. Resultados ausentes reduzem cobertura e permanecem desconhecidos, sem receber zero. O relatório inclui dimensões, cobertura, estado geral, gates, resultados e proveniência por hash.

Os gates candidatos da metodologia 0.1.0 não são ativados implicitamente. Eles aparecem como `inactive` até uma revisão metodológica aprovar sua ativação. O score CLEAR+ legado também permanece nulo: inferir as quatro facetas originais a partir dos indicadores novos sem um mapeamento validado criaria uma falsa equivalência científica.

## Exportação e auditoria

`GET /v1/analyses/{analysis_id}/export.json` exige a mesma autenticação, autorização por proprietário e rate limit da leitura do relatório. A resposta usa JSON determinístico, `Cache-Control: no-store`, `nosniff` e `Content-Disposition` com identificador já validado.

A ação `access.analysis_api_export` registra request, ator/sessão opacos, IP, análise, status e código estável. URL, relatório, evidências, credencial e conteúdo exportado não são copiados para o evento. O acesso ao snapshot continua gerando `access.analysis_report`, permitindo distinguir geração e entrega sem perder rastreabilidade.

## Limitações expostas

O arquivo declara que pesos e thresholds ainda são hipótese versionada, que gates candidatos não estão ativos e que a comparação CLEAR+ não está disponível. Essas limitações fazem parte do contrato público e não dependem de documentação externa à exportação.

## Histórico e comparação

`GET /v1/analyses` lista até 20 análises do proprietário autenticado em ordem decrescente de atualização. Cada item contém somente origem normalizada, versão metodológica, estado, tempos e resumo dimensional; caminho, query string, payloads e URLs descobertas não são expostos.

`GET /v1/analyses/{analysis_id}/comparison` procura a execução anterior mais recente do mesmo proprietário, alvo exato, metodologia e versão. Somente avaliações terminais são comparáveis. A saída enumera alterações de estado ou score por indicador e usa apenas `observed_change` ou `no_observed_change`. Uma execução isolada nunca é promovida automaticamente a “mudança estrutural”, e a resposta declara que não estabelece causalidade.

Leituras de histórico e comparação geram `access.analysis_history` e `access.analysis_comparison`, contendo apenas operação, análise opaca quando aplicável, contagens e código estável de erro.

## Próximo incremento

1. protocolo e dataset para a validação científica externa;
2. ativação de gates somente depois da decisão metodológica correspondente;
3. mapeamento CLEAR+ legado somente depois de validação explícita.

O critério técnico da fase é coberto por golden tests do `ScoringEngine` e por um hash golden do snapshot exportável completo na versão `1.1`.
