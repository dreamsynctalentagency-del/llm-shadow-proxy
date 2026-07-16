SHELL := /bin/bash

IMAGE ?= llm-shadow-proxy:local
COMPOSE ?= docker compose
COMPOSE_DEV ?= $(COMPOSE) -f docker-compose.yml -f docker-compose.dev.yml

.DEFAULT_GOAL := help

.PHONY: help
help:
	@printf "Targets:\n"
	@printf "  make install         Install python deps into ./.venv\n"
	@printf "  make test            Run pytest\n"
	@printf "  make lint            Run ruff\n"
	@printf "  make run             Run uvicorn locally (needs .env)\n"
	@printf "\nDocker targets:\n"
	@printf "  make build           Build the Docker image ($(IMAGE))\n"
	@printf "  make up              docker compose up (Postgres + proxy)\n"
	@printf "  make up-dev          Same as 'up' but SQLite instead of Postgres\n"
	@printf "  make down            docker compose down\n"
	@printf "  make logs            Tail proxy logs\n"
	@printf "  make ps              Show running compose services\n"
	@printf "  make sh              Open a shell in the running proxy container\n"
	@printf "  make health          Curl the proxy healthcheck\n"
	@printf "  make smoke           Send a demo request to /v1/chat\n"

.PHONY: install test lint run
install:
	python -m venv .venv && . .venv/bin/activate && pip install -U pip && pip install -e ".[dev]"

test:
	. .venv/bin/activate && pytest -q

lint:
	. .venv/bin/activate && ruff check src tests

run:
	. .venv/bin/activate && uvicorn shadow_proxy.main:app --reload --host 0.0.0.0 --port 8000

.PHONY: build up up-dev down logs ps sh health smoke
build:
	$(COMPOSE) build proxy

up:
	$(COMPOSE) up -d --build
	@echo "Proxy on http://localhost:8000 (Swagger: /docs)"

up-dev:
	$(COMPOSE_DEV) up -d --build proxy
	@echo "Proxy on http://localhost:8000 (SQLite mode)"

down:
	$(COMPOSE) down

logs:
	$(COMPOSE) logs -f proxy

ps:
	$(COMPOSE) ps

sh:
	$(COMPOSE) exec proxy /bin/bash || $(COMPOSE) exec proxy /bin/sh

health:
	curl -fsS http://localhost:8000/healthz && echo

smoke:
	curl -sS -X POST http://localhost:8000/v1/chat \
	  -H 'Content-Type: application/json' \
	  -d '{"messages":[{"role":"system","content":"Reply only as JSON {\"action\": \"...\"}."},{"role":"user","content":"book a flight to Paris"}],"temperature":0.0,"max_completion_tokens":32}' \
	  | python -m json.tool
