# Fase 5 — scoring e relatório

Status: scoring e exportação canônica implementados; histórico e comparação entre execuções permanecem no próximo incremento desta fase.

## Snapshot versionado

O relatório usa um único snapshot transacional para progresso, jobs, tentativas, observações agregadas, evidências, resultados e avaliação. `report_schema_version` começa em `1.0`; a resposta normal e a exportação JSON são projeções do mesmo objeto para evitar divergência entre a tela e o arquivo baixado.

Análises ainda em execução retornam `assessment: null`. Quando a análise termina com resultado completo ou parcial, o `ScoringEngine` avalia todos os indicadores declarados aplicáveis pela versão da metodologia. Resultados ausentes reduzem cobertura e permanecem desconhecidos, sem receber zero. O relatório inclui dimensões, cobertura, estado geral, gates, resultados e proveniência por hash.

Os gates candidatos da metodologia 0.1.0 não são ativados implicitamente. Eles aparecem como `inactive` até uma revisão metodológica aprovar sua ativação. O score CLEAR+ legado também permanece nulo: inferir as quatro facetas originais a partir dos indicadores novos sem um mapeamento validado criaria uma falsa equivalência científica.

## Exportação e auditoria

`GET /v1/analyses/{analysis_id}/export.json` exige a mesma autenticação, autorização por proprietário e rate limit da leitura do relatório. A resposta usa JSON determinístico, `Cache-Control: no-store`, `nosniff` e `Content-Disposition` com identificador já validado.

A ação `access.analysis_api_export` registra request, ator/sessão opacos, IP, análise, status e código estável. URL, relatório, evidências, credencial e conteúdo exportado não são copiados para o evento. O acesso ao snapshot continua gerando `access.analysis_report`, permitindo distinguir geração e entrega sem perder rastreabilidade.

## Limitações expostas

O arquivo declara que pesos e thresholds ainda são hipótese versionada, que gates candidatos não estão ativos e que a comparação CLEAR+ não está disponível. Essas limitações fazem parte do contrato público e não dependem de documentação externa à exportação.

## Próximo incremento

1. histórico privado por proprietário e origem normalizada;
2. comparação somente entre metodologia, escopo e configuração compatíveis;
3. interface server-rendered para submissão de URL, progresso, relatório e download.
