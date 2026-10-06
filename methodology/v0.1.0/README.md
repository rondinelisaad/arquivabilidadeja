# Metodologia candidata 0.1.0

Esta pasta contém a primeira especificação executável da metodologia Arquivabilidade JA. Ela ainda não é uma release científica: pesos novos, cobertura mínima e hard gates permanecem como decisões de projeto ou hipóteses a validar.

## Arquivos

- `manifest.yaml`: identidade, estado e proveniência do pacote.
- `dimensions.yaml`: seis dimensões do modelo novo.
- `indicators.yaml`: 45 indicadores com origem, classificação, evidências e limitações.
- `scoring.yaml`: semântica dos estados, cobertura, scores dimensionais e compatibilidade CLEAR+.
- `gates.yaml`: sete hard gates candidatos, todos inativos.
- `schemas/`: JSON Schemas Draft 2020-12.

Os arquivos `.yaml` usam deliberadamente o subconjunto JSON do YAML 1.2. Isso permite validação básica com a biblioteca padrão do Python e evita dependências antes da fundação da aplicação.

## Invariantes

- `unknown`, `not_applicable` e `context` nunca viram score zero.
- Confiança não multiplica score.
- Indicadores `observation_context` não pontuam.
- Nenhum hard gate candidato bloqueia uma avaliação sem ativação explícita numa nova decisão metodológica.
- `Legacy CLEAR+ Weighted Score` usa `fixed_max` por padrão; `paper_applicable` existe apenas para reprodução histórica rotulada.
- Uma mudança que altere resultados exige nova versão da metodologia.

## Executar os golden tests

```bash
python3 -m unittest discover -s tests/methodology -p 'test_*.py' -v
```

Os golden tests são uma referência independente, não o futuro scoring engine de produção. A implementação deverá produzir os mesmos resultados a partir das mesmas entradas.
