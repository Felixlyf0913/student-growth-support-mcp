# 学生管理 MCP 原型

## 目标

面向“学生成长画像与主动帮扶闭环”场景，提供一组已接入智能体平台的 MCP 工具，让智能体能够查询学生画像、筛选关注名单、创建和跟踪帮扶任务、写入帮扶记录、生成班级看板并联动钉钉群。

## 亮点

- 从“问答式学生管理”升级为“数据驱动的主动发现”。
- 把考勤、成绩、宿舍反馈、实训表现、资助状态、谈心记录合成学生成长画像。
- 支持生成“今日需关注名单”，体现学生管理从被动响应到主动预警。
- 支持写入帮扶记录，形成“发现问题-提出建议-落实跟进-记录回填”的闭环。
- 可生成班级看板数据，适合比赛答辩展示。

## 工具清单

| 工具 | 用途 |
|---|---|
| `get_student_profile` | 按学号或姓名查询学生成长画像 |
| `list_attention_students` | 筛选中/高关注学生名单 |
| `create_followup_record` | 写入谈心谈话或帮扶跟进记录 |
| `create_student_support_task` | 根据画像创建帮扶任务，可按明确要求推送辅导员群 |
| `update_student_support_task` | 更新任务状态、进展并可同步帮扶记录 |
| `list_student_support_tasks` | 查询待办、逾期和已完成任务 |
| `get_class_dashboard` | 生成班级学生状态看板数据 |
| `generate_and_send_class_weekly_report` | 生成匿名班级周报并推送教师工作群 |

## 本地运行

```powershell
cd D:\教育信息技术应用大赛\学生管理MCP原型
python -m uvicorn student_management_mcp_api:app --host 127.0.0.1 --port 8765
```

公网部署与智能体平台使用的是 `student_management_mcp_sse:app`，本地验证可运行：

```powershell
python -m uvicorn student_management_mcp_sse:app --host 127.0.0.1 --port 8000
```

## 调用示例

查询学生画像：

```json
{
  "tool": "get_student_profile",
  "arguments": {
    "query": "S004"
  }
}
```

查询需关注名单：

```json
{
  "tool": "list_attention_students",
  "arguments": {}
}
```

写入帮扶记录：

```json
{
  "tool": "create_followup_record",
  "arguments": {
    "student_id": "S002",
    "owner": "辅导员",
    "summary": "已完成首次谈心，学生反馈近期课程压力较大，实训日志补交意识不足。",
    "next_action": "联系实训指导教师确认补交要求，三天后复访。",
    "status": "跟进中"
  }
}
```

生成班级看板：

```json
{
  "tool": "get_class_dashboard",
  "arguments": {
    "class_name": "智能制造2401"
  }
}
```

创建帮扶任务（默认不推送）：

```json
{
  "tool": "create_student_support_task",
  "arguments": {
    "student_query": "S004",
    "owner": "辅导员",
    "due_date": "2026-07-18",
    "push_to_dingtalk": false
  }
}
```

完成任务并同步帮扶记录：

```json
{
  "tool": "update_student_support_task",
  "arguments": {
    "task_id": "任务编号",
    "status": "已完成",
    "progress_note": "已完成线下谈心。",
    "next_action": "联系任课教师并于一周后复访。",
    "sync_followup": true
  }
}
```

## 平台接入建议

平台实测支持两种入口：

1. 手动创建 MCP 服务
   - 服务名称
   - 描述
   - 链接方式：`sse`
   - 接口地址
   - Header 配置
   - 点击测试链接

2. 从 JSON 导入
   - 格式为 `mcpServers`
   - 示例见 `mcp_servers.example.json`

当前 SSE MCP 服务已部署到 Render，平台通过 `/sse` 地址发现和调用工具。本机 `127.0.0.1` 仅用于开发验证，不能作为平台的公网接入地址。
