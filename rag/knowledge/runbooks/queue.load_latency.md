---
doc_id: "runbook.queue.load_latency"
title: "负载下的排队延迟与空闲基线"
kind: "runbook"
language: "zh-CN"
source_uri: "https://www.rfc-editor.org/rfc/rfc7567"
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
symptoms: ["high_rtt","high_throughput"]
cause_ids: ["queueing_under_load"]
check_ids: ["inspect_queue_statistics"]
checks: [{"check_id":"inspect_queue_statistics","purpose":"结合负载窗口和 qdisc 统计判断排队是否可能参与延迟。","command_template":"tc -s qdisc show dev {interface}","permission":"依运行用户权限和目标授权核对；不自动执行。","timeout_s":15,"review_status":"approved","automatic_execution":false,"parameters_schema":{"type":"object","properties":{"interface":{"type":"string","minLength":1,"maxLength":255}},"required":["interface"],"additionalProperties":false},"parameter_sources":{"interface":"interface"}}]
references: ["https://www.rfc-editor.org/rfc/rfc7567"]
license: "project-authored-summary"
---

# 负载下的排队延迟与空闲基线

## 适用条件

存在同目标的空闲与负载窗口及流量测量。

## 现象

吞吐上升时 RTT 同步升高，负载结束后回落。

## 候选原因

瓶颈队列积压、应用排队或目标负载均需鉴别。

## 支持证据

对照负载前中后的 RTT、超时、接口速率与队列统计。

## 反证

没有持续发送需求时，低吞吐不支持瓶颈队列结论。

## 检查步骤

读取 qdisc 统计，在隔离环境重复有无负载的对照。 检查命令仅为待运维确认的建议，不由诊断服务执行。

## 限制

同涨同落只是候选证据，不能直接确定排队位置。

