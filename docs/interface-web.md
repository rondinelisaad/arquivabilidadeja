# Interface Web

## Jornada principal

A página inicial contém um único dado de entrada: a URL pública a avaliar. Ao enviar, o navegador chama `POST /v1/analyses`, recebe o identificador opaco e navega para `/analyses/{analysis_id}`. A página de acompanhamento consulta periodicamente o relatório até um estado terminal e representa as etapas coleta HTTP, browser isolado, derivação e relatório.

A submissão gera uma `Idempotency-Key` criptograficamente aleatória e a reutiliza enquanto a mesma URL permanecer pendente após uma falha de transporte. Assim, repetir o envio não duplica a análise; a chave é descartada ao receber uma resposta bem-sucedida ou ao mudar a URL.

Quando disponível, o relatório mostra estado geral, score e cobertura por dimensão, indicadores derivados, limitações metodológicas, exportação JSON e comparação com a execução anterior compatível. A página inicial também lista as 20 avaliações mais recentes do proprietário.

Enquanto uma avaliação está ativa, a página de relatório oferece “Cancelar avaliação”. O comando chama `POST /v1/analyses/{analysis_id}/cancel`, exige `analysis:cancel`, é idempotente e desaparece quando a análise alcança qualquer estado terminal.

## Metodologia pública

A rota `/methodology` apresenta versão, estado candidato, hash da fonte, dimensões e limitações. `/methodology/indicators` lista as 45 definições e cada URL `/methodology/indicators/{id-ou-slug}` documenta definição, relevância, medição, evidências, classificação, scoring, limitações e referências. A interface consome `/v1/methodology` e `/v1/indicators`; portanto, documentação humana e scoring partem do mesmo pacote versionado.

As páginas metodológicas usam o mesmo shell protegido, CSP e auditoria das páginas de análise. Conteúdo vindo da API é inserido com `textContent`, nunca como HTML interpretável.

## Internacionalização

A interface oferece português e inglês no mesmo shell, com português como idioma padrão. O seletor `PT / EN` grava somente a preferência `archivability.locale` no armazenamento local do navegador e também aceita `?lang=pt` ou `?lang=en`; nenhuma preferência de idioma é enviada à API ou incluída na auditoria.

A metodologia executável e o JSON exportado permanecem canônicos em inglês para preservar hashes, reprodutibilidade e comparabilidade. A camada Web localiza títulos, explicações, métodos e limitações dos 45 indicadores apenas na apresentação. Conteúdo dinâmico continua sendo inserido exclusivamente com `textContent`, inclusive após a tradução.

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

Para testes manuais no Compose local, `ARCHIVABILITY_LOCAL_AUTH_ENABLED=true` injeta uma identidade opaca fixa com os escopos mínimos da jornada. O modo falha na inicialização fora de `development` ou com banco remoto, rejeita cabeçalhos `Authorization` e registra `auth.local_development` sem credenciais. Ele não substitui OIDC e deve permanecer desabilitado em ambientes compartilhados ou públicos.

## Validação

Os testes ASGI verificam página inicial, shell do relatório, assets, `HEAD`, delegação das rotas JSON, método rejeitado, CSP e auditoria SQLite. A composição real com pool PostgreSQL e role restrita também serve a interface e persiste `access.web_ui`. O JavaScript passa por `node --check`; a prévia local foi inspecionada nos layouts estreito e amplo sem erros no console.
