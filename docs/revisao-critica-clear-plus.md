# Revisão crítica e matriz de rastreabilidade do CLEAR+

Status: revisão da fonte concluída
Fonte: Banos e Manolopoulos, 2016
Data da revisão: 2026-10-03

Este documento registra o que o artigo efetivamente especifica, o que a nova plataforma preserva para comparação histórica e o que será adaptado ou substituído. O CLEAR+ é uma referência metodológica; suas decisões não são tratadas como requisitos normativos.

## Relação entre o artigo e o ArchiveReady

O ArchiveReady é o resultado prático e a implementação de referência do CLEAR+ descrita na seção 4 do artigo. A relação deve ser documentada em três níveis distintos:

1. **Método:** CLEAR+ define facetas, avaliações, significâncias e fórmulas.
2. **Implementação de referência:** ArchiveReady operacionaliza esse método como aplicação web e API.
3. **Novo projeto:** Arquivabilidade JA usa ambos como precedente histórico, mas revê pressupostos, testes e arquitetura para a Web atual.

No artigo, o ArchiveReady é descrito com Python, Flask, BeautifulSoup, Redis, filas RQ, MariaDB, PhantomJS, JHOVE e validadores W3C. A execução é decomposta em tarefas assíncronas, e o sistema analisa uma única página como aproximação do website. Esses detalhes são decisões da implementação de referência, não partes necessárias da definição abstrata do CLEAR+.

O site público [archiveready.com](https://archiveready.com/) continua acessível na data desta revisão e informa:

- serviço iniciado em 2012-10-01;
- software versão 4.1;
- análise de HTML, imagens, CSS, JavaScript e sitemaps;
- resultados nas facetas Accessibility, Cohesion, Metadata e Standards Compliance;
- API JSON em `/api?url=...`, com scores e mensagens associadas a facetas, nível e peso;
- uso gratuito limitado e uso em escala sujeito às condições informadas pelo autor.

O rodapé público permanece identificado como 2012–2017. Portanto, a disponibilidade atual do site demonstra que o serviço responde, mas não comprova manutenção ativa, atualização tecnológica ou equivalência exata entre a versão online atual e o algoritmo publicado. A nova plataforma não deve depender da API ao vivo para testes, scoring ou reprodução científica. Exemplos documentados podem virar fixtures locais com atribuição e data de captura.

## Estrutura original confirmada

O artigo define Website Archivability como a extensão em que um website reúne condições para transferir seu conteúdo com segurança a um arquivo da Web para preservação. O método usa quatro facetas:

| Faceta | Testes | Significância e peso máximo |
|---|---:|---:|
| FA Accessibility | A1–A10 | 5 high × 4 + 4 medium × 2 + 1 low × 1 = 29 |
| FS Standards Compliance | S1–S10 | 2 high × 4 + 8 medium × 2 = 24 |
| FC Cohesion | C1–C6 | 6 medium × 2 = 12 |
| FM Metadata Usage | M1–M6 | 2 medium × 2 + 4 low × 1 = 8 |
| Total | 32 | 73 |

Cada faceta é uma média ponderada das avaliações aplicáveis. O modelo flat dá 0,25 a cada faceta. O modelo weighted usa a massa de pesos de cada faceta. O artigo também afirma que uma avaliação pode afetar mais de uma faceta.

## Matriz de testes CLEAR+

### Accessibility

| ID | Teste original resumido | Sig. | Peso | Destino proposto | Decisão |
|---|---|---:|---:|---|---|
| A1 | Percentual de URLs válidas versus inválidas em hyperlinks e CSS | high | 4 | D06, R01, R03 | Preservar a observação; separar descoberta, validade e falha de recuperação. |
| A2 | Existência de JavaScript inline | high | 4 | D07, O05 | Substituir presença binária por dependência e impacto após renderização. |
| A3 | Existência de `sitemap.xml` | high | 4 | D04 | Preservar, mas não assumir que o caminho convencional é a única localização. |
| A4 | Maior tempo inicial; 100% até 0,2 s e 0% acima de 2 s | high | 4 | O01, N01, N02 | Não reproduzir como atributo intrínseco; separar latência de falha persistente. |
| A5 | Uso de Flash ou QuickTime | high | 4 | S04, S06 | Generalizar para inventário, validação e risco versionado de formato. |
| A6 | Existência de regras `Disallow` em robots.txt | medium | 2 | D03 | Avaliar regra aplicável, escopo, User-Agent e política de coleta; não penalizar mera presença. |
| A7 | Regra `Sitemap` em robots.txt | medium | 2 | D04 | Preservar como um dos mecanismos de descoberta. |
| A8 | Percentual de mídias vinculadas que podem ser baixadas | medium | 2 | R01, R06 | Preservar por tipo, diferenciando falha do alvo, budget e erro da ferramenta. |
| A9 | Presença de Expires, Last-Modified ou ETag | medium | 2 | R07 | Preservar como suporte a recrawl, sem tratar ausência como falha grave. |
| A10 | RSS/Atom referenciado por autodiscovery | low | 1 | D05 | Preservar; ausência pode ser `NOT_APPLICABLE` ou baixa severidade. |

### Standards Compliance

| ID | Teste original resumido | Sig. | Peso | Destino proposto | Decisão |
|---|---|---:|---:|---|---|
| S1 | HTML conforme padrões W3C | high | 4 | S01 | Adaptar para parseabilidade e impacto; preservar validação como evidência. |
| S2 | Uso de QuickTime e Flash | high | 4 | S04, S06 | Generalizar; “proprietário” sozinho não determina risco. |
| S3 | Integridade e conformidade de imagens | medium | 2 | S05 | Preservar com cobertura explícita de validadores. |
| S4 | Feed RSS conforme padrões W3C | medium | 2 | S03 | Preservar como validade de feed e capacidade de extrair URLs/metadados. |
| S5 | Presença de Content-Encoding ou Transfer-Encoding | medium | 2 | Observação HTTP, R06 | Preservar headers; não exigir que estejam presentes quando desnecessários. |
| S6 | Presença de Expires, Last-Modified ou ETag | medium | 2 | R07 | Consolidar com A9 e M2 na observação, mantendo contribuição legada separada. |
| S7 | CSS conforme padrões W3C | medium | 2 | S02 | Adaptar para parseabilidade e impacto de renderização. |
| S8 | Integridade e conformidade de áudio HTML5 | medium | 2 | S05 | Preservar quando aplicável. |
| S9 | Integridade e conformidade de vídeo HTML5 | medium | 2 | S05 | Preservar quando aplicável. |
| S10 | Presença de `Content-Type` HTTP | medium | 2 | R05 | Ampliar para presença e consistência entre header, assinatura e parser. |

### Cohesion

| ID | Teste original resumido | Sig. | Peso | Destino proposto | Decisão |
|---|---|---:|---:|---|---|
| C1 | Percentual de imagens locais versus remotas | medium | 2 | C01 | Preservar como medida descritiva; penalidade depende de impacto. |
| C2 | Percentual de CSS local versus remoto | medium | 2 | C01, C04 | Preservar no grafo por tipo. |
| C3 | Percentual de scripts locais versus remotos | medium | 2 | C01, C04 | Preservar e distinguir conteúdo ativo/essencial. |
| C4 | Percentual de vídeos locais versus remotos | medium | 2 | C01 | Preservar no grafo por tipo. |
| C5 | Percentual de áudios locais versus remotos | medium | 2 | C01 | Preservar no grafo por tipo. |
| C6 | Percentual de objetos Flash/QuickTime locais versus remotos | medium | 2 | C01, S06 | Manter apenas no modo legado; substituir por classes atuais de recursos. |

O artigo considera “local” o mesmo domínio de topo e trata subdomínios como locais. A nova plataforma deve usar Public Suffix List e registrar pelo menos origem, hostname e domínio registrável; esses conceitos não podem ser colapsados, pois um subdomínio pode pertencer à mesma organização, a um CDN delegado ou a infraestrutura independente.

### Metadata Usage

| ID | Teste original resumido | Sig. | Peso | Destino proposto | Decisão |
|---|---|---:|---:|---|---|
| M1 | Presença de `Content-Type` HTTP | medium | 2 | R05, M02 | Preservar no legado; no modelo novo, tratar principalmente como interpretação técnica. |
| M2 | Presença de Expires, Last-Modified ou ETag | medium | 2 | R07, M03 | Separar cache/identidade de recurso de metadado temporal. |
| M3 | Uso de meta robots `noindex`, `nofollow`, `noarchive`, `nosnippet`, `noodp` | low | 1 | D03, indicador futuro específico | Interpretar diretivas individualmente; tags obsoletas não permanecem como requisito atual. |
| M4 | Uso de perfil Dublin Core | low | 1 | M01, M05 | Preservar detecção, sem preferir exclusivamente um vocabulário. |
| M5 | Uso de FOAF | low | 1 | M01, M05 | Preservar apenas no modo legado; aceitar vocabulários atuais. |
| M6 | Presença de meta description | low | 1 | M01 | Preservar como metadado descritivo opcional. |

## Fórmulas e política para itens não aplicáveis

O artigo associa high=4, medium=2 e low=1. Para cada faceta `Fλ`, a fórmula é uma média ponderada dos resultados das avaliações aplicáveis:

```text
Fλ = sum(weight[k] * result[k]) / sum(weight[k])
```

O score flat é:

```text
WA_flat = 0.25*FA + 0.25*FS + 0.25*FC + 0.25*FM
```

A Tabela 5 apresenta o máximo teórico:

```text
WA_weighted_max = (29*FA + 24*FS + 12*FC + 8*FM) / 73
```

No exemplo da Universidade Aristóteles de Tessalônica, S8/S9, C4/C5/C6 e alguns itens de metadata aparecem como não aplicáveis e o cálculo publicado usa pesos de faceta 29, 20, 6 e 6, somando 61. Portanto, há uma diferença entre o máximo teórico e a política aplicada no exemplo.

A nova plataforma registra essa escolha:

- `fixed_max`: fórmula fixa 29/24/12/8 sobre 73, conforme o requisito do projeto;
- `paper_applicable`: pesos recalculados a partir das avaliações aplicáveis, para reprodução do exemplo do artigo.

Resultados obtidos com políticas diferentes não podem compor a mesma série sem rótulo e recálculo.

## Evidência empírica relatada

O artigo apresenta três linhas de avaliação que devem ser interpretadas com seus limites:

1. Quatro datasets somaram inicialmente 864 sites e 785 avaliações concluídas. Os grupos de organizações, universidades e governos apresentaram médias WA entre 75,87 e 80,75; o grupo de spam teve média 58,37. Os próprios rótulos dos grupos, as exclusões por falha e outras diferenças entre eles podem explicar parte do contraste.
2. Três pesquisadores do mesmo laboratório atribuíram notas a versões vivas e arquivadas de 200 universidades. A correlação de Pearson entre WA e avaliação dos especialistas foi 0,516. Isso é evidência de associação moderada no desenho adotado, não prova de validade universal ou causalidade.
3. Para testar a homepage como proxy, foram selecionados 783 dos 1.000 sites aleatórios que tinham RSS detectável. Dez páginas por site foram comparadas à homepage. A diferença absoluta média foi 3,87 pontos, com desvio-padrão 3,76; a homepage foi maior em 510 casos, igual em 35 e menor em 238.

A terceira avaliação é relevante, mas condicionada a sites com RSS, à Web de 2014 e aos próprios testes CLEAR+. Ela não justifica eliminar o crawl amostral nem a renderização de aplicações modernas.

## Limitações reconhecidas pelo artigo

Os autores registram limitações que a nova arquitetura deve tornar visíveis:

- crawlers estritos e tolerantes podem obter resultados diferentes para o mesmo recurso inválido;
- validadores podem ficar atrás da evolução de HTML e CSS e produzir falsos negativos;
- Cohesion mede um risco latente que só se concretiza quando um serviço externo falha;
- ausência de metadata pode não afetar a reprodução imediata, mas comprometer uso futuro;
- avaliações binárias perdem granularidade, especialmente JS inline, HTML, RSS, CSS e robots;
- alguns sites não foram avaliados por bloqueio, indisponibilidade, dados problemáticos ou falha da API.

O último ponto sustenta diretamente a separação entre `FAIL` e `UNKNOWN`: término anormal do analisador não demonstra baixa arquivabilidade.

## Avaliação crítica para o novo projeto

### Elementos que devem ser preservados

- decomposição em facetas em vez de apenas um score opaco;
- rastreabilidade entre teste, evidência, peso e resultado;
- indicação explícita de significância;
- preocupação com descoberta, HTTP, formatos, dependências e metadata;
- execução assíncrona e resultados estruturados por API;
- tentativa de validação empírica e publicação de limitações.

### Elementos que devem ser adaptados

- A1 e A8 tornam-se observações por recurso e taxas com denominador/escopo explícitos;
- A2 deixa de penalizar presença de JS e mede dependência/impacto real;
- A6 interpreta regras aplicáveis e política, não a mera string `Disallow`;
- S1/S7 separam validade formal, parseabilidade e efeito observado;
- C1–C6 geram grafo, origem e essencialidade, sem pressupor que remoto é ruim;
- M4/M5 entram num conjunto extensível de vocabulários, não numa lista congelada.

### Elementos que não devem ser transportados

- A4 como penalidade intrínseca baseada em 0,2–2 s;
- Flash e QuickTime como lista permanente de formatos problemáticos;
- presença de encoding headers como requisito universal;
- `noodp`, hoje obsoleto, como sinal atual de arquivabilidade;
- falha da própria ferramenta interpretada como incapacidade do site;
- conclusão principal expressa apenas pela média compensatória.

## Consequências para a metodologia 0.1.0

1. Manter os 32 IDs CLEAR+ numa camada de compatibilidade, sem misturá-los aos IDs do modelo novo.
2. Armazenar uma observação uma vez e permitir que ela alimente mais de uma avaliação legada, preservando A9/S6/M2 e S10/M1.
3. Registrar `denominator_policy` e itens `NOT_APPLICABLE` em cada score legado.
4. Exibir a origem exata (`CLEAR+ A1`, `adaptação de A2`, `novo`) em cada indicador.
5. Tratar os pesos CLEAR+ como históricos e os pesos novos como hipóteses versionadas.
6. Construir fixtures que reproduzam o exemplo do artigo e confirmem tanto o denominador 61 quanto a variante fixa 73.
