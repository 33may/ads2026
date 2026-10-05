# Operator and demo commands for the word-count service. Run from project/.
SINGLE  := docker compose -f compose.yml
CLUSTER := docker compose -f compose.cluster.yml
CLIENT  := docker exec ads-client python

CACHE ?= on         # cache for the servers: on | off
ALGORITHM ?= least-connections # least-connections | lrt | combined
SVC  ?= server-1
N    ?= 10
RESULTS_DIR ?= results/Phase2+3/controlled/warmup-0-100-500

.PHONY: up up-cluster down ps logs mode seed get delete hot cache-flush query plot-results test-phase3

# --- cluster ---
up:                 ## scenario 1: redis, minio, server-1
	@CACHE=$(CACHE) $(SINGLE) up -d --build --wait

up-cluster:         ## scenario 2: redis, minio, server-1..3, lb
	@CACHE=$(CACHE) LB_ALGORITHM=$(ALGORITHM) $(CLUSTER) up -d --build --wait

down:               ## stop whichever scenario is running
	@$(SINGLE) down --remove-orphans
	@$(CLUSTER) down --remove-orphans

ps:
	@docker compose ps

logs:               ## make logs SVC=server-1
	@docker compose logs -f $(SVC)

mode:               ## make mode CACHE=off   (restart server-1 with the cache off, scenario 1)
	@CACHE=$(CACHE) $(SINGLE) up -d --wait server-1

# --- data (developer) ---
seed:
	@$(CLIENT) -m client.devcli seed

get:                ## make get REF=eindhoven
	@$(CLIENT) -m client.devcli get $(REF)

delete:             ## make delete REF=eindhoven
	@$(CLIENT) -m client.devcli delete $(REF)

hot:                ## make hot N=10
	@$(CLIENT) -m client.devcli hot -n $(N)

cache-flush:
	@$(CLIENT) -m client.devcli cache-flush

# --- queries (end user) ---
query:              ## make query K=eindhoven REF=eindhoven
	@$(CLIENT) -m client.cli query $(K) $(REF)

plot-results:       ## rebuild all combined Phase 2+3 tables and figures from saved CSVs
	@MPLCONFIGDIR=/tmp/matplotlib ./.venv/bin/python -m client.plot $(RESULTS_DIR)

test-phase3:         ## fast policy and transparent TCP-proxy regression tests
	@MPLCONFIGDIR=/tmp/matplotlib ./.venv/bin/python -m unittest -v tests.test_phase3 tests.test_cache
