# Runbook de implantação e rollback

## Pré-condições

- Registrar a mudança, responsáveis, janela e digest imutável da imagem.
- Confirmar backup recente do PostgreSQL e evidência de teste de restauração.
- Validar a imagem e as dependências contra vulnerabilidades conhecidas.
- Substituir os valores `.invalid`, criar o Secret por mecanismo externo e revisar as políticas de rede para o cluster real.
- Renderizar `kubectl kustomize deploy/kubernetes/base` e validar o resultado em homologação com dados fictícios.

## Implantação

1. Aplicar migrations com a role exclusiva de migrations.
2. Aplicar `deploy/postgresql/runtime_grants.sql` e validar a role de runtime.
3. Aplicar a base Kubernetes já customizada para o ambiente.
4. Aguardar `rollout status` primeiro da API e depois do worker.
5. Confirmar `/health/live`, `/health/ready` e `archivability-worker-health`.
6. Executar smoke tests de autenticação, criação, consulta e consumo de um job fictício.
7. Confirmar métricas de fila, eventos de lifecycle e ausência de erros 5xx anormais.

## Critérios de rollback

Iniciar rollback se readiness não estabilizar, a taxa de 5xx crescer, jobs expirados aumentarem continuamente, autenticação falhar para tokens válidos ou houver indício de exposição de dados.

## Rollback

1. Suspender novas entradas no gateway sem remover os pods ainda observáveis.
2. Escalar workers para zero se o defeito puder alterar jobs.
3. Reaplicar o digest anterior da API e do worker e acompanhar o rollout.
4. Não reverta migrations apagando tabelas. A migration `002_rate_limit_buckets` é aditiva e pode permanecer com a versão anterior da aplicação.
5. Restaurar o banco somente se houver corrupção confirmada, seguindo o procedimento testado e preservando evidências do incidente.
6. Reabrir tráfego após smoke tests e registrar resultado, horários e responsáveis na mudança.
