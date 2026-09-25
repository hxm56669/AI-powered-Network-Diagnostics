---
doc_id: "runbook.route.external_path"
title: "本地网关与外部路径对照"
kind: "runbook"
language: "zh-CN"
source_uri: "https://man7.org/linux/man-pages/man8/ip-route.8.html"
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
symptoms: ["high_rtt","icmp_timeout"]
cause_ids: ["external_path_degradation"]
check_ids: ["inspect_selected_route"]
checks: [{"check_id":"inspect_selected_route","purpose":"核对实际出口和路由选择，防止错误比较不同路径。","command_template":"ip route get {target}","permission":"依运行用户权限和目标授权核对；不自动执行。","timeout_s":15,"review_status":"approved","automatic_execution":false,"parameters_schema":{"type":"object","properties":{"target":{"type":"string","minLength":1,"maxLength":255}},"required":["target"],"additionalProperties":false},"parameter_sources":{"target":"target"}}]
references: ["https://man7.org/linux/man-pages/man8/ip-route.8.html"]
license: "project-authored-summary"
---

# 本地网关与外部路径对照

## 适用条件

本地网关与外部目标使用独立且同时段探测。

## 现象

网关稳定而外部目标延迟或超时明显增加。

## 候选原因

外部路径、目标服务或路径策略可能受限。

## 支持证据

核对路由选择、出口接口、目标和同窗网关数据。

## 反证

单个中间节点不回复不能证明该跳转发故障。

## 检查步骤

查询实际路由并换一个外部目标复测，保留目标差异。 检查命令仅为待运维确认的建议，不由诊断服务执行。

## 限制

现有对照只能缩小范围，不能直接定位运营商根因。

