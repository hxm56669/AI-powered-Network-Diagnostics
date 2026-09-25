---
doc_id: "runbook.interface.link_down"
title: "接口 down 事件与链路中断"
kind: "runbook"
language: "zh-CN"
source_uri: "https://docs.kernel.org/networking/operstates.html"
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
symptoms: ["link_down"]
cause_ids: ["interface_link_down"]
check_ids: ["inspect_link_state"]
checks: [{"check_id":"inspect_link_state","purpose":"核对接口状态与事件范围，区分管理关闭和链路不可用。","command_template":"ip -j link show dev {interface}","permission":"依运行用户权限和目标授权核对；不自动执行。","timeout_s":15,"review_status":"approved","automatic_execution":false,"parameters_schema":{"type":"object","properties":{"interface":{"type":"string","minLength":1,"maxLength":255}},"required":["interface"],"additionalProperties":false},"parameter_sources":{"interface":"interface"}}]
references: ["https://docs.kernel.org/networking/operstates.html"]
license: "project-authored-summary"
---

# 接口 down 事件与链路中断

## 适用条件

事件与所诊断主机、接口和时间窗匹配。

## 现象

明确的接口管理状态或运行状态 down 事件。

## 候选原因

可确认该接口不可用现象；物理断线、管理关闭或驱动问题仍需区分。

## 支持证据

保留事件原文、时间、接口标识、旧新状态及前后数据质量。

## 反证

其他接口仍可承载流量，主机整体不一定断网。

## 检查步骤

检查接口管理状态、运行状态、carrier 与切换事件。 检查命令仅为待运维确认的建议，不由诊断服务执行。

## 限制

down 事件不能单独确定拔线或硬件损坏。

