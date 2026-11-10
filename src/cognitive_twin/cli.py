"""Command line: run, migrate, operate and train.

  cognitive-twin serve                       start the API and web app
  cognitive-twin migrate                     apply database migrations
  cognitive-twin db create-app-role          least-privilege role for the service
  cognitive-twin school create NAME [--region IN] [--youngest under13|13to17|adult]
                                             add a school; prints its admin invite code
  cognitive-twin school list | invite SCHOOL_ID [--role educator|school_admin]
  cognitive-twin policy import FILE          store a policy version (and activate it)
  cognitive-twin policy import-bundled       activate the shipped policy for new subjects
  cognitive-twin policy list | activate VER  list versions / roll forward or back
  cognitive-twin validators add ID NAME STAKE
  cognitive-twin keys rotate VALIDATOR [--kms-key-arn ARN]
  cognitive-twin keys list
  cognitive-twin ledger verify               full hash-chain check (nightly job)
  cognitive-twin train | demo                simulation tools
"""

from __future__ import annotations

import argparse
import json
import sys
from importlib import resources


def _db():
    from .config import get_settings
    from .db.session import engine_for, session_factory
    s = get_settings()
    return s, session_factory(engine_for(s.database_url))


def cmd_serve(a: argparse.Namespace) -> None:
    import uvicorn
    uvicorn.run("cognitive_twin.app:app_from_env", factory=True, host=a.host, port=a.port,
                proxy_headers=True, forwarded_allow_ips=a.forwarded_allow_ips,
                workers=a.workers, log_config=None)


def cmd_migrate(a: argparse.Namespace) -> None:
    from alembic import command
    from alembic.config import Config

    from .config import get_settings
    cfg = Config()
    cfg.set_main_option("script_location",
                        str(resources.files("cognitive_twin").joinpath("db/migrations")))
    cfg.set_main_option("sqlalchemy.url", get_settings().database_url.replace("%", "%%"))
    command.upgrade(cfg, a.revision)
    print(f"database migrated to {a.revision}")


def cmd_db(a: argparse.Namespace) -> None:
    import os

    from .config import get_settings
    from .db.roles import APP_ROLE, create_app_role
    from .db.session import engine_for
    pw = os.environ.get("CT_APP_DB_PASSWORD", "")
    create_app_role(engine_for(get_settings().database_url), pw)
    print(f"role {APP_ROLE} created/updated with least-privilege grants")


def cmd_policy(a: argparse.Namespace) -> None:
    from .db.session import transaction
    from .policy_store import (
        activate_policy,
        import_bundled_policies,
        import_policy_file,
        list_policies,
    )
    _, sessions = _db()
    with transaction(sessions) as db:
        if a.action == "import-bundled":
            done = import_bundled_policies(db, actor_id="cli")
            print("activated bundled policies for: " + (", ".join(done) or "nothing new"))
        elif a.action == "import":
            v = import_policy_file(db, a.target, actor_id="cli", notes=a.notes)
            print(f"imported and activated policy {v}")
        elif a.action == "activate":
            activate_policy(db, a.target, actor_id="cli", reason=a.notes or "cli")
            print(f"activated policy {a.target}")
        else:
            for p in list_policies(db):
                print(f"{'*' if p['active'] else ' '} {p['track']:<22} {p['version']}  "
                      f"{p['notes']}")


def cmd_school(a: argparse.Namespace) -> None:
    from sqlalchemy import select

    from .db.models import AuditEvent, School
    from .db.session import transaction
    from .schools import AccountError, create_invite, create_school
    _, sessions = _db()
    with transaction(sessions) as db:
        try:
            if a.action == "create":
                school = create_school(db, a.target, a.region, a.youngest)
                code = create_invite(db, school, "school_admin", "cli")
                db.add(AuditEvent(action="school.create", target=school.id, outcome="ok",
                                  details={"name": school.name, "via": "cli"}))
                print(f"created {school.name} ({school.id})\n"
                      f"school administrator invitation code (valid 14 days): {code}\n"
                      "They open the app, choose 'I have an invitation code', and set a "
                      "password.")
            elif a.action == "invite":
                school = db.get(School, a.target)
                if school is None:
                    sys.exit(f"no school with id {a.target}")
                print(create_invite(db, school, a.role, "cli"))
            else:
                for sc in db.scalars(select(School).order_by(School.name)).all():
                    print(f"{sc.id}  {sc.name:<40} {sc.sign_in:<6} {sc.region:<4} "
                          f"youngest: {sc.youngest_band}")
        except AccountError as e:
            sys.exit(str(e))


def cmd_validators(a: argparse.Namespace) -> None:
    from .db.models import Validator
    from .db.session import transaction
    _, sessions = _db()
    with transaction(sessions) as db:
        db.add(Validator(id=a.id, name=a.name, stake=a.stake))
    print(f"validator {a.id} added; now register its key with `keys rotate {a.id}`")


def cmd_keys(a: argparse.Namespace) -> None:
    from .db.session import transaction
    from .keys import KeyRegistry, provider_from_settings
    s, sessions = _db()
    with transaction(sessions) as db:
        reg = KeyRegistry(db, provider_from_settings(s))
        if a.action == "rotate":
            k = reg.rotate(a.validator, a.kms_key_arn)
            print(f"validator {a.validator}: key {k.kid} (version {k.version}) is now active; "
                  "the previous key is retired and stays published for verification")
        else:
            for k in reg.published():
                print(f"{k['validator']:<30} v{k['version']:<3} {k['status']:<8} {k['kid']}")


def cmd_ledger(a: argparse.Namespace) -> None:
    from .db.models import AuditEvent
    from .db.session import transaction
    from .keys import KeyRegistry, provider_from_settings
    from .ledger.service import LedgerService
    from .observability import log, log_event, setup_logging
    s, sessions = _db()
    setup_logging(s.log_level)
    with transaction(sessions) as db:
        lg = LedgerService(db, KeyRegistry(db, provider_from_settings(s)))
        ok, bad = lg.verify_chain()
        db.add(AuditEvent(action="ledger.verify", target="scheduled",
                          outcome="ok" if ok else "broken", details={"first_bad_block": bad}))
        n = lg.block_count()
    # the log line is what the LedgerChainBroken metric filter and alarm match on
    log_event(log, "ledger chain intact" if ok else "LEDGER_CHAIN_BROKEN", blocks=n,
              first_bad_block=bad)
    sys.exit(0 if ok else 2)


def cmd_train(a: argparse.Namespace) -> None:
    from .rl.train import benchmark_all
    r = benchmark_all(episodes=a.episodes, learners=a.learners, steps=a.steps, seed=a.seed,
                      track_id=a.track)
    r["teacher"].save(a.out)
    print(f"policy saved to {a.out} (import it with `cognitive-twin policy import {a.out}`)")
    curve = r["learning_curve"]
    print("training curve (mean final knowledge while exploring):")
    print("  " + "  ".join(f"{c['episodes']}: {c['mean_knowledge']:.2f}" for c in curve))
    print(f"\nheld-out benchmark ({a.learners} simulated learners, {a.steps} steps each):")
    print(f"  {'teacher':<16}{'knowledge':>10}{'mastered':>10}{'breaks':>8}{'accuracy':>10}")
    for b in r["benchmarks"]:
        print(f"  {b['teacher']:<16}{b['mean_knowledge']:>10.2f}{b['mean_mastered']:>10.2f}"
              f"{b['mean_breaks']:>8.1f}{b['mean_accuracy']:>10.2f}")
    cp = r["credential_precision"]
    print(f"\ncredentials issued: {cp['credentials']} ({cp['per_learner']} per learner); "
          f"true knowledge >= 0.8 for {cp['share_true_at_least_0.8']:.0%}, "
          f">= 0.7 for {cp['share_true_at_least_0.7']:.0%}")
    if a.json:
        with open(a.json, "w") as f:
            json.dump({k: v for k, v in r.items() if k != "teacher"}, f, indent=2)


def cmd_demo(a: argparse.Namespace) -> None:
    import numpy as np

    from .consent import ConsentProfile
    from .edge.sensor import EdgeProcessor, NeuralBand
    from .env.simulator import SimulatedLearner
    from .rl.teacher import MultiAgentTeacher, run_episode
    from .skills import load_track
    from .twin import CognitiveTwin
    track = load_track(a.track)
    path = resources.files("cognitive_twin").joinpath(f"content/policies/{track.id}.json")
    teacher = MultiAgentTeacher.load(str(path), track, seed=a.seed)
    rng = np.random.default_rng(a.seed)
    learner = SimulatedLearner.sample(track, rng)
    twin = CognitiveTwin("demo", track)
    edge = EdgeProcessor(ConsentProfile(22, "IN", True), NeuralBand(rng))
    res = run_episode(teacher, learner, twin, edge, a.steps, learn=False)
    for att in twin.attempts:
        mark = "✓" if att.correct else "✗"
        print(f"  step {att.step:>3}  {att.skill:<18} d{att.difficulty} {att.modality:<19} "
              f"{mark}  belief {twin.p_known[att.skill]:.2f}")
    print(f"\nbreaks: {res.breaks}  mastered (belief): {twin.mastered() or 'none yet'}  "
          f"guardian interventions: {len(teacher.guardian.log)}")


def main() -> None:
    ap = argparse.ArgumentParser(prog="cognitive-twin", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(required=True)

    s = sub.add_parser("serve", help="start the API and web app")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8000)
    s.add_argument("--workers", type=int, default=1)
    s.add_argument("--forwarded-allow-ips", default="127.0.0.1")
    s.set_defaults(fn=cmd_serve)

    m = sub.add_parser("migrate", help="apply database migrations")
    m.add_argument("revision", nargs="?", default="head")
    m.set_defaults(fn=cmd_migrate)

    dbp = sub.add_parser("db", help="database administration")
    dbp.add_argument("action", choices=["create-app-role"])
    dbp.set_defaults(fn=cmd_db)

    p = sub.add_parser("policy", help="manage teaching policy versions")
    p.add_argument("action", choices=["import", "import-bundled", "list", "activate"])
    p.add_argument("target", nargs="?", default="")
    p.add_argument("--notes", default="")
    p.set_defaults(fn=cmd_policy)

    sc = sub.add_parser("school", help="manage schools")
    sc.add_argument("action", choices=["create", "list", "invite"])
    sc.add_argument("target", nargs="?", default="", help="school name (create) or id (invite)")
    sc.add_argument("--region", default="IN")
    sc.add_argument("--youngest", default="adult", choices=["under13", "13to17", "adult"],
                    help="youngest age group the school enrols")
    sc.add_argument("--role", default="educator", choices=["educator", "school_admin"])
    sc.set_defaults(fn=cmd_school)

    v = sub.add_parser("validators", help="register validators")
    v.add_argument("action", choices=["add"])
    v.add_argument("id")
    v.add_argument("name")
    v.add_argument("stake", type=float)
    v.set_defaults(fn=cmd_validators)

    k = sub.add_parser("keys", help="manage validator signing keys")
    k.add_argument("action", choices=["rotate", "list"])
    k.add_argument("validator", nargs="?", default="")
    k.add_argument("--kms-key-arn", default=None)
    k.set_defaults(fn=cmd_keys)

    lg = sub.add_parser("ledger", help="ledger operations")
    lg.add_argument("action", choices=["verify"])
    lg.set_defaults(fn=cmd_ledger)

    t = sub.add_parser("train", help="train the agents in simulation and benchmark them")
    t.add_argument("--episodes", type=int, default=800)
    t.add_argument("--learners", type=int, default=200)
    t.add_argument("--steps", type=int, default=80)
    t.add_argument("--seed", type=int, default=7)
    t.add_argument("--track", default="ml-foundations", help="subject id to train for")
    t.add_argument("--out", default="policy.json")
    t.add_argument("--json", help="also write benchmark results to this file")
    t.set_defaults(fn=cmd_train)

    d = sub.add_parser("demo", help="teach one simulated learner in the terminal")
    d.add_argument("--steps", type=int, default=60)
    d.add_argument("--seed", type=int, default=3)
    d.add_argument("--track", default="ml-foundations", help="subject id")
    d.set_defaults(fn=cmd_demo)

    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
