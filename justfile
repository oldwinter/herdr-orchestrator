set positional-arguments

workflow := "workflows/multi-harness.toml"
factory_workflow := "workflows/devin-factory.toml"
factory_backlog := "factory/backlog.toml"
python := "uv run python"

# List the stable command surface.
default:
    @just --list

# Start a dedicated interactive manager session in the current Herdr session.
manager harness="":
    @if test -n {{quote(harness)}}; then node bin/herdr-orchestrator.mjs manager {{quote(harness)}}; else node bin/herdr-orchestrator.mjs manager; fi

# Install the herdr-manager npm entry point globally.
install-manager:
    @npm install --global .
    @herdr-orchestrator manager-light install

# Probe harness and environment readiness before dispatching.
[positional-arguments]
doctor *args:
    @PYTHONPATH=src {{python}} -m herdr_orchestrator doctor --workflow {{quote(workflow)}} "$@"

# Print the readiness evidence matrix for enabled harnesses.
[positional-arguments]
readiness-matrix *args:
    @PYTHONPATH=src {{python}} -m herdr_orchestrator readiness-matrix --workflow {{quote(workflow)}} "$@"

# Run the test quality producer.
test:
    @python3 scripts/quality_bundle.py run --producer test

# Run the coverage quality producer.
test-coverage:
    @python3 scripts/quality_bundle.py run --producer coverage

# The bundled coverage command excludes this marker so the stable command contract stays explicit: -m "not installer_crash_matrix".
# Run the installer journal crash-matrix tests once, outside the bundled coverage pass.
test-installer-crash-matrix:
    @PYTHONPATH=src uv run pytest tests/test_installer_journal.py -q -m installer_crash_matrix

# Run the stability quality producer.
test-stability:
    @python3 scripts/quality_bundle.py run --producer stability

# Run the lint quality producer.
lint:
    @python3 scripts/quality_bundle.py run --producer lint

# Run the security quality producer.
security:
    @python3 scripts/quality_bundle.py run --producer security

# Run the build-metrics quality producer.
build-metrics:
    @python3 scripts/quality_bundle.py run --producer build

# Run the profiling quality producer.
profile-tests:
    @python3 scripts/quality_bundle.py run --producer profiling

# Render a Markdown summary for a quality result JSON.
quality-summary result="" output="":
    @root="${QUALITY_EVIDENCE_ROOT:-.orchestrator/quality}"; result={{quote(result)}}; if test -z "$result"; then result="$(python3 scripts/quality_bundle.py latest-result --root "$root")"; fi; output={{quote(output)}}; if test -z "$output"; then output="${result%.json}.md"; fi; python3 scripts/quality_summary.py --result "$result" --root "$root" --output "$output"

# Fail when a quality result misses the required full evidence.
quality-enforce result:
    @root="${QUALITY_EVIDENCE_ROOT:-.orchestrator/quality}"; python3 scripts/quality_bundle.py enforce --root "$root" --result {{quote(result)}} --require-full

# Regenerate the generated command reference docs.
docs-generate:
    @uv run python scripts/generate_reference.py

# Check docs and generated references are up to date.
docs-check:
    @uv run python scripts/check_docs.py
    @uv run python scripts/generate_reference.py --check

# Run the full quality bundle and enforce evidence completeness.
check:
    @uv sync --locked
    @PYTHONPATH=src {{python}} -m compileall -q src tests scripts
    @just test-installer-crash-matrix || installer_status=$?; installer_status=${installer_status:-0}; root="${QUALITY_EVIDENCE_ROOT:-.orchestrator/quality}"; mkdir -p "$root/results"; result="$(python3 -c 'import sys, tempfile; print(tempfile.NamedTemporaryFile(dir=sys.argv[1], prefix="check.", suffix=".json", delete=False).name)' "$root/results")"; summary="${result%.json}.md"; set +e; python3 scripts/quality_bundle.py run --all --root "$root" --result "$result"; collect_status=$?; python3 scripts/quality_summary.py --result "$result" --root "$root" --output "$summary"; summary_status=$?; python3 scripts/quality_bundle.py enforce --result "$result" --root "$root" --require-full; enforce_status=$?; set -e; printf 'bundle=%s summary=%s collect=%s render=%s enforce=%s installer=%s\n' "$result" "$summary" "$collect_status" "$summary_status" "$enforce_status" "$installer_status"; if test "$installer_status" -ne 0; then exit "$installer_status"; fi; if test "$enforce_status" -ne 0; then exit "$enforce_status"; fi; if test "$summary_status" -ne 0; then exit "$summary_status"; fi; exit "$collect_status"

# Seed workflow jobs from the TOML seed tasks.
seed:
    @PYTHONPATH=src {{python}} -m herdr_orchestrator seed --workflow {{quote(workflow)}}

# Show durable queue status for the workflow.
status:
    @PYTHONPATH=src {{python}} -m herdr_orchestrator status --workflow {{quote(workflow)}}

# Serve the read-only operations dashboard.
[positional-arguments]
dashboard *args:
    @PYTHONPATH=src {{python}} -m herdr_orchestrator dashboard --workflow {{quote(workflow)}} "$@"

# Print the compact harness catalog as text.
catalog:
    @PYTHONPATH=src {{python}} -m herdr_orchestrator catalog --workflow {{quote(workflow)}} --format text

# Print the compact harness catalog as JSON.
catalog-json:
    @PYTHONPATH=src {{python}} -m herdr_orchestrator catalog --workflow {{quote(workflow)}} --format json

# Print the full execution profile for one harness.
profile harness:
    @PYTHONPATH=src {{python}} -m herdr_orchestrator profile --workflow {{quote(workflow)}} {{quote(harness)}}

# Enqueue a job for an explicit harness.
[positional-arguments]
enqueue harness title prompt_file dedupe_key *args:
    @shift 4; PYTHONPATH=src {{python}} -m herdr_orchestrator enqueue --workflow {{quote(workflow)}} --harness {{quote(harness)}} --title {{quote(title)}} --prompt-file {{quote(prompt_file)}} --dedupe-key {{quote(dedupe_key)}} "$@"

# Dispatch a single scheduling cycle.
[positional-arguments]
run-once *args:
    @PYTHONPATH=src {{python}} -m herdr_orchestrator run --workflow {{quote(workflow)}} --once "$@"

# Drain the queue until no runnable jobs remain.
[positional-arguments]
run-until-idle *args:
    @PYTHONPATH=src {{python}} -m herdr_orchestrator run --workflow {{quote(workflow)}} --until-idle "$@"

# Run the scheduler loop.
run *args:
    @PYTHONPATH=src {{python}} -m herdr_orchestrator run --workflow {{quote(workflow)}} "$@"

# Requeue a failed job for another attempt.
[positional-arguments]
retry job_id *args:
    @shift 1; PYTHONPATH=src {{python}} -m herdr_orchestrator retry --workflow {{quote(workflow)}} --job-id {{quote(job_id)}} "$@"

# Resume a blocked job with an operator response file.
[positional-arguments]
resume job_id response_file:
    @PYTHONPATH=src {{python}} -m herdr_orchestrator resume --workflow {{quote(workflow)}} --job-id {{quote(job_id)}} --response-file {{quote(response_file)}}

# Dry-run cleanup of agents for succeeded jobs; pass --apply to close.
[positional-arguments]
gc *args:
    @PYTHONPATH=src {{python}} -m herdr_orchestrator gc --workflow {{quote(workflow)}} --succeeded-agents "$@"

# Dry-run cleanup of agents for failed jobs; pass --apply to close.
[positional-arguments]
gc-failed *args:
    @PYTHONPATH=src {{python}} -m herdr_orchestrator gc --workflow {{quote(workflow)}} --failed-agents "$@"

# Dry-run the factory backlog: validate items and show queue coverage.
[positional-arguments]
factory-validate *args:
    @PYTHONPATH=src {{python}} scripts/devin_factory.py --workflow {{quote(factory_workflow)}} --backlog {{quote(factory_backlog)}} validate "$@"

# Enqueue factory backlog items into the durable factory queue (idempotent).
[positional-arguments]
factory-intake *args:
    @PYTHONPATH=src {{python}} scripts/devin_factory.py --workflow {{quote(factory_workflow)}} --backlog {{quote(factory_backlog)}} intake "$@"

# Drain factory work items through their declared local checks.
[positional-arguments]
factory-run *args:
    @PYTHONPATH=src {{python}} scripts/devin_factory.py --workflow {{quote(factory_workflow)}} --backlog {{quote(factory_backlog)}} run "$@"

# Show factory queue counts, job states and unqueued backlog items.
[positional-arguments]
factory-status *args:
    @PYTHONPATH=src {{python}} scripts/devin_factory.py --workflow {{quote(factory_workflow)}} --backlog {{quote(factory_backlog)}} status "$@"

# Write the operator report for the factory queue.
[positional-arguments]
factory-report *args:
    @PYTHONPATH=src {{python}} scripts/devin_factory.py --workflow {{quote(factory_workflow)}} --backlog {{quote(factory_backlog)}} report "$@"

# Re-queue a failed factory job with extra attempts (recovery).
[positional-arguments]
factory-retry job_id *args:
    @shift 1; PYTHONPATH=src {{python}} -m herdr_orchestrator retry --workflow {{quote(factory_workflow)}} --job-id {{quote(job_id)}} "$@"

# Collect terminal factory job artifacts (dry-run by default).
[positional-arguments]
factory-gc *args:
    @PYTHONPATH=src {{python}} -m herdr_orchestrator gc --workflow {{quote(factory_workflow)}} --failed-agents "$@"

# Run the factory lane regression suite.
test-devin-factory:
    @PYTHONPATH=src uv run python -m pytest tests/test_devin_factory.py -q

# Enqueue a job and let the controller pick the harness.
enqueue-auto title prompt_file dedupe_key *args:
    @shift 3; PYTHONPATH=src {{python}} -m herdr_orchestrator enqueue --workflow {{quote(workflow)}} --title {{quote(title)}} --prompt-file {{quote(prompt_file)}} --dedupe-key {{quote(dedupe_key)}} "$@"

# Run the opt-in standardized delivery pipeline for a goal file.
[positional-arguments]
deliver goal_file *args:
    @shift 1; PYTHONPATH=src {{python}} -m herdr_orchestrator deliver --workflow {{quote(workflow)}} --goal-file {{quote(goal_file)}} "$@"

# Run a real-turn connectivity smoke test for enabled harnesses.
smoke *args:
    @PYTHONPATH=src {{python}} -m herdr_orchestrator smoke --workflow {{quote(workflow)}} "$@"
