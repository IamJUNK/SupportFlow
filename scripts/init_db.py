from supportflow.config import get_settings
from supportflow.db import Database


if __name__ == "__main__":
    settings = get_settings()
    db = Database(settings.database_path)
    db.seed_demo()
    print(f"initialized {settings.database_path}")
