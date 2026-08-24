set positional-arguments

workflow := "workflows/multi-harness.toml"
python := "python3"

default:
    @just --list

doctor:
    @PYTHONPATH=src {{python}} -m herdr_orchestrator doctor --workflow {{workflow}}

test:
    @PYTHONPATH=src {{python}} -m unittest discover -s tests -p 'test_*.py' -v

check:
    @PYTHONPATH=src {{python}} -m compileall -q src tests
    @just test

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

[positional-arguments]
enqueue harness title prompt_file dedupe_key *args:
    @PYTHONPATH=src {{python}} -m herdr_orchestrator enqueue --workflow {{workflow}} --harness {{quote(harness)}} --title {{quote(title)}} --prompt-file {{quote(prompt_file)}} --dedupe-key {{quote(dedupe_key)}} "$@"

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
    @PYTHONPATH=src {{python}} -m herdr_orchestrator retry --workflow {{workflow}} --job-id {{quote(job_id)}} "$@"

[positional-arguments]
gc *args:
    @PYTHONPATH=src {{python}} -m herdr_orchestrator gc --workflow {{workflow}} --succeeded-agents "$@"

enqueue-auto title prompt_file dedupe_key *args:
    @PYTHONPATH=src {{python}} -m herdr_orchestrator enqueue --workflow {{workflow}} --title {{quote(title)}} --prompt-file {{quote(prompt_file)}} --dedupe-key {{quote(dedupe_key)}} "$@"

[positional-arguments]
deliver goal_file *args:
    @PYTHONPATH=src {{python}} -m herdr_orchestrator deliver --workflow {{workflow}} --goal-file {{quote(goal_file)}} "$@"

smoke *args:
    @PYTHONPATH=src {{python}} -m herdr_orchestrator smoke --workflow {{workflow}} "$@"
