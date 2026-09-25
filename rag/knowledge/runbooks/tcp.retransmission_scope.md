---
doc_id: "runbook.tcp.retransmission_scope"
title: "TCP 重传计数的统计范围与分母"
kind: "runbook"
language: "zh-CN"
source_uri: "https://docs.kernel.org/networking/snmp_counter.html"
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
symptoms: ["tcp_retransmissions"]
cause_ids: ["tcp_retransmission_observed"]
check_ids: ["inspect_tcp_counters"]
checks: [{"check_id":"inspect_tcp_counters","purpose":"核对 TCP 计数来源与连接级重传，避免把命名空间指标归因给接口。","command_template":"cat /proc/net/snmp","permission":"依运行用户权限和目标授权核对；不自动执行。","timeout_s":15,"review_status":"approved","automatic_execution":false,"parameters_schema":{"type":"object","properties":{},"required":[],"additionalProperties":false},"parameter_sources":{}}]
references: ["https://docs.kernel.org/networking/snmp_counter.html"]
license: "project-authored-summary"
---

# TCP 重传计数的统计范围与分母

## 适用条件

同一网络命名空间、boot 和连续有效区间。

## 现象

RetransSegs 与 OutSegs 增量之比上升。

## 候选原因

拥塞、超时、乱序及链路问题都可能触发重传。

## 支持证据

从同一计数来源读取重传段和输出段增量；分母需足够，回退区间排除。

## 反证

接口上的业务可能只是命名空间流量的一部分。

## 检查步骤

核对命名空间及采集时间；按连接查看 TCP 状态与重传。 检查命令仅为待运维确认的建议，不由诊断服务执行。

## 限制

该比值不是特定接口的链路丢包概率。

