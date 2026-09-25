---
doc_id: "runbook.telemetry.counter_reset"
title: "计数器回退与跨启动差分"
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
symptoms: ["counter_reset"]
cause_ids: ["counter_reset"]
check_ids: ["verify_counter_epoch"]
checks: [{"check_id":"verify_counter_epoch","purpose":"确认计数所属启动周期，避免把回退差值写入速率。","command_template":"cat /proc/sys/kernel/random/boot_id","permission":"依运行用户权限和目标授权核对；不自动执行。","timeout_s":15,"review_status":"approved","automatic_execution":false,"parameters_schema":{"type":"object","properties":{},"required":[],"additionalProperties":false},"parameter_sources":{}}]
references: ["https://docs.kernel.org/networking/statistics.html"]
license: "project-authored-summary"
---

# 计数器回退与跨启动差分

## 适用条件

累计计数器采集，来源与启动标识可追踪。

## 现象

当前计数低于前值，或 boot、接口身份发生变化。

## 候选原因

可能发生重启、接口重建、计数器重置或读取范围改变。

## 支持证据

比较 boot、来源、接口身份及单调时钟；标记失效区间后重建基线。

## 反证

计数器重置不等同负吞吐或突然没有重传。

## 检查步骤

查看启动标识和接口状态，下一次有效样本作为新差分基线。 检查命令仅为待运维确认的建议，不由诊断服务执行。

## 限制

不能跨 boot 或不同 scope 计算增量。

