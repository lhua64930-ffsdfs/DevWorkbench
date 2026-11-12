"""Provision/revoke personal access tokens without a public registration endpoint."""
import argparse
import secrets
from datetime import datetime, timedelta, timezone
from app.core.config import settings
from uuid import UUID

from app.db.models import Repository, RepositoryMember, User
from app.db.session import SessionLocal, engine
from app.db.schema import verify_schema
from app.services.authentication import token_hash
from app.services.permissions import ROLE_PERMISSIONS


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("name")
    parser.add_argument("--role", choices=list(ROLE_PERMISSIONS), help="Existing role is preserved when omitted; new users default to viewer")
    parser.add_argument("--repo", action="append", help="Replace repository grants; repeat for multiple repositories")
    parser.add_argument("--clear-repos", action="store_true")
    state = parser.add_mutually_exclusive_group()
    state.add_argument("--disable", action="store_true")
    state.add_argument("--enable", action="store_true")
    parser.add_argument("--revoke", action="store_true")
    parser.add_argument("--expires-days", type=int, default=settings.token_ttl_days)
    args = parser.parse_args()
    if not 1 <= args.expires_days <= 90:
        parser.error("令牌有效期必须介于 1 到 90 天")
    verify_schema(engine)
    with SessionLocal() as db:
        users = db.query(User).filter_by(name=args.name).all()
        if len(users) > 1:
            parser.error("用户名称不唯一，请先修复重复记录")
        if args.revoke and not users:
            parser.error("用户不存在")
        user = users[0] if users else User(name=args.name, role="viewer")
        repo_ids = [UUID(value) for value in args.repo or []]
        for repo_id in repo_ids:
            if db.get(Repository, repo_id) is None:
                parser.error(f"仓库不存在：{repo_id}")
        if args.role:
            user.role = args.role
        if args.disable or args.enable:
            user.disabled = args.disable
        token = secrets.token_urlsafe(32)
        user.api_token_hash = None if args.revoke else token_hash(token)
        user.token_expires_at = datetime.now(timezone.utc) + timedelta(days=args.expires_days)
        db.add(user)
        db.flush()
        if args.repo is not None or args.clear_repos:
            db.query(RepositoryMember).filter_by(user_id=user.id).delete()
            db.add_all(RepositoryMember(user_id=user.id, repo_id=repo_id) for repo_id in set(repo_ids))
        db.commit()
        print("访问令牌已撤销" if args.revoke else f"访问令牌（仅显示一次）：{token}")


if __name__ == "__main__":
    main()
