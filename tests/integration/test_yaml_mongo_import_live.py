from pathlib import Path

from fastapi.testclient import TestClient
from pymongo import MongoClient

from src.core.api_document_yaml import read_yaml_document
from src.dao.mongo import MongoSettings
from src.main import app
from src.service.yaml_import_service import YamlImportSettings

PROJECT_ROOT = Path(__file__).resolve().parents[2]
TARGET_YAML_PATH = (
    PROJECT_ROOT
    / "ingest/output/Sub/SIMD_API/其他数据类型/BinaryRepeatParams.yaml"
)
TARGET_COLLECTION = "ascendc_910beta3"


def test_binary_repeat_params_yaml_import_is_idempotent() -> None:
    """通过真实 HTTP 接口导入同一个 YAML 两次并验证只保留一条记录。"""
    import_settings = YamlImportSettings(
        allowed_roots=(TARGET_YAML_PATH.parents[3],)
    )
    parsed_document = read_yaml_document(
        TARGET_YAML_PATH,
        max_file_bytes=import_settings.max_file_bytes,
    )
    content_hash = parsed_document.payload["source"]["content_hash"]
    identity_filter = {"source.content_hash": content_hash}
    mongo_settings = MongoSettings()
    mongo_client = MongoClient(
        mongo_settings.uri.get_secret_value(),
        serverSelectionTimeoutMS=mongo_settings.server_selection_timeout_ms,
    )

    try:
        collection = mongo_client[mongo_settings.database][TARGET_COLLECTION]
        existing_count = collection.count_documents(identity_filter)
        assert existing_count <= 1, (
            f"导入前已发现重复记录：collection={TARGET_COLLECTION}, "
            f"content_hash={content_hash}, count={existing_count}"
        )

        request_payload = {
            "items": [
                {
                    "custom_id": "binary-repeat-params-functional-test",
                    "file_path": str(TARGET_YAML_PATH),
                    "collection": TARGET_COLLECTION,
                }
            ]
        }
        with TestClient(app) as client:
            first_response = client.post(
                "/api/v1/admin/yaml-imports/batch",
                json=request_payload,
            )
            second_response = client.post(
                "/api/v1/admin/yaml-imports/batch",
                json=request_payload,
            )

        assert first_response.status_code == 200
        assert second_response.status_code == 200
        first_result = first_response.json()
        second_result = second_response.json()

        assert first_result["failed_count"] == 0
        assert first_result["results"][0]["status"] in {"inserted", "duplicate"}
        assert second_result["failed_count"] == 0
        assert second_result["duplicate_count"] == 1
        assert second_result["results"][0]["status"] == "duplicate"
        assert (
            first_result["results"][0]["inserted_id"]
            == second_result["results"][0]["inserted_id"]
        )
        assert collection.count_documents(identity_filter) == 1

        stored_document = collection.find_one(identity_filter)
        assert stored_document is not None
        stored_document.pop("_id")
        assert stored_document == parsed_document.payload
    finally:
        mongo_client.close()
