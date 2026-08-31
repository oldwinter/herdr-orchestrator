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
    @python3 scripts/quality_bundle.py run --producer test

test-coverage:
    @python3 scripts/quality_bundle.py run --producer coverage

test-stability:
    @python3 scripts/quality_bundle.py run --producer stability

lint:
    @python3 scripts/quality_bundle.py run --producer lint

security:
    @python3 scripts/quality_bundle.py run --producer security

build-metrics:
    @python3 scripts/quality_bundle.py run --producer build

profile-tests:
    @python3 scripts/quality_bundle.py run --producer profiling

quality-summary result="" output="":
    @result={{quote(result)}}; if test -z "$result"; then result="$(python3 scripts/quality_bundle.py latest-result)"; fi; output={{quote(output)}}; if test -z "$output"; then output="${result%.json}.md"; fi; python3 scripts/quality_summary.py --result "$result" --output "$output"

quality-enforce result:
    @python3 scripts/quality_bundle.py enforce --result {{quote(result)}} --require-full

docs-generate:
    @uv run python scripts/generate_reference.py

docs-check:
    @uv run python scripts/check_docs.py
    @uv run python scripts/generate_reference.py --check

check:
    @uv sync --locked
    @PYTHONPATH=src {{python}} -m compileall -q src tests scripts
    @mkdir -p .orchestrator/quality/results
    @result="$(mktemp .orchestrator/quality/results/check.XXXXXX.json)"; summary="${result%.json}.md"; set +e; python3 scripts/quality_bundle.py run --all --result "$result"; collect_status=$?; python3 scripts/quality_summary.py --result "$result" --output "$summary"; summary_status=$?; python3 scripts/quality_bundle.py enforce --result "$result" --require-full; enforce_status=$?; set -e; printf 'bundle=%s summary=%s collect=%s render=%s enforce=%s\n' "$result" "$summary" "$collect_status" "$summary_status" "$enforce_status"; if test "$enforce_status" -ne 0; then exit "$enforce_status"; fi; if test "$summary_status" -ne 0; then exit "$summary_status"; fi; exit "$collect_status"

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
