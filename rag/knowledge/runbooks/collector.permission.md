---
doc_id: "runbook.collector.permission"
title: "采集权限错误与网络异常的隔离"
kind: "runbook"
language: "zh-CN"
source_uri: "https://man7.org/linux/man-pages/man7/capabilities.7.html"
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
symptoms: ["permission_denied","missing_data"]
cause_ids: ["collector_permission_denied"]
check_ids: ["inspect_collector_permissions"]
checks: [{"check_id":"inspect_collector_permissions","purpose":"核对采集进程身份与 capability，区分采集失败和网络超时。","command_template":"getpcaps {pid}","permission":"依运行用户权限和目标授权核对；不自动执行。","timeout_s":15,"review_status":"approved","automatic_execution":false,"parameters_schema":{"type":"object","properties":{"pid":{"type":"integer","minimum":1,"maximum":2147483647}},"required":["pid"],"additionalProperties":false},"parameter_sources":{"pid":"pid"}}]
references: ["https://man7.org/linux/man-pages/man7/capabilities.7.html"]
license: "project-authored-summary"
---

# 采集权限错误与网络异常的隔离

## 适用条件

核心采集器返回明确权限错误且来源可核查。

## 现象

permission_denied、EPERM 或 EACCES 导致样本缺失。

## 候选原因

可确认采集权限受限；无法据此判断业务网络是否故障。

## 支持证据

记录失败系统调用或探测类型、错误码和服务运行身份。

## 反证

权限正常但目标无响应属于不同故障路径。

## 检查步骤

核对服务用户、命名空间及所需 capability；按最小权限修复。 检查命令仅为待运维确认的建议，不由诊断服务执行。

## 限制

不得将权限失败转为零延迟或探测成功。

