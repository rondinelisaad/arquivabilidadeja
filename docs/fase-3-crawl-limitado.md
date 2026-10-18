# Fase 3 — crawl limitado

Status: incrementos 1, 2, 3 e 4 concluídos em 2026-10-18.

## Resultado atual

O pacote `archivability.crawler` fornece uma fronteira FIFO determinística, sequencial e restrita à origem exata da URL inicial. O núcleo permanece independente da rede: ele recebe resultados de coleta, agenda os próximos alvos e produz somente um resumo agregado. No worker, `HttpMetadataProbe` liga esse núcleo ao cliente HTTP protegido.

Os limites padrão são profundidade 2, 100 URLs, 2 MiB por resposta, 20 MiB no total, 60 segundos no total e 1.000 links por documento. Cada alvo recebe apenas o orçamento restante; bytes ou tempo acima dele são rejeitados, e somente uma URL pode permanecer ativa por fronteira.

O processamento normaliza esquema, host IDNA, portas padrão e fragmentos. Credenciais, caracteres de controle, barras invertidas, esquemas não HTTP(S) e origens externas não entram na fila. Duplicatas são identificadas por digest SHA-256 em memória; o digest serve apenas à deduplicação e não autentica nem protege segredos.

O resumo contém contagens de referências, URLs internas únicas, visitas, sucessos, links internos quebrados, arestas, profundidade, bytes, tempo, classes HTTP, classes estáveis de falha, descartes e motivo de término. URLs não aparecem no resumo nem nos eventos de auditoria.

## Auditoria e persistência

`AuditedBoundedCrawl` registra o início e exatamente um resultado terminal por meio da ação `job.bounded_crawl`. Os eventos persistem somente IDs opacos, estado, limites, agregados ou um código estável de erro. SQLite e PostgreSQL reutilizam a tabela de auditoria e a role de runtime existente; este incremento não cria migration nem amplia privilégios.

O mesmo job durável de coleta persiste atomicamente a observação HTTP 1.2 e uma segunda observação `bounded_crawl` 1.0. Uma interrupção após essa transação reutiliza a observação HTTP existente e segue para a derivação sem repetir a rede. A nova observação possui schema JSON próprio e contém apenas limites, decisão agregada de robots e resumo do crawl.

O assessor localiza a observação de crawl pelo attempt, exige proveniência idêntica à observação HTTP e valida estritamente limites, contagens e decisões antes de derivar `D03`, `D08` e `R03`. As novas evidências usam métodos 1.0.0 e são gravadas atomicamente com os resultados HTTP e a conclusão da análise. Análises antigas sem observação de crawl permanecem compatíveis.

## Política de robots

`RobotsRules` interpreta grupos e regras conforme a RFC 9309 sob a política versionada `rfc9309-respect-1` e o parser `rfc9309-1`. O product token padrão é `arquivabilidade-ja`, contido no User-Agent HTTP já declarado pelo cliente.

O parser processa até 512 KiB por padrão, exige UTF-8, preserva regras válidas diante de linhas inválidas, combina grupos equivalentes sem usar o grupo `*` quando há correspondência explícita e aplica a regra com maior quantidade de octetos. Em empate, `Allow` prevalece. Curinga `*`, âncora `$`, caminho mais query, Unicode e percent-encoding são normalizados deterministicamente.

Uma resposta HTTP `4xx` torna o arquivo indisponível e a política permite a coleta; `5xx` ou falha de rede tornam o arquivo inalcançável e bloqueiam a coleta. Redirects precisam ser resolvidos e revalidados pelo broker HTTP antes da decisão. A decisão exportável contém apenas disposição, tipo e tamanho da regra correspondente, contagens, versões e product token; não contém URL, caminho ou padrão de regra.

## Integração com o worker

O worker limita a homepage ao orçamento por resposta do crawl, usa no máximo 512 KiB para `robots.txt`, reaplica a política SSRF antes de cada URL e restringe redirects do crawl à origem inicial. URLs proibidas por robots não abrem conexão; falhas usam classes estáveis e não persistem mensagens de exceção. A URL submetida continua sendo observada como semente da análise, e a decisão de robots sobre ela é registrada separadamente da expansão recursiva.

Os próximos incrementos são:

1. fixtures adicionais com servidor local controlado para limites, redirects, robots e recuperação de job;
2. exposição do resumo de crawl no relatório público da análise.

Browser e JavaScript permanecem na Fase 4.
