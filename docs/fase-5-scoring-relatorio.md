# Fase 5 — scoring e relatório

Status: scoring, exportação canônica, histórico privado, comparação entre execuções e interface Web implementados.

## Snapshot versionado

O relatório usa um único snapshot transacional para progresso, jobs, tentativas, observações agregadas, evidências, resultados e avaliação. `report_schema_version` começou em `1.0`, passou a `1.1` ao expor `probe_ids` e `tool_versions` por resultado, a `1.2` ao incluir país/região declarados, janela observada e ferramentas por probe e a `1.3` ao tornar explícitos o resumo e os dados agregados seguros de cada evidência; a resposta normal e a exportação JSON são projeções do mesmo objeto para evitar divergência entre a tela e o arquivo baixado.

Análises ainda em execução retornam `assessment: null`. Quando a análise termina com resultado completo ou parcial, o `ScoringEngine` avalia todos os indicadores declarados aplicáveis pela versão da metodologia. O relatório terminal enumera o catálogo completo: uma medição ainda não implementada gera evidência `measurement_unavailable`, estado `UNKNOWN`, confiança zero e motivo estável `measurement_not_implemented`. Ela reduz a cobertura e nunca recebe nota zero nem desaparece silenciosamente. Indicadores contextuais indisponíveis também podem usar `UNKNOWN`, sem participar do score. O relatório inclui dimensões, cobertura, estado geral, gates, resultados e proveniência por hash.

Os gates candidatos da metodologia 0.1.0 não são ativados implicitamente. Eles aparecem como `inactive` até uma revisão metodológica aprovar sua ativação. O score CLEAR+ legado também permanece nulo: inferir as quatro facetas originais a partir dos indicadores novos sem um mapeamento validado criaria uma falsa equivalência científica.

## Exportação e auditoria

`GET /v1/analyses/{analysis_id}/export.json` exige a mesma autenticação, autorização por proprietário e rate limit da leitura do relatório. A resposta usa JSON determinístico, `Cache-Control: no-store`, `nosniff` e `Content-Disposition` com identificador já validado.

A ação `access.analysis_api_export` registra request, ator/sessão opacos, IP, análise, status e código estável. URL, relatório, evidências, credencial e conteúdo exportado não são copiados para o evento. O acesso ao snapshot continua gerando `access.analysis_report`, permitindo distinguir geração e entrega sem perder rastreabilidade.

## Limitações expostas

O arquivo declara que pesos e thresholds ainda são hipótese versionada, que gates candidatos não estão ativos e que a comparação CLEAR+ não está disponível. Essas limitações fazem parte do contrato público e não dependem de documentação externa à exportação.

## Leitura humana do resultado

A interface organiza o mesmo snapshot em achados críticos, gates, perfil dimensional, comparação CLEAR+, indicadores/evidências, contexto de probes, dependências agregadas, detalhes técnicos e recomendações. Definições e severidades vêm do catálogo da mesma versão metodológica.

Recomendações são geradas somente para estados `FAIL` e `WARNING` e exibem problema, quantidade de evidências, confiança, impacto documentado, método de revisão e responsabilidade provável. Indicadores de `observation_context` permanecem contextuais; dependências são mostradas por agregados e por origens sanitizadas. URLs completas e conteúdo capturado não são persistidos pelo escopo atual.

## Histórico e comparação

`GET /v1/analyses` lista até 20 análises do proprietário autenticado em ordem decrescente de atualização. Cada item contém somente origem normalizada, versão metodológica, estado, tempos e resumo dimensional; caminho, query string, payloads e URLs descobertas não são expostos.

`GET /v1/analyses/{analysis_id}/comparison` procura a execução anterior mais recente do mesmo proprietário, alvo exato, metodologia e versão. Somente avaliações terminais são comparáveis. A saída enumera alterações de estado ou score por indicador e usa apenas `observed_change` ou `no_observed_change`. Uma execução isolada nunca é promovida automaticamente a “mudança estrutural”, e a resposta declara que não estabelece causalidade.

Leituras de histórico e comparação geram `access.analysis_history` e `access.analysis_comparison`, contendo apenas operação, análise opaca quando aplicável, contagens e código estável de erro.

## Próximo incremento

1. protocolo e dataset para a validação científica externa;
2. ativação de gates somente depois da decisão metodológica correspondente;
3. mapeamento CLEAR+ legado somente depois de validação explícita.

O critério técnico da fase é coberto por golden tests do `ScoringEngine` e por um hash golden do snapshot exportável completo na versão `1.3`.
