from prefect import flow


@flow
def my_workflow(name: str = "World"):
    return f"Hello, {name}!"

my_workflow() # This will fail with a type validation error
