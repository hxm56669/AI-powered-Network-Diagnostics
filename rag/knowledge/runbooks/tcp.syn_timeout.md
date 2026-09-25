---
doc_id: "runbook.tcp.syn_timeout"
title: "TCP 建连超时与 ICMP 可达性的区别"
kind: "runbook"
language: "zh-CN"
source_uri: "https://www.rfc-editor.org/rfc/rfc9293"
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
symptoms: ["connect_timeout"]
cause_ids: ["tcp_connect_timeout"]
check_ids: ["inspect_connect_state"]
checks: [{"check_id":"inspect_connect_state","purpose":"核对 TCP 建连状态与目标服务，区分主机可达和端口可用。","command_template":"ss -tan state syn-sent","permission":"依运行用户权限和目标授权核对；不自动执行。","timeout_s":15,"review_status":"approved","automatic_execution":false,"parameters_schema":{"type":"object","properties":{},"required":[],"additionalProperties":false},"parameter_sources":{}}]
references: ["https://www.rfc-editor.org/rfc/rfc9293"]
license: "project-authored-summary"
---

# TCP 建连超时与 ICMP 可达性的区别

## 适用条件

存在明确目标端口和 TCP 建连过程记录。

## 现象

SYN 重试或连接超时，ICMP 探测可能正常。

## 候选原因

端口过滤、服务未响应、回程路径或资源耗尽均可能。

## 支持证据

核对 SYN 与回复、目标端口、服务监听及连接错误。

## 反证

ICMP 成功只证明相应探测路径，不能证明应用端口可用。

## 检查步骤

在有权限的目标核对监听状态和连接轨迹，比较明确的服务端口。 检查命令仅为待运维确认的建议，不由诊断服务执行。

## 限制

不能把连接超时直接写成目标服务器宕机。

