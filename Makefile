PYTHON ?= python3
VENV := .venv
VENV_PYTHON := $(VENV)/bin/python
VENV_FORECAST := $(VENV)/bin/dual-forecast
SERIAL_PORT ?= /dev/cu.wchusbserial10

.PHONY: setup test serve receive serve-fast receive-fast receive-fast-wifi

$(VENV_PYTHON):
	$(PYTHON) -m venv $(VENV)

# Create the virtual environment and install this repository with its runtime
# and test dependencies. Pip selects compatible Windows/macOS/Linux wheels.
setup: $(VENV_PYTHON)
	$(VENV_PYTHON) -m pip install -r requirements.txt

test: setup
	$(VENV_PYTHON) -m pytest -q

serve: setup
	$(VENV_FORECAST) serve --host 127.0.0.1 --port 8000

receive: setup
	$(VENV_FORECAST) receive-esp32-serial --serial-port $(SERIAL_PORT)

serve-fast: setup
	$(VENV_FORECAST) serve --host 127.0.0.1 --port 8000 --fast-test

receive-fast: setup
	$(VENV_FORECAST) receive-esp32-serial --serial-port $(SERIAL_PORT) --fast-test

receive-fast-wifi: setup
	$(VENV_FORECAST) receive-esp32 --fast-test
