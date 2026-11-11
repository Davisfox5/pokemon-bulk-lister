"""The app as it runs on a serverless host (Vercel): no lasting disk, no
process between requests. The scenario itself is tests/serverless_smoke.py,
run here in a fresh interpreter because the app decides where it is running
when it is first imported."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent


def _run(tmp_path, database_url):
    env = {k: v for k, v in os.environ.items()
           if k not in ("TEST_DATABASE_URL", "BENCH_DATABASE_URL", "ALLOWED_HOSTS")}
    env.update(
        VERCEL="1", DATA_ROOT=str(tmp_path / "data"), DATABASE_URL=database_url,
        SECRET_KEY="test-secret", BLOB_READ_WRITE_TOKEN="fake", CRON_SECRET="cron-secret",
        JOB_SLICE_SECONDS="0", ADMIN_EMAILS="op@example.com", PRICE_REFRESH_ENABLED="0",
    )
    done = subprocess.run([sys.executable, str(HERE / "serverless_smoke.py")], env=env, cwd=HERE.parent,
                          capture_output=True, text=True, timeout=300)
    assert done.returncode == 0, done.stdout[-2000:] + done.stderr[-4000:]
    assert "serverless smoke ok" in done.stdout


def test_scan_bench_end_to_end_without_a_disk_or_a_long_running_process(tmp_path):
    _run(tmp_path, f"sqlite:///{tmp_path / 'app.sqlite'}")


@pytest.mark.skipif(not os.getenv("TEST_DATABASE_URL"), reason="needs a Postgres to run against")
def test_the_same_on_postgres(tmp_path):
    import psycopg

    url = os.environ["TEST_DATABASE_URL"]
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute("DROP SCHEMA public CASCADE")
        conn.execute("CREATE SCHEMA public")
    _run(tmp_path, url)
