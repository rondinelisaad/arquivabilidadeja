# Métodos de derivação HTTP 1.0.0

Este documento descreve as primeiras regras executáveis que transformam uma `Observation` de metadados HTTP, no schema `1.1`, em `Evidence` e `IndicatorResult`. As regras são determinísticas para a mesma metodologia e observação.

## D01 — Homepage accessible

Método: `http-homepage-accessibility@1.0.0`.

| Status HTTP final | Resultado |
|---|---|
| `200–299` | `pass` |
| `300–399` | `warning` — redirect sem terminação utilizável |
| `400–599` | `fail` |
| `100–199` | `unknown` |

A evidência registra status, classe, quantidade de redirects validados e se o transporte final usou HTTPS. O resultado caracteriza somente a tentativa observada; não estabelece disponibilidade global ou histórica.

## R06 — Response completeness

Método: `http-response-completeness@1.0.0`.

| Condição | Resultado | Motivo registrado |
|---|---|---|
| Corpo atingiu o limite configurado | `warning` | `response_byte_limit` |
| `Content-Length` inválido ou conflitante | `warning` | `invalid_content_length` |
| Tamanho declarado difere do observado | `warning` | `content_length_mismatch` |
| Transferência terminou sem as condições anteriores | `pass` | `complete` |

A ausência de `Content-Length` é válida e não reduz o resultado. O limite de bytes aplicado pelo probe sempre integra a evidência.

## Indicadores não derivados nesta versão

`R02`, `R05` e `R07` exigem, respectivamente, cadeia detalhada de redirects, detecção segura do formato e requisição condicional. Eles permanecem sem resultado até que essas observações existam; ausência de evidência não é convertida em aprovação ou reprovação.
