# Fase 6 — protocolo de validação científica

## Estado e limite da entrega

A infraestrutura reproduzível está implementada. A metodologia `0.1.0` continua
**candidata**: o repositório não contém uma amostra real, pareceres de especialistas
nem evidência suficiente para declarar pesos, limiares ou gates cientificamente
validados. Preencher o arquivo com dados sintéticos demonstra o processo, não valida
o construto.

## Protocolo mínimo

1. Pré-registrar hipótese, população, estratos, tamanho da amostra, critérios de
   exclusão e perfis de limiar antes de observar o holdout.
2. Selecionar casos por amostragem estratificada, incluindo sites estáticos,
   aplicações dinâmicas, escalas e domínios institucionais distintos. Não condicionar
   a seleção à existência de RSS.
3. Atribuir identificadores opacos aos casos e avaliadores. URLs, nomes, contatos,
   tokens e conteúdo coletado não pertencem ao dataset de concordância.
4. Fixar a versão da metodologia e gerar scores dimensionais pela exportação
   canônica. Não criar um score geral apenas para facilitar a validação.
5. Obter avaliações cegas de ao menos dois especialistas por caso e por dimensão,
   usando apenas `fail`, `warning` e `pass`. Registrar instruções e treinamento fora
   do dataset público.
6. Definir uma partição de calibração e outra de holdout antes do ajuste. Um
   `case_id` não pode ocorrer nas duas; perfis são escolhidos somente na calibração.
7. Gerar o relatório descritivo, congelar o perfil escolhido e avaliar o holdout uma
   única vez. Publicar protocolo, dataset desidentificado, hashes, relatório e
   justificativas de exclusão.
8. Toda alteração que mude resultados cria uma nova versão da metodologia e novos
   golden tests. Gates só podem ser ativados com decisão metodológica explícita.

## Métricas produzidas

- **Fleiss' kappa:** concordância entre especialistas quando há pelo menos dois casos
  e a mesma quantidade de avaliadores por item;
- **consenso:** maioria simples por item; empates ficam fora da comparação com o
  modelo e são contabilizados;
- **acurácia e Cohen's kappa:** classificação do perfil primário versus consenso;
- **sensibilidade:** quantidade e taxa de valores dimensionais cuja classe muda em
  cada perfil alternativo;
- contagem por split e estrato, sem juntar calibração e holdout.

Essas métricas são descritivas. Não demonstram causalidade, validade de construto,
representatividade ou equivalência com um crawler arquivístico real.

## Formato de entrada

O contrato `1.0` está exemplificado em
[`validation/example-dataset.json`](../validation/example-dataset.json). Os campos são
fechados e o parser limita o arquivo a 10 MiB, 100 mil casos, 32 perfis, 32 estratos,
32 dimensões e 100 avaliadores por caso. Scores devem ser finitos entre 0 e 100.

Execute:

```bash
archivability-validation validation/example-dataset.json \
  --output validation-report.json
```

O JSON de saída é determinístico, inclui SHA-256 da entrada normalizada e carrega
limitações explícitas. O arquivo gerado não deve ser interpretado como aprovação da
metodologia candidata.

## Critério de saída ainda externo

A Fase 6 só pode receber o estado de **validada cientificamente** quando existirem:

- protocolo pré-registrado e dataset real estratificado;
- pareceres independentes e documentação do instrumento usado pelos especialistas;
- decisão registrada sobre limiares, pesos e gates baseada na calibração;
- resultado de holdout não usado no ajuste;
- revisão científica e release nova da metodologia.

Até lá, a conclusão correta é “infraestrutura de validação concluída; validação
científica pendente de evidência externa”.
