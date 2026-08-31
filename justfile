set positional-arguments

workflow := "workflows/multi-harness.toml"
python := "uv run python"

default:
    @just --list

manager harness="":
    @if test -n {{quote(harness)}}; then node bin/herdr-orchestrator.mjs manager {{quote(harness)}}; else node bin/herdr-orchestrator.mjs manager; fi

install-manager:
    @npm install --global .
    @herdr-orchestrator manager-light install

[positional-arguments]
doctor *args:
    @PYTHONPATH=src {{python}} -m herdr_orchestrator doctor --workflow {{workflow}} "$@"

test:
    @mkdir -p .orchestrator/quality
    @PYTHONPATH=src uv run pytest tests --durations=0 --json-report --json-report-file=.orchestrator/quality/tests.json

test-coverage:
    @mkdir -p .orchestrator/quality
    @PYTHONPATH=src uv run pytest tests --durations=0 --cov=herdr_orchestrator --cov-branch --cov-report=term-missing --cov-report=json:.orchestrator/quality/coverage.json --cov-fail-under=80 --json-report --json-report-file=.orchestrator/quality/tests.json

test-stability:
    @PYTHONPATH=src uv run python scripts/test_stability.py --runs 3 --output .orchestrator/quality/stability.json

lint:
    @uv run ruff check src tests scripts
    @uv run black --check src tests scripts
    @uv run mypy
    @uv run pylint src/herdr_orchestrator --disable=all --enable=invalid-name,duplicate-code
    @uv run vulture src tests scripts --min-confidence 90
    @uv run xenon --max-absolute C --max-modules B --max-average A src
    @uv run lint-imports
    @uv run deptry src
    @uv run python scripts/check_repository.py
    @uv run python scripts/check_feature_flags.py
    @uv run python scripts/check_docs.py
    @uv run python scripts/generate_reference.py --check

security:
    @mkdir -p .orchestrator/quality
    @uv run detect-secrets-hook --baseline .secrets.baseline $(git ls-files --cached --others --exclude-standard)
    @uv run bandit -q -r src -ll -f json -o .orchestrator/quality/bandit.json
    @uv run pip-audit --local --format json --output .orchestrator/quality/pip-audit.json
    @npm audit --package-lock-only
    @npm audit --package-lock-only --prefix packages/herdr-manager

build-metrics:
    @uv run python scripts/build_metrics.py --output .orchestrator/quality/build.json

profile-tests:
    @mkdir -p .orchestrator/quality
    @PYTHONPATH=src uv run python -c 'import cProfile, pytest; profiler = cProfile.Profile(); status = profiler.runcall(pytest.main, ["tests/test_protocol.py", "-q"]); profiler.dump_stats(".orchestrator/quality/tests.pstats"); raise SystemExit(status)'

quality-summary:
    @uv run python scripts/quality_summary.py --output .orchestrator/quality/summary.md

docs-generate:
    @uv run python scripts/generate_reference.py

docs-check:
    @uv run python scripts/check_docs.py
    @uv run python scripts/generate_reference.py --check

check:
    @uv sync --locked
    @PYTHONPATH=src {{python}} -m compileall -q src tests scripts
    @just lint
    @just test-coverage
    @just test-stability
    @just security
    @just build-metrics
    @just profile-tests
    @just quality-summary

seed:
    @PYTHONPATH=src {{python}} -m herdr_orchestrator seed --workflow {{workflow}}

status:
    @PYTHONPATH=src {{python}} -m herdr_orchestrator status --workflow {{workflow}}

[positional-arguments]
dashboard *args:
    @PYTHONPATH=src {{python}} -m herdr_orchestrator dashboard --workflow {{workflow}} "$@"

catalog:
    @PYTHONPATH=src {{python}} -m herdr_orchestrator catalog --workflow {{workflow}} --format text

catalog-json:
    @PYTHONPATH=src {{python}} -m herdr_orchestrator catalog --workflow {{workflow}} --format json

profile harness:
    @PYTHONPATH=src {{python}} -m herdr_orchestrator profile --workflow {{workflow}} {{quote(harness)}}

# enqueue: 入队任务。extra flag 转发必须用 {{args}}，勿改回 "$@"（positional 模式下 "$@" 含全部参数，会把已绑定参数重复传给 CLI）
[positional-arguments]
enqueue harness title prompt_file dedupe_key *args:
    @PYTHONPATH=src {{python}} -m herdr_orchestrator enqueue --workflow {{workflow}} --harness {{quote(harness)}} --title {{quote(title)}} --prompt-file {{quote(prompt_file)}} --dedupe-key {{quote(dedupe_key)}} {{args}}

[positional-arguments]
run-once *args:
    @PYTHONPATH=src {{python}} -m herdr_orchestrator run --workflow {{workflow}} --once "$@"

[positional-arguments]
run-until-idle *args:
    @PYTHONPATH=src {{python}} -m herdr_orchestrator run --workflow {{workflow}} --until-idle "$@"

run *args:
    @PYTHONPATH=src {{python}} -m herdr_orchestrator run --workflow {{workflow}} "$@"

[positional-arguments]
retry job_id *args:
    @PYTHONPATH=src {{python}} -m herdr_orchestrator retry --workflow {{workflow}} --job-id {{quote(job_id)}} {{args}}

[positional-arguments]
resume job_id response_file:
    @PYTHONPATH=src {{python}} -m herdr_orchestrator resume --workflow {{workflow}} --job-id {{quote(job_id)}} --response-file {{quote(response_file)}}

[positional-arguments]
gc *args:
    @PYTHONPATH=src {{python}} -m herdr_orchestrator gc --workflow {{workflow}} --succeeded-agents "$@"

[positional-arguments]
gc-failed *args:
    @PYTHONPATH=src {{python}} -m herdr_orchestrator gc --workflow {{workflow}} --failed-agents "$@"

enqueue-auto title prompt_file dedupe_key *args:
    @PYTHONPATH=src {{python}} -m herdr_orchestrator enqueue --workflow {{workflow}} --title {{quote(title)}} --prompt-file {{quote(prompt_file)}} --dedupe-key {{quote(dedupe_key)}} {{args}}

[positional-arguments]
deliver goal_file *args:
    @PYTHONPATH=src {{python}} -m herdr_orchestrator deliver --workflow {{workflow}} --goal-file {{quote(goal_file)}} {{args}}

smoke *args:
    @PYTHONPATH=src {{python}} -m herdr_orchestrator smoke --workflow {{workflow}} "$@"
