"""文档同步框架的稳定错误类型。"""


class DocumentSyncError(Exception):
    """表示文档同步过程中可向 CLI 汇报的基础错误。"""


class SyncConfigError(DocumentSyncError):
    """表示同步 YAML 配置无效。"""


class AdapterRegistrationError(DocumentSyncError):
    """表示 Adapter 注册或创建失败。"""


class AdapterError(DocumentSyncError):
    """表示来源 Adapter 无法发现、获取或解析文档。"""


class FetchError(DocumentSyncError):
    """表示来源内容在重试后仍无法获取。"""


class PathSafetyError(DocumentSyncError):
    """表示候选路径逃逸、重叠或发生不安全碰撞。"""


class SyncLockError(DocumentSyncError):
    """表示已有同步任务持有运行锁。"""


class ResumeError(DocumentSyncError):
    """表示中断任务缺少可恢复的 journal 或 staging 文件。"""
