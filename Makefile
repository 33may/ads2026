# Operator and demo commands for the word-count service. Run from project/.
PY      := $(shell conda info --base)/envs/ads_client/bin/python
SINGLE  := docker compose -f compose.yml
CLUSTER := docker compose -f compose.cluster.yml

MODE ?= both        # cache mode for server-1: none | text | count | both
SVC  ?= server-1
N    ?= 10

.PHONY: up up-cluster down ps logs mode seed get delete hot cache-flush query

# --- cluster ---
up:                 ## scenario 1: redis, minio, server-1
	@CACHE_MODE=$(MODE) $(SINGLE) up -d --build --wait

up-cluster:         ## scenario 2: redis, minio, server-1..3, lb
	@CACHE_MODE=$(MODE) $(CLUSTER) up -d --build --wait

down:               ## stop whichever scenario is running
	@$(SINGLE) down --remove-orphans
	@$(CLUSTER) down --remove-orphans

ps:
	@docker compose ps

logs:               ## make logs SVC=server-1
	@docker compose logs -f $(SVC)

mode:               ## make mode MODE=none   (restart server-1 in another cache mode, scenario 1)
	@CACHE_MODE=$(MODE) $(SINGLE) up -d --wait server-1

# --- data (developer) ---
seed:
	@$(PY) -m client.devcli seed

get:                ## make get REF=eindhoven
	@$(PY) -m client.devcli get $(REF)

delete:             ## make delete REF=eindhoven
	@$(PY) -m client.devcli delete $(REF)

hot:                ## make hot N=10
	@$(PY) -m client.devcli hot -n $(N)

cache-flush:
	@$(PY) -m client.devcli cache-flush

# --- queries (end user) ---
query:              ## make query K=eindhoven REF=eindhoven
	@$(PY) -m client.cli query $(K) $(REF)
