"""One-shot bootstrap of an isolated Milvus instance, before application startup."""
import os
from pymilvus import MilvusClient
from app.core.config import settings


def main():
    root_password = os.environ["MILVUS_ROOT_PASSWORD"]
    app_password = os.environ["MILVUS_APP_PASSWORD"]
    if len(root_password) < 32 or len(app_password) < 32 or root_password == app_password:
        raise RuntimeError("Independent random Milvus passwords are required")
    try:
        root = MilvusClient(uri=settings.milvus_uri, token="root:" + root_password, timeout=10)
        root.list_users(timeout=10)
    except Exception:
        root = MilvusClient(uri=settings.milvus_uri, token="root:Milvus", timeout=10)
        root.update_password(user_name="root", old_password="Milvus", new_password=root_password, timeout=10)
        root.close()
        root = MilvusClient(uri=settings.milvus_uri, token="root:" + root_password, timeout=10)
    if "codepilot" not in root.list_users(timeout=10):
        root.create_user(user_name="codepilot", password=app_password, timeout=10)
    role = "codepilot_writer"
    if role not in root.list_roles(timeout=10):
        root.create_role(role_name=role, timeout=10)
    # The groups are not hierarchical: grant collection data/index operations
    # and only the explicit database operations required for initial creation.
    root.grant_privilege_v2(role_name=role, privilege="CollectionReadWrite", collection_name="*", db_name="default", timeout=10)
    for privilege in ("CreateCollection", "ShowCollections", "DescribeDatabase"):
        root.grant_privilege_v2(role_name=role, privilege=privilege, collection_name="*", db_name="default", timeout=10)
    root.grant_role(user_name="codepilot", role_name=role, timeout=10)
    # Also make this bootstrap safe to rerun after the initial implementation.
    root.revoke_role(user_name="codepilot", role_name="admin", timeout=10)
    root.close()
    app = MilvusClient(uri=settings.milvus_uri, token="codepilot:" + app_password, timeout=10)
    app.list_collections(timeout=10)
    app.close()
    print("Milvus bootstrap verified; default root password rotated")


if __name__ == "__main__":
    main()
