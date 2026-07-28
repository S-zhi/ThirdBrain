# RAG With Cold API Documents

为代码 Agent 提供版本化 API 文档检索与可信上下文服务。当前向量存储使用
Zvec，生产集合为 `ascendc_api`。

## 启动 Zvec Studio

在项目根目录执行：

```bash
.venv/bin/zvec-studio --host 127.0.0.1 --port 7860
```

也可以先激活虚拟环境再启动：

```bash
source .venv/bin/activate
zvec-studio --host 127.0.0.1 --port 7860
```

启动成功后访问：

```text
http://127.0.0.1:7860
```

在 Studio 中打开生产集合时选择：

```text
/Users/wenzhengfeng/code/agent/ragWithColdApiDocument/data/zvec_collections/ascendc_api
```

停止服务时，在启动 Studio 的终端按 `Ctrl+C`。

### 集合锁注意事项

- Studio 打开集合后会持有集合目录中的 `LOCK`。
- 运行摄取、重建或其他写入任务前，先在 Studio 中关闭集合，或者停止 Studio。
- 不要手动删除 `LOCK` 文件；应先释放持有集合句柄的进程。

### Sparse Vector 兼容性

Zvec 使用 `dimension=0` 表示动态维度的 sparse vector。当前项目虚拟环境中的
Zvec Studio 已做兼容修复：sparse vector 允许维度为 `0`，dense vector 仍要求
维度大于等于 `1`。

重新安装或升级 `zvec-studio` 可能覆盖该本地修复；若再次出现以下错误，需要
重新应用兼容修复或升级到已正式支持 sparse `dimension=0` 的 Studio 版本：

```text
ValidationError: VectorSchema.dimension
Input should be greater than or equal to 1, input_value=0
```
