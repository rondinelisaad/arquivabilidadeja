# Métodos de derivação HTTP e crawl

Este documento descreve as regras executáveis que transformam uma `Observation` de metadados HTTP em `Evidence` e `IndicatorResult`. O schema `1.1` permanece compatível com os métodos 1.0.0 de `D01` e `R06`; o schema `1.2` usa métodos 1.1.0 e acrescenta agregados de descoberta, formato e timing total; o schema `1.3` usa métodos 1.2.0 e acrescenta fases instrumentadas e uma distribuição homogênea. As regras são determinísticas para a mesma metodologia e observação.

Quando o mesmo attempt também contém uma observação `bounded_crawl` 1.0, o assessor valida a proveniência comum, os limites e a consistência dos agregados antes de derivar os métodos de crawl 1.0.0. A persistência de todas as evidências, resultados e transição terminal da análise ocorre na mesma transação.

## D01 — Homepage accessible

Método: `http-homepage-accessibility@1.0.0` no schema 1.1, `@1.1.0` no schema 1.2 e `@1.2.0` no schema 1.3.

| Status HTTP final | Resultado |
|---|---|
| `200–299` | `pass` |
| `300–399` | `warning` — redirect sem terminação utilizável |
| `400–599` | `fail` |
| `100–199` | `unknown` |

A evidência registra status, classe, quantidade de redirects validados e se o transporte final usou HTTPS. O resultado caracteriza somente a tentativa observada; não estabelece disponibilidade global ou histórica.

## R06 — Response completeness

Método: `http-response-completeness@1.0.0` no schema 1.1, `@1.1.0` no schema 1.2 e `@1.2.0` no schema 1.3.

| Condição | Resultado | Motivo registrado |
|---|---|---|
| Corpo atingiu o limite configurado | `warning` | `response_byte_limit` |
| `Content-Length` inválido ou conflitante | `warning` | `invalid_content_length` |
| Tamanho declarado difere do observado | `warning` | `content_length_mismatch` |
| Transferência terminou sem as condições anteriores | `pass` | `complete` |

A ausência de `Content-Length` é válida e não reduz o resultado. O limite de bytes aplicado pelo probe sempre integra a evidência.

## Indicadores adicionais dos schemas 1.2 e 1.3

| Indicador | Evidência limitada desta camada |
|---|---|
| `D02` | disponibilidade, status, parseabilidade por linhas e contagem de diretivas de `/robots.txt` |
| `D04` | disponibilidade e contagem limitada de elementos `loc` em `/sitemap.xml` |
| `D05` | quantidade de declarações RSS/Atom na homepage |
| `D06` | contagens agregadas de links internos e externos no HTML bruto |
| `R02` | terminação dentro do orçamento de redirects, com cada destino validado |
| `R04` | distribuição HTTP da amostra de uma homepage |
| `R05` | MIME declarado, família de formato e resultado do parser; confiança `declared_only` |
| `S01` | criação do parser HTML tolerante, sem afirmar validade normativa |
| `S03` | parsing XML do sitemap com DTD e entidades rejeitados |
| `S04` | inventário contextual da homepage por MIME declarado |
| `N01` | no 1.2, apenas tempo total; no 1.3, TCP/TLS/TTFB quando observáveis, DNS nulo com motivo, família IP e quantidade de exchanges sem persistir endereço |
| `N02` | no 1.3, mínimo, máximo, mediana, desvio-padrão amostral, taxa de sucesso e tamanho de uma série de GETs limitada a um byte; p95 somente com `n >= 20` |

## Indicadores da observação `bounded_crawl` 1.0

| Indicador | Método | Regra de estado |
|---|---|---|
| `D03` | `bounded-robots-scope-policy@1.0.0` | `fail` quando a regra aplicável proíbe a URL inicial; `warning` quando permite a semente, mas exclui parte da fronteira; `pass` quando a política parseada permite todo o escopo observado; `not_applicable` para robots ausente (`4xx`); `unknown` quando robots está inalcançável ou inválido |
| `D08` | `bounded-crawl-frontier-coverage@1.0.0` | `pass` quando a fronteira termina completamente; `warning` quando termina por orçamento de URLs, bytes ou tempo |
| `R03` | `bounded-internal-link-retrieval@1.0.0` | `not_applicable` sem URL interna efetivamente verificada; `pass` sem quebra; `warning` quando parte da amostra quebra; `fail` quando toda a amostra interna verificada quebra |

As evidências preservam apenas escopo, budgets, contagens, classes de falha, decisão sanitizada de robots e motivos de descarte. URLs descobertas, padrões de regras e mensagens de exceção não são persistidos.

## Indicadores não derivados nesta camada

`R01`, `R07`, `R08`, `S02` e os demais indicadores exigem recursos adicionais, detecção por assinatura, requisição condicional, validação especializada, browser ou repetição temporal. Eles permanecem sem resultado; ausência de evidência não é convertida em aprovação ou reprovação.
