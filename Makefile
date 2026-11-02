PY ?= .venv/bin/python
COV_MODULES := $(wildcard src/zipbot/media.py src/zipbot/naming.py src/zipbot/store.py)

.PHONY: check
check:  ## G-auto gate (PLAN.md §12)
	$(PY) -m ruff check src tests
	$(PY) -m ruff format --check src tests
	$(PY) -m pyright
	$(PY) -m pytest --cov --cov-report=term-missing:skip-covered
ifneq ($(COV_MODULES),)
	$(PY) -m coverage report --include=$(subst $() ,$(comma),$(COV_MODULES)) --fail-under=90
endif
	$(PY) -m pip_audit -r requirements.lock --disable-pip --no-deps --progress-spinner off

comma := ,
