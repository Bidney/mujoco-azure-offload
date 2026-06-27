# Contributing

Thanks for your interest! This is a small, focused tool — issues and PRs welcome.

## Dev setup

```bash
git clone https://github.com/bidney/mujoco-azure-offload
cd mujoco-azure-offload
python -m venv .venv && . .venv/bin/activate
pip install -e .          # installs the `offload` console script + deps
```

## Running the tests

The suite is fully offline — it runs the **real** on-VM `runner.py` against a
filesystem-backed fake Azure SDK (see `tests/fakeazure/`), so no Azure account is
needed.

```bash
# unit + state-machine + security tests (integration auto-skips without a job venv)
python tests/run_tests.py

# include the end-to-end MuJoCo integration test
python -m venv jobenv && ./jobenv/bin/pip install numpy mujoco
MJOFF_TEST_JOBENV=$PWD/jobenv python tests/run_tests.py
```

What's covered: naming/config/pricing, cloud-init rendering + shell-injection
hardening, safe tar extraction (path-traversal guard), the monitor abort/ETA state
machine (every exit path), blob Store round-trip, idempotent teardown, and a full
parallel MuJoCo sweep through `runner.py`.

## Layout

See the "Layout" section of [README.md](README.md). The on-VM scripts live in
`azoffload/remote/` and are base64-embedded into the VM's cloud-init at launch.

## The job bundle contract

A bundle exposes `run_scenario(scenario: dict, workdir: str) -> dict`, called once per
scenario across all cores. See `sample_bundle/` for a complete example and the README
for the full contract.

## Conventions

- Keep the controller's dependencies minimal (stdlib + the three Azure/yaml deps).
- Match the surrounding style; no formatter config is enforced.
- Cost-safety and idempotent teardown are the project's hard invariants — please add a
  test in `tests/run_tests.py` for any change that touches them.
