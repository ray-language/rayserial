# rayserial — common tasks. `make` (or `make help`) lists them.

.DEFAULT_GOAL := help
.PHONY: help build test test-vm test-native unit unit-native e2e e2e-native oracle check fmt bench clean

PY := /opt/homebrew/bin/python3

help: ## List the targets
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*## "} {printf "  %-12s %s\n", $$1, $$2}'

build: ## The native binary, ./rayserial
	rm -f rayserial
	ray build --native --release -o rayserial src/main.ray

test: test-vm test-native ## Everything: unit + pty tests and the E2E/oracle suite, VM and native

test-vm: unit e2e ## VM only

test-native: unit-native e2e-native ## Native only

unit: ## ray test (pure logic + in-process pty tests) on the VM
	ray test

unit-native: ## The same, as native binaries
	ray test --native

e2e: ## E2E against the fake board + Python termios oracle, VM (`ray run`)
	$(PY) -I scripts/e2e.py --engine vm

e2e-native: build ## The same E2E suite against ./rayserial
	$(PY) -I scripts/e2e.py --engine native

check: ## Type-check without running
	ray check

fmt: ## Format the sources
	ray fmt -w src/*.ray tests/*.ray

bench: build ## Throughput VM vs native (pty, Python peer and fake board)
	$(PY) -I scripts/bench.py

clean: ## Remove the binary and ./out
	rm -f rayserial
	rm -rf out
