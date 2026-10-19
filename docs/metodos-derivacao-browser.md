# Métodos de derivação do browser 1.2.0

Status: hipótese de projeto, ainda sem validação científica externa.

Este método interpreta exclusivamente a observação agregada e limitada produzida pelo perfil `chromium-baseline-1`. Nenhum resultado prova causalidade geral, essencialidade de conteúdo ou comportamento fora da janela observada. Se o render termina por budget, os cinco indicadores ficam `UNKNOWN` com confiança zero.

## Medidas comuns

- `rendered_gain_share = additional_url_count / max(rendered_url_count, 1)`;
- `external_request_share = external_request_count / max(request_count, 1)`;
- `external_api_share = external_api_request_count / max(api_request_count, 1)`;
- `dom_delta_share = abs(rendered_dom_bytes - raw_dom_bytes) / max(raw_dom_bytes, rendered_dom_bytes, 1)`.

Hashes diferentes indicam conteúdo serializado diferente, não relevância editorial. Contagens que atingem um budget são truncadas e não devem ser interpretadas como universo total.

## Regras iniciais

### D07 — rendered URL gain

- `PASS`: nenhuma URL adicional;
- `FAIL`: HTML bruto sem URLs ou ganho superior a 50% do conjunto renderizado;
- `WARNING`: algum ganho abaixo desses limites.

### C01 — external resource share

- `WARNING`: mais de 25% dos requests observados são externos;
- `PASS`: até 25%.

O limiar não declara que recursos externos são ruins; sinaliza ampliação relevante do escopo de captura. Esta regra deve ser reavaliada antes de validação científica porque a configuração metodológica atual ainda pontua C01.

### C04 — third-party active content

- `PASS`: nenhuma aresta externa ativa observada;
- `WARNING`: ao menos um script, stylesheet, fonte, API ou frame externo;
- `UNKNOWN`: observação legada sem grafo agregado ou render incompleto.

O grafo possui uma raiz lógica `site` e arestas agregadas por tipo. A observação 1.2 preserva, para no máximo 100 origens públicas externas, apenas protocolo, domínio e porta, além da quantidade de requisições por tipo de recurso. A lista é ordenada pelas origens mais frequentes; um marcador informa eventual truncamento. URLs completas, credenciais, caminhos, parâmetros e fragmentos são descartados. `WARNING` significa dependência que merece inspeção para captura, não erro nem indisponibilidade comprovada.

### C05 — external API dependency

- `NOT_APPLICABLE`: nenhum XHR/fetch observado;
- `PASS`: APIs observadas, mas nenhuma externa;
- `UNKNOWN`: API externa observada sem mudança correlata no DOM ou nas URLs;
- `FAIL`: mudança correlata e pelo menos 50% das chamadas de API externas;
- `WARNING`: mudança correlata abaixo de 50%.

A correlação temporal não prova causalidade nem essencialidade.

### O05 — JavaScript dependency

- `PASS`: DOM, URLs e chamadas de API não mudaram;
- `FAIL`: conteúdo apareceu a partir de DOM bruto vazio, ganho de URLs superou 50%, ou delta de tamanho do DOM superou 50% junto com API;
- `WARNING`: outra diferença observada após JavaScript.

## Fixture controlada

`BrowserFixtureSite` fornece HTML inicial vazio, JavaScript, uma chamada de API em outra origem e um link inserido no DOM. Ela separa os sinais necessários para testes determinísticos. O teste unitário valida os recursos sem relaxar a proteção SSRF; a execução Chromium fim a fim exige a imagem sidecar e uma rede de teste explicitamente isolada.
