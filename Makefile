.PHONY: dev dev-stop dev-status dev-logs dev-tail check pre-pr help

SOCKET := ./.overmind.sock
SETUP_DOC := docs/development.md

define require_command
	@command -v $(1) >/dev/null 2>&1 || { \
		echo "Missing required command: $(1)"; \
		echo "See $(SETUP_DOC) for installation steps."; \
		exit 1; \
	}
endef

define require_file
	@test -f $(1) || { \
		echo "Missing required file: $(1)"; \
		exit 1; \
	}
endef

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-15s\033[0m %s\n", $$1, $$2}'

dev: ## Start the dev environment (daemonized)
	$(call require_command,overmind)
	$(call require_command,tmux)
	$(call require_file,Procfile.dev)
	@if [ -S $(SOCKET) ] && overmind ps -s $(SOCKET) > /dev/null 2>&1; then \
		echo "Dev environment already running"; \
		overmind ps -s $(SOCKET); \
	else \
		rm -f $(SOCKET); \
		overmind start -f Procfile.dev -s $(SOCKET) -D; \
		sleep 2; \
		overmind ps -s $(SOCKET); \
	fi

dev-stop: ## Stop the dev environment
	$(call require_command,overmind)
	@if [ -S $(SOCKET) ]; then overmind quit -s $(SOCKET) || true; fi
	@rm -f $(SOCKET)
	@if command -v tmux >/dev/null 2>&1; then \
		tmux list-sessions 2>/dev/null | grep overmind | cut -d: -f1 | xargs -r -n1 tmux kill-session -t 2>/dev/null || true; \
	fi

dev-status: ## Check if dev environment is running
	$(call require_command,overmind)
	@if [ -S $(SOCKET) ] && overmind ps -s $(SOCKET) > /dev/null 2>&1; then \
		echo "running"; \
	else \
		echo "stopped"; \
	fi

dev-logs: ## Stream all logs (Ctrl+C to stop)
	$(call require_command,overmind)
	overmind echo -s $(SOCKET)

dev-tail: ## Show last 100 lines of logs (non-blocking)
	$(call require_command,tmux)
	@if [ -S $(SOCKET) ]; then \
		for pane in $$(tmux -S $(SOCKET) list-panes -a -F '#{pane_id}' 2>/dev/null); do \
			tmux -S $(SOCKET) capture-pane -p -t "$$pane" -S -100 2>/dev/null; \
		done; \
	else \
		echo "Dev environment not running"; \
	fi

check: ## Run formatting checks, linting, type checking, and tests
	$(call require_command,uv)
	uv run ruff format --check .
	uv run ruff check .
	uv run mypy .
	uv run pytest

pre-pr: ## Run pre-PR checks
	./scripts/pre-pr.sh

# Connect to specific service terminal (replace 'app' with service name from Procfile.dev)
# connect-app: ## Connect to app terminal
# 	overmind connect -s $(SOCKET) app
