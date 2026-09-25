---
doc_id: "runbook.telemetry.freshness"
title: "重复快照、迟到与真实采样覆盖"
kind: "runbook"
language: "zh-CN"
source_uri: "https://man7.org/linux/man-pages/man2/clock_gettime.2.html"
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
symptoms: ["missing_data","stale_data"]
cause_ids: ["telemetry_stale"]
check_ids: ["inspect_sampling_timestamps"]
checks: [{"check_id":"inspect_sampling_timestamps","purpose":"比较源时间和接收时间，确认缺测属于采集还是传输阶段。","command_template":"cat /proc/uptime","permission":"依运行用户权限和目标授权核对；不自动执行。","timeout_s":15,"review_status":"approved","automatic_execution":false,"parameters_schema":{"type":"object","properties":{},"required":[],"additionalProperties":false},"parameter_sources":{}}]
references: ["https://man7.org/linux/man-pages/man2/clock_gettime.2.html"]
license: "project-authored-summary"
---

# 重复快照、迟到与真实采样覆盖

## 适用条件

源采样时间、接收时间与独立采样标识可用。

## 现象

重复读取旧值、接收乱序，或窗口中有效槽数偏少。

## 候选原因

可能是采集停滞、传输积压或调度延迟。

## 支持证据

按源时间归窗并以采样标识去重；接收截止时间限制当时可见数据。

## 反证

最近值仍能展示状态，但不代表每秒都产生新观测。

## 检查步骤

比较源时间、接收时间和序号，确认时钟跳变与采集健康状态。 检查命令仅为待运维确认的建议，不由诊断服务执行。

## 限制

补齐展示值不能提高统计覆盖率。

