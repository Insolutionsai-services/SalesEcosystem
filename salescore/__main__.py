"""CLI.
  python -m salescore dev [--port 8000] [--no-browser] [--reload]   seed demo data, run API + UI, open browser
  python -m salescore seed                               create the demo tenant, print its API key
  python -m salescore tick [--daily]                     cron: every 5 min (--daily once a day)
  python -m salescore train                              relearn every tenant's data (catalog, FAQ, intents, scoring)
  python -m salescore models                             list models available from the configured LLM provider
  python -m salescore clean                              delete the local SQLite database and trained models
  python -m salescore user EMAIL [--name N] [--role admin|member] [--company NAME]   add a sign-in (asks for the password)
  python -m salescore plan COMPANY trial|unlimited       lift (or restore) the 14-day trial caps for one company
"""
import argparse
import logging

from .core.utils import load_env_file

load_env_file(".env")  # before anything reads config


def for_each_tenant(job) -> None:
    from sqlalchemy import select

    from .core.models import Session, Tenant, init_db

    init_db()
    with Session() as s:
        for tenant_id in s.scalars(select(Tenant.id)).all():
            try:
                print(tenant_id, job(s, s.get(Tenant, tenant_id)))
                s.commit()
            except Exception:  # one tenant's failure must not stop the others
                s.rollback()
                logging.exception("job failed for tenant %s", tenant_id)


def clean() -> None:
    import shutil
    from pathlib import Path

    from .core.config import DATABASE_URL, MODEL_DIR
    db = Path(DATABASE_URL.removeprefix("sqlite:///")) if DATABASE_URL.startswith("sqlite:///") else None
    try:
        if db:
            for f in (db, Path(f"{db}-wal"), Path(f"{db}-shm")):
                f.unlink(missing_ok=True)
    except PermissionError:
        raise SystemExit(f"{db} is in use: stop make dev (Ctrl+C) first, then run make clean again.")
    shutil.rmtree(MODEL_DIR, ignore_errors=True)
    print("Removed the local database and trained models.")


def _company(s, name: str | None):
    """The one company this command is about: by name, or the only one there is."""
    from sqlalchemy import select

    from .core.models import Tenant
    companies = s.scalars(select(Tenant).where(Tenant.name == name) if name else select(Tenant)).all()
    if len(companies) != 1:
        names = ", ".join(t.name for t in s.scalars(select(Tenant))) or "none yet (run make dev or POST /v1/tenants)"
        raise SystemExit(f"Pick the company by name. Companies: {names}")
    return companies[0]


def add_user(args) -> None:
    from getpass import getpass

    from .core.models import Session, init_db
    from .services.plan import PlanLimit
    from .services.users import create_user
    init_db()
    with Session() as s:
        company = _company(s, args.company)
        password = getpass("Password (8+ characters): ")
        try:
            create_user(s, company.id, args.email, args.name, password, args.role)
        except (ValueError, PlanLimit) as e:
            raise SystemExit(str(e)) from None
        s.commit()
    print(f"{args.email} can now sign in to {company.name} as {args.role}.")


def set_plan(args) -> None:
    from .core.models import Session, init_db
    from .services import plan
    init_db()
    with Session() as s:
        company = _company(s, args.company)
        plan.set_plan(company, args.plan)
        s.commit()
        print(f"{company.name}: plan is now {company.plan}.")


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(prog="python -m salescore")
    sub = parser.add_subparsers(dest="cmd", required=True)
    dev = sub.add_parser("dev", help="seed demo data, run API + UI with reload, open browser")
    dev.add_argument("--host", default="127.0.0.1")
    dev.add_argument("--port", type=int, default=8000)
    dev.add_argument("--no-browser", action="store_true")
    dev.add_argument("--reload", action="store_true", help="restart on code changes (for developing salescore)")
    sub.add_parser("seed", help="create the demo tenant and print its API key")
    tick = sub.add_parser("tick", help="run due follow-ups for every tenant")
    tick.add_argument("--daily", action="store_true", help="also retrain, run analytics + AI insights")
    sub.add_parser("train", help="relearn every tenant's data")
    sub.add_parser("models", help="list models from the configured LLM provider")
    sub.add_parser("clean", help="delete the local SQLite database and trained models")
    user = sub.add_parser("user", help="add a person who can sign in (admin = sales lead, member = sales rep)")
    user.add_argument("email")
    user.add_argument("--name", default="")
    user.add_argument("--role", default="member", choices=["admin", "member"])
    user.add_argument("--company", help="company name (needed when there is more than one)")
    pl = sub.add_parser("plan", help="set a company's plan: trial (14 days, small caps) or unlimited")
    pl.add_argument("company")
    pl.add_argument("plan", choices=["trial", "unlimited"])
    args = parser.parse_args()

    if args.cmd == "clean":
        clean()
        return
    if args.cmd == "user":
        add_user(args)
        return
    if args.cmd == "plan":
        set_plan(args)
        return
    from . import dev as devtools
    if args.cmd == "dev":
        devtools.run(args.host, args.port, not args.no_browser, args.reload)
    elif args.cmd == "seed":
        print(devtools.seed_demo().api_key)
    elif args.cmd == "tick":
        from .workflows.scheduler import tick as tick_job
        for_each_tenant(lambda s, t: tick_job(s, t, daily=args.daily))
    elif args.cmd == "train":
        from .analytics.training import train_tenant
        for_each_tenant(train_tenant)
    else:
        from .ai import llm
        cfg = llm.settings()
        print(f"provider={cfg.provider} base_url={cfg.base_url} model={cfg.model} lite={cfg.model_lite}")
        print("\n".join(llm.list_models()))


main()
