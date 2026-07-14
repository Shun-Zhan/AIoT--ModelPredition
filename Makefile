PYTHON ?= python3
VENV := .venv
VENV_PYTHON := $(VENV)/bin/python
VENV_FORECAST := $(VENV)/bin/dual-forecast

.PHONY: setup test serve receive

$(VENV_PYTHON):
	$(PYTHON) -m venv $(VENV)

# Create the virtual environment, install the verified lock file, then install
# this repository as an editable package. Run this once after cloning.
setup: $(VENV_PYTHON)
	$(VENV_PYTHON) -m pip install -r requirements.txt
	$(VENV_PYTHON) -m pip install --no-build-isolation --no-deps -e .

test: setup
	$(VENV_PYTHON) -m pytest -q

serve: setup
	$(VENV_FORECAST) serve --host 127.0.0.1 --port 8000

receive: setup
	$(VENV_FORECAST) receive-esp32
