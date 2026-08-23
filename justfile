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

run-once:
    @PYTHONPATH=src {{python}} -m herdr_orchestrator run --workflow {{workflow}} --once

run:
    @PYTHONPATH=src {{python}} -m herdr_orchestrator run --workflow {{workflow}}

status:
    @PYTHONPATH=src {{python}} -m herdr_orchestrator status --workflow {{workflow}}

enqueue harness title prompt_file dedupe_key:
    @PYTHONPATH=src {{python}} -m herdr_orchestrator enqueue --workflow {{workflow}} --harness "$harness" --title "$title" --prompt-file "$prompt_file" --dedupe-key "$dedupe_key"

[positional-arguments]
smoke *args:
    @PYTHONPATH=src {{python}} -m herdr_orchestrator smoke --workflow {{workflow}} "$@"
