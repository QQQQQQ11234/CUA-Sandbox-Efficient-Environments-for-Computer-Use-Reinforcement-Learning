.PHONY: test compile lint clean

PYTHON ?= python

compile:
	$(PYTHON) -m compileall -q rl_web_agent scripts experiments

test:
	@if $(PYTHON) -c "import pytest" >/dev/null 2>&1; then \
		$(PYTHON) -m pytest -q tests/test_agent_interface.py tests/test_route_lifecycle.py tests/test_state_audit.py tests/test_non_db_state.py; \
	else \
		$(PYTHON) -m unittest -q tests.test_agent_interface tests.test_route_lifecycle tests.test_state_audit tests.test_non_db_state; \
	fi

lint:
	$(PYTHON) -m ruff check rl_web_agent scripts experiments tests

clean:
	./scripts/clean_workspace.sh
