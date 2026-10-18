# Fase 3 — crawl limitado

Status: incremento 1 concluído em 2026-10-18.

## Resultado atual

O pacote `archivability.crawler` fornece uma fronteira FIFO determinística, sequencial e restrita à origem exata da URL inicial. O núcleo não realiza rede: ele recebe resultados de coleta, agenda os próximos alvos e produz somente um resumo agregado.

Os limites padrão são profundidade 2, 100 URLs, 2 MiB por resposta, 20 MiB no total, 60 segundos no total e 1.000 links por documento. Cada alvo recebe apenas o orçamento restante; bytes ou tempo acima dele são rejeitados, e somente uma URL pode permanecer ativa por fronteira.

O processamento normaliza esquema, host IDNA, portas padrão e fragmentos. Credenciais, caracteres de controle, barras invertidas, esquemas não HTTP(S) e origens externas não entram na fila. Duplicatas são identificadas por digest SHA-256 em memória; o digest serve apenas à deduplicação e não autentica nem protege segredos.

O resumo contém contagens de referências, URLs internas únicas, visitas, sucessos, links internos quebrados, arestas, profundidade, bytes, tempo, classes HTTP, classes estáveis de falha, descartes e motivo de término. URLs não aparecem no resumo nem nos eventos de auditoria.

## Auditoria e persistência

`AuditedBoundedCrawl` registra o início e exatamente um resultado terminal por meio da ação `job.bounded_crawl`. Os eventos persistem somente IDs opacos, estado, limites, agregados ou um código estável de erro. SQLite e PostgreSQL reutilizam a tabela de auditoria e a role de runtime existente; este incremento não cria migration nem amplia privilégios.

## Limite desta entrega

A fronteira ainda não está conectada ao cliente HTTP. Isso evita coletar URLs adicionais antes de interpretar e registrar a política aplicável de `robots.txt`. Os próximos incrementos são:

1. parser e decisão versionada de robots para o user-agent declarado;
2. integração da fronteira com o worker e a política SSRF em cada conexão e redirect;
3. persistência da observação agregada e derivação de `D08` e indicadores relacionados;
4. fixtures de integração para links quebrados, limites, redirects, robots e recuperação de job.

Browser e JavaScript permanecem na Fase 4.
