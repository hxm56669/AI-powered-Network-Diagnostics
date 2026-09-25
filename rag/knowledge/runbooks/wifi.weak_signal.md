---
doc_id: "runbook.wifi.weak_signal"
title: "无线信号偏弱与外部路径延迟的鉴别"
kind: "runbook"
language: "zh-CN"
source_uri: "https://wireless.docs.kernel.org/en/latest/en/users/documentation/iw.html"
source_type: "reviewed_internal"
owner: "netdiag-rag-seed"
review_status: "approved"
reviewed_at: "2026-09-23T16:55:40Z"
reviewer: "project-user"
available_at: "2026-09-23T16:55:40Z"
valid_from: "2026-09-22T17:20:16Z"
valid_until: null
scope: {"organizations":["public"]}
applies_to: {"os":["linux"],"interface_type":["wifi"],"protocols":["generic"],"product_versions":["generic"]}
symptoms: ["high_rtt","low_rssi"]
cause_ids: ["wifi_signal_degradation"]
check_ids: ["compare_gateway_and_external_icmp"]
checks: [{"check_id":"compare_gateway_and_external_icmp","purpose":"对照网关与外部目标，区分本地无线链路与外部路径。","command_template":null,"permission":"依运行用户权限和目标授权核对；不自动执行。","timeout_s":15,"review_status":"approved","automatic_execution":false,"parameters_schema":{"type":"object","properties":{"gateway":{"type":"string","minLength":1,"maxLength":255},"external_target":{"type":"string","minLength":1,"maxLength":255}},"required":["gateway","external_target"],"additionalProperties":false},"parameter_sources":{"gateway":"gateway","external_target":"external_target"}}]
references: ["https://wireless.docs.kernel.org/en/latest/en/users/documentation/iw.html"]
license: "project-authored-summary"
---

# 无线信号偏弱与外部路径延迟的鉴别

## 适用条件

无线接口且 RSSI 来自有效独立采样。

## 现象

RSSI 偏弱与 RTT 上升同时出现，但二者相关不等于因果。

## 候选原因

信号衰减、接入点负载或外部路径均可能参与异常。

## 支持证据

比较相同时间窗的 RSSI、成功探测 RTT、超时次数及目标；检查独立网关探测。

## 反证

网关稳定而仅外部目标异常时需优先检查外部路径。

## 检查步骤

比较默认网关和外部目标；再复测位置变化前后的信号与时延。 检查命令仅为待运维确认的建议，不由诊断服务执行。

## 限制

单个 RSSI 值不能确认 AP 故障或无线丢包率。

