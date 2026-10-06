# Requisitos de segurança do Arquivabilidade JA

Versão: 1.0-draft
Data: 2026-10-03
Responsável: a definir
Classificação: Interna, conforme NSI.04

Este documento define requisitos de segurança para a fase de requisitos e análise. Os valores marcados como proposta precisam de aprovação antes de implantação. O risco dominante é a plataforma tornar-se um intermediário para acessar redes internas ou causar carga indevida em terceiros.

## 1. Dados pessoais envolvidos

| Campo ou conjunto | Finalidade | Base legal | Retenção proposta | Compartilhamento |
|---|---|---|---|---|
| Identificador opaco de usuário | Autorização e auditoria | A definir com responsável LGPD | Enquanto a conta estiver ativa mais prazo de auditoria aprovado | Não compartilhar por padrão |
| E-mail da conta, se houver login | Identificação e recuperação de acesso | A definir com responsável LGPD | Enquanto a conta estiver ativa | Provedor de identidade, se adotado |
| IP do solicitante | Segurança, prevenção de abuso e auditoria | A definir com responsável LGPD | 180 dias, proposta sujeita à aprovação | SIEM/equipe de segurança |
| URL submetida | Executar e reproduzir a análise | A definir conforme natureza do serviço e pesquisa | Metadado histórico; prazo configurável por projeto | Conforme visibilidade da análise |
| Conteúdo e metadata coletados | Produzir evidências de arquivabilidade | A definir; pode conter dados pessoais publicados pelo site-alvo | Minimização e prazo por classe de artefato | Somente usuários autorizados e exportações solicitadas |
| Logs técnicos e de auditoria | Diagnóstico, integridade e responsabilização | Obrigação/legítimo interesse a confirmar | 180 dias, proposta inicial | Equipe operacional e auditoria |

Decisões LGPD pendentes: controlador e operador, finalidade institucional, base legal por modalidade de uso, atendimento a titulares, política de exclusão, tratamento de conteúdo público com dados pessoais e transferências internacionais por probes.

## 2. Classificação dos dados

| Tipo de dado | Classificação proposta | Criptografado em repouso | Criptografado em trânsito |
|---|---|---|---|
| Metodologia publicada e documentação | Público | Integridade por hash/controle de versão | TLS |
| Resultados públicos aprovados | Público | Integridade por hash/backup | TLS |
| Configuração operacional e filas | Interno | Sim | TLS fora de localhost |
| URLs ainda não publicadas e histórico privado | Restrito | Sim | TLS |
| Observações, HTML, DOM, HAR e screenshots | Restrito por padrão | Sim | TLS |
| Credenciais, chaves e tokens | Sigiloso | Vault/secret store | TLS; nunca em payloads ou logs |
| Trilha de auditoria | Restrito | Sim e proteção contra alteração | TLS |

## 3. Requisitos de autenticação e autorização

- Mecanismo proposto: OAuth2/OIDC institucional quando houver contas; nenhuma implementação local de senha no MVP sem necessidade aprovada.
- Análises anônimas: decisão pendente. Se habilitadas, exigem quotas estritas, rate limiting, CAPTCHA adaptativo ou controle equivalente e nenhum acesso a resultados privados.
- MFA obrigatório: administradores, editores de metodologia e operadores; recomendado para pesquisadores com lotes.
- Papéis propostos: `anonymous`, `analyst`, `researcher`, `methodology_editor`, `operator`, `auditor`, `admin`.
- Timeout de sessão proposto: 30 minutos de inatividade para funções privilegiadas; reautenticação para mudanças de metodologia e acesso.
- Tentativas antes de bloqueio: máximo 5, com limitação por conta e origem conforme NSI.04.
- Toda autorização deve ser verificada no backend. IDs de análise e artefato devem ser opacos e sujeitos a verificação de proprietário/escopo.
- Mudanças de metodologia, gates, pesos, retenção e permissões exigem auditoria com estado anterior e posterior sanitizados.

## 4. Requisitos não funcionais de segurança

| Requisito | Valor-alvo proposto | Observação |
|---|---:|---|
| Disponibilidade do plano de controle | 99,5% mensal | Não inclui disponibilidade de sites-alvo ou probes externos |
| Resposta da API para criação de job | p95 ≤ 500 ms | Não inclui duração da análise assíncrona |
| Retenção de logs de auditoria | 180 dias | Confirmar com política institucional e LGPD |
| Retenção de artefatos brutos | 30 dias por padrão | Projetos de pesquisa podem definir prazo distinto aprovado |
| RTO | 8 horas | Proposta para implantação inicial |
| RPO | 24 horas | Metodologia publicada deve permanecer no Git além do backup |
| Limite de redirects | 10 | Revalidar destino em cada salto |
| Protocolos de destino | HTTP e HTTPS | Bloquear outros esquemas |
| Tamanho máximo por resposta | Configurável, deny-by-default acima do limite | Limites distintos por tipo, com valor final a validar |
| Tempo, URLs, bytes e profundidade | Obrigatórios em toda análise | Nenhum collector pode operar sem budget |

Disponibilidade e desempenho são metas operacionais da plataforma, não critérios automáticos de arquivabilidade do site analisado.

## 5. Ameaças identificadas

| Ameaça | Categoria STRIDE | Ativo afetado | Probabilidade | Impacto | Controle obrigatório |
|---|---|---|---|---|---|
| URL aponta para localhost, RFC1918, link-local ou metadata de cloud | Spoofing/Elevation | Rede interna e credenciais de infraestrutura | Alta | Crítico | Resolver A/AAAA, bloquear ranges internos/reservados, egress deny-by-default e revalidar conexão/redirect |
| DNS rebinding altera o IP entre validação e conexão | Spoofing | Rede interna | Média | Crítico | Resolver/conectar por broker controlado e validar o IP efetivamente conectado |
| Plataforma usada para varredura ou DoS | Denial of Service | Terceiros e reputação institucional | Alta | Alto | Quotas, concorrência por domínio, rate limiting, budgets e User-Agent honesto |
| JavaScript hostil compromete o browser worker | Elevation/Tampering | Worker e infraestrutura | Média | Crítico | Container/VM isolado, sem segredos/mounts/socket Docker, perfil efêmero e limites de CPU/memória/processos |
| Resposta comprimida ou XML malicioso esgota recursos | Denial of Service | Workers | Alta | Alto | Streaming, limites comprimido/descomprimido, razão máxima, DTD e entidades externas desabilitadas |
| Evidência do site executa XSS na interface | Tampering/Information disclosure | Usuários e sessões | Alta | Alto | Escape contextual, sanitização, CSP forte e nunca renderizar HTML-alvo diretamente |
| Cookies ou tokens do site-alvo chegam a logs/artefatos | Information disclosure | Dados de terceiros | Média | Alto | Allowlist de headers, redaction central, ausência de credenciais em crawls e testes de não vazamento |
| Usuário acessa análise ou artefato de outro projeto | Elevation | Resultados privados | Média | Alto | Autorização por objeto no backend e testes de IDOR |
| Metodologia é alterada sem rastreabilidade | Tampering/Repudiation | Integridade científica | Média | Alto | Releases imutáveis, hashes, revisão, assinatura/tag e trilha de auditoria |
| Job duplicado ou replay altera resultados/carga | Tampering/DoS | Fila, alvo e dataset | Média | Médio | Idempotency key, leases, transações e deduplicação |
| Dependência ou imagem comprometida | Tampering/Elevation | Toda a plataforma | Média | Crítico | Dependências pinadas, SBOM, verificação de imagens e scanners no CI |

## 6. Controles de rede e coleta

- A API web nunca faz fetch direto; somente agenda jobs após validação sintática.
- Todo acesso externo passa por uma política comum de egress usada por HTTP e Playwright.
- Bloquear IPv4 e IPv6 loopback, privado, link-local, multicast, reservado, documentação e metadata de cloud.
- Proibir credenciais embutidas em URL e remover fragmentos antes de persistir.
- Revalidar DNS e IP conectado em redirects, retries e novas conexões.
- Não encaminhar cookies, Authorization, Proxy-Authorization ou headers do usuário ao alvo.
- Aplicar limite de concorrência global, por usuário, por domínio registrável e por probe.
- Registrar término por budget separadamente de timeout ou falha do site.

## 7. Logs e auditoria

Cada evento administrativo registra timestamp UTC, ator opaco, sessão opaca, IP de origem, ação, recurso, ID, resultado, correlação e diff sanitizado. Eventos mínimos:

- criação e cancelamento de análise;
- início, conclusão, cancelamento e falha de job;
- publicação ou retirada de versão metodológica;
- ativação ou desativação de gate;
- alteração de peso, threshold, retenção ou permissão;
- exportação ou exclusão de artefato;
- autenticação e falhas de autorização.

Nunca registrar senhas, tokens, cookies, headers de autorização, variáveis de ambiente, corpos completos sem filtragem ou dados pessoais sensíveis. Erros provenientes do site-alvo pertencem às observações técnicas; ações de usuários e operadores pertencem à trilha de auditoria.

## 8. Banco de dados e segredos

- O usuário PostgreSQL de runtime não pode ser superuser nem possuir DDL.
- Migrations usam identidade separada e temporária no pipeline.
- Desenvolvimento, homologação e produção têm credenciais distintas.
- Secrets ficam em variáveis injetadas ou vault; arquivos `.env` reais não são versionados.
- PostgreSQL e Redis não são expostos à internet.
- TLS é obrigatório fora de localhost.
- Backups, object storage, probes e exportadores usam identidades distintas e privilégios mínimos.

## 9. Padrões e regulações aplicáveis

- [x] NSI.04, desenvolvimento seguro SciELO/FapUNIFESP.
- [x] LGPD, sujeita à definição formal de papéis, finalidade e bases legais.
- [ ] ISO/IEC 27001:2022, confirmar como requisito institucional ou referência.
- [x] RFC 9309 para interpretação de robots.txt.
- [x] ISO 28500 para futuros artefatos WARC.
- [ ] Política institucional de pesquisa, retenção e direitos autorais, a identificar.

## 10. Critérios de aprovação antes da codificação de rede

- [ ] Threat model revisado por responsável de segurança.
- [ ] Política de egress e ranges bloqueados aprovada.
- [ ] Limites padrão de URLs, bytes, redirects, tempo e concorrência definidos.
- [ ] Política de robots e User-Agent institucional definida.
- [ ] Retenção e visibilidade de artefatos aprovadas.
- [ ] Papéis, autenticação e possibilidade de uso anônimo decididos.
- [ ] Bases legais e responsabilidades LGPD registradas.
- [ ] Casos SSRF e vazamento incluídos nos testes de segurança.

## 11. Aprovação

| Nome | Cargo | Data | Assinatura ou registro |
|---|---|---|---|
| A definir | Responsável pelo produto |  |  |
| A definir | Segurança da informação |  |  |
| A definir | Encarregado ou responsável LGPD |  |  |
| A definir | Responsável científico/metodológico |  |  |
