"""Command line: train, benchmark, demo and serve."""

from __future__ import annotations

import argparse
import json

from .consent import ConsentProfile


def cmd_train(a: argparse.Namespace) -> None:
    from .rl.train import benchmark_all
    r = benchmark_all(episodes=a.episodes, learners=a.learners, steps=a.steps, seed=a.seed)
    r["teacher"].save(a.out)
    print(f"policy saved to {a.out}")
    print("training curve (mean final knowledge while exploring):")
    curve = r["learning_curve"]
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
    from .service import Platform
    p = Platform(seed=a.seed)
    s = p.enrol("Demo learner", ConsentProfile(age=22, region="IN", neural_opt_in=True))
    print(f"enrolled {s.id}; neural sensing active: {s.edge.active}")
    for step in p.autoplay(s, a.steps):
        if step.get("action") == "break":
            print(f"  step {step['step']:>3}  BREAK")
        else:
            mark = "✓" if step["correct"] else "✗"
            print(f"  step {step['step']:>3}  {step['skill']:<18} d{step['difficulty']} "
                  f"{step['modality']:<19} {mark}  belief {step['belief']:.2f}")
    print("\nmastered:", s.twin.mastered() or "none yet")
    for skill in s.twin.mastered():
        res = p.request_credential(s, skill)
        print(f"credential for {skill}: {'ISSUED' if res.issued else 'refused'} ({res.reason})")
        if res.issued:
            v = p.ledger.verify_credential(res.credential)
            print(f"  verified: {v['valid']}  signed stake {v['signed_stake']}")
    print(f"guardian interventions: {len(s.teacher.guardian.log)}")


def cmd_serve(a: argparse.Namespace) -> None:
    import uvicorn
    uvicorn.run("cognitive_twin.api:app", host=a.host, port=a.port)


def main() -> None:
    ap = argparse.ArgumentParser(prog="cognitive-twin")
    sub = ap.add_subparsers(required=True)
    t = sub.add_parser("train", help="train the agents in simulation and benchmark them")
    t.add_argument("--episodes", type=int, default=800)
    t.add_argument("--learners", type=int, default=200)
    t.add_argument("--steps", type=int, default=80)
    t.add_argument("--seed", type=int, default=7)
    t.add_argument("--out", default="policy.json")
    t.add_argument("--json", help="also write benchmark results to this file")
    t.set_defaults(fn=cmd_train)
    d = sub.add_parser("demo", help="run one simulated learner end to end")
    d.add_argument("--steps", type=int, default=60)
    d.add_argument("--seed", type=int, default=3)
    d.set_defaults(fn=cmd_demo)
    s = sub.add_parser("serve", help="start the API and dashboard")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8000)
    s.set_defaults(fn=cmd_serve)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
