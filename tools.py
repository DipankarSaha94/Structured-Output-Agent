"""A local read-only tool: no accounts, network requests, or refunds."""


def lookup_order(order_id: str) -> dict:
    orders = {
        "ORD-1024": "paid",
        "ORD-1025": "pending",
        "ORD-1026": "cancelled",
    }
    return {"order_id": order_id, "status": orders.get(order_id, "not_found")}


def invalid_order_lookup(order_id: str) -> dict:
    """Deliberately broken fixture for the invalid-tool demo."""
    return {"order_id": order_id, "status": "unexpected_status"}
