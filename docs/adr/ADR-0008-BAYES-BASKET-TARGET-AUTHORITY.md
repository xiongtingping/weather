---
status: accepted
authority: L0
owner: bayes_basket_target
effective_from: 2026-07-26
---

# ADR-0008：Bayes × 自适应篮子目标线唯一文档权威

- 状态：Accepted
- 生效日期：2026-07-26
- 执行模式：`production_no_live`
- Live授权：false

## 决策

`bayes_basket_target_architecture_v1` 是目标线唯一架构权威；Markdown 是人读
正本，JSON 是同版本机器可读镜像，两者合称一个逻辑 authority，不构成双权威。

开发进度只由 `bayes_basket_development_roadmap_v1_6` 声明；站点策略层级与现金
阶段版本是下位合同，不得改写 ProbabilityEnvelope、方向、执行模式或 live 授权。

仓库中的 `CURRENT_ARCHITECTURE`、`TARGET_ARCHITECTURE_V2`、旧 Bayes、旧
Target Realization、Shadow/研究文档和旧执行策略只属于其他生产线、历史证据或
研究材料。它们不是本 target lane 的 current authority，不得被 target runtime
导入、回退或用于解释冲突。

## 不变量

- 唯一模式是 `production_no_live`。
- `live_submit_authorized=false`、`calls_order_gateway=false`、订单数为0。
- 权威清单必须恰有一个 current logical authority。
- 架构 Markdown/JSON 版本必须一致。
- 架构引用的 roadmap 版本必须与 roadmap 正本一致。
- 下位合同不得声明 live 授权。
- authority audit 只读、有界，不读取共享 live DB。

## 证据

- `config/bayes_basket_target/document_authority_v1.json`
- `scripts/bayes_basket_target_document_authority_audit.py`
- `output/audits/bayes_basket_target_document_authority.json`
