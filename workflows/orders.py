"""Order processing workflow example."""
from pyworkflow import workflow, step


@step()
async def validate_order(order_id: str) -> dict:
    """Validate the order exists and is valid."""
    # In a real app, check database or external service
    return {"order_id": order_id, "valid": True}


@step()
async def process_payment(order_id: str, amount: float) -> dict:
    """Process payment for the order."""
    # In a real app, integrate with payment provider
    return {"order_id": order_id, "payment_status": "completed", "amount": amount}


@step()
async def update_inventory(order_id: str) -> dict:
    """Update inventory after order."""
    # In a real app, update inventory database
    return {"order_id": order_id, "inventory_updated": True}


@workflow()
async def process_order(order_id: str, amount: float = 99.99) -> dict:
    """
    Process an order through validation, payment, and inventory update.

    This is a sample workflow demonstrating:
    - Multiple steps executed in sequence
    - Data passing between steps
    - Automatic retry on failures

    Run with:
        pyworkflow workflows run process_order --input '{"order_id": "123", "amount": 49.99}'
    """
    validation = await validate_order(order_id)
    if not validation["valid"]:
        raise ValueError(f"Order {order_id} is not valid")

    payment = await process_payment(order_id, amount)
    inventory = await update_inventory(order_id)

    return {
        "order_id": order_id,
        "status": "completed",
        "payment": payment,
        "inventory": inventory,
    }
