"""Backup data tren VPS theo ngay, roi GOP catalog local vao DB server.

Khong de file uppromote.db (tranh mat cot extra). Khong dam billing.db / users.json.
Mat khau SSH lay tu web/_push_vietqr.py (gitignored).

    python update_data.py
    update.bat
"""

from __future__ import annotations

import importlib.util
import sys
import tarfile
from datetime import datetime
from pathlib import Path

import paramiko

from storage import catalog_schema_problems, connect

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
LOCAL_DB = DATA / "uppromote.db"
LOCAL_BRANDS = DATA / "brands"
STORAGE_PY = ROOT / "storage.py"
TMP_DIR = DATA / "_upload_tmp"
LOCAL_TAR = TMP_DIR / "brands.tar.gz"
CRED_PATH = ROOT / "web" / "_push_vietqr.py"

REMOTE_APP = "/var/www/upproinfo"
REMOTE_DATA = f"{REMOTE_APP}/data"
REMOTE_BACKUPS = "/var/backups/upproinfo"
REMOTE_TAR = "/tmp/uppro_brands.tar.gz"
REMOTE_INCOMING = "/tmp/uppromote.incoming.db"
REMOTE_STORAGE = "/tmp/upproinfo_storage.py"
REMOTE_LIVE = f"{REMOTE_DATA}/uppromote.db"


def load_ssh() -> tuple[str, str, str]:
    if not CRED_PATH.exists():
        raise SystemExit(
            f"Thieu {CRED_PATH}. Copy file gitignored web/_push_vietqr.py tu may da co SSH."
        )
    spec = importlib.util.spec_from_file_location("uppro_push", CRED_PATH)
    if spec is None or spec.loader is None:
        raise SystemExit("Khong load duoc web/_push_vietqr.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return str(mod.HOST), str(mod.USER), str(mod.PASSWORD)


def say(*parts: object) -> None:
    text = " ".join(str(p) for p in parts)
    try:
        print(text, flush=True)
    except UnicodeEncodeError:
        print(text.encode("ascii", "replace").decode("ascii"), flush=True)


def run(client: paramiko.SSHClient, command: str, check: bool = True) -> str:
    _stdin, stdout, stderr = client.exec_command(command)
    out = stdout.read().decode("utf-8", "replace")
    err = stderr.read().decode("utf-8", "replace")
    code = stdout.channel.recv_exit_status()
    if out.strip():
        say(out.rstrip())
    if err.strip() and code != 0:
        say(err.rstrip())
    if check and code != 0:
        raise RuntimeError(f"Lenh that bai ({code}): {command}")
    return out


def put(sftp: paramiko.SFTPClient, local: Path, remote: str) -> None:
    size_mb = local.stat().st_size / (1024 * 1024)
    say(f"Upload {local.name} ({size_mb:.1f} MB) -> {remote}")
    last = [-10]

    def cb(transferred: int, total: int) -> None:
        pct = int(transferred * 100 / total) if total else 100
        if pct >= last[0] + 20 or pct >= 100:
            last[0] = pct
            say(f"  {pct}%")

    sftp.put(str(local), remote, callback=cb)


def make_brands_tar() -> None:
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    if LOCAL_TAR.exists():
        LOCAL_TAR.unlink()
    count = sum(1 for _ in LOCAL_BRANDS.glob("*.json"))
    say(f"Dong goi {count} file brand -> {LOCAL_TAR.name}")
    with tarfile.open(LOCAL_TAR, "w:gz") as tar:
        tar.add(LOCAL_BRANDS, arcname="brands")
    say(f"Tar xong ({LOCAL_TAR.stat().st_size / (1024 * 1024):.1f} MB)")


def backup_name(client: paramiko.SSHClient) -> str:
    day = datetime.now().strftime("%Y-%m-%d")
    path = f"{REMOTE_BACKUPS}/{day}"
    out = run(client, f"test -d {path} && echo exists || echo ok", check=False)
    if "exists" in out:
        path = f"{REMOTE_BACKUPS}/{datetime.now().strftime('%Y-%m-%d_%H%M')}"
    return path


def prepare_local_db() -> None:
    connect()
    problems = catalog_schema_problems(LOCAL_DB)
    if problems:
        raise SystemExit(
            "DB local thieu schema, khong day len server: " + ", ".join(problems)
        )


def main() -> int:
    if not LOCAL_DB.exists():
        say(f"Khong thay {LOCAL_DB}")
        return 1
    if not LOCAL_BRANDS.exists():
        say(f"Khong thay {LOCAL_BRANDS}")
        return 1
    if not STORAGE_PY.exists():
        say(f"Khong thay {STORAGE_PY}")
        return 1

    say("Kiem tra schema local truoc khi day...")
    prepare_local_db()
    host, user, password = load_ssh()
    make_brands_tar()

    say(f"Ket noi {user}@{host} ...")
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        host,
        username=user,
        password=password,
        timeout=30,
        allow_agent=False,
        look_for_keys=False,
    )
    try:
        say("1/3 Backup data tren server theo ngay")
        run(client, "systemctl stop upproinfo")
        dest = backup_name(client)
        run(client, f"mkdir -p {dest}")
        run(
            client,
            "set -e; "
            f"cp -a {REMOTE_DATA}/uppromote.db {dest}/uppromote.db; "
            f"if [ -f {REMOTE_DATA}/billing.db ]; then cp -a {REMOTE_DATA}/billing.db {dest}/billing.db; fi; "
            f"if [ -d {REMOTE_DATA}/brands ]; then tar -C {REMOTE_DATA} -czf {dest}/brands.tar.gz brands; fi; "
            f"ls -lh {dest}",
        )
        say(f"Backup xong: {dest}")

        say("2/3 Upload catalog local (incoming), khong de file DB server")
        runner = TMP_DIR / "merge_run.py"
        runner.write_text(
            "from importlib.util import module_from_spec, spec_from_file_location\n"
            "from pathlib import Path\n"
            f"spec = spec_from_file_location('upproinfo_storage', {REMOTE_STORAGE!r})\n"
            "mod = module_from_spec(spec)\n"
            "spec.loader.exec_module(mod)\n"
            f"stats = mod.merge_catalog(Path({REMOTE_INCOMING!r}), Path({REMOTE_LIVE!r}))\n"
            "print('merge', stats)\n"
            f"problems = mod.catalog_schema_problems(Path({REMOTE_LIVE!r}))\n"
            "print('schema_ok', not problems, problems)\n"
            "if problems:\n"
            "    raise SystemExit('Schema server thieu: ' + ', '.join(problems))\n",
            encoding="utf-8",
        )
        sftp = client.open_sftp()
        try:
            put(sftp, LOCAL_DB, REMOTE_INCOMING)
            put(sftp, STORAGE_PY, REMOTE_STORAGE)
            put(sftp, LOCAL_TAR, REMOTE_TAR)
            put(sftp, runner, "/tmp/upproinfo_merge_run.py")
        finally:
            sftp.close()
            if runner.exists():
                runner.unlink()

        say("3/3 Gop offers/details/metrics vao DB server + giai nen brands")
        run(
            client,
            "set -e; "
            f"rm -rf {REMOTE_DATA}/brands; "
            f"tar -C {REMOTE_DATA} -xzf {REMOTE_TAR}; "
            f"rm -f {REMOTE_TAR}; "
            f"{REMOTE_APP}/.venv/bin/python /tmp/upproinfo_merge_run.py; "
            f"rm -f {REMOTE_INCOMING} {REMOTE_STORAGE} /tmp/upproinfo_merge_run.py; "
            f"chown www-data:www-data {REMOTE_LIVE}; "
            f"chown -R www-data:www-data {REMOTE_DATA}/brands; "
            "systemctl start upproinfo; sleep 2; systemctl is-active upproinfo; "
            f"{REMOTE_APP}/.venv/bin/python -c \""
            "import sqlite3; "
            f"c=sqlite3.connect('{REMOTE_LIVE}'); "
            "print('offers', c.execute('select count(*) from offers').fetchone()[0]); "
            "print('details', c.execute('select count(*) from details').fetchone()[0]); "
            "print('metrics', c.execute('select count(*) from brand_metrics').fetchone()[0]); "
            "cols=[r[1] for r in c.execute('pragma table_info(brand_metrics)')]; "
            "print('has_allows_search_ads', 'allows_search_ads' in cols); "
            "c.close()\"",
        )
        say("Cap nhat xong. billing.db tren server giu nguyen. uppromote.db duoc GOP, khong de file.")
        say(f"Rollback neu can: copy tu {dest}")
    except Exception:
        say("Loi. Dang thu start lai upproinfo...")
        run(client, "systemctl start upproinfo", check=False)
        raise
    finally:
        client.close()
        if LOCAL_TAR.exists():
            LOCAL_TAR.unlink()
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
