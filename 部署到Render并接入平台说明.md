# 学生管理 MCP 服务部署与平台接入说明

## 当前目标

把“学生成长画像与主动帮扶闭环”做成一个平台可访问的 SSE MCP 服务。平台需要填写的是公网地址，不适合填写本机 `127.0.0.1`。

## 推荐方式

使用 Render 部署本目录中的服务文件，得到一个 HTTPS 地址，例如：

```text
https://student-growth-support-mcp.onrender.com/sse
```

平台 MCP 服务中填写的接口地址应使用上面这种 `/sse` 结尾的地址。

## 文件说明

| 文件 | 用途 |
|---|---|
| `student_management_mcp_sse.py` | 正式 SSE MCP 服务入口，包含 4 个学生管理工具 |
| `requirements.txt` | Render 安装依赖 |
| `render.yaml` | Render 一键部署配置 |
| `student_records.json` | 模拟学生画像数据 |
| `followup_records.json` | 模拟帮扶跟进记录 |
| `mcp_servers.example.json` | 平台 JSON 导入示例 |

## MCP 工具清单

| 工具 | 作用 |
|---|---|
| `get_student_profile` | 按学号或姓名查询学生成长画像，返回关注等级、原因和帮扶建议 |
| `list_attention_students` | 生成中关注、高关注学生名单 |
| `create_followup_record` | 写入谈心谈话或帮扶跟进记录 |
| `get_class_dashboard` | 汇总班级关注等级、缺勤、实训异常和帮扶情况 |

## Render 部署步骤

1. 把 `学生管理MCP原型` 目录上传到一个 GitHub 仓库。
2. 登录 Render，选择 New Web Service。
3. 连接这个 GitHub 仓库。
4. Render 检测到 `render.yaml` 后，按配置部署。
5. 部署完成后打开 `/health`，看到 `status: ok` 即表示服务在线。
6. 将 Render 服务地址后面加 `/sse`，填入平台 MCP 服务配置。

## 平台接入方式

手动创建 MCP 服务：

- 服务名称：学生成长画像与主动帮扶 MCP 服务
- 描述：查询学生画像、生成关注名单、写入帮扶记录、生成班级态势看板。
- 链接方式：`sse`
- 接口地址：`https://你的-render-服务地址/sse`
- Header 配置：演示阶段可为空；正式环境建议增加访问令牌。

JSON 导入示例：

```json
{
  "mcpServers": {
    "student-growth-support": {
      "type": "sse",
      "url": "https://your-render-service.onrender.com/sse"
    }
  }
}
```

## 展示口径

这个 MCP 服务不是另一个业务系统，而是“校策通枢”给智能体开放的学生管理工具层。它让智能体不只回答政策，还能调用学生画像数据，主动发现需要关注的学生，生成帮扶建议，并回写跟进记录，形成“发现-建议-跟进-沉淀”的闭环。

## 注意事项

- 当前数据为比赛演示模拟数据，不接入真实学生隐私数据。
- 帮扶建议只作学生工作辅助，不替代处分、资助、成绩、安全责任等正式认定。
- Render 免费服务可能会休眠，首次访问可能较慢；正式答辩前建议提前唤醒服务。
