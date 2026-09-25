---
doc_id: "runbook.icmp.timeout"
title: "ICMP 超时与探测失败的区分"
kind: "runbook"
language: "zh-CN"
source_uri: "https://www.rfc-editor.org/rfc/rfc792"
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
symptoms: ["icmp_timeout"]
cause_ids: ["icmp_probe_timeout"]
check_ids: ["compare_icmp_and_service"]
checks: [{"check_id":"compare_icmp_and_service","purpose":"比较 ICMP 与应用端口可达性，辨别探测协议特有的限制。","command_template":"ping -n -c 5 -W 1 {target}","permission":"依运行用户权限和目标授权核对；不自动执行。","timeout_s":15,"review_status":"approved","automatic_execution":false,"parameters_schema":{"type":"object","properties":{"target":{"type":"string","minLength":1,"maxLength":255}},"required":["target"],"additionalProperties":false},"parameter_sources":{"target":"target"}}]
references: ["https://www.rfc-editor.org/rfc/rfc792"]
license: "project-authored-summary"
---

# ICMP 超时与探测失败的区分

## 适用条件

已知探测目标、协议、发送次数和等待期限。

## 现象

探测未在期限内收到回复，或收到不可达错误。

## 候选原因

可能存在拥塞、目标限制 ICMP、路径异常或本地探测失败。

## 支持证据

分别记录回复、超时、不可达和发送失败，分母使用真实尝试次数。

## 反证

目标限制 ICMP 时，应用 TCP 连接仍可能正常。

## 检查步骤

比较另一目标及同路径上的业务连接，核对本地权限错误。 检查命令仅为待运维确认的建议，不由诊断服务执行。

## 限制

ICMP 超时比例不能直接等同业务丢包率。

