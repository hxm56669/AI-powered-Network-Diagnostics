---
doc_id: "runbook.icmp.rtt_window"
title: "RTT 分位数与成功样本偏差"
kind: "runbook"
language: "zh-CN"
source_uri: "https://www.rfc-editor.org/rfc/rfc2681"
source_type: "reviewed_internal"
owner: "netdiag-rag-seed"
review_status: "approved"
reviewed_at: "2026-09-23T16:55:40Z"
reviewer: "project-user"
available_at: "2026-09-23T16:55:40Z"
valid_from: "2026-09-22T17:20:16Z"
valid_until: null
scope: {"organizations":["public"]}
applies_to: {"os":["linux"],"interface_type":["generic"],"protocols":["generic"],"product_versions":["generic"]}
symptoms: ["high_rtt"]
cause_ids: ["rtt_increase"]
check_ids: ["inspect_rtt_sample_quality"]
checks: [{"check_id":"inspect_rtt_sample_quality","purpose":"核对成功样本和超时样本的统计口径，避免分位数掩盖失败。","command_template":"ping -n -c 10 -W 1 {target}","permission":"依运行用户权限和目标授权核对；不自动执行。","timeout_s":15,"review_status":"approved","automatic_execution":false,"parameters_schema":{"type":"object","properties":{"target":{"type":"string","minLength":1,"maxLength":255}},"required":["target"],"additionalProperties":false},"parameter_sources":{"target":"target"}}]
references: ["https://www.rfc-editor.org/rfc/rfc2681"]
license: "project-authored-summary"
---

# RTT 分位数与成功样本偏差

## 适用条件

同一目标、同一协议且时间窗与采样量已记录。

## 现象

成功探测 RTT p95 升高；部分探测同时超时。

## 候选原因

排队、路径变化或目标响应延迟均可能导致变慢。

## 支持证据

使用成功 RTT 样本计算分位数，同时报告尝试数和超时数。

## 反证

成功 RTT 不变也不能排除大量超时导致的业务恶化。

## 检查步骤

比较相邻历史窗口及独立目标，保留样本量和统计算法版本。 检查命令仅为待运维确认的建议，不由诊断服务执行。

## 限制

不得把超时填零或当作实际 RTT。

