"""Print a compact snapshot; used inside the balancer container by make health."""
import json
import os
import socket


def main():
    port = int(os.environ.get("LB_METRICS_PORT", "18860"))
    with socket.create_connection(("127.0.0.1", port), timeout=3) as sock:
        with sock.makefile("r") as stream:
            state = json.loads(stream.readline())
    print("Algorithm:", state["policy"], "| Fault tolerance:", state.get("fault_tolerance", False))
    print(f"{'SERVER':12} {'HEALTH':10} {'ACTIVE':>7} {'ATTEMPTS':>9} {'OK/FAIL':>9}  LAST ERROR")
    for backend in state["backends"]:
        status = backend.get("health", "disabled") if state.get("fault_tolerance") else "disabled"
        streak = f"{backend.get('consecutive_successes', 0)}/{backend.get('consecutive_failures', 0)}"
        print(f"{backend['host']:12} {status:10} {backend['active']:7} "
              f"{backend['selections']:9} {streak:>9}  {backend.get('last_error') or '-'}")
    for event in state.get("health_events", [])[-6:]:
        print(f"health: {event['server']} {event['previous']} -> {event['health']} ({event['reason']})")


if __name__ == "__main__":
    main()
