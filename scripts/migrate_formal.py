from __future__ import annotations

import subprocess
import sys

from scripts.provision_database_roles import provision


def main() -> None:
    provision()
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], check=True)


if __name__ == "__main__":
    main()
