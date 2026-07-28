你是一个 API 文档分析专家。下面提供了若干 API 文档的摘要内容。

请为每个文档提取以下信息：
1. **api_list**：文档中涉及的 API / 函数 / 类 / 方法的名称列表
2. **summary**：一句话功能摘要（≤ 50 字）
3. **module**：所属模块 / 分类（如 "pandas.DataFrame", "torch.nn", "os.path" 等）

---

## 输入文档摘要

{doc_summaries}

---

## 输出格式

请严格按以下 JSON 格式输出，不要输出其他内容：

```json
{
  "scan_result": [
    {
      "filename": "xxx.md",
      "api_list": ["api_name_1", "api_name_2"],
      "summary": "一句话摘要",
      "module": "模块名"
    }
  ]
}
```

要求：
- api_list 只提取明确出现的 API/函数/类名，不要编造
- summary 要精炼，突出核心功能
- module 从文档标题或内容推断
