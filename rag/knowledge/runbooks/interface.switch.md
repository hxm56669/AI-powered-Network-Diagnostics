---
doc_id: "runbook.interface.switch"
title: "接口切换与范围连续性"
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
symptoms: ["interface_changed"]
cause_ids: ["interface_switch"]
check_ids: ["inspect_interface_selection"]
checks: [{"check_id":"inspect_interface_selection","purpose":"确认出口切换时刻与当前路由，保持证据 scope 一致。","command_template":"ip -j route show","permission":"依运行用户权限和目标授权核对；不自动执行。","timeout_s":15,"review_status":"approved","automatic_execution":false,"parameters_schema":{"type":"object","properties":{},"required":[],"additionalProperties":false},"parameter_sources":{}}]
references: ["https://docs.kernel.org/networking/operstates.html"]
license: "project-authored-summary"
---

# 接口切换与范围连续性

## 适用条件

接口切换事件与每个样本的接口身份均可追踪。

## 现象

出口由有线切到无线，前后指标口径或质量变化。

## 候选原因

切换恢复连接、路由变化或新接口质量下降可能影响体验。

## 支持证据

按接口分开构建窗口，记录路由、地址和切换时间。

## 反证

两个接口同一时刻出现不同数值不是重复采样。

## 检查步骤

检查当前出口与切换事件，分别比较各接口有效窗口。 检查命令仅为待运维确认的建议，不由诊断服务执行。

## 限制

不能把旧接口计数器与新接口计数器相减。

