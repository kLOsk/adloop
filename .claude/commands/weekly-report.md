Gerar relatório semanal COMPLETO de performance do Google Ads + GA4 e salvar no Diesel BI.

## 1. Puxar dados (adloop MCP)

- `get_campaign_performance` — últimos 7 dias
- `get_campaign_performance` — 7 dias anteriores (para comparação semana vs semana)
- `analyze_campaign_conversions` — dados cruzados Ads + GA4 com GDPR gap
- `get_keyword_performance` — top keywords por gasto
- `get_search_terms` — identificar termos irrelevantes e promissores
- `get_negative_keywords` — verificar o que já está bloqueado (evitar duplicatas)

## 2. Analisar

- Calcular variação % semana vs semana para cada KPI
- Destacar campanhas com gasto > R$200 e 0 conversões
- Destacar keywords com QS < 5
- Listar search terms irrelevantes como candidatos a negativa
- Listar search terms com boa conversão como candidatos a keyword
- Calcular gasto desperdiçado em termos irrelevantes
- Considerar GDPR antes de diagnosticar tracking como quebrado

## 3. Gerar relatório (OBRIGATÓRIO: seguir esta estrutura)

O body do relatório DEVE conter TODAS estas seções em markdown. Não resumir, não pular seções.

```markdown
# Disbra — Relatório Semanal {ANO}-W{SEMANA}

> {data_inicio} a {data_fim} | Gerado via Claude + AdLoop

## Resumo

- **Gasto:** R$ X | **Conversões:** X | **CPA:** R$ X
- **Clicks:** X | **CTR:** X% | **CPC médio:** R$ X
- vs semana anterior: gasto {+/-X%}, conversões {+/-X%}, CPA {+/-X%}

## Campanhas

| Campanha | Status | Gasto | Conv | CPA | CTR | vs Anterior |
|----------|--------|-------|------|-----|-----|-------------|
(todas as campanhas com dados)

**Observações:**
- (destaques, problemas, oportunidades por campanha)

## Top Keywords

| Keyword | Match | Gasto | Conv | CPA | QS | Tendência |
|---------|-------|-------|------|-----|----|-----------|
(top 10 por gasto)

**Problemas:**
- (keywords com QS baixo, CPA alto, etc)

## Search Terms — Desperdício

**R$ X em Y termos sem conversão**

| Termo | Clicks | Gasto | Problema |
|-------|--------|-------|----------|
(todos os termos com gasto > R$5 e 0 conversões)

**Termos promissores:**

| Termo | Clicks | Conv | Taxa |
|-------|--------|------|------|
(termos com boa taxa de conversão)

## GDPR Gap

| Ads Clicks | GA4 Sessions | Ratio | Status |
|-----------|-------------|-------|--------|
(dados do analyze_campaign_conversions)

## Diagnóstico

### O que está bom
- (bullets)

### O que precisa de atenção
- (bullets com números)

## Ações Recomendadas

| # | Ação | Prioridade | Impacto Estimado |
|---|------|-----------|-----------------|
(todas as ações, numeradas, priorizadas)
```

## 4. Salvar no Diesel BI (diesel-bi MCP)

Chamar as 3 tools com company_slug "disbra":

### 4a. salvar_relatorio
- report_type: "weekly"
- title: "Relatório Semanal — {ANO}-W{SEMANA}"
- period_start / period_end: datas da semana
- body: **O MARKDOWN COMPLETO das seções acima. NÃO resumir.**
- summary: 2-3 frases do resumo executivo (para o card na listagem)
- kpis: { "spend": X, "conversions": X, "cpa": X, "clicks": X, "ctr": X, "cpc": X }
- source: "adloop"
- status: "published"

### 4b. salvar_insights (com o report_id retornado)
Cada ação recomendada da seção "Ações Recomendadas" vira um insight separado:
- category: "negative_keywords" | "budget" | "bidding" | "ad_copy" | "campaign_structure" | "tracking" | "landing_page" | "general"
- priority: "high" | "medium" | "low"
- title: descrição curta da ação
- description: detalhe com números (ex: "Sol Diesel gastou R$17 sem conversão em 30 dias")
- impact: estimativa (ex: "~R$360/ano economia", "+15% CTR", "evita R$500/mês desperdício")

### 4c. salvar_kpi_snapshot
- period_type: "weekly"
- period_start / period_end
- spend, clicks, impressions, conversions, cpa, ctr, cpc
- top_campaign: nome da campanha com menor CPA
- waste_amount: total gasto em termos sem conversão
- insights_count: quantos insights gerados

## 5. Também salvar .md local

Salvar cópia em `reports/weekly/{ANO}-W{SEMANA}.md` para histórico git.

## 6. Apresentar ao usuário

Resumo executivo com as 3 ações mais importantes.
Se o Diesel BI MCP não estiver disponível, gerar só o .md e avisar.
