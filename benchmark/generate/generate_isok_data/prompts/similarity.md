你是一个 API 分析专家。下面提供了多个 API 文档的摘要信息。

请识别其中**功能相似、可对比**的 API 组。功能相似的判断标准：
- 属于同一模块 / 库
- 解决类似的问题或完成类似的任务
- 参数或用法有可比性（用户可能会纠结该用哪个）

---

## 输入 API 摘要

{api_summaries}

---

## 输出格式

请严格按以下 JSON 格式输出，不要输出其他内容：

```json
{
  "similar_groups": [
    {
      "group_id": 1,
      "apis": ["api_name_1", "api_name_2"],
      "reason": "为什么这组 API 功能相似（一句话）",
      "source_files": ["file1.md", "file2.md"]
    }
  ]
}
```

要求：
- 只输出确实功能相似的组，不要强行凑组
- 每组 2~5 个 API
- reason 要简洁说明相似点
- source_files 列出这些 API 所在的文档文件名
