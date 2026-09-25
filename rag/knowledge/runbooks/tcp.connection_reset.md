---
doc_id: "runbook.tcp.connection_reset"
title: "ECONNRESET 与 TCP 连接复位"
kind: "runbook"
language: "zh-CN"
source_uri: "https://man7.org/linux/man-pages/man7/tcp.7.html"
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
symptoms: ["ECONNRESET"]
cause_ids: ["tcp_connection_reset"]
check_ids: ["inspect_reset_connection"]
checks: [{"check_id":"inspect_reset_connection","purpose":"将复位错误关联到具体连接及服务日志。","command_template":"ss -tin","permission":"依运行用户权限和目标授权核对；不自动执行。","timeout_s":15,"review_status":"approved","automatic_execution":false,"parameters_schema":{"type":"object","properties":{},"required":[],"additionalProperties":false},"parameter_sources":{}}]
references: ["https://man7.org/linux/man-pages/man7/tcp.7.html"]
license: "project-authored-summary"
---

# ECONNRESET 与 TCP 连接复位

## 适用条件

有连接级错误及本地远端地址、端口、时间。

## 现象

应用收到 ECONNRESET 或抓取到相关 RST。

## 候选原因

远端关闭、代理行为、中间设备策略或协议不匹配需结合日志判断。

## 支持证据

关联应用错误、连接生命周期、RST 与对应服务日志。

## 反证

仅有主机重传比上升并不能确认连接复位原因。

## 检查步骤

核对连接状态和服务端同时间日志，确认哪端主动复位。 检查命令仅为待运维确认的建议，不由诊断服务执行。

## 限制

一个 ECONNRESET 错误码不能证明物理链路异常。

