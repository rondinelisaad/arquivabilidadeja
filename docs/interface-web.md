# Interface Web

## Jornada principal

A página inicial contém um único dado de entrada: a URL pública a avaliar. Ao enviar, o navegador chama `POST /v1/analyses`, recebe o identificador opaco e navega para `/analyses/{analysis_id}`. A página de acompanhamento consulta periodicamente o relatório até um estado terminal e representa as etapas coleta HTTP, browser isolado, derivação e relatório.

Quando disponível, o relatório mostra estado geral, score e cobertura por dimensão, indicadores derivados, limitações metodológicas, exportação JSON e comparação com a execução anterior compatível. A página inicial também lista as 20 avaliações mais recentes do proprietário.

## Segurança da fronteira

- assets próprios, sem CDN ou script de terceiro;
- CSP `default-src 'none'`, com script, estilo e conexão restritos à mesma origem;
- nenhuma injeção de HTML dinâmico: dados da API entram somente por `textContent` e propriedades tipadas;
- páginas e respostas de dados com `Cache-Control: no-store`;
- URL analisada nunca é incluída em logs da interface;
- `POST` usa `application/json`, e a aplicação não habilita CORS;
- autenticação, autorização por proprietário e rate limit permanecem no backend;
- acessos a `/` e `/analyses/{id}` geram `access.web_ui` sanitizado.

## Autenticação no navegador

A aplicação verifica access tokens OIDC Bearer e não armazena senha nem token no JavaScript. Em implantação Web, use um identity-aware proxy na mesma origem: ele mantém sua própria sessão segura, obtém o access token do provedor e o encaminha para o upstream. O proxy precisa sobrescrever — nunca preservar — um cabeçalho `Authorization` vindo do cliente.

Os escopos mínimos para a jornada são `analysis:create` e `analysis:read`. Sem sessão, a interface continua carregando, mas mostra uma mensagem de autenticação ao tentar acessar dados ou criar uma avaliação.

## Validação

Os testes ASGI verificam página inicial, shell do relatório, assets, `HEAD`, delegação das rotas JSON, método rejeitado, CSP e auditoria SQLite. A composição real com pool PostgreSQL e role restrita também serve a interface e persiste `access.web_ui`. O JavaScript passa por `node --check`; a prévia local foi inspecionada nos layouts estreito e amplo sem erros no console.
