# Implantação Kubernetes

Esta base é segura por padrão, mas precisa ser adaptada ao cluster antes do uso.

1. Substitua os endpoints `.invalid` do ConfigMap pelos valores do provedor OIDC.
2. Publique uma imagem imutável e troque as referências por digest (`image@sha256:...`).
3. Crie externamente o Secret `arquivabilidade-runtime` com a chave `database-dsn`; não versione o Secret nem use `stringData` neste diretório.
4. Rotule o namespace cliente com `arquivabilidade-ja-client-access=true`.
5. Rotule o namespace do PostgreSQL com `arquivabilidade-ja-database-access=true` e mantenha o label `app.kubernetes.io/name=postgresql` no pod. Para banco gerenciado, substitua a regra por uma política compatível com o CNI e o destino real.
6. Restrinja a saída HTTPS da API aos IPs do JWKS quando o provedor oferecer faixas estáveis. A regra base bloqueia redes privadas, mas permite qualquer destino HTTPS público.

`/health/live` não consulta dependências. `/health/ready` confirma o pool e a migration mínima. `/internal/metrics` expõe somente agregados Prometheus e deve permanecer interno; não publique essa rota em Ingress ou Gateway.

Antes do deploy, aplique migrations e grants com a role separada, faça backup, renderize e valide os manifests, execute smoke tests e registre o procedimento de rollback. A base pode ser inspecionada com:

```bash
kubectl kustomize deploy/kubernetes/base
```

Use `RUNBOOK.md` como checklist mínimo de implantação e rollback e complete responsáveis, janela e evidências no registro de mudança do ambiente.
