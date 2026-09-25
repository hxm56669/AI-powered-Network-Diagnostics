---
doc_id: "runbook.ip.pmtu"
title: "路径 MTU 与大包传输异常"
kind: "runbook"
language: "zh-CN"
source_uri: "https://www.rfc-editor.org/rfc/rfc1191"
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
symptoms: ["large_packet_failure"]
cause_ids: ["path_mtu_issue"]
check_ids: ["inspect_path_mtu"]
checks: [{"check_id":"inspect_path_mtu","purpose":"取得路径 MTU 线索并验证包大小相关性。","command_template":"tracepath -n {target}","permission":"依运行用户权限和目标授权核对；不自动执行。","timeout_s":15,"review_status":"approved","automatic_execution":false,"parameters_schema":{"type":"object","properties":{"target":{"type":"string","minLength":1,"maxLength":255}},"required":["target"],"additionalProperties":false},"parameter_sources":{"target":"target"}}]
references: ["https://www.rfc-editor.org/rfc/rfc1191"]
license: "project-authored-summary"
---

# 路径 MTU 与大包传输异常

## 适用条件

已观察包大小相关失败并明确 IP 协议版本。

## 现象

小包正常而较大传输超时，连接可能建立后停滞。

## 候选原因

可能存在路径 MTU、ICMP 反馈过滤或隧道封装开销。

## 支持证据

比较包大小、DF/分片相关反馈、路径信息和应用症状。

## 反证

应用超时也可能由服务端限流或拥塞导致。

## 检查步骤

在隔离目标验证不同包大小，并收集路径 MTU 提示。 检查命令仅为待运维确认的建议，不由诊断服务执行。

## 限制

常规 ICMP 小包正常不能排除 MTU 问题。

