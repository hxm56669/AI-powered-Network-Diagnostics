---
doc_id: "runbook.netdev.idle_throughput"
title: "低吞吐与无业务需求的区别"
kind: "runbook"
language: "zh-CN"
source_uri: "https://docs.kernel.org/networking/statistics.html"
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
symptoms: ["low_throughput"]
cause_ids: ["throughput_demand_unknown"]
check_ids: ["inspect_interface_counters"]
checks: [{"check_id":"inspect_interface_counters","purpose":"结合接口字节计数与应用需求判断低吞吐是否异常。","command_template":"ip -s link show dev {interface}","permission":"依运行用户权限和目标授权核对；不自动执行。","timeout_s":15,"review_status":"approved","automatic_execution":false,"parameters_schema":{"type":"object","properties":{"interface":{"type":"string","minLength":1,"maxLength":255}},"required":["interface"],"additionalProperties":false},"parameter_sources":{"interface":"interface"}}]
references: ["https://docs.kernel.org/networking/statistics.html"]
license: "project-authored-summary"
---

# 低吞吐与无业务需求的区别

## 适用条件

同一接口有效字节计数与单调时钟区间。

## 现象

接收或发送速率较低，连接数可能很少。

## 候选原因

业务空闲、应用限速、接收端受限及路径瓶颈均有可能。

## 支持证据

记录字节增量、实际间隔、业务负载和连接状态；B/s 与 bit/s 分开。

## 反证

没有业务发送需求时，低速率本身不构成故障。

## 检查步骤

先核对应用是否持续传输，再在隔离测试中观察有负载的吞吐。 检查命令仅为待运维确认的建议，不由诊断服务执行。

## 限制

不能从低速率直接推导网络带宽不足。

