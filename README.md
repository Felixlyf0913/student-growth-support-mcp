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
| `get_support_task_audit` | 查询任务创建和状态变更审计台账 |
| `get_system_readiness` | 只读检查存储、任务数据和钉钉渠道配置 |
| `get_class_dashboard` | 生成班级学生状态看板数据 |
| `get_data_ingestion_status` | 展示 Word/Excel/PDF 业务台账的文件入库状态和记录统计 |
| `get_student_growth_timeline` | 查询学生考勤、实训、筛查建议、谈话、任务和预警时间线 |
| `get_class_operational_records` | 查询班级动态台账和治理概览 |
| `list_followup_reminders` | 查询即将到期或逾期的复访提醒 |
| `generate_and_send_class_weekly_report` | 生成匿名班级周报并推送教师工作群 |
| `get_training_room_availability` | 查询指定实训室在指定时段是否有排课或预约冲突 |
| `list_available_training_rooms` | 按日期、时段、容量和设备条件筛选可用实训室 |
| `get_class_training_schedule` | 查询班级实训安排与场地信息 |
| `get_equipment_repair_status` | 查询异常设备、报修工单与预计处理状态 |

## 本地运行

```powershell
cd D:\教育信息技术应用大赛\学生管理MCP原型
python -m uvicorn student_management_mcp_api:app --host 127.0.0.1 --port 8765
```

公网部署与智能体平台使用的是 `student_management_mcp_sse:app`，本地验证可运行：

```powershell
python -m uvicorn student_management_mcp_sse:app --host 127.0.0.1 --port 8000
```

## 数据持久化

- 配置 `DATABASE_URL` 时使用 PostgreSQL，源台账入库、任务、跟进记录和审计台账可跨 Render 部署保留。
- 未配置时自动使用 `.data/student_management.db` 本地 SQLite，适合开发测试，但 Render 重新部署后可能丢失。
- 首次启用数据库时会自动迁移仓库内 `followup_records.json` 和 `support_tasks.json` 的基础数据。
- 首次部署还会读取 `演示业务源文件` 中的 5 份 Excel、1 份 Word 和 1 份 PDF，将比赛演示名册、考勤学业、实训、筛查建议、谈话、任务以及实训室资源排课台账规范化后写入数据库；后续 MCP 新增的谈话和任务会继续累积。
- 健康检查 `/health` 会返回存储类型和记录数量，但不会返回数据库地址或钉钉密钥。

PostgreSQL 连接变量示例：

```text
DATABASE_URL=postgresql://用户名:密码@主机:5432/数据库名
```

## 演示数据维护

`演示业务源文件` 内全部为比赛演示模拟数据，不对应真实个人。推荐的录屏主线为：

```text
工作人员维护 Excel / Word / PDF 台账
  -> 服务自动解析并入库 PostgreSQL
  -> 班级态势与学生成长时间线
  -> 生成帮扶任务并回写谈话记录
  -> 按期复访提醒与任务审计
  -> 按日期、时段、容量和设备条件查询可用实训室
```

可先调用 `get_data_ingestion_status` 展示文件、入库数量和数据链路，再按角色调用受控查询工具：

- `get_authorized_student_growth_timeline`：学生限本人，班主任和辅导员限授权班级。
- `get_authorized_class_operational_records`：班主任和辅导员查看授权班级动态，行政人员仅匿名聚合。
- `list_authorized_followup_reminders`：班主任和辅导员查看授权范围内的复访待办。

恢复基线数据：

```powershell
python demo_data_maintenance.py --mode baseline --confirm RESET-DEMO-DATA
```

生成两条固定演示任务：

```powershell
python demo_data_maintenance.py --mode sample --confirm RESET-DEMO-DATA
```

该脚本会清理当前任务和审计台账，因此只允许维护人员在录屏准备阶段使用；它不是 MCP 工具，也不会发送钉钉消息。

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
