"""Re-encrypt stored GitHub tokens using the current key, in one transaction."""
import argparse
from app.core.config import settings
from app.core.security import decrypt_token, encrypt_token
from app.db.models import Repository, User
from app.db.session import SessionLocal, engine
from app.db.schema import verify_schema


def rotate(apply=False):
    verify_schema(engine)
    count = 0
    with SessionLocal() as db:
        for model in (Repository, User):
            for row in db.query(model).filter(model.github_token_encrypted.isnot(None)).with_for_update():
                token = decrypt_token(row.github_token_encrypted, settings.token_encryption_key)
                if token is None:
                    raise RuntimeError("A stored credential cannot be decrypted; nothing was committed")
                row.github_token_encrypted = encrypt_token(token, settings.token_encryption_key)
                count += 1
        if apply:
            db.commit()
        else:
            db.rollback()
    return count


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Commit; otherwise validate without writing")
    args = parser.parse_args()
    print(f"Credentials {'rotated' if args.apply else 'validated'}: {rotate(args.apply)}")
