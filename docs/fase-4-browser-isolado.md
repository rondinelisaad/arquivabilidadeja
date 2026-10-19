# Fase 4 — browser isolado

Status: implementação e validação operacional local concluídas em 2026-10-19.

## Contrato inicial

O pacote `archivability.browser` define a fronteira entre o processo principal e um futuro worker Chromium isolado. Este incremento não inicia navegador nem habilita nova coleta externa; ele torna explícitos e testáveis os invariantes que qualquer adaptador terá de cumprir.

`BrowserRenderRequest` transporta somente IDs da análise/tentativa, URL validada, budgets e um perfil determinístico. O perfil fixa viewport, User-Agent honesto `arquivabilidade-ja-browser/0.1`, locale `en-US`, timezone `UTC`, cache desabilitado, movimento reduzido e service workers bloqueados. Os budgets limitam navegação, janela de observação, tempo total, quantidade de requests, bytes por resposta e no total, DOM e eventos de console.

`BrowserIsolationAttestation` falha fechado se o sandbox estiver desligado, o processo executar como root, o filesystem raiz for gravável, o perfil não for efêmero ou houver mounts, segredos ou socket Docker disponíveis. Essa atestação é parte do resultado e deverá ser produzida pelo launcher isolado, não pelo site analisado.

`BrowserRenderAggregate` não aceita DOM, corpos, mensagens de console nem URLs completas. Ele contém apenas hashes SHA-256 de integridade, contagens de URLs e requests, escopo interno/externo, requests de API, origens externas distintas e arestas agregadas por tipo (script, stylesheet, imagem, fonte, API, frame e outros), falhas, bloqueios, bytes, tamanho do DOM, tempo e motivo de término. Para evidência humana, preserva até 100 origens públicas sanitizadas — somente protocolo, domínio e porta — com contagens por tipo; caminhos, parâmetros, fragmentos e credenciais são descartados. `BrowserRenderResult.validate_against` exige identidade correspondente, timestamps coerentes, isolamento seguro e respeito aos budgets.

## Execução, persistência e auditoria

`BrowserExecutionService` autoriza o vínculo com uma tentativa HTTP concluída, exige resultado compatível com a requisição, cria uma observação imutável `browser_render` 1.2 e persiste somente o contrato agregado. As versões 1.0 e 1.1 permanecem legíveis; a 1.0 representa o grafo como `UNKNOWN` e a 1.1 não contém os nomes sanitizados das origens. Uma repetição do mesmo trabalho reutiliza a observação existente sem executar novamente o renderer; mudanças de budgets ou perfil falham fechadas.

SQLite e PostgreSQL localizam no máximo uma observação de browser por tentativa e validam o estado do ciclo de vida antes da execução. A ação auditável `job.browser_render` registra início, sucesso, reuso ou falha com código estável. Os eventos incluem apenas IDs opacos, budgets, perfil, política de isolamento e contagens; não incluem URL, DOM, hashes do conteúdo, mensagens do console ou corpos de resposta.

## Contrato da fila

`BrowserJob` e `BrowserQueueService` definem enqueue idempotente por tentativa HTTP, perfil e budgets imutáveis, leases, recuperação de lease expirado, retries limitados e estados terminais. Apenas uma tentativa HTTP registrada e bem-sucedida de uma análise em execução pode originar o job.

SQLite e PostgreSQL persistem a fila com revisão otimista, constraint de unicidade sobre a tentativa, índice de claim e bloqueio de exclusão. O runtime PostgreSQL recebe somente `SELECT`, `INSERT` e `UPDATE` sobre `browser_jobs`; o validador de inicialização exige esses grants e continua recusando DDL, `TRUNCATE`, `TRIGGER` e `DELETE`. Enqueue, claim, retry, sucesso e falha usam eventos `job.browser_queue_*` sanitizados.

`BrowserWorker` consome essa fila separadamente, monta a requisição somente com a análise/tentativa autorizada e entrega o agregado ao `BrowserExecutionService`. Depois de um render válido, ele enfileira a observação HTTP original para a derivação existente; se o enqueue falhar, a repetição reutiliza a observação de browser já persistida. Falhas usam códigos estáveis e backoff exponencial limitado. Ao esgotar tentativas de browser, o worker ainda enfileira a avaliação HTTP como resultado degradado, preservando o erro terminal do browser sem deixar a análise permanentemente em execução.

## Sidecar Chromium e ativação

`LoopbackBrowserRenderer` aceita somente um endpoint HTTP explícito em loopback, limita request/response e valida o JSON integral antes de criar o resultado. O sidecar Node/Playwright fixa versão e integridade das dependências e da imagem-base, escuta apenas em `127.0.0.1`, processa um render por vez e não registra request, URL, DOM ou erro bruto.

Cada navegação e subrequest revalida esquema, credenciais, DNS e todos os endereços resolvidos, bloqueando redes privadas, loopback, link-local, documentação, multicast e especiais. Uma origem só pode entrar na evidência explícita depois dessa validação. A NetworkPolicy do pod fornece a segunda barreira contra rebinding. O resultado guarda somente hashes, contagens, origens sanitizadas e medidas truncadas nos budgets.

O container só responde saudável quando confirma sandbox habilitado, UID não-root, raiz read-only, perfil efêmero e ausência de secrets, mounts de host e socket Docker. No Compose, o perfil seccomp recomendado pelo Playwright libera a criação do namespace de usuário, todas as capabilities são removidas e somente `SYS_CHROOT` é devolvida para o sandbox do Chromium; `no-new-privileges` permanece ativo. No Kubernetes ele não recebe `envFrom`, Secret ou token de ServiceAccount e usa volume temporário próprio. O worker de produção passa a drenar, em ordem, coleta HTTP, browser e avaliação; a coleta só pula o browser nos testes legados que injetam explicitamente a fila HTTP.

## Derivação inicial

A observação 1.2 acrescenta as origens externas sanitizadas ao tamanho bruto/renderizado do DOM, requests de API externos, diversidade de origens e grafo agregado por tipo já presentes na 1.1. O método versionado 1.2.0 deriva `D07`, `C01`, `C04`, `C05` e `O05` com thresholds declarados como hipótese de projeto. `C04` usa `WARNING` somente como sinal de atenção quando há conteúdo ativo externo; a presença externa não é tratada como falha. Budget interrompido gera `UNKNOWN`; ausência de observação após falha terminal também gera evidência `UNKNOWN` com confiança zero. A fixture controlada separa HTML bruto, script, API em outra origem e link inserido no DOM.

As fórmulas, limites e ressalvas estão em `docs/metodos-derivacao-browser.md`. Esses resultados não devem ser apresentados como cientificamente validados até revisão externa e calibração com dataset estratificado.

## Relatório público

O snapshot transacional inclui jobs `collection`, `browser` e `assessment`, com fases `browser_queued`, `rendering`, `browser_failed` e `assessment_queued_degraded`. `browser_runs` valida novamente o contrato persistido e expõe somente IDs opacos, hash da observação, perfil, budgets e contagens/medidas. Hashes de DOM e conjuntos de URL, atestação interna, URL, DOM e payload bruto não entram na resposta. A auditoria registra apenas `browser_count` junto às demais contagens.

## Validação operacional

Em 2026-10-19, o sidecar respondeu saudável e concluiu uma renderização HTTPS real com Chromium 156.0.8078.4. A atestação confirmou sandbox ativo, UID não-root, raiz read-only, perfil efêmero e ausência de segredos, mounts de host e socket Docker. A validação em Kubernetes permanece parte do runbook do ambiente alvo.

