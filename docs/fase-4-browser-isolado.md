# Fase 4 — browser isolado

Status: implementação funcional concluída em 2026-10-18; validação operacional da imagem pendente do daemon Docker local.

## Contrato inicial

O pacote `archivability.browser` define a fronteira entre o processo principal e um futuro worker Chromium isolado. Este incremento não inicia navegador nem habilita nova coleta externa; ele torna explícitos e testáveis os invariantes que qualquer adaptador terá de cumprir.

`BrowserRenderRequest` transporta somente IDs da análise/tentativa, URL validada, budgets e um perfil determinístico. O perfil fixa viewport, User-Agent honesto `arquivabilidade-ja-browser/0.1`, locale `en-US`, timezone `UTC`, cache desabilitado, movimento reduzido e service workers bloqueados. Os budgets limitam navegação, janela de observação, tempo total, quantidade de requests, bytes por resposta e no total, DOM e eventos de console.

`BrowserIsolationAttestation` falha fechado se o sandbox estiver desligado, o processo executar como root, o filesystem raiz for gravável, o perfil não for efêmero ou houver mounts, segredos ou socket Docker disponíveis. Essa atestação é parte do resultado e deverá ser produzida pelo launcher isolado, não pelo site analisado.

`BrowserRenderAggregate` não aceita DOM, corpos, mensagens de console nem URLs. Ele contém apenas hashes SHA-256 de integridade, contagens de URLs e requests, escopo interno/externo, requests de API, falhas, bloqueios, bytes, tamanho do DOM, tempo e motivo de término. `BrowserRenderResult.validate_against` exige identidade correspondente, timestamps coerentes, isolamento seguro e respeito aos budgets.

## Execução, persistência e auditoria

`BrowserExecutionService` autoriza o vínculo com uma tentativa HTTP concluída, exige resultado compatível com a requisição, cria uma observação imutável `browser_render` 1.0 e persiste somente o contrato agregado. Uma repetição do mesmo trabalho reutiliza a observação existente sem executar novamente o renderer; mudanças de budgets ou perfil falham fechadas.

SQLite e PostgreSQL localizam no máximo uma observação de browser por tentativa e validam o estado do ciclo de vida antes da execução. A ação auditável `job.browser_render` registra início, sucesso, reuso ou falha com código estável. Os eventos incluem apenas IDs opacos, budgets, perfil, política de isolamento e contagens; não incluem URL, DOM, hashes do conteúdo, mensagens do console ou corpos de resposta.

## Contrato da fila

`BrowserJob` e `BrowserQueueService` definem enqueue idempotente por tentativa HTTP, perfil e budgets imutáveis, leases, recuperação de lease expirado, retries limitados e estados terminais. Apenas uma tentativa HTTP registrada e bem-sucedida de uma análise em execução pode originar o job.

SQLite e PostgreSQL persistem a fila com revisão otimista, constraint de unicidade sobre a tentativa, índice de claim e bloqueio de exclusão. O runtime PostgreSQL recebe somente `SELECT`, `INSERT` e `UPDATE` sobre `browser_jobs`; o validador de inicialização exige esses grants e continua recusando DDL, `TRUNCATE`, `TRIGGER` e `DELETE`. Enqueue, claim, retry, sucesso e falha usam eventos `job.browser_queue_*` sanitizados.

`BrowserWorker` consome essa fila separadamente, monta a requisição somente com a análise/tentativa autorizada e entrega o agregado ao `BrowserExecutionService`. Depois de um render válido, ele enfileira a observação HTTP original para a derivação existente; se o enqueue falhar, a repetição reutiliza a observação de browser já persistida. Falhas usam códigos estáveis e backoff exponencial limitado. Ao esgotar tentativas de browser, o worker ainda enfileira a avaliação HTTP como resultado degradado, preservando o erro terminal do browser sem deixar a análise permanentemente em execução.

## Sidecar Chromium e ativação

`LoopbackBrowserRenderer` aceita somente um endpoint HTTP explícito em loopback, limita request/response e valida o JSON integral antes de criar o resultado. O sidecar Node/Playwright fixa versão e integridade das dependências e da imagem-base, escuta apenas em `127.0.0.1`, processa um render por vez e não registra request, URL, DOM ou erro bruto.

Cada navegação e subrequest revalida esquema, credenciais, DNS e todos os endereços resolvidos, bloqueando redes privadas, loopback, link-local, documentação, multicast e especiais. A NetworkPolicy do pod fornece a segunda barreira contra rebinding. O resultado guarda somente hashes, contagens e medidas truncadas nos budgets.

O container só responde saudável quando confirma sandbox habilitado, UID não-root, raiz read-only, perfil efêmero e ausência de secrets, mounts de host e socket Docker. No Kubernetes ele não recebe `envFrom`, Secret ou token de ServiceAccount e usa volume temporário próprio. O worker de produção passa a drenar, em ordem, coleta HTTP, browser e avaliação; a coleta só pula o browser nos testes legados que injetam explicitamente a fila HTTP.

## Derivação inicial

A observação 1.0 agora inclui tamanho bruto/renderizado do DOM e contagem explícita de requests de API externos. O método versionado deriva `D07`, `C01`, `C05` e `O05` com thresholds declarados como hipótese de projeto. Budget interrompido gera `UNKNOWN`; ausência de observação após falha terminal também gera evidência `UNKNOWN` com confiança zero. A fixture controlada separa HTML bruto, script, API em outra origem e link inserido no DOM.

As fórmulas, limites e ressalvas estão em `docs/metodos-derivacao-browser.md`. Esses resultados não devem ser apresentados como cientificamente validados até revisão externa e calibração com dataset estratificado.

## Relatório público

O snapshot transacional inclui jobs `collection`, `browser` e `assessment`, com fases `browser_queued`, `rendering`, `browser_failed` e `assessment_queued_degraded`. `browser_runs` valida novamente o contrato persistido e expõe somente IDs opacos, hash da observação, perfil, budgets e contagens/medidas. Hashes de DOM e conjuntos de URL, atestação interna, URL, DOM e payload bruto não entram na resposta. A auditoria registra apenas `browser_count` junto às demais contagens.

## Próximos incrementos

1. validação operacional da imagem em Docker/Kubernetes com filesystem read-only e sandbox ativo.

