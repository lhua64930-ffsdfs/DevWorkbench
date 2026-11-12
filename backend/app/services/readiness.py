"""Dependency probes without model calls, data mutation or credential disclosure."""
from app.core.config import settings
from app.db.doctor import database_readiness


def service_readiness(engine):
    result = database_readiness(engine)
    if result["status"] != "ready" or not settings.milvus_enabled:
        return result
    client = None
    try:
        from pymilvus import MilvusClient
        client = MilvusClient(uri=settings.milvus_uri, token=settings.milvus_token or "", timeout=3)
        client.list_collections(timeout=3)
        result["vector_store"] = "ok"
    except Exception:
        result.update(status="unavailable", vector_store="unavailable", message="向量库连接或身份验证失败，请检查内部服务。")
    finally:
        if client is not None:
            client.close()
    return result
