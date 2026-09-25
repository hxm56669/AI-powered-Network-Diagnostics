---
doc_id: "runbook.dns.resolve"
title: "DNS 解析错误需要独立证据"
kind: "runbook"
language: "zh-CN"
source_uri: "https://www.rfc-editor.org/rfc/rfc1035"
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
symptoms: ["dns_error"]
cause_ids: ["dns_resolution_failure"]
check_ids: ["inspect_dns_response"]
checks: [{"check_id":"inspect_dns_response","purpose":"取得 DNS 返回码和解析路径，避免从网络慢推断 DNS 故障。","command_template":"resolvectl query {hostname}","permission":"依运行用户权限和目标授权核对；不自动执行。","timeout_s":15,"review_status":"approved","automatic_execution":false,"parameters_schema":{"type":"object","properties":{"hostname":{"type":"string","minLength":1,"maxLength":255}},"required":["hostname"],"additionalProperties":false},"parameter_sources":{"hostname":"hostname"}}]
references: ["https://www.rfc-editor.org/rfc/rfc1035"]
license: "project-authored-summary"
---

# DNS 解析错误需要独立证据

## 适用条件

存在明确域名、解析器和 DNS 查询结果。

## 现象

域名解析超时、SERVFAIL 或 NXDOMAIN。

## 候选原因

解析器不可达、记录不存在、上游失败或策略限制需要区分。

## 支持证据

记录请求域名、查询类型、解析器、返回码、缓存与耗时。

## 反证

直接访问 IP 的 ICMP 正常不能证明 DNS 正常。

## 检查步骤

对照系统解析路径与指定解析器，保留原始返回码。 检查命令仅为待运维确认的建议，不由诊断服务执行。

## 限制

只有 RTT 和 RSSI 时不能确认 DNS 根因。

